import os
import logging
import psycopg2
from psycopg2.extras import DictCursor
import boto3
from botocore.config import Config
from app.core.config import settings

logger = logging.getLogger("AMALDA_API")

class InsForgeClient:
    """
    Stateless Managed Infrastructure Layer.
    Connects to InsForge Cloud (PostgreSQL/S3). Local disk writes are disabled.
    """

    def __init__(self):
        if not settings.INSFORGE_DB_URL or not settings.INSFORGE_S3_ENDPOINT:
            logger.warning("InsForge env vars missing - client will be disabled until configured.")
            self.s3_client = None
            self.db_available = False
            return

        self.db_available = True
        self._init_s3()
        # Defer DB table creation until first use to avoid crashing startup if DB is slow
        try:
            self._init_db()
        except Exception as e:
            logger.warning(f"InsForge DB tables not created at startup: {e}")
            self.db_available = False

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
        try:
            with self.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query_docs)
                    cursor.execute(query_jobs)
                conn.commit()
            logger.info("🟢 Connected to InsForge PostgreSQL successfully.")
        except Exception as e:
            logger.error(f"InsForge DB Init Failed: {e}")
            self.db_available = False

    def _init_s3(self):
        try:
            region = os.getenv("S3_REGION", "us-east-2")
            self.s3_client = boto3.client(
                's3',
                endpoint_url=settings.INSFORGE_S3_ENDPOINT,
                aws_access_key_id=settings.INSFORGE_S3_ACCESS_KEY,
                aws_secret_access_key=settings.INSFORGE_S3_SECRET_KEY,
                region_name=region,
                config=Config(
                    signature_version='s3v4',
                    s3={'addressing_style': 'path'}
                )
            )
            logger.info(f"🟢 Initialized InsForge S3 Storage for bucket: {settings.INSFORGE_S3_BUCKET}")
        except Exception as e:
            logger.error(f"InsForge S3 Init Failed: {e}")
            self.s3_client = None

    def get_db_connection(self):
        return psycopg2.connect(settings.INSFORGE_DB_URL, cursor_factory=DictCursor)

    def save_file_bytes(self, filename: str, file_bytes: bytes) -> str:
        if not self.s3_client:
            raise RuntimeError("InsForge S3 client not initialized.")
        self.s3_client.put_object(
            Bucket=settings.INSFORGE_S3_BUCKET,
            Key=filename,
            Body=file_bytes,
            ContentType="application/pdf"
        )
        return f"s3://{settings.INSFORGE_S3_BUCKET}/{filename}"

    def get_file_bytes(self, filename: str) -> bytes:
        if not self.s3_client:
            raise RuntimeError("InsForge S3 client not initialized.")
        response = self.s3_client.get_object(
            Bucket=settings.INSFORGE_S3_BUCKET,
            Key=filename
        )
        return response['Body'].read()

    def delete_file(self, filename: str):
        if not self.s3_client:
            return
        try:
            self.s3_client.delete_object(Bucket=settings.INSFORGE_S3_BUCKET, Key=filename)
        except Exception as e:
            logger.error(f"Failed to delete {filename} from S3: {e}")

    def generate_presigned_url(self, filename: str, expires_in=3600) -> str:
        base_url = settings.INSFORGE_S3_ENDPOINT.split("/storage/")[0]
        return f"{base_url}/storage/v1/object/public/{settings.INSFORGE_S3_BUCKET}/{filename}"

    def update_job_status(self, filename: str, processed_pages: int, total_pages: int, status: str):
        if not self.db_available:
            return
        query = """
            INSERT INTO ingestion_jobs (filename, total_pages, processed_pages, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (filename) DO UPDATE SET
                processed_pages=excluded.processed_pages,
                total_pages=excluded.total_pages,
                status=excluded.status
        """
        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (filename, total_pages, processed_pages, status))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to update job status: {e}")

    def get_job_status(self, filename: str):
        if not self.db_available:
            return None
        query = "SELECT total_pages, processed_pages, status FROM ingestion_jobs WHERE filename = %s"
        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (filename,))
                row = cursor.fetchone()
                if row:
                    return {"total_pages": row[0], "processed_pages": row[1], "status": row[2]}
        except Exception as e:
            logger.error(f"Failed to get job status: {e}")
        return None

insforge = InsForgeClient()
