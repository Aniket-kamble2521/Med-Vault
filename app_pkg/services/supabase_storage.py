import os
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "medical-records")

def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)

def upload_file_to_supabase(file_data: bytes, filename: str, content_type: str) -> str:
    """
    Uploads file to Supabase storage bucket.
    Returns the stored path / filename in the bucket.
    """
    if not is_supabase_configured():
        raise Exception("Supabase is not configured.")
        
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type
    }
    response = requests.post(url, data=file_data, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Supabase upload failed (status {response.status_code}): {response.text}")
    return filename

def download_file_from_supabase(filename: str) -> bytes:
    """
    Downloads file from Supabase storage.
    """
    if not is_supabase_configured():
        raise Exception("Supabase is not configured.")
        
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/authenticated/{SUPABASE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Supabase download failed (status {response.status_code}): {response.text}")
    return response.content

def delete_file_from_supabase(filename: str):
    """
    Deletes file from Supabase storage.
    """
    if not is_supabase_configured():
        return
        
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    response = requests.delete(url, headers=headers)
    if response.status_code != 200:
        print(f"Supabase delete warning (status {response.status_code}): {response.text}")
