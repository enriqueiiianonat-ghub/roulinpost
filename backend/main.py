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

from firebase_admin import messaging

from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form, Request, Response, APIRouter
import hashlib

def compute_etag(data) -> str:
    raw = py_json.dumps(data, sort_keys=True, default=str).encode('utf-8')
    return 'W/"' + hashlib.md5(raw).hexdigest() + '"'


def detect_image_signature(file_bytes: bytes) -> bool:
    """
    True byte-level sniffing for the most common image formats. This is
    the authoritative check — it can never be fooled by a wrong filename
    extension or a wrong Content-Type header, unlike string-based checks.
    Used as a guard so a photo can NEVER be misclassified as a video,
    even if its filename happens to end in .mp4 (the root cause of the
    "photo renders as a broken black video" bug on desktop web).
    """
    if len(file_bytes) < 12:
        return False
    if file_bytes[0:3] == b"\xff\xd8\xff":          # JPEG
        return True
    if file_bytes[0:8] == b"\x89PNG\r\n\x1a\n":      # PNG
        return True
    if file_bytes[0:6] in (b"GIF87a", b"GIF89a"):    # GIF
        return True
    if file_bytes[0:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":  # WEBP
        return True
    return False


SUPPORTED_IMAGE_TYPES_MSG = "JPG, JPEG, PNG, GIF, WEBP, BMP"
SUPPORTED_VIDEO_TYPES_MSG = "MP4, MOV, AVI, MKV, 3GP, WEBM"
SUPPORTED_DOCUMENT_TYPES_MSG = "PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, TXT"
FILE_NOT_RECOGNIZED_MSG = (
    "File not recognized. Supported file types — "
    f"Images: {SUPPORTED_IMAGE_TYPES_MSG}; "
    f"Videos: {SUPPORTED_VIDEO_TYPES_MSG}; "
    f"Documents: {SUPPORTED_DOCUMENT_TYPES_MSG}."
)


def delete_storage_blob_from_url(url: str):

    
    """
    Reliably deletes a Firebase Storage blob given its PUBLIC url
    (the kind produced by blob.make_public()), regardless of which
    folder it lives in (posts/, videos/, documents/, avatars/).

    Old logic used to do url.split("/")[-1] + string replaces that were
    written for legacy %2F-encoded download URLs — that pattern doesn't
    match make_public() URLs at all, so blob.exists() was always False
    and files were silently never removed from Storage.
    """
    if not url:
        return
    try:
        bucket = storage.bucket()
        marker = f"/{BUCKET_NAME}/"
        if marker in url:
            # e.g. https://storage.googleapis.com/<bucket>/posts/uuid.jpg
            blob_path = url.split(marker, 1)[1].split("?")[0]
        else:
            # Fallback for legacy encoded firebasestorage.app links
            blob_path = url.split("/o/")[-1].split("?")[0]
            blob_path = blob_path.replace("%2F", "/").replace("%2f", "/")

        blob = bucket.blob(blob_path)
        if blob.exists():
            blob.delete()
        else:
            print(f"⚠️ Storage cleanup: blob not found for path '{blob_path}' (url: {url})")
    except Exception as e:
        print(f"⚠️ Storage cleanup failed for url {url}: {e}")


DOCUMENT_MIME_MAP = {
    'pdf': 'application/pdf',
    'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'txt': 'text/plain',
}


def migrate_username_references(old_username: str, new_username: str):
    """
    Called after a username rename, BEFORE the old user document is
    deleted. Firestore does NOT cascade-move subcollections or update
    string references when a doc is renamed/recreated — every place the
    old username was stored as data has to be migrated here manually:

      1. Chats + messages   — conversation doc IDs are username-derived
                               (get_conversation_id sorts the two names),
                               so renaming a participant changes the doc
                               ID itself. This is why messages "orphan":
                               the old conversation becomes unreachable
                               under the new username.
      2. This user's own 'friends' and 'rooms' subcollections — these
         live under users/{old_username}/... and are silently abandoned
         when the parent doc is deleted (subcollections don't cascade).
      3. Other users' 'friends' doc that points at this username (doc ID
         == old username) and their rooms' profiles[]/joined_members[]
         arrays that list this username as a string.
      4. Comments this user left on other people's posts.
    """
    old_clean = old_username.strip().lower()
    new_clean = new_username.strip().lower()

    # ---------- 1. CHATS + MESSAGES ----------
    chats_query = db_fs.collection('chats').where(
        filter=firestore.FieldFilter("participants", "array_contains", old_clean)
    ).get()

    for chat_doc in chats_query:
        chat_data = chat_doc.to_dict()
        old_participants = chat_data.get("participants", [])
        new_participants = [
            new_clean if p.lower().strip() == old_clean else p
            for p in old_participants
        ]

        new_conv_id = (
            get_conversation_id(new_participants[0], new_participants[1])
            if len(new_participants) == 2
            else chat_doc.id
        )

        new_chat_ref = db_fs.collection('chats').document(new_conv_id)
        new_chat_data = dict(chat_data)
        new_chat_data["participants"] = new_participants
        new_chat_ref.set(new_chat_data)

        # Copy every message into the new conversation doc, rewriting
        # sender/recipient so the message history stays attributed correctly
        old_messages = chat_doc.reference.collection('messages').get()
        for m_doc in old_messages:
            m_data = m_doc.to_dict()
            if (m_data.get("sender") or "").lower().strip() == old_clean:
                m_data["sender"] = new_clean
            if (m_data.get("recipient") or "").lower().strip() == old_clean:
                m_data["recipient"] = new_clean
            new_chat_ref.collection('messages').document(m_doc.id).set(m_data)
            m_doc.reference.delete()

        # Only remove the old doc if the ID actually changed
        if new_conv_id != chat_doc.id:
            chat_doc.reference.delete()

    # ---------- 2a. OWN 'friends' SUBCOLLECTION ----------
    old_friends = db_fs.collection('users').document(old_clean).collection('friends').get()
    for f_doc in old_friends:
        db_fs.collection('users').document(new_clean).collection('friends').document(f_doc.id).set(f_doc.to_dict())
        f_doc.reference.delete()

    # ---------- 2b. OWN 'rooms' SUBCOLLECTION ----------
    old_rooms = db_fs.collection('users').document(old_clean).collection('rooms').get()
    for r_doc in old_rooms:
        r_data = r_doc.to_dict()
        if r_data.get("owner") == old_clean:
            r_data["owner"] = new_clean
        db_fs.collection('users').document(new_clean).collection('rooms').document(r_doc.id).set(r_data)
        r_doc.reference.delete()

    # ---------- 3. OTHER USERS' REFERENCES TO THIS USERNAME ----------
    all_users = db_fs.collection('users').get()
    for u_doc in all_users:
        if u_doc.id in (old_clean, new_clean):
            continue

        # 3a. their friend-doc keyed by the old username
        friend_ref = db_fs.collection('users').document(u_doc.id).collection('friends').document(old_clean)
        friend_snap = friend_ref.get()
        if friend_snap.exists:
            f_data = friend_snap.to_dict()
            f_data["username"] = new_clean
            db_fs.collection('users').document(u_doc.id).collection('friends').document(new_clean).set(f_data)
            friend_ref.delete()

        # 3b. their rooms' profiles[] / joined_members[] string arrays
        rooms_ref = db_fs.collection('users').document(u_doc.id).collection('rooms')
        for room_doc in rooms_ref.get():
            r_data = room_doc.to_dict()
            changed = False
            profiles = r_data.get("profiles", [])
            if old_clean in profiles:
                profiles = [new_clean if p == old_clean else p for p in profiles]
                changed = True
            joined_members = r_data.get("joined_members", [])
            if old_clean in joined_members:
                joined_members = [new_clean if m == old_clean else m for m in joined_members]
                changed = True
            if changed:
                room_doc.reference.update({"profiles": profiles, "joined_members": joined_members})

    # ---------- 4. COMMENTS authored under the old username ----------
    all_posts = db_fs.collection('posts').get()
    for p_doc in all_posts:
        old_comments = p_doc.reference.collection('comments').where(
            filter=firestore.FieldFilter("username", "==", old_clean)
        ).get()
        for c_doc in old_comments:
            c_doc.reference.update({"username": new_clean})


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
    expose_headers=["ETag"],   # ✨ NEW — without this, web can't read the ETag header at all
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


def send_fcm_push_notification(target_username: str, title: str, body: str, badge_count: int = 1):
        """
        Looks up a target user's FCM registration tokens from Firestore 
        and sends a background push notification to trigger system alerts and icon dots.
        """
        clean_target = target_username.strip().lower()
        try:
            # Fetch target user metadata to grab their active device notification tokens
            user_doc = db_fs.collection('users').document(clean_target).get()
            if not user_doc.exists:
                return
            
            user_data = user_doc.to_dict()
            # ✨ Note: Your frontend will need to save this token to the user document upon login
            fcm_tokens = user_data.get("fcm_tokens", [])
            if not fcm_tokens:
                print(f"ℹ️ Skipping push: No registered device tokens found for @{clean_target}")
                return

            for token in fcm_tokens:
                message = messaging.Message(
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                    ),
                    # 🟢 Android Subsystem Layer Configuration:
                    android=messaging.AndroidConfig(
                        priority="high",  # Wakes device from sleep / closed state
                        notification=messaging.AndroidNotification(
                            title=title,
                            body=body,
                            sound="default",  # Plays system-default alert ringtone
                            channel_id="high_importance_channel", # Maps to native channel id
                            notification_priority=messaging.AndroidNotificationPriority.PRIORITY_HIGH
                        )
                    ),
                    # 🟢 iOS PWA WebKit Layer Configuration:
                    webpush=messaging.WebpushConfig(
                        headers={
                            "urgency": "high"  # Demands prompt APNs cellular wake up
                        },
                        notification=messaging.WebpushNotification(
                            title=title,
                            body=body,
                            icon="/icons/icon-192x192.png",
                            badge="/icons/badge-72x72.png",
                            # Note: iOS PWA environment sound behavior is determined by the phone's 
                            # active ring/silent side switch and the web browser sound permission.
                        ),
                        fcm_options=messaging.WebpushFCMOptions(
                            link="/"
                        )
                    ),
                    token=token,
                )
                messaging.send(message)
            print(f"🚀 Push notification broadcast successfully to @{clean_target}")
        except Exception as push_err:
            print(f"⚠️ FCM Push Dispatch Engine Failed: {push_err}")

class FcmTokenPayload(BaseModel):
    username: str
    token: str

@app.post("/users/save-fcm-token")
def save_fcm_token(payload: FcmTokenPayload):
    clean_user = payload.username.strip().lower()
    user_ref = db_fs.collection('users').document(clean_user)
    user_snap = user_ref.get()
    if user_snap.exists:
        user_ref.update({
            "fcm_tokens": firestore.ArrayUnion([payload.token])
        })
        return {"status": "success", "message": "FCM device token registered for background delivery"}
    raise HTTPException(status_code=404, detail="User not found")


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
                "-crf", "26",              # ✨ Higher quality (Lower CRF means much sharper details)
                "-preset", "veryfast",     # ✨ Better frame compression logic than superfast
                "-vf", "scale=w='if(gte(iw,ih),min(720,iw),-2)':h='if(lt(iw,ih),min(720,iw),-2)'", # ✨ Bumped up to HD 720p
                "-acodec", "aac", 
                "-b:a", "64k",             # ✨ Crisp, clear mobile audio track
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
            compressed_video_data = compress_video_heavy(file_bytes)
            unique_id = uuid.uuid4()
            blob_path = f"videos/{unique_id}.mp4"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": "video/mp4", "contentDisposition": "inline"}
            blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
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
            blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
            blob.upload_from_string(file_bytes, content_type=determined_type)
            blob.content_type = determined_type
            blob.patch()
            blob.make_public()
            return blob.public_url

        else:
            # ✨ FIX: if the bytes can't actually be decoded as an image
            # (e.g. HEIC/HEIF straight from an iPhone — standard Pillow
            # can't read it without an extra codec), the old code silently
            # uploaded the raw, undecoded bytes mislabeled as image/jpeg.
            # That's exactly the broken/black-photo bug. Now we reject
            # clearly instead of pretending it's a valid JPEG.
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img.load()  # forces full decode now, not lazily later
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
                blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
                blob.upload_from_string(compressed_data, content_type="image/jpeg")
                blob.make_public()
                return blob.public_url
            except Exception as img_err:
                print(f"⚠️ Unrecognized file format rejected: {img_err}")
                raise HTTPException(status_code=415, detail=FILE_NOT_RECOGNIZED_MSG)
            
    except HTTPException:
        raise
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
        blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
        blob.upload_from_string(output.getvalue(), content_type="image/jpeg")
        blob.make_public()
        return blob.public_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Avatar upload failed: {str(e)}")

@app.post("/auth/register")
async def register(user: UserRegister):
    try:
        clean_username = user.username.strip().lower()

        # ✨ FIX: Block registration if username is already a confirmed account.
        user_ref = db_fs.collection('users').document(clean_username)
        if user_ref.get().exists:
            raise HTTPException(status_code=400, detail="User Name Already Exist")

        pending_ref = db_fs.collection('unverified_users').document(clean_username)
        pending_snap = pending_ref.get()
        if pending_snap.exists:
            pending_data = pending_snap.to_dict()
            otp_created_at = pending_data.get("created_at")

            # ✨ NEW: If a previous OTP request for this exact username is still
            # within its valid window (10 minutes), block re-registration too —
            # someone else may be mid-verification for this same username.
            # Only allow overwrite if that pending request has gone stale/expired.
            is_still_valid = False
            if otp_created_at is not None:
                try:
                    age_seconds = time.time() - otp_created_at.timestamp()
                    is_still_valid = age_seconds < 600  # 10 minute OTP validity window
                except Exception:
                    is_still_valid = False

            if is_still_valid:
                raise HTTPException(status_code=400, detail="User Name Already Exist")

            # Stale/expired pending registration — safe to clear and let this request proceed
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

class ForgotPasswordRequest(BaseModel):
    username: str

@app.post("/auth/forgot-password")
def forgot_password(payload: ForgotPasswordRequest):
    clean_user = payload.username.strip().lower()
    user_ref = db_fs.collection('users').document(clean_user)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="No account found with that username.")

    user_data = snap.to_dict()
    email = user_data.get("email")
    password = user_data.get("password")
    if not email:
        raise HTTPException(status_code=400, detail="No email on file for this account.")

    email_html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px;">
        <h2>ROULIN POST — Password Recovery</h2>
        <p>Hi @{clean_user},</p>
        <p>You (or someone using your username) requested your account password. Here it is:</p>
        <div style="font-size: 22px; font-weight: bold; padding: 16px; background: #f2f2f2; text-align: center; letter-spacing: 2px;">
            {password}
        </div>
        <p>For your security, consider changing it after logging in if you suspect anyone else has access to this email inbox.</p>
        <p>If you didn't request this, please secure your email account — someone else may be trying to access your Roulin Post profile.</p>
    </div>
    """
    try:
        resend.Emails.send({
            "from": "no-reply@roulinpost.com",
            "to": email,
            "subject": "ROULIN POST - Your Account Password",
            "html": email_html,
        })
    except Exception as email_error:
        raise HTTPException(status_code=500, detail=f"Failed to send recovery email: {str(email_error)}")

    return {"message": "Your password has been sent to your registered email."}


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

        # ✨ FIX: Re-tag every post authored under the OLD username to the
        # NEW username BEFORE the old user doc is deleted. Without this,
        # the posts still carry "username": old_name forever — so
        # /posts?username=new_name finds nothing (posts "disappear"),
        # and the author-avatar lookup in get_posts() fails too, since it
        # looks up users/{old_name}, which no longer exists.
        old_posts = db_fs.collection('posts').where(
            filter=firestore.FieldFilter("username", "==", padding_current)
        ).get()

        batch = db_fs.batch()
        batch_count = 0
        for p_doc in old_posts:
            batch.update(p_doc.reference, {"username": clean_new})
            batch_count += 1
            if batch_count >= 450:  # stay safely under Firestore's 500-write batch limit
                batch.commit()
                batch = db_fs.batch()
                batch_count = 0
        if batch_count > 0:
            batch.commit()

        # ✨ NEW: Migrate every other place the old username was stored —
        # chats/messages (the source of your orphaned-messages bug), this
        # user's own friends/rooms subcollections, other users' references
        # to this username, and comments. Must run BEFORE user_ref.delete()
        # below, since chats/friends/rooms read from users/{old_username}.
        migrate_username_references(padding_current, clean_new)

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
        delete_storage_blob_from_url(avatar_url)

    try:
        user_posts_query = db_fs.collection('posts').where(filter=firestore.FieldFilter("username", "==", clean_username)).get()
        for doc in user_posts_query:
            for url in doc.to_dict().get("image_urls", []):
                delete_storage_blob_from_url(url)

            # Purge each post's comments subcollection too
            comment_docs = doc.reference.collection('comments').get()
            for c_doc in comment_docs:
                c_doc.reference.delete()

            db_fs.collection('posts').document(doc.id).delete()
        user_ref.delete()
        return {"message": "Account, posts, comments, and files deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/posts")
def get_posts(request: Request, response: Response, limit: int = 10, offset: int = 0, username: Optional[str] = None):
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

    # ✨ NEW — ETag check
    etag = compute_etag(posts)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
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

    for old_url in old_post.get("image_urls", []):
        if old_url not in retained_urls:
            delete_storage_blob_from_url(old_url)

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
    
    # ✨ FIX: Calculate unread count to drive the badging, then dispatch push notification
    summary = get_incoming_unread_chat_notifications(payload.recipient)
    total_unread = sum(summary.values()) if summary else 1

    notification_body = "Sent you an attachment" if not payload.text.strip() else payload.text
    send_fcm_push_notification(
        target_username=payload.recipient,
        title=f"New message from @{payload.sender}",
        body=notification_body,
        badge_count=total_unread
    )
    
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


@app.get("/sync/{username}")
def get_sync_status(username: str):
    """
    Lightweight polling endpoint. Returns only counts/badges needed to drive
    the bell icon and chat badges — never full post/comment bodies.
    Replaces 3+ separate polling calls (chat/notifications, rooms/invitations/pending,
    posts + per-post comment counts) with a single round trip.
    """
    clean_user = username.strip().lower()

    # 1. Chat unread badges (same logic as /chat/notifications, just inlined)
    chat_badges = {}
    chats_query = db_fs.collection('chats').where(
        filter=firestore.FieldFilter("participants", "array_contains", clean_user)
    ).get()

    for chat_doc in chats_query:
        participants = chat_doc.to_dict().get("participants", [])
        sender_targets = [p for p in participants if p.lower().strip() != clean_user]
        if not sender_targets:
            continue
        sender_label = sender_targets[0]

        messages_ref = chat_doc.reference.collection('messages')
        unread_docs = messages_ref.where(
            filter=firestore.FieldFilter("sender", "==", sender_label)
        ).where(
            filter=firestore.FieldFilter("read", "==", False)
        ).get()

        if len(unread_docs) > 0:
            chat_badges[sender_label] = len(unread_docs)

    # 2. Pending room invitations — just the count, the modal fetches the full list on-demand
    invite_docs = db_fs.collection('room_invitations').where(
        filter=firestore.FieldFilter("recipient", "==", clean_user)
    ).where(
        filter=firestore.FieldFilter("status", "==", "pending")
    ).get()
    pending_invites_count = len(invite_docs)

    # 3. Unread comments on my posts — count only, never the comment text itself
    my_posts_query = db_fs.collection('posts').where(
        filter=firestore.FieldFilter("username", "==", clean_user)
    ).get()

    unread_comment_count = 0
    for post_doc in my_posts_query:
        comment_docs = post_doc.reference.collection('comments').get()
        unread_comment_count += sum(
            1 for c in comment_docs
            if (c.to_dict().get("username") or "").strip().lower() != clean_user
        )

    return {
        "chat_badges": chat_badges,
        "pending_room_invites": pending_invites_count,
        "unread_comment_count": unread_comment_count,
    }



@app.get("/posts/{post_id}/comments")
def get_comments(request: Request, response: Response, post_id: str):
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

    # ✨ NEW — L3: ETag/304. Comments are re-fetched constantly (notification
    # modal, reopening the comments sheet) — an unchanged thread now costs
    # an empty 304 instead of the full list every time.
    etag = compute_etag(comments_list)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
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
def get_chat_history(request: Request, response: Response, conversation_id: str):
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
            "id": doc.id,
            "sender": d.get("sender"),
            "recipient": d.get("recipient"),
            "text": d.get("text"),
            "media_url": d.get("media_url"), 
            "timestamp": d.get("timestamp")
        })

    # ✨ NEW — L3 ETag Optimization for Chat Threads
    # Computes a hash of the entire message thread representation.
    # If a message is sent, edited, or deleted, the hash instantly shifts.
    etag = compute_etag(messages)
    if_none_match = request.headers.get("if-none-match")
    
    if if_none_match and if_none_match == etag:
        # Client already has the exact message snapshot locally. Skip payload delivery completely.
        return Response(status_code=304)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache" # Tells browser/app to revalidate every time
    return messages

@app.get("/chat/conversations/{username}")
def get_chat_conversations(request: Request, response: Response, username: str):
    """
    Returns EVERY conversation this user has ever had — read or unread —
    each with the partner's current avatar and last message preview.
    This replaces the old approach of deriving the chat list from
    /sync's unread-only badges, which is why names used to vanish the
    moment you'd read all of someone's messages.
    """
    clean_user = username.strip().lower()
    chats_query = db_fs.collection('chats').where(
        filter=firestore.FieldFilter("participants", "array_contains", clean_user)
    ).get()

    conversations = []
    for chat_doc in chats_query:
        chat_data = chat_doc.to_dict()
        participants = chat_data.get("participants", [])
        partner_candidates = [p for p in participants if p.lower().strip() != clean_user]
        if not partner_candidates:
            continue
        partner = partner_candidates[0]

        messages_ref = chat_doc.reference.collection('messages')
        unread_docs = messages_ref.where(
            filter=firestore.FieldFilter("sender", "==", partner)
        ).where(
            filter=firestore.FieldFilter("read", "==", False)
        ).get()

        partner_snap = db_fs.collection('users').document(partner).get()
        partner_avatar = partner_snap.to_dict().get("profile_url", "") if partner_snap.exists else ""

        conversations.append({
            "conversation_id": chat_doc.id,
            "username": partner,
            "profile_url": partner_avatar,
            "last_message": chat_data.get("last_message", ""),
            "last_updated": chat_data.get("last_updated", 0),
            "unread_count": len(unread_docs),
        })

    conversations.sort(key=lambda c: c.get("last_updated", 0), reverse=True)

    # ✨ NEW — L3: ETag/304. This is polled every 3 seconds while Chat Mode
    # is open. Without this, that's a full JSON payload every 3 seconds,
    # forever, for every logged-in user. With it, an unchanged list costs
    # an empty 304 — easily the single biggest egress saver in this pass.
    etag = compute_etag(conversations)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return conversations


@app.delete("/chat/conversation/{conversation_id}")
def delete_conversation(conversation_id: str, username: str):
    """Deletes an entire conversation (all messages + the chat doc itself).
    Either participant of a 1-on-1 DM is allowed to clear it."""
    clean_user = username.strip().lower()
    chat_ref = db_fs.collection('chats').document(conversation_id)
    chat_snap = chat_ref.get()
    if not chat_snap.exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    participants = [p.lower().strip() for p in chat_snap.to_dict().get("participants", [])]
    if clean_user not in participants:
        raise HTTPException(status_code=403, detail="Unauthorized")

    messages = chat_ref.collection('messages').get()
    for m in messages:
        m.reference.delete()
    chat_ref.delete()

    return {"status": "success", "message": "Conversation deleted"}


@app.delete("/chat/message/{conversation_id}/{message_id}")
def delete_chat_message(conversation_id: str, message_id: str, username: str):
    """Deletes a single message. Only the original sender may delete it."""
    clean_user = username.strip().lower()
    msg_ref = (
        db_fs.collection('chats')
        .document(conversation_id)
        .collection('messages')
        .document(message_id)
    )
    msg_snap = msg_ref.get()
    if not msg_snap.exists:
        raise HTTPException(status_code=404, detail="Message not found")

    if (msg_snap.to_dict().get("sender") or "").lower().strip() != clean_user:
        raise HTTPException(status_code=403, detail="Only the sender can delete this message")

    msg_ref.delete()
    return {"status": "success", "message": "Message deleted"}


@app.delete("/posts/{post_id}")
def delete_post(post_id: str, username: str):
    post_ref = db_fs.collection('posts').document(post_id)
    snap = post_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Post not found")
    if snap.to_dict().get("username") != username:
        raise HTTPException(status_code=403, detail="Unauthorized execution")

    post_data = snap.to_dict()

    # 1. Purge every attached media file (images/videos) from Storage
    for url in post_data.get("image_urls", []):
        delete_storage_blob_from_url(url)

    # 2. Purge the post's comments subcollection — Firestore does NOT
    #    cascade-delete subcollections automatically, so without this
    #    every comment ever made on this post stays orphaned forever.
    comments_ref = post_ref.collection('comments')
    comment_docs = comments_ref.get()
    for c_doc in comment_docs:
        c_doc.reference.delete()

    # 3. Finally, delete the post document itself
    post_ref.delete()

    return {
        "message": "Deleted",
        "media_files_removed": len(post_data.get("image_urls", [])),
        "comments_removed": len(comment_docs),
    }

@app.get("/users/profile/{username}")
def get_user_profile(request: Request, response: Response, username: str):
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

    result = {
        "username": clean_user,
        "profile_url": user_data.get("profile_url", ""),
        "country": user_data.get("country", ""),
        "city": user_data.get("city", ""),
        "post_count": post_count
    }

    # ✨ NEW — L3: ETag/304 for profile metadata.
    etag = compute_etag(result)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return result

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

    # ✨ FIX: Trigger Push notification to user_b
    send_fcm_push_notification(
        target_username=user_b,
        title="New Friend Request",
        body=f"@{user_a} sent you a friend request!",
        badge_count=1
    )

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
        "joined_members": [],  # ✨ NEW: explicit, so Edit Room can show it even on day one
        "owner": user_id,      # ✨ NEW: explicit owner tag for permission checks
        "created_at": int(time.time())
    })
    return {"status": "success", "room_id": room_id}

@app.get("/rooms/list/{username}")
def list_user_rooms(request: Request, response: Response, username: str):
    user_id = username.strip().lower()
    rooms = db_fs.collection('users').document(user_id).collection('rooms').get()
    user_snap = db_fs.collection('users').document(user_id).get()
    default_room_id = user_snap.to_dict().get("default_room_id", "") if user_snap.exists else ""
    result = [{**r.to_dict(), "is_default": (default_room_id != "" and r.id == default_room_id)} for r in rooms]

    # ✨ NEW — L3: ETag/304. Hit by the Rooms Hub, login restore, invite/
    # add-to-room dialogs, and the My Rooms list.
    etag = compute_etag(result)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return result

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
def get_room_filtered_posts(request: Request, response: Response, username: str, room_id: str, limit: int = 10, offset: int = 0):
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
    paged_posts = posts[start_index:end_index]

    etag = compute_etag(paged_posts)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return paged_posts



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
    joined_members: Optional[List[str]] = None  # ✨ NEW

@app.put("/rooms/update")
def update_custom_room(payload: RoomUpdatePayload):
    user_id = payload.username.strip().lower()
    room_id = payload.room_id.strip()
    new_room_name = payload.new_name.strip()
    if not new_room_name:
        raise HTTPException(status_code=400, detail="Room name cannot be blank.")
        
    room_ref = db_fs.collection('users').document(user_id).collection('rooms').document(room_id)
    room_snap = room_ref.get()
    if not room_snap.exists:
        raise HTTPException(status_code=404, detail="Room not found.")

    # ✨ NEW: only the real owner may edit. A joined member's local copy
    # is tagged is_shared_collaboration=True and is never the master doc.
    existing_data = room_snap.to_dict()
    if existing_data.get("is_shared_collaboration") is True:
        raise HTTPException(status_code=403, detail="Only the room owner can edit this room.")

    cleaned_profiles = [p.strip().lower() for p in payload.profiles]
    update_payload = {
        "name": new_room_name,
        "profiles": cleaned_profiles,
    }

    if payload.joined_members is not None:
        cleaned_members = [m.strip().lower() for m in payload.joined_members]
        update_payload["joined_members"] = cleaned_members

        # ✨ NEW: if the owner removed a member here, also delete that
        # member's stale local copy so it stops appearing in their
        # own "My Rooms" list.
        old_members = set(existing_data.get("joined_members", []))
        removed_members = old_members - set(cleaned_members)
        for removed in removed_members:
            stale_ref = db_fs.collection('users').document(removed).collection('rooms').document(room_id)
            if stale_ref.get().exists:
                stale_ref.delete()

    room_ref.update(update_payload)
    return {"status": "success", "message": "Room updated successfully."}

@app.delete("/rooms/delete")
def delete_room(username: str, room_id: str):
    """Deletes a room entirely. Only the actual owner may do this."""
    user_id = username.strip().lower()
    room_ref = db_fs.collection('users').document(user_id).collection('rooms').document(room_id)
    room_snap = room_ref.get()
    if not room_snap.exists:
        raise HTTPException(status_code=404, detail="Room not found.")

    room_data = room_snap.to_dict()
    if room_data.get("is_shared_collaboration") is True:
        raise HTTPException(status_code=403, detail="Only the room owner can delete this room.")

    for member in room_data.get("joined_members", []):
        member_room_ref = db_fs.collection('users').document(member).collection('rooms').document(room_id)
        if member_room_ref.get().exists:
            member_room_ref.delete()

    pending_invites = db_fs.collection('room_invitations').where(
        filter=firestore.FieldFilter("room_id", "==", room_id)
    ).get()
    for inv in pending_invites:
        inv.reference.delete()

    room_ref.delete()
    return {"status": "success", "message": "Room deleted successfully."}


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
        # ── Step 1: Detect image FIRST via real byte sniffing. This must
        # win over any filename/content-type claim — it's what stops a
        # photo with a wrongly-defaulted ".mp4" filename from ever being
        # treated as a video. ──
        is_definitely_image = detect_image_signature(file_bytes)

        # ── Step 2: Detect video (by signature, content-type, or extension) ──
        is_mp4_signature = len(file_bytes) > 12 and b"ftyp" in file_bytes[4:12]
        is_video = (not is_definitely_image) and (
            c_type.startswith("video/") or
            "video" in c_type or
            f_name.endswith(('.mp4', '.mov', '.avi', '.mkv', '.3gp', '.webm')) or
            is_mp4_signature
        )

        # ── Step 3: Detect image (by content-type, extension, OR signature) ──
        IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif'}
        is_image = (
            is_definitely_image or
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
        # VIDEO BRANCH (Saved to videos/ folder)
        # ────────────────────────────────────────────────────────────────────
        if is_video:
            compressed_video_data = compress_video_heavy(file_bytes)
            blob_path = f"videos/{unique_id}.mp4"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": "video/mp4", "contentDisposition": "inline"}
            blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
            blob.upload_from_string(compressed_video_data, content_type="video/mp4")
            blob.content_type = "video/mp4"
            blob.patch()
            blob.make_public()
            return {"public_url": blob.public_url}

        # ────────────────────────────────────────────────────────────────────
        # DOCUMENT BRANCH (Saved to documents/ folder)
        # ────────────────────────────────────────────────────────────────────
        elif is_document:
            # ✨ FIX: prefer a known extension→MIME mapping over whatever
            # the client sent. Now that the client sends the real content
            # type too, this is a defensive backstop for any client that
            # still sends something generic.
            determined_type = DOCUMENT_MIME_MAP.get(
                ext,
                c_type if c_type and c_type != "application/octet-stream" else "application/octet-stream"
            )
            blob_path = f"documents/{unique_id}.{ext if ext else 'dat'}"
            blob = bucket.blob(blob_path)
            blob.metadata = {"contentType": determined_type, "contentDisposition": "attachment"}
            blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
            blob.upload_from_string(file_bytes, content_type=determined_type)
            blob.content_type = determined_type
            blob.patch()
            blob.make_public()
            return {"public_url": blob.public_url}

        # ────────────────────────────────────────────────────────────────────
        # IMAGE BRANCH (Compressed AND saved to documents/ folder)
        # ────────────────────────────────────────────────────────────────────
        else:
            try:
                # 1. Open and automatically rotate image
                img = Image.open(io.BytesIO(file_bytes))
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                # 2. Re-scale to match your post quality footprint
                max_resolution = (640, 640)
                img.thumbnail(max_resolution, Image.Resampling.LANCZOS)

                # 3. Apply JPEG Quality 50 optimization (<50KB target)
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=50, optimize=True)
                compressed_data = output.getvalue()

                # 4. Save into the separated 'documents/' folder as requested
                blob_path = f"documents/{unique_id}.jpg"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": "image/jpeg", "contentDisposition": "inline"}
                blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
                blob.upload_from_string(compressed_data, content_type="image/jpeg")
                blob.make_public()
                return {"public_url": blob.public_url}

            except Exception as img_err:
                # Fallback path if data stream cannot be decoded as a valid image file
                print(f"⚠️ Attachment image optimization bypassed: {img_err}")
                determined_type = c_type if c_type and c_type != "application/octet-stream" else "application/octet-stream"
                blob_path = f"documents/{unique_id}.{ext if ext else 'dat'}"
                blob = bucket.blob(blob_path)
                blob.metadata = {"contentType": determined_type, "contentDisposition": "attachment"}
                blob.cache_control = "public, max-age=31536000, immutable"  # ✨ NEW
                blob.upload_from_string(file_bytes, content_type=determined_type)
                blob.make_public()
                return {"public_url": blob.public_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Attachment processor engine failure: {str(e)}")
    

    