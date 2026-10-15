import os
import sys
import pytest
from PIL import Image, ImageDraw, ImageText

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.parsers.extractors import registry

def test_registry_text_extraction(tmp_path):
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("This is a standard text clause for the vector DB.")
    
    doc = registry.extract(str(txt_file), "sample.txt")
    assert doc.filename == "sample.txt"
    assert len(doc.nodes) > 0
    assert "standard text clause" in doc.nodes[0].text
    assert doc.nodes[0].metadata["format"] == "text"

def test_registry_image_ocr_extraction(tmp_path):
    img_file = tmp_path / "scanned_invoice.png"
    
    # Create a dummy image with text using PIL
    img = Image.new('RGB', (400, 100), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    # Tesseract needs clear text to OCR
    d.text((10, 40), "INVOICE TOTAL: $500", fill=(0, 0, 0))
    img.save(img_file)
    
    doc = registry.extract(str(img_file), "scanned_invoice.png")
    
    # Assert Tesseract OCR engine successfully ran
    assert len(doc.nodes) > 0
    # Allow for slight OCR noise/casing variations
    assert "INVOICE" in doc.nodes[0].text.upper() or "500" in doc.nodes[0].text
    assert doc.nodes[0].metadata["format"] == "image"

def test_registry_unsupported_rejection(tmp_path):
    fake_exe = tmp_path / "malware.exe"
    # MZ header for windows executable
    fake_exe.write_bytes(b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00')
    
    with pytest.raises(ValueError, match="Unsupported document format"):
        registry.extract(str(fake_exe), "malware.exe")
