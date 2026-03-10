"""
Diagnostic: compare what PyMuPDF sees vs what the viewer expects.
"""
import fitz

pdf_path = "/app/sample/testset2.pdf"
doc = fitz.open(pdf_path)
page = doc[0]

print("=== PAGE INFO ===")
print(f"MediaBox: {page.mediabox}")
print(f"CropBox:  {page.cropbox}")
print(f"rect:     {page.rect}")
print(f"rotation: {page.rotation}")

print(f"\nDerotation matrix: {page.derotation_matrix}")
print(f"Transformation matrix: {page.transformation_matrix}")

# Sample a few text blocks to see their coordinates
print("\n=== FIRST 5 TEXT BLOCKS (raw, no derotation) ===")
raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
count = 0
for block in raw.get("blocks", []):
    if block.get("type") != 0:
        continue
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            text = span.get("text", "").strip()
            if not text:
                continue
            bbox = span.get("bbox", (0,0,0,0))
            print(f"  '{text[:20]}' → bbox={tuple(round(v,1) for v in bbox)}")
            count += 1
            if count >= 5:
                break
        if count >= 5:
            break
    if count >= 5:
        break

# Now apply derotation matrix and see what happens
print("\n=== FIRST 5 TEXT BLOCKS (derotated) ===")
dm = page.derotation_matrix
count = 0
for block in raw.get("blocks", []):
    if block.get("type") != 0:
        continue
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            text = span.get("text", "").strip()
            if not text:
                continue
            bbox = span.get("bbox", (0,0,0,0))
            corners = [fitz.Point(bbox[0], bbox[1]), fitz.Point(bbox[2], bbox[1]),
                       fitz.Point(bbox[2], bbox[3]), fitz.Point(bbox[0], bbox[3])]
            tr = [p * dm for p in corners]
            xs = [p.x for p in tr]
            ys = [p.y for p in tr]
            print(f"  '{text[:20]}' → derotated=({round(min(xs),1)},{round(min(ys),1)},{round(max(xs),1)},{round(max(ys),1)})")
            count += 1
            if count >= 5:
                break
        if count >= 5:
            break
    if count >= 5:
        break

# Sample some drawings
print("\n=== FIRST 5 LINE SEGMENTS ===")
paths = page.get_drawings()
seg_count = 0
for path in paths:
    for item in path.get("items", []):
        if item[0] == "l":
            p0, p1 = item[1], item[2]
            print(f"  Line: ({round(p0.x,1)},{round(p0.y,1)}) → ({round(p1.x,1)},{round(p1.y,1)})")
            seg_count += 1
            if seg_count >= 5:
                break
    if seg_count >= 5:
        break

doc.close()
