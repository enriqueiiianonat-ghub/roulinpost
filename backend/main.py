import os
import random
import io
import uuid
import json as py_json
from typing import List, Optional
from pydantic import BaseModel, EmailStr
from PIL import Image, ImageOps

from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

import firebase_admin
from firebase_admin import credentials, firestore, storage

app = FastAPI(title="EZGEE Social API")

# Enable CORS for Flutter app (Web, Mobile, Desktop)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Firebase Secure Initialisation ---
LOCAL_CERT_PATH = "meshmeedb-firebase-adminsdk-fbsvc-e7ce47abd7.json"
RENDER_SECRET_PATH = "/etc/secrets/meshmeedb-firebase-adminsdk-fbsvc-e7ce47abd7.json"
BUCKET_NAME = "meshmeedb.firebasestorage.app"

if not firebase_admin._apps:
    if os.path.exists(RENDER_SECRET_PATH):
        cred = credentials.Certificate(RENDER_SECRET_PATH)
        print("🚀 Firebase connected securely via Render Secret File path!")
    elif os.path.exists(LOCAL_CERT_PATH):
        cred = credentials.Certificate(LOCAL_CERT_PATH)
        print("💻 Firebase connected successfully via local workspace JSON key.")
    else:
        raise FileNotFoundError("Could not find Firebase credentials file.")

    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})

db_fs = firestore.client()

# --- Secure SMTP Configuration Engine ---
# --- Secure Clean SMTP Configuration Matrix inside main.py ---
mail_config = ConnectionConfig(
    MAIL_USERNAME="enriqueiiianonat@gmail.com",
    MAIL_PASSWORD="tvcu lhwz qnbk qusx",
    MAIL_FROM="enriqueiiianonat@gmail.com",
    MAIL_PORT=465,
    MAIL_SERVER="smtp.gmail.com",
    MAIL_STARTTLS=False,
    MAIL_SSL_TLS=True,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=False
)

# --- Pydantic Data Matrix Models ---
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

# --- Helper Functions (Image Processing Engines) ---
def process_and_upload_image(file_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail((1080, 1080), Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        
        bucket = storage.bucket()
        blob_path = f"posts/{uuid.uuid4()}.jpg"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(output.getvalue(), content_type="image/jpeg")
        blob.make_public()
        return blob.public_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image upload failed: {str(e)}")

# --- Production API Route Definitions ---

@app.post("/api/register")
@app.post("/auth/register")
async def register(user: UserRegister):
    clean_username = user.username.strip().lower()
    
    # Check if they are already fully verified users
    user_ref = db_fs.collection('users').document(clean_username)
    if user_ref.get().exists:
        raise HTTPException(status_code=400, detail="Username is already taken.")
    
    otp_code = f"{random.randint(100000, 999999)}"
    
    # Secure Quarantine: Save to unverified bucket pool first!
    db_fs.collection('unverified_users').document(clean_username).set({
        'username': clean_username,
        'email': user.email,
        'password': user.password,
        'otp_code': otp_code,
        'created_at': firestore.SERVER_TIMESTAMP
    })

    email_html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #607d8b;">ROULIN POST — Email Verification</h2>
        <p>Thank you for signing up! Use the verification pin below to complete your registration:</p>
        <div style="background-color: #f5f5f5; padding: 15px; text-align: center; font-size: 24px; font-weight: bold; letter-spacing: 5px; margin: 20px 0; border-radius: 4px;">
            {otp_code}
        </div>
        <p style="color: #777; font-size: 12px;">If you didn't request this code, you can safely ignore this document link rules.</p>
    </div>
    """
    
    message = MessageSchema(
        subject="ROULIN POST - Verify Your Account",
        recipients=[user.email],
        body=email_html,
        subtype=MessageType.html
    )

    try:
        fm = FastMail(mail_config)
        await fm.send_message(message)
        return {"message": "OTP verification code sent to email."}
    except Exception as e:
        # Roll back database creation statement if mail transport network rules crash
        db_fs.collection('unverified_users').document(clean_username).delete()
        raise HTTPException(status_code=500, detail=f"Mail pipeline transmission failed: {str(e)}")

@app.post("/api/verify-otp")
@app.post("/auth/verify-otp")
def verify_otp(payload: VerifyOTP):
    target_username = payload.username.strip().lower()
    unverified_ref = db_fs.collection('unverified_users').document(target_username)
    snap = unverified_ref.get()
    
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Registration session expired or user entry token invalid.")
        
    data = snap.to_dict()
    if data.get("otp_code") != payload.otp_code.strip():
        raise HTTPException(status_code=400, detail="Invalid verification code mismatch.")
        
    # Promote data payload record to primary verified production pool
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
        raise HTTPException(status_code=401, detail="Account not verified. Please check your mailbox profile folder.")

    user_ref = db_fs.collection('users').document(login_username).get()
    if not user_ref.exists or user_ref.to_dict().get("password") != user.password:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.")
        
    u_data = user_ref.to_dict()
    return {
        "username": login_username, 
        "email": u_data.get("email"),
        "profile_url": u_data.get("profile_url", "")
    }

@app.get("/posts")
def get_posts(limit: int = 10, offset: int = 0, username: Optional[str] = None):
    query = db_fs.collection('posts')
    if username:
        query = query.where(filter=firestore.FieldFilter("username", "==", username))
    
    docs = query.order_by("timestamp", direction=firestore.Query.DESCENDING).offset(offset).limit(limit).get()
    posts = []
    for doc in docs:
        d = doc.to_dict()
        posts.append({
            "id": doc.id,
            "username": d.get("username"),
            "message": d.get("message"),
            "image_urls": d.get("image_urls", []),
            "likes": d.get("likes", 0)
        })
    return posts

@app.post("/posts")
async def create_post(
    username: str = Form(...),
    message: Optional[str] = Form(None),
    files: List[UploadFile] = File([])
):
    image_urls = []
    for file in files:
        file_bytes = await file.read()
        url = process_and_upload_image(file_bytes)
        if url:
            image_urls.append(url)
            
    post_ref = db_fs.collection('posts').document()
    post_ref.set({
        'username': username,
        'message': message or "",
        'image_urls': image_urls,
        'likes': 0,
        'timestamp': firestore.SERVER_TIMESTAMP
    })
    return {"message": "Post created successfully"}

@app.post("/posts/{post_id}/like")
def like_post(post_id: str):
    post_ref = db_fs.collection('posts').document(post_id)
    if not post_ref.get().exists:
        raise HTTPException(status_code=404, detail="Post target map missing.")
    post_ref.update({'likes': firestore.Increment(1)})
    return {"message": "Liked"}

@app.delete("/posts/{post_id}")
def delete_post(post_id: str, username: str):
    post_ref = db_fs.collection('posts').document(post_id)
    snap = post_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Post not found")
    if snap.to_dict().get("username") != username:
        raise HTTPException(status_code=403, detail="Unauthorized route call context execution.")
    post_ref.delete()
    return {"message": "Deleted"}