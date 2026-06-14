import os
import json
import io
import threading

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

_drive_service = None
_lock = threading.Lock()


def _get_service():
    """Build (and cache) the Drive API client from env credentials."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    if not GOOGLE_CREDENTIALS_JSON:
        print("[GDRIVE] GOOGLE_CREDENTIALS_JSON not set, sync disabled")
        return None

    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _drive_service = build("drive", "v3", credentials=creds)
        return _drive_service
    except Exception as e:
        print("[GDRIVE] Failed to init credentials:", e)
        return None


def _find_file_id(service, filename):
    query = f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false"
    res = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def get_db_link(filename="music.db"):
    """Return a shareable Drive link for the DB file, or None if unavailable."""
    service = _get_service()
    if not service or not GDRIVE_FOLDER_ID:
        return None

    try:
        query = f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false"
        res = service.files().list(
            q=query, spaces="drive",
            fields="files(id, webViewLink, webContentLink)"
        ).execute()
        files = res.get("files", [])
        if not files:
            return None

        file_id = files[0]["id"]

        try:
            service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
            ).execute()
        except Exception as e:
            print("[GDRIVE] permission set failed:", e)

        return files[0].get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    except Exception as e:
        print("[GDRIVE] get_db_link failed:", e)
        return None
        
def download_db(local_path, filename="music.db"):
    """Download the DB from Drive to local_path if it exists. Safe to call at startup."""
    service = _get_service()
    if not service or not GDRIVE_FOLDER_ID:
        print("[GDRIVE] Skipping download (not configured)")
        return False

    try:
        file_id = _find_file_id(service, filename)
        if not file_id:
            print("[GDRIVE] No remote DB found, starting fresh")
            return False

        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO(local_path, "wb")
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.close()
        print("[GDRIVE] Downloaded music.db from Drive")
        return True
    except Exception as e:
        print("[GDRIVE] Download failed:", e)
        return False


def upload_db(local_path, filename="music.db"):
    """Upload/replace the DB file on Drive. Thread-safe."""
    service = _get_service()
    if not service or not GDRIVE_FOLDER_ID:
        return False

    if not os.path.exists(local_path):
        return False

    with _lock:
        try:
            media = MediaFileUpload(local_path, mimetype="application/x-sqlite3", resumable=True)
            file_id = _find_file_id(service, filename)

            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
                service.files().create(body=metadata, media_body=media, fields="id").execute()

            print("[GDRIVE] Synced music.db to Drive")
            return True
        except Exception as e:
            print("[GDRIVE] Upload failed:", e)
            return False
