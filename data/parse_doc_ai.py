import json

with open("d:\\chandreshRaoRepos\\hvac-duct-annotation-system\\sample\\document.json", "r", encoding="utf-8") as f:
    data = json.load(f)

text = data.get("text", "")
pages = data.get("pages", [])

found = []
for p in pages:
    for block in p.get("blocks", []):
        text_anchor = block.get("layout", {}).get("textAnchor", {})
        segments = text_anchor.get("textSegments", [])
        
        block_text = ""
        for seg in segments:
            start = int(seg.get("startIndex", 0))
            end = int(seg.get("endIndex", 0))
            block_text += text[start:end]
            
        block_text = block_text.replace('\n', ' ').strip()
        
        if '18' in block_text or '14' in block_text or '12' in block_text or '8' in block_text:
            found.append(block_text)

for b in found:
    print(b)
