import app.services.centerline_tracer as tracer
from app.services.pdf_parser import parse_pdf
from app.services.duct_text_extractor import extract_duct_text_annotations

pdf_path = "/app/sample/testset2.pdf"
print("Parsing PDF for lines...")
parsed = parse_pdf(pdf_path)
page_lines = parsed.lines

print(f"Total lines: {len(page_lines)}")

print("Extracting text annotations...")
texts = extract_duct_text_annotations(pdf_path)

for item in texts:
    lbl = str(item.get("label", ""))
    if "⌀" not in lbl and "18" not in lbl and "11" not in lbl: continue
    
    print(f"\n--- Checking label '{lbl}' ---")
    is_round = tracer.is_round_duct_label(lbl)
    print(f"is_round_duct_label? {is_round}")
    
    if is_round:
        cx, cy = item["center"]
        print(f"Center: ({cx:.1f}, {cy:.1f})")
        # Try finding passing line directly
        for axis in ["horizontal", "vertical"]:
            seg = tracer.find_passing_line(cx, cy, page_lines, axis, snap_px=60.0)
            if seg:
                print(f"Found passing line on {axis} axis: len={tracer._seg_length(seg):.1f}")
            else:
                print(f"No passing line on {axis} axis")
                
        # Try full trace
        res_h = tracer.trace_from_label(lbl, cx, cy, "horizontal", page_lines, snap_px=60.0)
        res_v = tracer.trace_from_label(lbl, cx, cy, "vertical", page_lines, snap_px=60.0)
        print(f"Trace result (horizontal): {res_h}")
        print(f"Trace result (vertical): {res_v}")
