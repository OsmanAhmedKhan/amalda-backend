import os
import sqlite3
import logging
from typing import Optional
from app.core.config import settings

logger = logging.getLogger("AMALDA_API")

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None


class InsForgeClient:
    """
    Managed Infrastructure Layer.
    Provides dual-backend support: InsForge Cloud (Postgres/S3) or Local (SQLite/Disk).
    """
    def __init__(self):
        self.use_cloud_db = bool(settings.INSFORGE_DB_URL) and psycopg2 is not None
        self.use_cloud_storage = bool(settings.INSFORGE_S3_ENDPOINT) and boto3 is not None
        
        self._init_db()
        if self.use_cloud_storage:
            self._init_s3()

    def _init_db(self):
        query_docs = """
        CREATE TABLE IF NOT EXISTS documents (
            filename VARCHAR(255) PRIMARY KEY,
            status VARCHAR(50),
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        query_jobs = """
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            filename VARCHAR(255) PRIMARY KEY,
            total_pages INTEGER,
            processed_pages INTEGER,
            status VARCHAR(50)
        )
        """
        if self.use_cloud_db:
            try:
                with self.get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query_docs)
                        cursor.execute(query_jobs)
                    conn.commit()
                logger.info("Connected to InsForge PostgreSQL successfully.")
            except Exception as e:
                logger.error(f"InsForge DB Init Failed: {e}. Falling back to SQLite.")
                self.use_cloud_db = False
        
        if not self.use_cloud_db:
            with sqlite3.connect(settings.LOCAL_DB_PATH) as conn:
                conn.execute(query_docs.replace("VARCHAR(255)", "TEXT").replace("VARCHAR(50)", "TEXT").replace("TIMESTAMP", "DATETIME"))
                conn.execute(query_jobs.replace("VARCHAR(255)", "TEXT").replace("VARCHAR(50)", "TEXT"))
            logger.info("Using Local SQLite for metadata persistence.")

    def _init_s3(self):
        try:
            # We initialize boto3 for putting/getting objects, which usually works fine.
            # Using us-east-1 as the standard signature region for custom S3 endpoints.
            self.s3_client = boto3.client(
                's3',
                endpoint_url=settings.INSFORGE_S3_ENDPOINT,
                aws_access_key_id=settings.INSFORGE_S3_ACCESS_KEY,
                aws_secret_access_key=settings.INSFORGE_S3_SECRET_KEY,
                region_name="us-east-1"
            )
            
            # Instead of using boto3.head_bucket (which throws 400s on custom endpoints),
            # we just log that we are bypassing the strict check for InsForge.
            logger.info(f"Initialized InsForge Object Storage for bucket: {settings.INSFORGE_S3_BUCKET}")
            
        except Exception as e:
            logger.error(f"InsForge S3 Init Failed: {e}. Falling back to Local Disk.")
            self.use_cloud_storage = False

    def get_db_connection(self):
        if self.use_cloud_db:
            return psycopg2.connect(settings.INSFORGE_DB_URL, cursor_factory=DictCursor)
        return sqlite3.connect(settings.LOCAL_DB_PATH)

    def save_file(self, filename: str, file_bytes: bytes) -> str:
        """Stores document in S3 or Local Disk."""
        if self.use_cloud_storage:
            self.s3_client.put_object(
                Bucket=settings.INSFORGE_S3_BUCKET,
                Key=filename,
                Body=file_bytes
            )
            return f"s3://{settings.INSFORGE_S3_BUCKET}/{filename}"
        else:
            local_path = os.path.join(settings.UPLOAD_DIR, filename)
            with open(local_path, "wb") as f:
                f.write(file_bytes)
            return local_path

    def get_file_path(self, filename: str) -> str:
        """Returns local path (downloads from S3 if necessary for parser)."""
        local_path = os.path.join(settings.UPLOAD_DIR, filename)
        if self.use_cloud_storage and not os.path.exists(local_path):
            self.s3_client.download_file(settings.INSFORGE_S3_BUCKET, filename, local_path)
        return local_path

    def update_job_status(self, filename: str, processed_pages: int, total_pages: int, status: str):
        query = """
            INSERT INTO ingestion_jobs (filename, total_pages, processed_pages, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET
            processed_pages=excluded.processed_pages, total_pages=excluded.total_pages, status=excluded.status
        """
        if self.use_cloud_db:
            query = query.replace("?", "%s").replace("ON CONFLICT(filename) DO UPDATE", "ON CONFLICT (filename) DO UPDATE")
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor() if self.use_cloud_db else conn
            cursor.execute(query, (filename, total_pages, processed_pages, status))
            conn.commit()

    def get_job_status(self, filename: str) -> Optional[dict]:
        query = "SELECT total_pages, processed_pages, status FROM ingestion_jobs WHERE filename = ?"
        if self.use_cloud_db:
            query = query.replace("?", "%s")
            
        with self.get_db_connection() as conn:
            cursor = conn.cursor() if self.use_cloud_db else conn
            cursor.execute(query, (filename,))
            row = cursor.fetchone()
            if row:
                return {"total_pages": row[0], "processed_pages": row[1], "status": row[2]}
        return None

insforge = InsForgeClient()