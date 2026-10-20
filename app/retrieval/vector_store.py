import os
import requests
import chromadb
import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from app.core.config import settings
from typing import List, Dict, Any
from rank_bm25 import BM25Okapi

class SafeGoogleEmbedder(EmbeddingFunction):
    """Custom adapter that calls the Gemini API directly via HTTP, bypassing broken SDKs."""
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY
        
        raw_model = settings.EMBEDDING_MODEL.replace("models/", "")
        if not raw_model or raw_model == "all-MiniLM-L6-v2":
            raw_model = "text-embedding-004"
            
        self.model = f"models/{raw_model}"
        self.url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:batchEmbedContents?key={self.api_key}"

    def __call__(self, input: Documents) -> Embeddings:
        if not input:
            return []
            
        requests_payload = []
        for text in input:
            requests_payload.append({
                "model": self.model,
                "content": {"parts": [{"text": text}]}
            })
            
        response = requests.post(
            self.url,
            json={"requests": requests_payload},
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            raise Exception(f"Gemini API Error {response.status_code}: {response.text}")
            
        data = response.json()
        
        embeddings = []
        for item in data.get("embeddings", []):
            embeddings.append(item["values"])
            
        return embeddings


class VectorDatabase:
    def __init__(self):
        # 🟢 STATELSS ARCHITECTURE: Use purely in-memory Vector Storage.
        self.client = chromadb.EphemeralClient()
        
        self.embedding_fn = SafeGoogleEmbedder()
        self.collection = self.client.get_or_create_collection(
            name="legal_documents",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

    def index_document(self, parsed_doc) -> None:
        ids = []
        documents = []
        metadatas = []
        
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
                page_num = node.metadata.get("page_number", node.metadata.get("page", node.metadata.get("page_label", 1)))
            elif hasattr(node, "metadata") and hasattr(node.metadata, "page_number"):
                page_num = node.metadata.page_number

            meta = {
                "filename": parsed_doc.filename,
                "page": int(page_num),
                "page_number": int(page_num),
                "bounding_box": ""
            }
            metadatas.append(meta)
            
        batch_size = 90 
        for i in range(0, len(ids), batch_size):
            self.collection.add(
                ids=ids[i:i + batch_size],
                documents=documents[i:i + batch_size],
                metadatas=metadatas[i:i + batch_size]
            )

    def delete_document_embeddings(self, filename: str) -> int:
        try:
            results = self.collection.get(where={"filename": filename}, include=[])
            if results and results["ids"]:
                self.collection.delete(ids=results["ids"])
                return len(results["ids"])
            return 0
        except Exception as e:
            raise Exception(f"Failed to delete embeddings for {filename}: {str(e)}")

    def search(self, query: str, document_ids: List[str] = None, n_results: int = 10, lambda_mult: float = 0.5) -> Dict[str, Any]:
        pool_size = max(20, n_results * 3)
        
        where_clause = None
        if document_ids and len(document_ids) > 0:
            if len(document_ids) == 1:
                target_doc = document_ids[0]
                where_clause = {
                    "$or": [
                        {"filename": target_doc},
                        {"document_id": target_doc},
                        {"source": target_doc}
                    ]
                }
            else:
                where_clause = {"filename": {"$in": document_ids}}
        
        dense_results = self.collection.query(
            query_texts=[query],
            n_results=pool_size,
            where=where_clause,
            include=["documents", "metadatas", "distances"]
        )
        
        if not dense_results['documents'] or not dense_results['documents'][0]:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        pool_ids = dense_results['ids'][0]
        pool_docs = dense_results['documents'][0]
        pool_metas = dense_results['metadatas'][0]
        pool_dists = dense_results.get('distances', [[0.5] * len(pool_ids)])[0]

        tokenized_pool = [doc.lower().split() for doc in pool_docs]
        bm25 = BM25Okapi(tokenized_pool)
        tokenized_query = query.lower().split()
        bm25_scores = bm25.get_scores(tokenized_query)

        dense_ranks = {doc_id: rank for rank, doc_id in enumerate(pool_ids)}
        sparse_sorted_indices = np.argsort(bm25_scores)[::-1]
        sparse_ranks = {pool_ids[idx]: rank for rank, idx in enumerate(sparse_sorted_indices)}

        rrf_k = 60
        hybrid_scores = {}
        for doc_id in pool_ids:
            dense_score = 1.0 / (rrf_k + dense_ranks[doc_id])
            sparse_score = 1.0 / (rrf_k + sparse_ranks[doc_id])
            hybrid_scores[doc_id] = dense_score + sparse_score

        sorted_ids = sorted(hybrid_scores, key=hybrid_scores.get, reverse=True)[:n_results]

        final_docs = []
        final_metas = []
        final_dists = []
        for doc_id in sorted_ids:
            idx = pool_ids.index(doc_id)
            final_docs.append(pool_docs[idx])
            final_metas.append(pool_metas[idx])
            final_dists.append(pool_dists[idx])

        return {
            "ids": [sorted_ids],
            "documents": [final_docs],
            "metadatas": [final_metas],
            "distances": [final_dists]
        }