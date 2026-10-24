from typing import Dict, Any, Type, List
from abc import ABC, abstractmethod
from dataclasses import dataclass
import zipfile
import io

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
    def extract(self, file_bytes: bytes, filename: str) -> ExtractedDocument:
        pass

class FormatRegistry:
    def __init__(self):
        self._extractors: Dict[str, Type[BaseExtractor]] = {}

    def register(self, mime_type: str, extractor_class: Type[BaseExtractor]):
        self._extractors[mime_type] = extractor_class

    def get_extractor(self, mime_type: str) -> BaseExtractor:
        extractor_class = self._extractors.get(mime_type)
        if not extractor_class:
            # Fallback to Text Extractor if no specific extractor exists
            return self._extractors.get("text/plain")()
        return extractor_class()

    def sniff_mime(self, file_bytes: bytes) -> str:
        # 🔥 MEMORY OPTIMIZATION: Fallback simplistic byte sniffing since 'python-magic' C-library is removed
        header = file_bytes[:4]
        if header.startswith(b'%PDF'):
            return 'application/pdf'
        elif header.startswith(b'\x89PNG'):
            return 'image/png'
        elif header.startswith(b'\xff\xd8'):
            return 'image/jpeg'
        elif header.startswith(b'PK\x03\x04'):
            # It's a zip file, could be docx/xlsx/pptx
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
                pass
            return 'application/zip'
        return 'text/plain'

    def extract(self, file_bytes: bytes, filename: str) -> ExtractedDocument:
        mime_type = self.sniff_mime(file_bytes)
        extractor = self.get_extractor(mime_type)
        return extractor.extract(file_bytes, filename)

registry = FormatRegistry()