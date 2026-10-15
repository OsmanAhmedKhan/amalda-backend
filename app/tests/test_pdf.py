import os
import pytest
import fitz

def test_real_pdf_ingestion_and_extraction():
    pdf_path = os.path.join(os.getenv("UPLOAD_DIR", "./data/uploads"), "EMPLOYMENT-AGREEMENT.pdf")
    assert os.path.exists(pdf_path), "Target PDF missing"

    doc = fitz.open(pdf_path)
    assert doc.page_count > 0, "PDF is empty"

    # Extract all text to verify OCR/Parsing works against ground truth
    full_text = "\n".join([page.get_text() for page in doc])
    doc.close()

    # Assert meaningful extraction based on the Golden Dataset ground truth
    assert "Base Compensation" in full_text or "$175,000" in full_text, "Failed to extract core financial terms from PDF."
    assert "Texas" in full_text, "Failed to extract governing law jurisdiction."
