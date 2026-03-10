import fitz
doc = fitz.open("/app/sample/testset2.pdf")
p = doc[0]
raw = p.get_text("dict")
for b in raw.get("blocks", []):
    if b.get("type") == 0:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                t = s.get("text","").strip()
                if "18" in t or "11" in t:
                    print(f"RAW: '{t}' bbox: {s.get('bbox')}")
                    
paths = p.get_drawings()
for p in paths[:2]:
    for it in p.get("items", [])[:2]:
        print(f"RAW DRAWING item: {it}")
