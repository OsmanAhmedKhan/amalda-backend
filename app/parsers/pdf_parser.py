import os
import uuid
import logging
import pytesseract
import fitz  # PyMuPDF
from PIL import Image
import io
from typing import List, Optional
from pydantic import BaseModel
from fastapi import HTTPException

from app.core.insforge import insforge

logger = logging.getLogger("AMALDA_API")
# NOTE: Ensure Tesseract is installed in your local environment or Docker container.
# If running on Linux (Docker), remove the Windows path below.
if os.name == 'nt':
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

class BoundingBox(BaseModel):
    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float

class ParsedNode(BaseModel):
    node_id: str
    text: str
    bounding_box: BoundingBox
    clause_heading: Optional[str] = None

class ParsedDocument(BaseModel):
    filename: str
    nodes: List[ParsedNode]

class LegalDocumentParser:
    def __init__(self):
        pass

    def _perform_ocr_on_page(self, pdf_bytes: bytes, page_index: int) -> str:
        """Performs OCR directly from a memory stream."""
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page = doc.load_page(page_index)
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes()))
            ocr_text = pytesseract.image_to_string(img)
            doc.close()
            return ocr_text
        except Exception as e:
            logger.error(f"OCR failed on page {page_index+1}. Error: {e}")
            return "[OCR FAILED OR UNAVAILABLE]"

    def _semantic_chunking(self, pages_data: List[dict], filename: str) -> List[ParsedNode]:
        """Creates ~1000 character chunks with a 3-line overlap."""
        nodes = []
        current_chunk = []
        current_length = 0
        current_page = 1
        page_width, page_height = 0, 0
        
        for page in pages_data:
            lines = page["text"].split('\n')
            current_page = page["page_number"]
            page_width = page.get("width", 500)
            page_height = page.get("height", 800)
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                current_chunk.append(line)
                current_length += len(line) + 1
                
                if current_length > 1000:
                    chunk_text = " ".join(current_chunk)
                    nodes.append(self._create_node(
                        filename, chunk_text, "Legal_Clause", current_page, page_width, page_height
                    ))
                    current_chunk = current_chunk[-3:]
                    current_length = sum(len(l) + 1 for l in current_chunk)
                    
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            if len(chunk_text) > 50:
                nodes.append(self._create_node(
                    filename, chunk_text, "Legal_Clause", current_page, page_width, page_height
                ))
            
        return nodes

    def _create_node(self, filename: str, text: str, heading: str, page_num: int, width: float, height: float) -> ParsedNode:
        unique_id = str(uuid.uuid4())[:8]
        node_id = f"{filename}_p{page_num}_{unique_id}"
        
        return ParsedNode(
            node_id=node_id,
            text=text,
            clause_heading=heading,
            bounding_box=BoundingBox(
                page_number=page_num,
                x0=0, y0=0, x1=width, y1=height
            )
        )

    def parse_pdf_incrementally(self, file_bytes: bytes, filename: str, vector_db, batch_size=50):
        """
        🟢 STATELESS: Processes PDFs safely from memory bytes.
        Uses InsForge to track progress so it can resume if the server crashes.
        """
        job_status = insforge.get_job_status(filename)
        start_page = job_status["processed_pages"] if job_status else 0
        
        logger.info(f"Starting resumable ingestion for {filename} at page {start_page}...")

        with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
            total_pages = len(pdf)
            
            if not job_status:
                insforge.update_job_status(filename, 0, total_pages, 'PROCESSING')

            for i in range(start_page, total_pages, batch_size):
                end_page = min(i + batch_size, total_pages)
                pages_data = []
                batch_has_text = False
                
                for page_num in range(i, end_page):
                    page = pdf.load_page(page_num)
                    text = page.get_text("text")
                    
                    if not text or len(text.strip()) < 50:
                        text = self._perform_ocr_on_page(file_bytes, page_num)
                    
                    clean_text = text.strip() if text else ""
                    if clean_text:
                        batch_has_text = True
                        
                    # Handle page bounds safely
                    rect = page.rect
                    pages_data.append({
                        "page_number": page_num + 1,
                        "text": clean_text,
                        "width": rect.width if rect else 500,
                        "height": rect.height if rect else 800
                    })

                if not batch_has_text:
                    logger.error(f"SILENT FAILURE DETECTED: No readable text extracted in batch {i} to {end_page} from {filename}.")
                    raise HTTPException(
                        status_code=422, 
                        detail=f"Unprocessable Entity: PDF extraction yielded no readable text between pages {i} and {end_page}. OCR failed or document is empty."
                    )

                nodes = self._semantic_chunking(pages_data, filename)
                
                class TmpDoc: pass
                tmp = TmpDoc()
                tmp.filename = filename
                tmp.nodes = nodes
                vector_db.index_document(tmp)

                insforge.update_job_status(filename, end_page, total_pages, 'PROCESSING')
                logger.info(f"✅ Checkpoint Saved: Processed pages {i} to {end_page} of {total_pages}")

        insforge.update_job_status(filename, total_pages, total_pages, 'COMPLETED')
        logger.info(f"✅ Fully completed processing {filename}.")
        
    def parse_pdf(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        """🟢 STATELESS: Legacy compatibility wrapper for older endpoints"""
        logger.info(f"Extracting text from {filename} (Memory Stream Mode)...")
        pages_data = []
        document_has_text = False
        
        with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
            for i in range(len(pdf)):
                page = pdf.load_page(i)
                text = page.get_text("text")
                
                if not text or len(text.strip()) < 50:
                    text = self._perform_ocr_on_page(file_bytes, i)
                
                clean_text = text.strip() if text else ""
                if clean_text:
                    document_has_text = True
                    
                rect = page.rect
                pages_data.append({
                    "page_number": i + 1,
                    "text": clean_text,
                    "width": rect.width if rect else 500,
                    "height": rect.height if rect else 800
                })

        if not document_has_text:
            logger.error(f"SILENT FAILURE DETECTED: No readable text extracted from {filename}.")
            raise HTTPException(
                status_code=422, 
                detail="Unprocessable Entity: PDF extraction yielded no readable text. If this is a scanned document, OCR is required."
            )

        nodes = self._semantic_chunking(pages_data, filename)
        logger.info(f"Generated {len(nodes)} dense legal nodes.")
        return ParsedDocument(filename=filename, nodes=nodes)