import firebase_admin
from firebase_admin import credentials, storage, firestore
import uuid
import mimetypes

from PIL import Image
import io
from PIL import Image, ImageOps  # 1. Add ImageOps to your imports




# --- CONFIGURATION ---
# Your specific service account and bucket
SERVICE_ACCOUNT_KEY = "meshmeedb-firebase-adminsdk-fbsvc-e7ce47abd7.json"
STORAGE_BUCKET_URL = "meshmeedb.firebasestorage.app"

# Initialize Firebase Admin SDK
if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT_KEY)
    firebase_admin.initialize_app(cred, {
        'storageBucket': STORAGE_BUCKET_URL
    })

# Initialize Firestore (for Database)
db = firestore.client()

def upload_post_photo(file_name, file_bytes):
    if not file_bytes:
        return None
        
    try:
        # Load the bytes into Pillow
        img = Image.open(io.BytesIO(file_bytes))
        
        # --- THE FIX: Apply EXIF Orientation ---
        # This looks at the metadata and physically rotates the pixels 
        # so the orientation is "baked in" before we strip the metadata.
        img = ImageOps.exif_transpose(img) 
        # ---------------------------------------

        # Convert to RGB
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Define maximum dimensions
        max_size = (1080, 1080)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        # Save to buffer
        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=85, optimize=True)
        processed_bytes = output_buffer.getvalue()
        # ----------------------

        bucket = storage.bucket()
        blob_path = f"posts/{uuid.uuid4()}.jpg" # Changed extension to jpg
        blob = bucket.blob(blob_path)

        # Upload the PROCESSED bytes instead of the original ones
        blob.upload_from_string(processed_bytes, content_type="image/jpeg")

        blob.make_public()
        return blob.public_url

    except Exception as e:
        print(f"🔥 Firebase Upload/Resize Error: {e}")
        return None

def delete_post_photo(image_url):
    """
    Deletes an image from Firebase Storage using its URL.
    """
    try:
        if not image_url:
            return False
            
        bucket = storage.bucket()

        # Extract the blob path from the public URL
        # Logic: get the last part of the URL and strip query parameters
        path_parts = image_url.split("/")
        file_name = path_parts[-1].split("?")[0] 
        
        blob = bucket.blob(f"posts/{file_name}")

        blob.delete()
        print(f"🗑 Deleted from Firebase Storage: {file_name}")
        return True

    except Exception as e:
        print(f"🔥 Firebase Delete Error: {e}")
        return False

def save_post_to_db(username, message, image_url):
    """
    Saves the post metadata to Firestore.
    """
    try:
        post_data = {
            "username": username,
            "message": message,
            "image_url": image_url,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        # Adds a new document to the "posts" collection
        db.collection("posts").add(post_data)
        return True
    except Exception as e:
        print(f"🔥 Firestore Save Error: {e}")
        return False