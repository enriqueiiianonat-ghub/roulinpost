from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import List, Optional
import firebase_admin
from firebase_admin import credentials, firestore, storage
import io
import os
import uuid
import random
import resend
import asyncio 
import json as py_json
from pathlib import Path
from PIL import Image, ImageOps
import tempfile
import subprocess
import time

from fastapi import APIRouter

resend.api_key = "re_Wbh3nvip_D3hUtXrB1DQTDVrzasgLDsLU"

app = FastAPI(title="EZGEE Social API")

UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RoomInvitePayload(BaseModel):
    username: str        
    room_id: str         
    target_user: str     

class HandleInvitePayload(BaseModel):
    username: str        
    invitation_id: str   
    action: str          


# --- FIREBASE INITIALIZATION BLOCK ---
CERT_PATH = "meshmeedb-firebase-adminsdk-fbsvc-c33dc12e77.json"
BUCKET_NAME = "meshmeedb.firebasestorage.app"

if not firebase_admin._apps:
    firebase_json_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    
    if firebase_json_env:
        try:
            cred_dict = py_json.loads(firebase_json_env)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})
            print("🚀 Firebase successfully initialized via Render Environment Variable.")
        except Exception as json_err:
            print(f"🔥 Error parsing Environment Variable JSON: {json_err}")
            raise json_err
    else:
        if os.path.exists(CERT_PATH):
            cred = credentials.Certificate(CERT_PATH)
            firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})
            print(f"🚀 Firebase successfully initialized via local file target: {CERT_PATH}")
        else:
            raise RuntimeError(f"❌ Critical Error: Credentials not found via Env or local path: {CERT_PATH}")

db_fs = firestore.client()
# ---------------------------------------------------

class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class VerifyOTP(BaseModel):
    username: str
    otp_code: str

class UserLogin(BaseModel):
    username: str
    password: str

def compress_video_heavy(file_bytes: bytes) -> bytes:
    """Forces aggressive H.264 video compression onto the input video bytes directly on the server."""
    try:
        in_fd, in_name = tempfile.mkstemp(suffix=".mp4")
        out_fd, out_name = tempfile.mkstemp(suffix=".mp4")
        
        try:
            with os.fdopen(in_fd, 'wb') as tmp:
                tmp.write(file_bytes)
                tmp.flush()
            
            # Universal heavy compression engine settings:
            # CRF 35 (Super tiny file footprint), Scale bounds to 360p, crush audio down to 24k mono
            cmd = [
                "ffmpeg", "-y", "-i", in_name,
                "-vcodec", "libx264", 
                "-crf", "35", 
                "-preset", "ultrafast",
                "-vf", "scale=w='if(gte(iw,ih),min(360,iw),-2)':h='if(lt(iw,ih),min(360,ih),-2)'",
                "-acodec", "aac", 
                "-b:a", "24k", 
                "-ac", "1",
                "-f", "mp4",
                out_name
            ]
            
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            
            with open(out_name, "rb") as f:
                compressed_bytes = f.read()
                
            if len(compressed_bytes) > 0:
                print(f"✅ Video universally compressed down to {len(compressed_bytes)} bytes")
                return compressed_bytes
        finally:
            if os.path.exists(in_name):
                os.unlink(in_name)
            if os.path.exists(out_name):
                os.unlink(out_name)
                
        return file_bytes
    except Exception as e:
        print(f"⚠️ Video compression pipeline skipped or missing server dependencies, uploading direct payload: {e}")
        return file_bytes

async def process_and_upload_media(file: UploadFile) -> str:
    try:
        bucket = storage.bucket()
        c_type = (file.content_type or "").lower()
        f_name = (file.filename or "").lower()
        
        await file.seek(0)
        file_bytes = await file.read()
        
        if not file_bytes or len(file_bytes) == 0:
            return ""

        is_mp4_signature = len(file_bytes) > 12 and b"ftyp" in file_bytes[4:12]
        is_video = (
            c_type.startswith("video/") or 
            "video" in c_type or
            f_name.endswith(('.mp4', '.mov', '.avi', '.mkv', '.3gp', '.webm')) or
            is_mp4_signature
        )
        
        is_document = (
            c_type.startswith("application/pdf") or
            "msword" in c_type or
            "officedocument" in c_type or
            f_name.endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt'))
        )
        
        if is_video:
            # ✨ UNIVERSAL FILTER: Compresses video regardless of what platform uploaded it
            compressed_video_data = compress_video_heavy(file_bytes)
            unique_id = uuid.uuid4()
            blob_path = f"videos/{unique_id}.mp4"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": "video/mp4", "contentDisposition": "inline"}
            blob.upload_from_string(compressed_video_data, content_type="video/mp4")
            blob.content_type = "video/mp4"
            blob.patch()
            blob.make_public()
            return blob.public_url

        elif is_document:
            unique_id = uuid.uuid4()
            ext = f_name.split('.')[-1] if '.' in f_name else 'dat'
            blob_path = f"documents/{unique_id}.{ext}"
            blob = bucket.blob(blob_path)
            
            determined_type = c_type if c_type else "application/octet-stream"
            blob.metadata = {"contentType": determined_type, "contentDisposition": "attachment"}
            blob.upload_from_string(file_bytes, content_type=determined_type)
            blob.content_type = determined_type
            blob.patch()
            blob.make_public()
            return blob.public_url

        else:
            try:
                # ✨ UNIVERSAL FILTER: Compresses any image down to a 640x640 pixel frame with 50 quality
                img = Image.open(io.BytesIO(file_bytes))
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                    
                max_resolution = (640, 640)
                img.thumbnail(max_resolution, Image.Resampling.LANCZOS)
                
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=50, optimize=True)
                compressed_data = output.getvalue()
                
                blob_path = f"posts/{uuid.uuid4()}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(compressed_data, content_type="image/jpeg")
                blob.make_public()
                return blob.public_url
            except Exception as img_err:
                blob_path = f"posts/{uuid.uuid4()}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(file_bytes, content_type="image/jpeg")
                blob.make_public()
                return blob.public_url
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal media handler crash: {str(e)}")

def process_and_upload_avatar(file_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img = ImageOps.fit(img, (150, 150), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        bucket = storage.bucket()
        blob_path = f"avatars/{uuid.uuid4()}.jpg"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(output.getvalue(), content_type="image/jpeg")
        blob.make_public()
        return blob.public_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Avatar upload failed: {str(e)}")

@app.post("/auth/register")
async def register(user: UserRegister):
    try:
        clean_username = user.username.strip().lower()
        user_ref = db_fs.collection('users').document(clean_username)
        if user_ref.get().exists:
            raise HTTPException(status_code=400, detail="Username is already taken.")

        pending_ref = db_fs.collection('unverified_users').document(clean_username)
        if pending_ref.get().exists:
            pending_ref.delete()

        otp_code = f"{random.randint(100000, 999999)}"
        db_fs.collection('unverified_users').document(clean_username).set({
            'username': clean_username,
            'email': user.email,
            'password': user.password,
            'otp_code': otp_code,
            'created_at': firestore.SERVER_TIMESTAMP
        })

        email_html = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px;">
            <h2>ROULIN POST — Email Verification</h2>
            <p>Your OTP code is:</p>
            <div style="font-size: 30px; font-weight: bold; padding: 20px; background: #f2f2f2; text-align: center; letter-spacing: 5px;">
                {otp_code}
            </div>
            <p>If you didn't request this, ignore this email.</p>
        </div>
        """
        try:
            resend.Emails.send({
                "from": "no-reply@roulinpost.com",
                "to": user.email,
                "subject": "ROULIN POST - Verify Your Account",
                "html": email_html,
            })
        except Exception as email_error:
            raise HTTPException(status_code=500, detail=f"Failed To Send OTP Email: {str(email_error)}")

        return {"message": "Registration successful. OTP process started."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/verify-otp")
def verify_otp(payload: VerifyOTP):
    target_username = payload.username.strip().lower()
    unverified_ref = db_fs.collection('unverified_users').document(target_username)
    snap = unverified_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Registration session expired or user not found.")
    data = snap.to_dict()
    if data.get("otp_code") != payload.otp_code.strip():
        raise HTTPException(status_code=400, detail="Invalid verification code.")
    db_fs.collection('users').document(target_username).set({
        'username': data['username'],
        'email': data['email'],
        'password': data['password'],
        'profile_url': "",  
        'created_at': firestore.SERVER_TIMESTAMP
    })
    unverified_ref.delete()
    return {"message": "Email authenticated! You can now log in."}

@app.post("/auth/login")
def login(user: UserLogin):
    login_username = user.username.strip().lower()
    if db_fs.collection('unverified_users').document(login_username).get().exists:
        raise HTTPException(status_code=401, detail="Account not verified.")
    user_ref = db_fs.collection('users').document(login_username).get()
    if not user_ref.exists or user_ref.to_dict().get("password") != user.password:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    u_data = user_ref.to_dict()
    return {
        "username": login_username, 
        "email": u_data.get("email"), 
        "profile_url": u_data.get("profile_url", ""),
        "country": u_data.get("country", ""),  
        "city": u_data.get("city", "")         
    }

@app.put("/auth/profile/{current_username}")
async def update_profile(
    current_username: str,
    new_username: str = Form(...),
    new_email: str = Form(...),
    country: Optional[str] = Form(""),  
    city: Optional[str] = Form(""),     
    new_password: Optional[str] = Form(None),
    avatar_file: Optional[UploadFile] = File(None)
):
    padding_current = current_username.strip().lower()
    clean_new = new_username.strip().lower()
    user_ref = db_fs.collection('users').document(padding_current)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="User profile not found")
    user_data = snap.to_dict()
    bucket = storage.bucket()

    if avatar_file:
        old_avatar = user_data.get("profile_url", "")
        if old_avatar:
            try:
                old_file_name = old_avatar.split("/")[-1].split("?")[0].replace("avatars%", "avatars/")
                blob = bucket.blob(old_file_name)
                if blob.exists():
                    blob.delete()
            except:
                pass
        avatar_bytes = await avatar_file.read()
        user_data['profile_url'] = process_and_upload_avatar(avatar_bytes)

    user_data['country'] = country or ""
    user_data['city'] = city or ""

    if clean_new != padding_current:
        new_ref = db_fs.collection('users').document(clean_new)
        if new_ref.get().exists:
            raise HTTPException(status_code=400, detail="New username is already taken")
        user_data['username'] = clean_new
        user_data['email'] = new_email
        if new_password:
            user_data['password'] = new_password
        new_ref.set(user_data)
        user_ref.delete()
        return {
            "username": clean_new, 
            "email": new_email, 
            "profile_url": user_data.get("profile_url", ""),
            "country": user_data['country'],
            "city": user_data['city']
        }

    user_data['email'] = new_email
    if new_password:
        user_data['password'] = new_password
    user_ref.set(user_data)
    return {
        "username": padding_current, 
        "email": new_email, 
        "profile_url": user_data.get("profile_url", ""),
        "country": user_data['country'],
        "city": user_data['city']
    }

@app.delete("/auth/profile/{username}")
def delete_user_account(username: str):
    clean_username = username.strip().lower()
    user_ref = db_fs.collection('users').document(clean_username)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="User account record not found.")
    bucket = storage.bucket()
    user_data = snap.to_dict()
    
    avatar_url = user_data.get("profile_url", "")
    if avatar_url:
        try:
            file_name = avatar_url.split("/")[-1].split("?")[0].replace("avatars%", "avatars/")
            bucket.blob(file_name).delete()
        except:
            pass

    try:
        user_posts_query = db_fs.collection('posts').where(filter=firestore.FieldFilter("username", "==", clean_username)).get()
        for doc in user_posts_query:
            for url in doc.to_dict().get("image_urls", []):
                try:
                    file_name = url.split("/")[-1].split("?")[0].replace("posts%", "posts/").replace("videos%", "videos/")
                    bucket.blob(file_name).delete()
                except:
                    pass
            db_fs.collection('posts').document(doc.id).delete()
        user_ref.delete()
        return {"message": "Account, posts, and files deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/posts")
def get_posts(limit: int = 10, offset: int = 0, username: Optional[str] = None):
    query = db_fs.collection('posts')
    if username:
        query = query.where(filter=firestore.FieldFilter("username", "==", username))
    
    docs = query.order_by("timestamp", direction=firestore.Query.DESCENDING).offset(offset).limit(limit).get()
    posts = []
    author_cache = {}
    
    for doc in docs:
        d = doc.to_dict()
        author = d.get("username", "")
        post_id = doc.id
        
        if author not in author_cache:
            author_ref = db_fs.collection('users').document(author).get()
            if author_ref.exists:
                auth_data = author_ref.to_dict()
                author_cache[author] = {
                    "avatar": auth_data.get("profile_url", ""),
                    "country": auth_data.get("country", ""),
                    "city": auth_data.get("city", "")
                }
            else:
                author_cache[author] = {"avatar": "", "country": "", "city": ""}
        
        comments_ref = db_fs.collection('posts').document(post_id).collection('comments')
        comment_count = comments_ref.count().get()[0][0].value
                
        posts.append({
            "id": post_id,
            "username": author,
            "user_avatar": author_cache[author]["avatar"], 
            "author_country": author_cache[author]["country"], 
            "author_city": author_cache[author]["city"],      
            "message": d.get("message"),
            "image_urls": d.get("image_urls", []), 
            "likes": d.get("likes", 0),
            "room_only": d.get("room_only", False),
            "comment_count": comment_count,
            "timestamp": str(d.get("timestamp")) if d.get("timestamp") else None 
        })
        
    posts = [p for p in posts if not p.get("room_only", False)]
    return posts

@app.post("/posts")
async def create_post(
    username: str = Form(...),
    message: Optional[str] = Form(None),
    target_room_id: Optional[str] = Form(None), 
    room_only: str = Form("false"), 
    files: List[UploadFile] = File([])
):
    if len(files) > 12:
        raise HTTPException(status_code=400, detail="Cannot upload more than 12 elements.")

    tasks = [process_and_upload_media(f) for f in files]
    results = await asyncio.gather(*tasks)
    media_urls = [url for url in results if url]
            
    post_ref = db_fs.collection('posts').document()
    is_room_only = room_only.strip().lower() == "true"

    post_data = {
        'username': username,
        'message': message or "",
        'image_urls': media_urls, 
        'likes': 0,
        'room_only': is_room_only, 
        'timestamp': firestore.SERVER_TIMESTAMP  
    }
    if target_room_id:
        post_data['target_room_id'] = target_room_id
        
    post_ref.set(post_data)
    return {"message": "Post created successfully"}

@app.put("/posts/{post_id}")
async def update_post(
    post_id: str,
    username: str = Form(...),
    message: Optional[str] = Form(None),
    retained_image_urls: str = Form("[]"),
    files: List[UploadFile] = File([])
):
    post_ref = db_fs.collection('posts').document(post_id)
    snap = post_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Post target not found")
    old_post = snap.to_dict()
    if old_post.get("username") != username:
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        retained_urls = py_json.loads(retained_image_urls)
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON array mapping")

    if len(retained_urls) + len(files) > 12:
        raise HTTPException(status_code=400, detail="Total attachments cannot exceed 12 items.")

    bucket = storage.bucket()
    for old_url in old_post.get("image_urls", []):
        if old_url not in retained_urls:
            try:
                file_name = old_url.split("/")[-1].split("?")[0].replace("posts%", "posts/").replace("videos%", "videos/")
                bucket.blob(file_name).delete()
            except:
                pass

    tasks = [process_and_upload_media(f) for f in files]
    results = await asyncio.gather(*tasks)
    new_uploaded_urls = [url for url in results if url]

    final_media_list = retained_urls + new_uploaded_urls
    post_ref.update({"message": message or "", "image_urls": final_media_list})
    return {"message": "Post updated successfully", "image_urls": final_media_list}

@app.post("/posts/{post_id}/like")
def like_post(post_id: str):
    post_ref = db_fs.collection('posts').document(post_id)
    if not post_ref.get().exists:
        raise HTTPException(status_code=404, detail="Post not found")
    post_ref.update({'likes': firestore.Increment(1)})
    return {"message": "Liked"}

router = APIRouter()

class ChatMessageSchema(BaseModel):
    sender: str
    recipient: str
    text: str
    media_url: Optional[str] = None 

def get_conversation_id(user1: str, user2: str) -> str:
    sorted_users = sorted([user1.lower().strip(), user2.lower().strip()])
    return f"{sorted_users[0]}_{sorted_users[1]}"

@app.post("/chat/send")
def send_direct_message(payload: ChatMessageSchema):
    if not payload.text.strip() and not payload.media_url:
        raise HTTPException(status_code=400, detail="Message body or media attachment required.")
        
    conv_id = get_conversation_id(payload.sender, payload.recipient)
    now_ts = int(time.time())
    
    chat_room_ref = db_fs.collection('chats').document(conv_id)
    chat_room_ref.set({
        "participants": [payload.sender, payload.recipient],
        "last_message": "[Media File]" if not payload.text.strip() else payload.text,
        "last_updated": now_ts
    }, merge=True)
    
    message_ref = chat_room_ref.collection('messages').document()
    message_ref.set({
        "sender": payload.sender,
        "recipient": payload.recipient,
        "text": payload.text,
        "media_url": payload.media_url, 
        "timestamp": now_ts,
        "read": False
    })
    
    return {"status": "success", "message_id": message_ref.id}

@app.get("/chat/notifications")
def get_incoming_unread_chat_notifications(recipient: str):
    clean_recipient = recipient.strip().lower()
    chats_query = db_fs.collection('chats').where(
        filter=firestore.FieldFilter("participants", "array_contains", recipient)
    ).get()
    
    unread_summary_map = {}
    for chat_doc in chats_query:
        participants = chat_doc.to_dict().get("participants", [])
        sender_targets = [p for p in participants if p.lower().strip() != clean_recipient]
        if not sender_targets:
            continue
        sender_label = sender_targets[0]
        
        messages_ref = chat_doc.reference.collection('messages')
        unread_docs = messages_ref.where(filter=firestore.FieldFilter("sender", "==", sender_label)).where(filter=firestore.FieldFilter("read", "==", False)).get()
        
        unread_summary_map[sender_label] = len(unread_docs)
        
    return unread_summary_map

class CommentModel(BaseModel):
    username: str
    text: str

@app.get("/posts/{post_id}/comments")
def get_comments(post_id: str):
    comments_ref = db_fs.collection('posts').document(post_id).collection('comments')
    docs = comments_ref.order_by("timestamp", direction=firestore.Query.ASCENDING).get()
    
    comments_list = []
    for doc in docs:
        d = doc.to_dict()
        comments_list.append({
            "username": d.get("username"),
            "text": d.get("text"),
            "timestamp": d.get("timestamp")
        })
    return comments_list

@app.post("/posts/{post_id}/comments")
def add_comment(post_id: str, comment: CommentModel):
    if not comment.text.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be blank")
        
    post_ref = db_fs.collection('posts').document(post_id)
    if not post_ref.get().exists:
        raise HTTPException(status_code=404, detail="Post not found")
        
    comment_data = {
        "username": comment.username.strip(),
        "text": comment.text.strip(),
        "timestamp": int(time.time())
    }
    post_ref.collection('comments').add(comment_data)
    return {"status": "success"}

@app.get("/chat/history/{conversation_id}")
def get_chat_history(conversation_id: str):
    chat_ref = (
        db_fs.collection("chats")
        .document(conversation_id)
        .collection("messages")
    )
    docs = (
        chat_ref
        .order_by("timestamp", direction=firestore.Query.ASCENDING)
        .stream()
    )
    messages = []
    for doc in docs:
        d = doc.to_dict()
        messages.append({
            "sender": d.get("sender"),
            "recipient": d.get("recipient"),
            "text": d.get("text"),
            "media_url": d.get("media_url"), 
            "timestamp": d.get("timestamp")
        })
    return messages

@app.delete("/posts/{post_id}")
def delete_post(post_id: str, username: str):
    post_ref = db_fs.collection('posts').document(post_id)
    snap = post_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Post not found")
    if snap.to_dict().get("username") != username:
        raise HTTPException(status_code=403, detail="Unauthorized execution")
        
    bucket = storage.bucket()
    for url in snap.to_dict().get("image_urls", []):
        try:
            file_name = url.split("/")[-1].split("?")[0].replace("posts%", "posts/").replace("videos%", "videos/")
            bucket.blob(file_name).delete()
        except:
            pass
            
    post_ref.delete()
    return {"message": "Deleted"}

@app.get("/users/profile/{username}")
def get_user_profile(username: str):
    clean_user = username.strip() # Removed .lower() to avoid profile missing errors
    user_doc = db_fs.collection("users").document(clean_user).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail=f"User [{clean_user}] profile map not found.")

    user_data = user_doc.to_dict()
    post_count = (
        db_fs.collection("posts")
        .where(filter=firestore.FieldFilter("username", "==", clean_user))
        .count()
        .get()[0][0]
        .value
    )

    return {
        "username": clean_user,
        "profile_url": user_data.get("profile_url", ""),
        "country": user_data.get("country", ""),
        "city": user_data.get("city", ""),
        "post_count": post_count
    }

@app.get("/users/friends")
def get_all_friends():
    try:
        users_ref = db_fs.collection('users').get()
        friends_list = []
        for doc in users_ref:
            u_data = doc.to_dict()
            friends_list.append({
                "username": u_data.get("username"),
                "profile_url": u_data.get("profile_url", "")
            })
        return friends_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class FriendActionModel(BaseModel):
    current_user: str
    target_user: str

@app.post("/users/add-friend")
def add_friend(payload: FriendActionModel):
    user_a = payload.current_user.strip().lower()
    user_b = payload.target_user.strip().lower()
    if user_a == user_b:
        raise HTTPException(status_code=400, detail="You cannot add yourself.")
    
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).set({
        "username": user_b,
        "status": "outgoing",
        "timestamp": int(time.time())
    })
    
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).set({
        "username": user_a,
        "status": "incoming",
        "timestamp": int(time.time())
    })
    return {"status": "success", "message": "Friend request sent."}

@app.post("/users/accept-friend")
def accept_friend(payload: FriendActionModel):
    user_a = payload.current_user.strip().lower() 
    user_b = payload.target_user.strip().lower()  
    
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).update({"status": "accepted"})
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).update({"status": "accepted"})
    return {"status": "success"}

@app.post("/users/decline-friend")
def decline_friend(payload: FriendActionModel):
    user_a = payload.current_user.strip().lower()
    user_b = payload.target_user.strip().lower()
    
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).delete()
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).delete()
    return {"status": "success"}

@app.get("/users/friends/{username}")
def get_user_friends(username: str, search: Optional[str] = None):
    clean_user = username.strip().lower()
    friends_ref = db_fs.collection('users').document(clean_user).collection('friends').where(filter=firestore.FieldFilter("status", "==", "accepted"))
    docs = friends_ref.get()
    
    friends_list = []
    for doc in docs:
        f_user = doc.id
        if search and search.strip().lower() not in f_user:
            continue
        u_snap = db_fs.collection('users').document(f_user).get()
        if u_snap.exists:
            u_data = u_snap.to_dict()
            friends_list.append({
                "username": f_user,
                "profile_url": u_data.get("profile_url", "")
            })
    return friends_list

@app.get("/users/friend-status")
def check_friend_status(current_user: str, target_user: str):
    user_a = current_user.strip().lower()
    user_b = target_user.strip().lower()
    
    doc = db_fs.collection('users').document(user_a).collection('friends').document(user_b).get()
    if not doc.exists:
        return {"status": "none"}
    return {"status": doc.to_dict().get("status", "none")}

@app.get("/users/notifications-count/{username}")
def get_notifications_badge_count(username: str):
    clean_user = username.strip().lower()
    pending_count = db_fs.collection('users').document(clean_user).collection('friends').where(filter=firestore.FieldFilter("status", "==", "incoming")).count().get()[0][0].value
    return {"incoming_requests": pending_count}

@app.get("/users/pending-requests/{username}")
def get_pending_requests(username: str):
    clean_user = username.strip().lower()
    docs = db_fs.collection('users').document(clean_user).collection('friends').where(filter=firestore.FieldFilter("status", "==", "incoming")).get()
    
    requests_list = []
    for doc in docs:
        req_user = doc.id
        u_snap = db_fs.collection('users').document(req_user).get()
        if u_snap.exists:
            u_data = u_snap.to_dict()
            requests_list.append({
                "username": req_user,
                "profile_url": u_data.get("profile_url", "")
            })
    return requests_list

@app.get("/users/is-friend")
def check_is_friend_status(current_user: str, target_user: str):
    user_a = current_user.strip().lower()
    user_b = target_user.strip().lower()
    is_friend = db_fs.collection('users').document(user_a).collection('friends').document(user_b).get().exists
    return {"is_friend": is_friend}

class RoomCreatePayload(BaseModel):
    username: str
    room_name: str

class RoomProfilePayload(BaseModel):
    username: str
    room_id: str
    target_profile: str

class RoomDefaultPayload(BaseModel):
    username: str
    room_id: str

@app.post("/rooms/create")
def create_custom_room(payload: RoomCreatePayload):
    user_id = payload.username.strip().lower()
    room_name_clean = payload.room_name.strip()
    if not room_name_clean:
        raise HTTPException(status_code=400, detail="Room name cannot be blank.")
    
    room_id = str(uuid.uuid4())[:8]
    room_ref = db_fs.collection('users').document(user_id).collection('rooms').document(room_id)
    room_ref.set({
        "id": room_id,
        "name": room_name_clean,
        "profiles": [],
        "created_at": int(time.time())
    })
    return {"status": "success", "room_id": room_id}

@app.get("/rooms/list/{username}")
def list_user_rooms(username: str):
    user_id = username.strip().lower()
    rooms = db_fs.collection('users').document(user_id).collection('rooms').get()
    user_snap = db_fs.collection('users').document(user_id).get()
    default_room_id = user_snap.to_dict().get("default_room_id", "") if user_snap.exists else ""
    return [{**r.to_dict(), "is_default": (default_room_id != "" and r.id == default_room_id)} for r in rooms]

@app.post("/rooms/add-profile")
def add_profile_to_room(payload: RoomProfilePayload):
    user_id = payload.username.strip().lower()
    target = payload.target_profile.strip().lower()
    
    room_ref = db_fs.collection('users').document(user_id).collection('rooms').document(payload.room_id)
    room_snap = room_ref.get()
    if not room_snap.exists:
        raise HTTPException(status_code=404, detail="Room target not found.")
        
    profiles = room_snap.to_dict().get("profiles", [])
    if target not in profiles:
        profiles.append(target)
        room_ref.update({"profiles": profiles})
    return {"status": "success"}

@app.post("/rooms/set-default")
def set_default_app_room(payload: RoomDefaultPayload):
    user_id = payload.username.strip().lower()
    db_fs.collection('users').document(user_id).update({
        "default_room_id": payload.room_id
    })
    return {"status": "success"}

@app.get("/posts/room/{username}/{room_id}")
def get_room_filtered_posts(username: str, room_id: str, limit: int = 10, offset: int = 0):
    user_id = username.strip().lower()
    room_snap = db_fs.collection('users').document(user_id).collection('rooms').document(room_id).get()
    if not room_snap.exists:
        return []
        
    room_data = room_snap.to_dict()
    owner_id = room_data.get("owner", user_id) 
    master_room = db_fs.collection('users').document(owner_id).collection('rooms').document(room_id).get()
    if not master_room.exists:
        return []
        
    master_data = master_room.to_dict()
    profiles = master_data.get("profiles", [])
    joined_members = master_data.get("joined_members", [])
    query_profiles = list(set(profiles + joined_members + [owner_id]))

    query = db_fs.collection('posts').where(filter=firestore.FieldFilter("username", "in", query_profiles))
    query = query.order_by("timestamp", direction=firestore.Query.DESCENDING)
    docs = query.get()
    
    posts = []
    author_cache = {}
    for doc in docs:
        d = doc.to_dict()
        author = d.get("username", "")
        
        is_joined_member = author in joined_members and author != owner_id
        if is_joined_member and d.get("target_room_id") != room_id:
            continue
            
        if author not in author_cache:
            a_ref = db_fs.collection('users').document(author).get()
            author_cache[author] = a_ref.to_dict() if a_ref.exists else {}
            
        posts.append({
            "id": doc.id,
            "username": author,
            "user_avatar": author_cache[author].get("profile_url", ""),
            "author_country": author_cache[author].get("country", ""),
            "author_city": author_cache[author].get("city", ""),
            "message": d.get("message"),
            "image_urls": d.get("image_urls", []),
            "likes": d.get("likes", 0),
            "comment_count": db_fs.collection('posts').document(doc.id).collection('comments').count().get()[0][0].value,
            "timestamp": str(d.get("timestamp")) if d.get("timestamp") else None
        })
        
    start_index = offset
    end_index = offset + limit
    return posts[start_index:end_index]

class ReadReceiptPayload(BaseModel):
    conversation_id: str
    recipient: str  
    sender: str     

@app.post("/chat/read")
def mark_messages_as_read(payload: ReadReceiptPayload):
    chat_ref = db_fs.collection("chats").document(payload.conversation_id).collection("messages")
    unread_messages = chat_ref.where(filter=firestore.FieldFilter("sender", "==", payload.sender.strip())).where(filter=firestore.FieldFilter("read", "==", False)).get()
    for doc in unread_messages:
        doc.reference.update({"read": True})
    return {"status": "success", "updated_count": len(unread_messages)}

class RoomUpdatePayload(BaseModel):
    username: str
    room_id: str
    new_name: str
    profiles: List[str]

@app.put("/rooms/update")
def update_custom_room(payload: RoomUpdatePayload):
    user_id = payload.username.strip().lower()
    room_id = payload.room_id.strip()
    new_room_name = payload.new_name.strip()
    if not new_room_name:
        raise HTTPException(status_code=400, detail="Room name cannot be blank.")
        
    room_ref = db_fs.collection('users').document(user_id).collection('rooms').document(room_id)
    if not room_ref.get().exists:
        raise HTTPException(status_code=404, detail="Room not found.")
        
    cleaned_profiles = [p.strip().lower() for p in payload.profiles]
    room_ref.update({
        "name": new_room_name,
        "profiles": cleaned_profiles
    })
    return {"status": "success", "message": "Room updated successfully."}

@app.get("/users/profile/{username}/media")
def get_user_profile_media(username: str):
    clean_user = username.strip() # Removed .lower() so images show up on walls correctly
    posts_query = db_fs.collection('posts').where(filter=firestore.FieldFilter("username", "==", clean_user)).get()
    photos = []
    videos = []
    for doc in posts_query:
        post_data = doc.to_dict()
        image_urls = post_data.get("image_urls", [])
        for url in image_urls:
            if "videos/" in url or url.lower().endswith(('.mp4', '.mov', '.avi', '.webm')):
                videos.append(url)
            else:
                photos.append(url)
    return {"photos": photos, "videos": videos}

@app.post("/rooms/invite")
def send_room_invitation(payload: RoomInvitePayload):
    owner = payload.username.strip().lower()
    target = payload.target_user.strip().lower()
    room_ref = db_fs.collection('users').document(owner).collection('rooms').document(payload.room_id)
    room_snap = room_ref.get()
    if not room_snap.exists:
        raise HTTPException(status_code=404, detail="Target room configuration not found.")
    
    room_name = room_snap.to_dict().get("name", "Unnamed Room")
    existing = db_fs.collection('room_invitations').where(filter=firestore.FieldFilter("room_id", "==", payload.room_id)).where(filter=firestore.FieldFilter("recipient", "==", target)).where(filter=firestore.FieldFilter("status", "==", "pending")).get()
    if existing:
        raise HTTPException(status_code=400, detail="An invitation to this room is already pending.")
        
    invite_id = str(uuid.uuid4())[:8]
    db_fs.collection('room_invitations').document(invite_id).set({
        "id": invite_id,
        "room_id": payload.room_id,
        "room_name": room_name,
        "sender": owner,
        "recipient": target,
        "status": "pending",
        "timestamp": int(time.time())
    })
    return {"status": "success", "message": "Room invitation sent successfully."}

@app.get("/rooms/invitations/pending/{username}")
def get_pending_room_invitations(username: str):
    clean_user = username.strip().lower()
    docs = db_fs.collection('room_invitations').where(filter=firestore.FieldFilter("recipient", "==", clean_user)).where(filter=firestore.FieldFilter("status", "==", "pending")).get()
    return [doc.to_dict() for doc in docs]

@app.post("/rooms/invitations/handle")
def handle_room_invitation(payload: HandleInvitePayload):
    recipient = payload.username.strip().lower()
    action = payload.action.strip().lower()
    
    invite_ref = db_fs.collection('room_invitations').document(payload.invitation_id)
    invite_snap = invite_ref.get()
    if not invite_snap.exists:
        raise HTTPException(status_code=404, detail="Invitation not found.")
        
    invite_data = invite_snap.to_dict()
    if invite_data.get("recipient") != recipient:
        raise HTTPException(status_code=403, detail="Unauthorized action.")
        
    if action == "accept":
        sender = invite_data.get("sender")
        room_id = invite_data.get("room_id")
        room_ref = db_fs.collection('users').document(sender).collection('rooms').document(room_id)
        room_snap = room_ref.get()
        
        if room_ref.get().exists:
            room_data = room_snap.to_dict()
            joined_members = room_data.get("joined_members", [])
            if recipient not in joined_members:
                joined_members.append(recipient)
                room_ref.update({"joined_members": joined_members})
                
            db_fs.collection('users').document(recipient).collection('rooms').document(room_id).set({
                "id": room_id,
                "name": room_data.get("name"),
                "owner": sender,
                "is_shared_collaboration": True,
                "created_at": int(time.time())
            })
        invite_ref.update({"status": "accepted"})
    else:
        invite_ref.update({"status": "declined"})
    return {"status": "success"}

@app.post("/chat/upload-attachment")
async def chat_upload_attachment(
    username: str = Form(...),
    file: UploadFile = File(...)
):
    try:
        bucket = storage.bucket()
        c_type = (file.content_type or "").lower()
        f_name = (file.filename or "").lower()

        await file.seek(0)
        file_bytes = await file.read()

        if not file_bytes:
            raise HTTPException(status_code=400, detail="Empty attachment file bytes array received.")

        unique_id = uuid.uuid4()
        ext = f_name.split('.')[-1] if '.' in f_name else ''

        # ── Step 1: Detect video (by signature, content-type, or extension) ──
        is_mp4_signature = len(file_bytes) > 12 and b"ftyp" in file_bytes[4:12]
        is_video = (
            c_type.startswith("video/") or
            "video" in c_type or
            f_name.endswith(('.mp4', '.mov', '.avi', '.mkv', '.3gp', '.webm')) or
            is_mp4_signature
        )

        # ── Step 2: Detect image (by content-type OR extension) ──
        # Must be checked BEFORE document to prevent misrouting when
        # content-type is application/octet-stream but file is actually an image.
        IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif'}
        is_image = (
            c_type.startswith("image/") or
            ext in IMAGE_EXTENSIONS
        )

        # ── Step 3: Detect document (only true office/pdf types, never images) ──
        DOCUMENT_EXTENSIONS = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt'}
        is_document = (
            not is_image and
            not is_video and
            (
                c_type.startswith("application/pdf") or
                "msword" in c_type or
                "officedocument" in c_type or
                "spreadsheet" in c_type or
                "presentation" in c_type or
                ext in DOCUMENT_EXTENSIONS
            )
        )

        # ────────────────────────────────────────────────────────────────────
        if is_video:
            # Mirrors process_and_upload_media: compress → upload as video/mp4
            compressed_video_data = compress_video_heavy(file_bytes)
            blob_path = f"videos/{unique_id}.mp4"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": "video/mp4", "contentDisposition": "inline"}
            blob.upload_from_string(compressed_video_data, content_type="video/mp4")
            blob.content_type = "video/mp4"
            blob.patch()
            blob.make_public()
            return {"public_url": blob.public_url}

        elif is_document:
            # Mirrors process_and_upload_media: raw bytes, attachment disposition
            determined_type = c_type if c_type and c_type != "application/octet-stream" else "application/octet-stream"
            blob_path = f"documents/{unique_id}.{ext if ext else 'dat'}"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": determined_type, "contentDisposition": "attachment"}
            blob.upload_from_string(file_bytes, content_type=determined_type)
            blob.content_type = determined_type
            blob.patch()
            blob.make_public()
            return {"public_url": blob.public_url}

        else:
            # ── Image branch: mirrors process_and_upload_media exactly ──
            # 640×640 thumbnail, JPEG quality 50 → target ~18–36 KB like posts
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                max_resolution = (640, 640)
                img.thumbnail(max_resolution, Image.Resampling.LANCZOS)

                output = io.BytesIO()
                img.save(output, format="JPEG", quality=50, optimize=True)
                compressed_data = output.getvalue()

                blob_path = f"posts/{unique_id}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(compressed_data, content_type="image/jpeg")
                blob.make_public()
                return {"public_url": blob.public_url}

            except Exception as img_err:
                # Fallback: upload raw bytes (mirrors process_and_upload_media fallback)
                print(f"⚠️ Chat attachment image optimization bypassed: {img_err}")
                blob_path = f"posts/{unique_id}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(file_bytes, content_type="image/jpeg")
                blob.make_public()
                return {"public_url": blob.public_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Attachment processor engine failure: {str(e)}")