import os
import uuid
import logging
import gc
import io
from typing import List, Optional
from pydantic import BaseModel
from fastapi import HTTPException

try:
    import pytesseract
    from PIL import Image
    import pymupdf
    HAS_OCR = True
except Exception as e:
    HAS_OCR = False
    pytesseract = None
    Image = None
    pymupdf = None
    logging.getLogger("AMALDA_API").warning(f"OCR dependencies not available: {e}")

if os.name == 'nt' and HAS_OCR:
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

logger = logging.getLogger("AMALDA_API")

from app.core.insforge import insforge

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
        """OCR a single page with low DPI to fit 512MB RAM. Returns empty if OCR unavailable."""
        if not HAS_OCR:
            return ""
        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            page = doc.load_page(page_index)
            pix = page.get_pixmap(dpi=110)  # Low DPI to minimize RAM
            img = Image.open(io.BytesIO(pix.tobytes()))
            ocr_text = pytesseract.image_to_string(img)

            img.close()
            del img
            del pix
            page = None
            doc.close()
            gc.collect()

            return ocr_text
        except Exception as e:
            logger.error(f"OCR failed on page {page_index+1}: {e}")
            return ""

    def _semantic_chunking(self, pages_data: List[dict], filename: str) -> List[ParsedNode]:
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

    def parse_pdf_incrementally(self, file_bytes: bytes, filename: str, vector_db, batch_size=10):
        job_status = insforge.get_job_status(filename) if 'insforge' in dir() else None
        start_page = job_status["processed_pages"] if job_status else 0
        logger.info(f"Starting incremental ingestion for {filename} at page {start_page}...")

        with pymupdf.open(stream=file_bytes, filetype="pdf") as pdf:
            total_pages = len(pdf)
            if not job_status:
                try:
                    insforge.update_job_status(filename, 0, total_pages, 'PROCESSING')
                except Exception:
                    pass

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

                    rect = page.rect
                    pages_data.append({
                        "page_number": page_num + 1,
                        "text": clean_text,
                        "width": rect.width if rect else 500,
                        "height": rect.height if rect else 800
                    })
                    page = None

                if not batch_has_text:
                    logger.warning(f"No readable text in batch {i} to {end_page} from {filename}.")
                    continue

                nodes = self._semantic_chunking(pages_data, filename)

                class TmpDoc: pass
                tmp = TmpDoc()
                tmp.filename = filename
                tmp.nodes = nodes
                vector_db.index_document(tmp)

                try:
                    insforge.update_job_status(filename, end_page, total_pages, 'PROCESSING')
                except Exception:
                    pass
                logger.info(f"✅ Checkpoint: Processed pages {i} to {end_page} of {total_pages}")

                del pages_data
                del nodes
                del tmp
                gc.collect()

        try:
            insforge.update_job_status(filename, total_pages, total_pages, 'COMPLETED')
        except Exception:
            pass
        logger.info(f"✅ Fully completed processing {filename}.")

    def parse_pdf(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        logger.info(f"Extracting text from {filename} (Memory Stream Mode)...")
        pages_data = []
        document_has_text = False

        with pymupdf.open(stream=file_bytes, filetype="pdf") as pdf:
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
                page = None

        if not document_has_text:
            logger.error(f"No readable text extracted from {filename}.")
            raise HTTPException(
                status_code=422,
                detail="Unprocessable Entity: PDF extraction yielded no readable text. Ensure it is a valid document."
            )

        nodes = self._semantic_chunking(pages_data, filename)
        logger.info(f"Generated {len(nodes)} dense legal nodes.")

        del pages_data
        gc.collect()

        return ParsedDocument(filename=filename, nodes=nodes)
