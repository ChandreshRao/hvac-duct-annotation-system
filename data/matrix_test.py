import fitz, json
doc = fitz.open("/app/sample/testset2.pdf")
p = doc[0]
cb = p.cropbox
c0, c1, c2, c3 = fitz.Point(cb.x0, cb.y0), fitz.Point(cb.x1, cb.y0), fitz.Point(cb.x1, cb.y1), fitz.Point(cb.x0, cb.y1)
dm = p.derotation_matrix
tr = [pt * dm for pt in (c0,c1,c2,c3)]
out = {
    "rot": p.rotation,
    "cb": [cb.x0, cb.y0, cb.x1, cb.y1],
    "dm": [dm.a, dm.b, dm.c, dm.d, dm.e, dm.f],
    "tr": [[pt.x, pt.y] for pt in tr]
}
with open("/tmp/matrix_out.json", "w") as f:
    json.dump(out, f, indent=2)
