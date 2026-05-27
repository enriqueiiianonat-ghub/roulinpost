from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import List, Optional
import firebase_admin
from firebase_admin import credentials, firestore, storage
import io
import uuid
import random
import json as py_json
from PIL import Image, ImageOps
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

app = FastAPI(title="EZGEE Social API")

# --- Enable CORS Globally ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Firebase Initialization ---
CERT_PATH = "meshmeedb-firebase-adminsdk-fbsvc-e7ce47abd7.json"
BUCKET_NAME = "meshmeedb.firebasestorage.app"

if not firebase_admin._apps:
    cred = credentials.Certificate(CERT_PATH)
    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})

db_fs = firestore.client()

# --- SMTP Configuration Matrix ---
# IMPORTANT: Remember to replace MAIL_PASSWORD with a generated 16-character Google App Password
mail_config = ConnectionConfig(
    MAIL_USERNAME="enriqueiiianonat@gmail.com",
    MAIL_PASSWORD="wssw nxsa bamf fpqg", 
    MAIL_FROM="king@devgloyd.com",
    MAIL_PORT=587,
    MAIL_SERVER="smtp.gmail.com",
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True
)

# --- Pydantic Schemas ---
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

# --- Helper Functions (Image Processing Engine) ---
def process_and_upload_image(file_bytes: bytes) -> str:
    """Processes post images up to 1080x1080 resolution."""
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
        
        bucket = storage.bucket()
        blob_path = f"posts/{uuid.uuid4()}.jpg"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(compressed_data, content_type="image/jpeg")
        blob.make_public()
        return blob.public_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing pipeline failed: {str(e)}")


def process_and_upload_avatar(file_bytes: bytes) -> str:
    """Processes profile pictures down to an optimized 150x150 square icon."""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        # Hard scale to a compact square icon profile dimensions
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
        raise HTTPException(status_code=500, detail=f"Avatar upload processing failed: {str(e)}")

# --- API Endpoints ---

@app.post("/auth/register")
async def register(user: UserRegister):
    clean_username = user.username.strip().lower()
    
    # Check if the user is already permanently registered
    user_ref = db_fs.collection('users').document(clean_username)
    if user_ref.get().exists:
        raise HTTPException(status_code=400, detail="Username is already taken.")
        
    otp_code = f"{random.randint(100000, 999999)}"
    
    email_html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #607d8b;">ROULIN POST — Email Verification</h2>
        <p>Thank you for signing up! Use the code below to complete your registration and activate your account:</p>
        <div style="background-color: #f5f5f5; padding: 15px; text-align: center; font-size: 24px; font-weight: bold; letter-spacing: 5px; margin: 20px 0; border-radius: 4px;">
            {otp_code}
        </div>
        <p style="color: #777; font-size: 12px;">If you didn't request this code, you can safely ignore this email.</p>
    </div>
    """
    
    message = MessageSchema(
        subject="ROULIN POST - Verify Your Account",
        recipients=[user.email],
        body=email_html,
        subtype=MessageType.html
    )

    # Dispatch email before touching the database staging area
    try:
        fm = FastMail(mail_config)
        await fm.send_message(message)
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Mailing server rejected dispatch transmission. Verify credentials. Error: {str(e)}"
        )

    # Save to staging unverified area only if email sent out successfully
    db_fs.collection('unverified_users').document(clean_username).set({
        'username': clean_username,
        'email': user.email,
        'password': user.password,
        'otp_code': otp_code,
        'created_at': firestore.SERVER_TIMESTAMP
    })

    return {"message": "OTP verification code sent to email successfully."}


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
        
    # Promote staging user over to true production collection database area
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
        raise HTTPException(status_code=401, detail="Account not verified. Please check your email for the code.")

    user_ref = db_fs.collection('users').document(login_username).get()
    if not user_ref.exists or user_ref.to_dict().get("password") != user.password:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
        
    u_data = user_ref.to_dict()
    return {
        "username": login_username, 
        "email": u_data.get("email"),
        "profile_url": u_data.get("profile_url", "")
    }


@app.put("/auth/profile/{current_username}")
async def update_profile(
    current_username: str,
    new_username: str = Form(...),
    new_email: str = Form(...),
    new_password: Optional[str] = Form(None),
    avatar_file: Optional[UploadFile] = File(None)
):
    clean_current = current_username.strip().lower()
    clean_new = new_username.strip().lower()

    user_ref = db_fs.collection('users').document(clean_current)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="User profile not found")

    user_data = snap.to_dict()
    bucket = storage.bucket()

    if avatar_file:
        old_avatar = user_data.get("profile_url", "")
        if old_avatar:
            try:
                old_file_name = old_avatar.split("/")[-1].split("?")[0]
                if "avatars%" in old_file_name:
                    old_file_name = old_file_name.replace("avatars%", "avatars/")
                blob = bucket.blob(old_file_name)
                if blob.exists():
                    blob.delete()
            except Exception:
                pass
        
        avatar_bytes = await avatar_file.read()
        user_data['profile_url'] = process_and_upload_avatar(avatar_bytes)

    if clean_new != clean_current:
        new_ref = db_fs.collection('users').document(clean_new)
        if new_ref.get().exists:
            raise HTTPException(status_code=400, detail="New username is already taken")
        
        user_data['username'] = clean_new
        user_data['email'] = new_email
        if new_password:
            user_data['password'] = new_password
            
        new_ref.set(user_data)
        user_ref.delete()
        return {"username": clean_new, "email": new_email, "profile_url": user_data.get("profile_url", "")}

    user_data['email'] = new_email
    if new_password:
        user_data['password'] = new_password
        
    user_ref.set(user_data)
    return {"username": clean_current, "email": new_email, "profile_url": user_data.get("profile_url", "")}


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
            file_name = avatar_url.split("/")[-1].split("?")[0]
            if "avatars%" in file_name:
                file_name = file_name.replace("avatars%", "avatars/")
            blob = bucket.blob(file_name)
            if blob.exists():
                blob.delete()
        except:
            pass

    try:
        user_posts_query = db_fs.collection('posts').where(
            filter=firestore.FieldFilter("username", "==", clean_username)
        ).get()
        
        for doc in user_posts_query:
            post_data = doc.to_dict()
            image_urls = post_data.get("image_urls", [])
            for url in image_urls:
                try:
                    file_name = url.split("/")[-1].split("?")[0]
                    if "posts%" in file_name:
                        file_name = file_name.replace("posts%", "posts/")
                    blob = bucket.blob(file_name)
                    if blob.exists():
                        blob.delete()
                except Exception:
                    pass
            db_fs.collection('posts').document(doc.id).delete()
            
        user_ref.delete()
        return {"message": "Account, posts, and files deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cascading deletion broke down: {str(e)}")


@app.get("/posts")
def get_posts(limit: int = 10, offset: int = 0, username: Optional[str] = None):
    query = db_fs.collection('posts')
    if username:
        query = query.where(filter=firestore.FieldFilter("username", "==", username))
    
    docs = query.order_by("timestamp", direction=firestore.Query.DESCENDING).offset(offset).limit(limit).get()
    
    posts = []
    avatar_cache = {}
    
    for doc in docs:
        d = doc.to_dict()
        author = d.get("username", "")
        
        if author not in avatar_cache:
            author_ref = db_fs.collection('users').document(author).get()
            if author_ref.exists:
                avatar_cache[author] = author_ref.to_dict().get("profile_url", "")
            else:
                avatar_cache[author] = ""
                
        posts.append({
            "id": doc.id,
            "username": author,
            "user_avatar": avatar_cache[author], 
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
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Cannot upload more than 5 images per post.")

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
        raise HTTPException(status_code=403, detail="Unauthorized post modification attempt")
        
    try:
        retained_urls = py_json.loads(retained_image_urls)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid retained images list format")

    if len(retained_urls) + len(files) > 5:
        raise HTTPException(status_code=400, detail="Total post images cannot exceed 5.")

    bucket = storage.bucket()
    for old_url in old_post.get("image_urls", []):
        if old_url not in retained_urls:
            try:
                file_name = old_url.split("/")[-1].split("?")[0]
                if "posts%" in file_name:
                    file_name = file_name.replace("posts%", "posts/")
                blob = bucket.blob(file_name)
                if blob.exists():
                    blob.delete()
            except Exception as e:
                print(f"Error removing modified image file: {e}")

    new_uploaded_urls = []
    for file in files:
        file_bytes = await file.read()
        url = process_and_upload_image(file_bytes)
        if url:
            new_uploaded_urls.append(url)

    final_image_list = retained_urls + new_uploaded_urls

    post_ref.update({
        "message": message or "",
        "image_urls": final_image_list
    })
    return {"message": "Post updated successfully", "image_urls": final_image_list}


@app.post("/posts/{post_id}/like")
def like_post(post_id: str):
    post_ref = db_fs.collection('posts').document(post_id)
    if not post_ref.get().exists:
        raise HTTPException(status_code=404, detail="Post not found")
    post_ref.update({'likes': firestore.Increment(1)})
    return {"message": "Liked"}


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
            file_name = url.split("/")[-1].split("?")[0]
            if "posts%" in file_name:
                file_name = file_name.replace("posts%", "posts/")
            bucket.blob(file_name).delete()
        except:
            pass
            
    post_ref.delete()
    return {"message": "Deleted"}