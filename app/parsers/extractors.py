import os
import io
import pymupdf
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.parsers.registry import BaseExtractor, ExtractedDocument, ExtractedNode, registry
from app.parsers.pdf_parser import LegalDocumentParser

class TextExtractor(BaseExtractor):
    def __init__(self):
        self.splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)

    def extract(self, file_bytes: bytes, filename: str) -> ExtractedDocument:
        text = file_bytes.decode("utf-8", errors="replace")
        chunks = self.splitter.split_text(text)
        nodes = [ExtractedNode(text=chunk, metadata={"page_number": 1, "page_label": "1", "source": filename, "format": "text"}) for chunk in chunks]
        return ExtractedDocument(filename=filename, nodes=nodes)

class PDFExtractor(BaseExtractor):
    def __init__(self):
        self.robust_parser = LegalDocumentParser()

    def extract(self, file_bytes: bytes, filename: str):
        return self.robust_parser.parse_pdf(file_bytes, filename)

class PlaceholderExtractor(BaseExtractor):
    """Fallback extractor for removed heavy libraries (Word, Excel, PPTX, Images) to save RAM."""
    def extract(self, file_bytes: bytes, filename: str) -> ExtractedDocument:
        text = f"[Extraction disabled on Render free tier to save memory. Cannot parse {filename}]"
        nodes = [ExtractedNode(text=text, metadata={"page_number": 1, "page_label": "1", "source": filename, "format": "unsupported"})]
        return ExtractedDocument(filename=filename, nodes=nodes)

# Register Extractors Securely
registry.register("application/pdf", PDFExtractor)
registry.register("text/plain", TextExtractor)

# Map heavy formats to the placeholder to prevent crashes
heavy_formats = [
    "image/jpeg", "image/png", "image/tiff", "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
]

for fmt in heavy_formats:
    registry.register(fmt, PlaceholderExtractor)