import json
from pathlib import Path
import re
from app.models.schemas import TextBlock

def parse_document_ai_json(json_path: str | Path, pdf_width: float = 1728.0, pdf_height: float = 2592.0) -> list[TextBlock]:
    """
    Parses a Google Cloud Document AI JSON export and extracts text segments
    that match round duct dimensions (e.g. 14", 12", 8", 18⌀).
    Returns them as TextBlock objects mapped to the PDF's coordinate space.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading Document AI JSON: {e}")
        return []

    text_content = data.get("text", "")
    pages = data.get("pages", [])
    
    # We only care about strings that contain digits and a quote mark/phi
    target_pattern = re.compile(r'\d+.*["”\u2300]', re.IGNORECASE)

    blocks: list[TextBlock] = []

    for page in pages:
        page_num = page.get("pageNumber", 1) - 1 # 0-indexed page
        
        for token in page.get("tokens", []):
            layout = token.get("layout", {})
            text_anchor = layout.get("textAnchor", {})
            segments = text_anchor.get("textSegments", [])
            
            token_text = ""
            for seg in segments:
                start = int(seg.get("startIndex", 0))
                end = int(seg.get("endIndex", 0))
                token_text += text_content[start:end]
                
            token_text = token_text.strip()
            
            if target_pattern.search(token_text):
                norm_verts = layout.get("boundingPoly", {}).get("normalizedVertices", [])
                if norm_verts:
                    xs = [v.get("x", 0.0) * pdf_width for v in norm_verts]
                    ys = [v.get("y", 0.0) * pdf_height for v in norm_verts]
                    
                    x0 = min(xs)
                    y0 = min(ys)
                    x1 = max(xs)
                    y1 = max(ys)
                    
                    blocks.append(
                        TextBlock(
                            text=token_text,
                            x0=x0,
                            y0=y0,
                            x1=x1,
                            y1=y1,
                            page=page_num,
                            source="document_ai"
                        )
                    )
                    
    return blocks
import json
from pathlib import Path
from pydantic import BaseModel
import re

from app.models.schemas import TextBlock

def parse_document_ai_json(json_path: str | Path, pdf_width: float = 1728.0, pdf_height: float = 2592.0) -> list[TextBlock]:
    """
    Parses a Google Cloud Document AI JSON export and extracts text segments
    that match round duct dimensions (e.g. 14", 12", 8", 18⌀).
    Returns them as TextBlock objects mapped to the PDF's coordinate space.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading Document AI JSON: {e}")
        return []

    text_content = data.get("text", "")
    pages = data.get("pages", [])
    
    # We only care about strings that contain digits and a quote mark/phi
    target_pattern = re.compile(r'\d+.*["”\u2300]', re.IGNORECASE)

    blocks: list[TextBlock] = []

    for page in pages:
        page_num = page.get("pageNumber", 1) - 1 # 0-indexed page
        
        # We can either use tokens or blocks. In Doc AI, tokens are usually single words.
        # "Blocks" or "Paragraphs" provide the full context. We'll iterate over tokens
        # to get tight bounding boxes for the exact measurements.
        for token in page.get("tokens", []):
            layout = token.get("layout", {})
            text_anchor = layout.get("textAnchor", {})
            segments = text_anchor.get("textSegments", [])
            
            token_text = ""
            for seg in segments:
                start = int(seg.get("startIndex", 0))
                end = int(seg.get("endIndex", 0))
                token_text += text_content[start:end]
                
            token_text = token_text.strip()
            
            # If token matches something like 14" or 12"
            if target_pattern.search(token_text):
                norm_verts = layout.get("boundingPoly", {}).get("normalizedVertices", [])
                if norm_verts:
                    xs = [v.get("x", 0.0) * pdf_width for v in norm_verts]
                    ys = [v.get("y", 0.0) * pdf_height for v in norm_verts]
                    
                    x0 = min(xs)
                    y0 = min(ys)
                    x1 = max(xs)
                    y1 = max(ys)
                    
                    blocks.append(
                        TextBlock(
                            text=token_text,
                            x0=x0,
                            y0=y0,
                            x1=x1,
                            y1=y1,
                            page=page_num,
                            source="document_ai"
                        )
                    )
                    
    return blocks
