from pydantic import BaseModel, Field
from typing import List, Optional

class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    page_number: int

class ASTNode(BaseModel):
    node_id: str
    text: str
    bounding_box: BoundingBox
    block_type: int = 0 # 0 for text, 1 for image

class Document(BaseModel):
    filename: str
    total_pages: int
    nodes: List[ASTNode] = Field(default_factory=list)