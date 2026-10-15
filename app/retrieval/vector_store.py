import os
import chromadb
import numpy as np
from chromadb.utils import embedding_functions
from app.core.config import settings
from typing import List, Dict, Any
from rank_bm25 import BM25Okapi

class VectorDatabase:
    def __init__(self):
        self.persist_directory = settings.CHROMA_PERSIST_DIR
        
        # Initialize ChromaDB client
        self.client = chromadb.PersistentClient(path=self.persist_directory)
        
        # Initialize Dense Embedding Function (Local Model)
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=settings.EMBEDDING_MODEL
        )
        
        # Get or create the main legal document collection
        self.collection = self.client.get_or_create_collection(
            name="legal_documents",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

    def index_document(self, parsed_doc) -> None:
        """Indexes dense chunks into ChromaDB."""
        ids = []
        documents = []
        metadatas = []
        
        for idx, node in enumerate(parsed_doc.nodes):
            chunk_id = f"{parsed_doc.filename}_chunk_{idx}"
            ids.append(chunk_id)
            documents.append(node.text)
            
            page_num = "1"
            if hasattr(node, "metadata") and isinstance(node.metadata, dict):
                page_num = str(node.metadata.get("page_label", "1"))
            elif hasattr(node, "metadata") and hasattr(node.metadata, "page_number"):
                page_num = str(node.metadata.page_number)
            
            meta = {
                "filename": parsed_doc.filename,
                "page": page_num,
                "bounding_box": "" # Empty default to prevent serialization errors
            }
            metadatas.append(meta)
            
        # Batch insert to handle large documents efficiently
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            self.collection.add(
                ids=ids[i:i + batch_size],
                documents=documents[i:i + batch_size],
                metadatas=metadatas[i:i + batch_size]
            )

    def delete_document_embeddings(self, filename: str) -> int:
        """Removes all vector embeddings associated with a specific document."""
        try:
            results = self.collection.get(where={"filename": filename}, include=[])
            if results and results["ids"]:
                self.collection.delete(ids=results["ids"])
                return len(results["ids"])
            return 0
        except Exception as e:
            raise Exception(f"Failed to delete embeddings for {filename}: {str(e)}")

    def search(self, query: str, n_results: int = 10, lambda_mult: float = 0.5) -> Dict[str, Any]:
        """
        Research-Grade Retrieval Pipeline:
        Retrieves top 10 chunks using Dense + Sparse Fusion (RRF). 
        Since chunks are now ~1200 characters each, 10 chunks provide rich, self-contained context.
        """
        # 1. Fetch pool from ChromaDB
        pool_size = max(20, n_results * 3)
        
        dense_results = self.collection.query(
            query_texts=[query],
            n_results=pool_size,
            include=["documents", "metadatas", "distances"]
        )
        
        if not dense_results['documents'] or not dense_results['documents'][0]:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]]}

        pool_ids = dense_results['ids'][0]
        pool_docs = dense_results['documents'][0]
        pool_metas = dense_results['metadatas'][0]

        # 2. Sparse Search (BM25)
        tokenized_pool = [doc.lower().split() for doc in pool_docs]
        bm25 = BM25Okapi(tokenized_pool)
        tokenized_query = query.lower().split()
        bm25_scores = bm25.get_scores(tokenized_query)

        # 3. Reciprocal Rank Fusion (RRF)
        dense_ranks = {doc_id: rank for rank, doc_id in enumerate(pool_ids)}
        sparse_sorted_indices = np.argsort(bm25_scores)[::-1]
        sparse_ranks = {pool_ids[idx]: rank for rank, idx in enumerate(sparse_sorted_indices)}

        rrf_k = 60
        hybrid_scores = {}
        for doc_id in pool_ids:
            dense_score = 1.0 / (rrf_k + dense_ranks[doc_id])
            sparse_score = 1.0 / (rrf_k + sparse_ranks[doc_id])
            hybrid_scores[doc_id] = dense_score + sparse_score

        # 4. Sort and select final n_results
        sorted_ids = sorted(hybrid_scores, key=hybrid_scores.get, reverse=True)[:n_results]

        final_docs = []
        final_metas = []
        for doc_id in sorted_ids:
            idx = pool_ids.index(doc_id)
            final_docs.append(pool_docs[idx])
            final_metas.append(pool_metas[idx])

        return {
            "ids": [sorted_ids],
            "documents": [final_docs],
            "metadatas": [final_metas]
        }