from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import List, Optional
import firebase_admin
from firebase_admin import credentials, firestore, storage
import io
import uuid
from PIL import Image, ImageOps

app = FastAPI(title="EZGEE Social API")

# Enable CORS for Flutter app (Web, Mobile, Desktop)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Firebase Initialisation ---
CERT_PATH = "meshmeedb-firebase-adminsdk-fbsvc-e7ce47abd7.json"
BUCKET_NAME = "meshmeedb.firebasestorage.app"

if not firebase_admin._apps:
    cred = credentials.Certificate(CERT_PATH)
    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})

db_fs = firestore.client()

# --- Pydantic Schemas ---
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    new_username: str
    new_email: EmailStr
    new_password: Optional[str] = None

# --- Helper Functions (Image Processing) ---
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

# --- API Endpoints ---

@app.post("/auth/register")
def register(user: UserRegister):
    user_ref = db_fs.collection('users').document(user.username)
    if user_ref.get().exists:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    user_ref.set({
        'username': user.username,
        'email': user.email,
        'password': user.password, # Note: In production, hash this using passlib!
        'created_at': firestore.SERVER_TIMESTAMP
    })
    return {"message": "Registration successful"}

@app.post("/auth/login")
def login(user: UserLogin):
    user_ref = db_fs.collection('users').document(user.username).get()
    if not user_ref.exists or user_ref.to_dict().get("password") != user.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"username": user.username, "email": user_ref.to_dict().get("email")}

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
        
    # Delete storage assets
    bucket = storage.bucket()
    for url in snap.to_dict().get("image_urls", []):
        try:
            file_name = url.split("/")[-1].split("?")[0]
            bucket.blob(f"posts/{file_name}").delete()
        except:
            pass
            
    post_ref.delete()
    return {"message": "Deleted"}


@app.put("/auth/profile/{current_username}")
def update_profile(current_username: str, profile: ProfileUpdate):
    # 1. Fetch current user record
    user_ref = db_fs.collection('users').document(current_username)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="User profile not found")

    # 2. Handle Username Migration if changed
    if profile.new_username != current_username:
        new_ref = db_fs.collection('users').document(profile.new_username)
        if new_ref.get().exists:
            raise HTTPException(status_code=400, detail="New username is already taken")
        
        # Move document data over to the new key destination
        user_data = snap.to_dict()
        user_data['username'] = profile.new_username
        user_data['email'] = profile.new_email
        if profile.new_password:
            user_data['password'] = profile.new_password
            
        new_ref.set(user_data)
        user_ref.delete() # Remove old record name reference
        return {"username": profile.new_username, "email": profile.new_email}

    # 3. Handle simple value updates if username remains identical
    update_payload = {"email": profile.new_email}
    if profile.new_password:
        update_payload["password"] = profile.new_password
        
    user_ref.update(update_payload)
    return {"username": current_username, "email": profile.new_email}