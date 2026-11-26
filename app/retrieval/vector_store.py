import os
import logging
import numpy as np
import httpx
import psycopg2
from typing import List, Dict, Any
from rank_bm25 import BM25Okapi
from app.core.config import settings

logger = logging.getLogger("AMALDA_API")


class GeminiHTTPEmbeddingFunction:
    """Lightweight HTTP wrapper for Gemini gemini-embedding-001 (768 dimensions)."""
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-embedding-001:batchEmbedContents"
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "requests": [
                {
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": 768,
                }
                for text in texts
            ]
        }
        with httpx.Client() as client:
            response = client.post(self.url, headers=headers, json=payload, timeout=45.0)
            response.raise_for_status()
            data = response.json()
            return [item["values"] for item in data["embeddings"]]


class VectorDatabase:
    """
    100% Persistent Cloud Vector Store powered by Supabase PostgreSQL + pgvector.
    Replaces local ChromaDB to survive Render restarts and save >200MB RAM.
    """
    def __init__(self):
        gemini_key = os.getenv("GEMINI_API_KEY") or settings.GEMINI_API_KEY
        if not gemini_key:
            raise ValueError("GEMINI_API_KEY is required for remote embeddings.")
        if not settings.DATABASE_URL:
            raise ValueError("DATABASE_URL is required for Supabase pgvector.")

        self.embedding_fn = GeminiHTTPEmbeddingFunction(api_key=gemini_key)
        self._init_vector_table()

    def _get_conn(self):
        return psycopg2.connect(settings.DATABASE_URL)

    def _init_vector_table(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            page_number INTEGER DEFAULT 1,
            content TEXT NOT NULL,
            embedding extensions.vector(768),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_document_chunks_filename
            ON document_chunks (filename);
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
        logger.info("🟢 Initialized Supabase pgvector table: document_chunks")

    def count(self) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM document_chunks")
                return cur.fetchone()[0]

    def get_chunks_by_filename(self, filename: str) -> List[Dict[str, Any]]:
        query = """
            SELECT id, content, page_number, filename
            FROM document_chunks
            WHERE filename = %s
            ORDER BY page_number ASC
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (filename,))
                rows = cur.fetchall()
        return [
            {
                "node_id": r[0],
                "text": r[1],
                "metadata": {"page_number": r[2], "page": r[2], "filename": r[3]},
            }
            for r in rows
        ]

    def index_document(self, parsed_doc) -> None:
        ids, documents, page_nums = [], [], []

        for idx, node in enumerate(parsed_doc.nodes):
            chunk_id = getattr(node, "node_id", f"{parsed_doc.filename}_chunk_{idx}")
            ids.append(chunk_id)
            documents.append(node.text)

            page_num = 1
            if hasattr(node, "bounding_box") and hasattr(node.bounding_box, "page_number"):
                page_num = node.bounding_box.page_number
            elif hasattr(node, "page_number"):
                page_num = node.page_number
            elif hasattr(node, "metadata") and isinstance(node.metadata, dict):
                page_num = node.metadata.get("page_number", node.metadata.get("page", 1))
            page_nums.append(int(page_num))

        batch_size = 50
        insert_sql = """
            INSERT INTO document_chunks (id, filename, page_number, content, embedding)
            VALUES (%s, %s, %s, %s, %s::extensions.vector)
            ON CONFLICT (id) DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for i in range(0, len(ids), batch_size):
                    batch_docs = documents[i:i + batch_size]
                    embeddings = self.embedding_fn.embed(batch_docs)
                    for cid, pnum, text_val, vec in zip(
                        ids[i:i + batch_size],
                        page_nums[i:i + batch_size],
                        batch_docs,
                        embeddings,
                    ):
                        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
                        cur.execute(insert_sql, (cid, parsed_doc.filename, pnum, text_val, vec_str))
            conn.commit()

    def delete_document_embeddings(self, filename: str) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM document_chunks WHERE filename = %s", (filename,))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    def search(
        self,
        query: str,
        document_ids: List[str] = None,
        n_results: int = 10,
        lambda_mult: float = 0.5,
    ) -> Dict[str, Any]:
        pool_size = max(20, n_results * 3)
        query_vec = self.embedding_fn.embed([query])[0]
        vec_str = "[" + ",".join(str(x) for x in query_vec) + "]"

        if document_ids and len(document_ids) > 0:
            sql = """
                SELECT id, content, filename, page_number,
                       (embedding OPERATOR(extensions.<=>) %s::extensions.vector) AS distance
                FROM document_chunks
                WHERE filename = ANY(%s)
                ORDER BY distance ASC
                LIMIT %s
            """
            params = (vec_str, document_ids, pool_size)
        else:
            sql = """
                SELECT id, content, filename, page_number,
                       (embedding OPERATOR(extensions.<=>) %s::extensions.vector) AS distance
                FROM document_chunks
                ORDER BY distance ASC
                LIMIT %s
            """
            params = (vec_str, pool_size)

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        if not rows:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        pool_ids = [r[0] for r in rows]
        pool_docs = [r[1] for r in rows]
        pool_metas = [{"filename": r[2], "page": r[3], "page_number": r[3]} for r in rows]
        pool_dists = [float(r[4]) for r in rows]

        # Hybrid RRF (Dense pgvector + Sparse BM25)
        tokenized_pool = [doc.lower().split() for doc in pool_docs]
        bm25 = BM25Okapi(tokenized_pool)
        bm25_scores = bm25.get_scores(query.lower().split())

        dense_ranks = {doc_id: rank for rank, doc_id in enumerate(pool_ids)}
        sparse_sorted_indices = np.argsort(bm25_scores)[::-1]
        sparse_ranks = {pool_ids[idx]: rank for rank, idx in enumerate(sparse_sorted_indices)}

        rrf_k = 60
        hybrid_scores = {
            doc_id: (1.0 / (rrf_k + dense_ranks[doc_id])) + (1.0 / (rrf_k + sparse_ranks[doc_id]))
            for doc_id in pool_ids
        }
        sorted_ids = sorted(hybrid_scores, key=hybrid_scores.get, reverse=True)[:n_results]

        final_docs, final_metas, final_dists = [], [], []
        for doc_id in sorted_ids:
            idx = pool_ids.index(doc_id)
            final_docs.append(pool_docs[idx])
            final_metas.append(pool_metas[idx])
            final_dists.append(pool_dists[idx])

        return {
            "ids": [sorted_ids],
            "documents": [final_docs],
            "metadatas": [final_metas],
            "distances": [final_dists],
        }