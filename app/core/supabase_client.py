import logging
from typing import Optional
import httpx
import psycopg2
from psycopg2.extras import DictCursor
from app.core.config import settings

logger = logging.getLogger("AMALDA_API")


class SupabaseClient:
    """
    Stateless Managed Infrastructure Layer for Supabase.
    Uses PostgreSQL (Session Pooler) + per-user namespaced Private Storage via httpx.
    """
    def __init__(self):
        if not settings.DATABASE_URL or not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
            raise ValueError(
                "CRITICAL: Missing Supabase environment variables "
                "(DATABASE_URL, SUPABASE_URL, or SUPABASE_SECRET_KEY)."
            )

        self.base_url = settings.SUPABASE_URL.rstrip("/")
        self.bucket = settings.SUPABASE_STORAGE_BUCKET
        self.headers = {
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
            "apikey": settings.SUPABASE_SECRET_KEY,
        }

        self._init_db()
        logger.info(f"🟢 Initialized Supabase Storage for private bucket: {self.bucket}")

    def _init_db(self):
        query_jobs = """
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            filename VARCHAR(255) PRIMARY KEY,
            total_pages INTEGER,
            processed_pages INTEGER,
            status VARCHAR(50)
        )
        """
        try:
            with self.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query_jobs)
                conn.commit()
            logger.info("🟢 Connected to Supabase PostgreSQL successfully.")
        except Exception as e:
            logger.error(f"Supabase DB Init Failed: {e}")
            raise

    def get_db_connection(self):
        return psycopg2.connect(settings.DATABASE_URL, cursor_factory=DictCursor)

    @staticmethod
    def _object_path(filename: str, user_id: Optional[str] = None) -> str:
        return f"{user_id}/{filename}" if user_id else filename

    # ---------------------------------------------------------
    # SUPABASE PRIVATE STORAGE (Per-User Namespaced REST API)
    # ---------------------------------------------------------

    def save_file_bytes(
        self,
        filename: str,
        file_bytes: bytes,
        content_type: str = "application/pdf",
        user_id: Optional[str] = None,
    ) -> str:
        path = self._object_path(filename, user_id)
        url = f"{self.base_url}/storage/v1/object/{self.bucket}/{path}"
        headers = {
            **self.headers,
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, content=file_bytes, headers=headers)
            resp.raise_for_status()
        return f"supabase://{self.bucket}/{path}"

    def get_file_bytes(self, filename: str, user_id: Optional[str] = None) -> bytes:
        path = self._object_path(filename, user_id)
        url = f"{self.base_url}/storage/v1/object/{self.bucket}/{path}"
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.content

    def list_files(self, user_id: Optional[str] = None) -> set:
        url = f"{self.base_url}/storage/v1/object/list/{self.bucket}"
        payload = {"prefix": f"{user_id}" if user_id else "", "limit": 1000, "offset": 0}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload, headers=self.headers)
                resp.raise_for_status()
                items = resp.json()
                return {item["name"] for item in items if "name" in item}
        except Exception as e:
            logger.error(f"Failed to list files in Supabase Storage: {e}")
            return set()

    def delete_file(self, filename: str, user_id: Optional[str] = None):
        path = self._object_path(filename, user_id)
        url = f"{self.base_url}/storage/v1/object/{self.bucket}/{path}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.delete(url, headers=self.headers)
                resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to delete {path} from Supabase Storage: {e}")

    def generate_presigned_url(
        self, filename: str, expires_in: int = 3600, user_id: Optional[str] = None
    ) -> str:
        path = self._object_path(filename, user_id)
        url = f"{self.base_url}/storage/v1/object/sign/{self.bucket}/{path}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json={"expiresIn": expires_in}, headers=self.headers)
            resp.raise_for_status()
            signed_path = resp.json().get("signedURL", "")
            return f"{self.base_url}/storage/v1{signed_path}"

    # ---------------------------------------------------------
    # INGESTION JOB TRACKING
    # ---------------------------------------------------------

    def update_job_status(self, filename: str, processed_pages: int, total_pages: int, status: str):
        query = """
            INSERT INTO ingestion_jobs (filename, total_pages, processed_pages, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (filename) DO UPDATE SET
            processed_pages = excluded.processed_pages,
            total_pages = excluded.total_pages,
            status = excluded.status
        """
        with self.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, (filename, total_pages, processed_pages, status))
            conn.commit()

    def get_job_status(self, filename: str):
        query = "SELECT total_pages, processed_pages, status FROM ingestion_jobs WHERE filename = %s"
        with self.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, (filename,))
                row = cursor.fetchone()
                if row:
                    return {
                        "total_pages": row[0],
                        "processed_pages": row[1],
                        "status": row[2],
                    }
        return None


supabase_client = SupabaseClient()