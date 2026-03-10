import json
import difflib
from app.services.pdf_parser import parse_pdf
from app.services.duct_text_extractor import extract_duct_text_annotations
from app.services.centerline_tracer import is_round_duct_label, extract_round_duct_size

pdf_path = "/app/sample/testset2.pdf"
hardcode_path = "/app/sample/response_hardcoded.json"

print("Parsing hardcoded references...")
with open(hardcode_path, "r") as f:
    hardcode_data = json.load(f)

expected_counts = {}
for a in hardcode_data.get("annotations", []):
    lbl = a.get("label", "").replace('"', '').replace('⌀', '').strip()
    expected_counts[lbl] = expected_counts.get(lbl, 0) + 1

print("\n--- Expected Duct Sizes (from manual annotations) ---")
for k, v in expected_counts.items():
    print(f"Size {k}⌀ : {v} occurrences")

print("\nRunning text extractor pipeline...")
extracted = extract_duct_text_annotations(pdf_path)

found_counts = {}
for item in extracted:
    lbl = str(item.get("label", ""))
    size = extract_round_duct_size(lbl)
    if size is not None:
        key = str(int(size)) if size.is_integer() else str(size)
        found_counts[key] = found_counts.get(key, 0) + 1

print("\n--- Extracted Round Duct Sizes ---")
for k, v in found_counts.items():
    print(f"Size {k}⌀ : {v} occurrences")

print("\n--- Comparison ---")
all_keys = set(expected_counts.keys()) | set(found_counts.keys())
missing = []
for k in sorted(all_keys, key=lambda x: float(x) if x.replace('.','').isdigit() else 0):
    exp = expected_counts.get(k, 0)
    fnd = found_counts.get(k, 0)
    status = "✅ MATCH" if exp == fnd else "⚠️ MISMATCH"
    if fnd > exp: status = "✅ MORE FOUND"
    if exp > fnd: missing.append(f"{exp - fnd}x {k}⌀")
    print(f"Size {k:<4}: Expected={exp:<2} Found={fnd:<2} | {status}")

if missing:
    print(f"\nMissing entirely: {', '.join(missing)}")
else:
    print("\nAll manual sizes successfully extracted!")
