from app.services.duct_text_extractor import _extract_page0_text_spans, _first_pattern_match
import app.services.centerline_tracer as tracer

pdf_path = "/app/sample/testset2.pdf"
print("Extracting raw spans...")
spans = _extract_page0_text_spans(pdf_path)

print(f"Total raw spans extracted: {len(spans)}")

print("\n--- Spans containing '14' or '12' ---")
for s in spans:
    t = s.text.strip()
    if '14' in t or '12' in t:
        match = _first_pattern_match(t)
        direct_re = tracer.is_round_duct_label(t)
        print(f"TEXT: [{t!r}] -> duct_text_extractor match: {match} (tracer direct_re: {direct_re})")
