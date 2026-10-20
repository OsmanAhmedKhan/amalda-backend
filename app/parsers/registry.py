import magic
import zipfile
import io
from typing import List, Dict, Type, Any
from dataclasses import dataclass
from abc import ABC, abstractmethod

@dataclass
class ExtractedNode:
    text: str
    metadata: Dict[str, Any]

@dataclass
class ExtractedDocument:
    filename: str
    nodes: List[ExtractedNode]

class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, file_bytes: bytes, filename: str):
        # 🟢 STATELESS UPDATE: Replaced file_path with file_bytes
        pass

class ExtractorRegistry:
    def __init__(self):
        self._extractors: Dict[str, Type[BaseExtractor]] = {}

    def register(self, mime_type: str, extractor_class: Type[BaseExtractor]):
        self._extractors[mime_type] = extractor_class

    def get_mime_type(self, file_bytes: bytes) -> str:
        # Sniff MIME type directly from the byte buffer
        mime = magic.from_buffer(file_bytes[:2048], mime=True)
        
        # OOXML structural inspection: .docx, .xlsx, .pptx are ZIP archives
        if mime in ['application/zip', 'application/octet-stream', 'application/x-zip-compressed']:
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as zf:
                    namelist = zf.namelist()
                    if 'word/document.xml' in namelist:
                        return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    elif 'xl/workbook.xml' in namelist:
                        return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    elif 'ppt/presentation.xml' in namelist:
                        return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
            except zipfile.BadZipFile:
                pass # Fall back to default detected mime
                
        return mime

    def extract(self, file_bytes: bytes, filename: str):
        mime_type = self.get_mime_type(file_bytes)
        
        if mime_type.startswith("text/") or mime_type in ["inode/x-empty", "application/csv"]:
            extractor_class = self._extractors.get("text/plain")
        else:
            extractor_class = self._extractors.get(mime_type)

        if not extractor_class:
            raise ValueError(f"Unsupported document format: {mime_type}")
            
        extractor = extractor_class()
        return extractor.extract(file_bytes, filename)

registry = ExtractorRegistry()