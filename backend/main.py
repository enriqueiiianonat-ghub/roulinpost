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
import ffmpeg  
from pathlib import Path
from PIL import Image, ImageOps
import tempfile
import subprocess
from pydantic import BaseModel
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
import time

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

async def process_and_upload_media(file: UploadFile) -> str:
    try:
        bucket = storage.bucket()
        c_type = (file.content_type or "").lower()
        f_name = (file.filename or "").lower()
        
        await file.seek(0)
        file_bytes = await file.read()
        
        if not file_bytes or len(file_bytes) == 0:
            print("⚠️ Upload Blocked: File byte array empty.")
            return ""

        is_mp4_signature = len(file_bytes) > 12 and b"ftyp" in file_bytes[4:12]
        is_video = (
            c_type.startswith("video/") or 
            "video" in c_type or
            f_name.endswith(('.mp4', '.mov', '.avi', '.mkv', '.3gp', '.webm')) or
            is_mp4_signature
        )
        
        if is_video:
            unique_id = uuid.uuid4()
            blob_path = f"videos/{unique_id}.mp4"
            blob = bucket.blob(blob_path)

            blob.metadata = {
                "contentType": "video/mp4",
                "contentDisposition": "inline"
            }

            blob.upload_from_string(
                file_bytes,
                content_type="video/mp4"
            )

            blob.content_type = "video/mp4"
            blob.patch()
            blob.make_public()

            return blob.public_url

        else:
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                    
                max_resolution = (1080, 1080)
                img.thumbnail(max_resolution, Image.Resampling.LANCZOS)
                
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=80, optimize=True)
                compressed_data = output.getvalue()
                
                blob_path = f"posts/{uuid.uuid4()}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(compressed_data, content_type="image/jpeg")
                blob.make_public()
                return blob.public_url
            except Exception as img_err:
                print(f"⚠️ Image parsing failed: {img_err}")
                blob_path = f"posts/{uuid.uuid4()}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg"}
                blob.upload_from_string(file_bytes, content_type="image/jpeg")
                blob.make_public()
                return blob.public_url
            
    except Exception as e:
        print(f"🔥 Critical Pipeline Failure: {str(e)}")
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
        "country": u_data.get("country", ""),  # ✨ Fetches saved string or sets blank
        "city": u_data.get("city", "")         # ✨ Fetches saved string or sets blank
    }

@app.put("/auth/profile/{current_username}")
async def update_profile(
    current_username: str,
    new_username: str = Form(...),
    new_email: str = Form(...),
    country: Optional[str] = Form(""),  # ✨ Intercept country field element stream
    city: Optional[str] = Form(""),     # ✨ Intercept city field element stream
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

    # ✨ Append the metadata updates cleanly into structural state payload
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
        
        # Pull extra profile info for metadata presentation fields
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
        
        # ✨ FIX: Compute real-time subcollection document length on database read
        comments_ref = db_fs.collection('posts').document(post_id).collection('comments')
        
        # Use aggregation count directly for optimized, fast lookups
        comment_count = comments_ref.count().get()[0][0].value
                
        posts.append({
            "id": post_id,
            "username": author,
            "user_avatar": author_cache[author]["avatar"], 
            "author_country": author_cache[author]["country"], # ✨ NEW field payload
            "author_city": author_cache[author]["city"],       # ✨ NEW field payload
            "message": d.get("message"),
            "image_urls": d.get("image_urls", []), 
            "likes": d.get("likes", 0),
            "comment_count": comment_count                     # ✨ Integrated live count variable
        })
    return posts

@app.post("/posts")
async def create_post(
    username: str = Form(...),
    message: Optional[str] = Form(None),
    files: List[UploadFile] = File([])
):
    if len(files) > 12:
        raise HTTPException(status_code=400, detail="Cannot upload more than 12 elements.")

    tasks = [process_and_upload_media(f) for f in files]
    results = await asyncio.gather(*tasks)
    
    media_urls = [url for url in results if url]
            
    post_ref = db_fs.collection('posts').document()
    post_ref.set({
        'username': username,
        'message': message or "",
        'image_urls': media_urls, 
        'likes': 0,
        'timestamp': firestore.SERVER_TIMESTAMP  
    })
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

def get_conversation_id(user1: str, user2: str) -> str:
    # Sort usernames alphabetically to maintain a unified channel between two users
    sorted_users = sorted([user1.lower().strip(), user2.lower().strip()])
    return f"{sorted_users[0]}_{sorted_users[1]}"

@app.post("/chat/send")
def send_direct_message(payload: ChatMessageSchema):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Message body cannot be blank.")
        
    conv_id = get_conversation_id(payload.sender, payload.recipient)
    now_ts = int(time.time())
    
    # 1. Update conversational overview channel meta documentation
    chat_room_ref = db_fs.collection('chats').document(conv_id)
    chat_room_ref.set({
        "participants": [payload.sender, payload.recipient],
        "last_message": payload.text,
        "last_updated": now_ts
    }, merge=True)
    
    # 2. Inject structural message document directly into subcollection payload
    message_ref = chat_room_ref.collection('messages').document()
    message_ref.set({
        "sender": payload.sender,
        "recipient": payload.recipient,
        "text": payload.text,
        "timestamp": now_ts
    })
    
    return {"status": "success", "message_id": message_ref.id}

class CommentModel(BaseModel):
    username: str
    text: str

@app.get("/posts/{post_id}/comments")
def get_comments(post_id: str):
    # Fetch subcollection documents inside target post document
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
        
    # Append comment payload directly into targeted inner Firestore subcollection
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
    
    # Send a request: user_a saves it as 'outgoing' status pending
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).set({
        "username": user_b,
        "status": "outgoing",
        "timestamp": int(time.time())
    })
    
    # Target user receives it as 'incoming' status pending
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).set({
        "username": user_a,
        "status": "incoming",
        "timestamp": int(time.time())
    })
    return {"status": "success", "message": "Friend request sent."}

@app.post("/users/accept-friend")
def accept_friend(payload: FriendActionModel):
    user_a = payload.current_user.strip().lower() # The person accepting
    user_b = payload.target_user.strip().lower()  # The person who sent it
    
    # Update both sides to accepted status
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).update({"status": "accepted"})
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).update({"status": "accepted"})
    return {"status": "success"}

@app.post("/users/decline-friend")
def decline_friend(payload: FriendActionModel):
    user_a = payload.current_user.strip().lower()
    user_b = payload.target_user.strip().lower()
    
    # Delete relations entirely if declined/removed
    db_fs.collection('users').document(user_a).collection('friends').document(user_b).delete()
    db_fs.collection('users').document(user_b).collection('friends').document(user_a).delete()
    return {"status": "success"}

@app.get("/users/friends/{username}")
def get_user_friends(username: str, search: Optional[str] = None):
    clean_user = username.strip().lower()
    # Filter query directly for accepted friendships
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
    
    # Count incoming requests that are pending approval
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
def check_friend_status(current_user: str, target_user: str):
    user_a = current_user.strip().lower()
    user_b = target_user.strip().lower()
    is_friend = db_fs.collection('users').document(user_a).collection('friends').document(user_b).get().exists
    return {"is_friend": is_friend}