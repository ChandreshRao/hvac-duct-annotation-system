import hashlib
import json
from app.models.schemas import ManualAnnotationPayload, DuctBBox
from app.services.manual_annotation_store import replace_document_annotations

def bootstrap():
    with open('sample/testset2.pdf', 'rb') as f:
        pdf_bytes = f.read()
    file_hash = hashlib.md5(pdf_bytes).hexdigest()
    
    with open('sample/response_hardcoded.json', 'r') as f:
        data = json.load(f)
        
    payloads = []
    for ann in data.get('annotations', []):
        bb = ann['bbox']
        # If bbox is an array, map it. Otherwise it should be a dict.
        if isinstance(bb, list):
            bb_dict = {'x0': bb[0], 'y0': bb[1], 'x1': bb[2], 'y1': bb[3], 'page': 0}
        else:
            bb_dict = bb
            
        payloads.append(ManualAnnotationPayload(
            bbox=DuctBBox(**bb_dict),
            label=ann.get('label', ''),
            pressure_class=ann.get('pressure_class'),
            dimension=ann.get('dimension'),
            material=ann.get('material'),
            confidence=ann.get('confidence', 1.0),
            orientation=ann.get('orientation', 'horizontal'),
            source=ann.get('source', 'api'),
            line=ann.get('line')
        ))
        
    replace_document_annotations(file_hash, 'testset2.pdf', payloads)
    print(f"Bootstrapped {len(payloads)} annotations for hash {file_hash}")

if __name__ == '__main__':
    bootstrap()
