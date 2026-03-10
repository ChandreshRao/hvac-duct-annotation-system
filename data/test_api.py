import urllib.request, json

boundary = "testboundary"
with open("/app/sample/testset2.pdf", "rb") as f:
    pdf_bytes = f.read()

header = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"testset2.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
footer = ("\r\n--" + boundary + "--\r\n").encode()
body = header + pdf_bytes + footer

req = urllib.request.Request("http://localhost:8000/api/v1/annotate", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
with urllib.request.urlopen(req, timeout=120) as resp:
    d = json.loads(resp.read())

results = []
for a in d.get("annotations", []):
    ln = a.get("line")
    results.append({
        "id": a.get("id"),
        "label": str(a.get("dimension") or a.get("label") or ""),
        "source": str(a.get("source", "")),
        "line": ln
    })

with open("/tmp/test_output.json", "w") as f:
    json.dump(results, f, indent=2)
print("Finished. Output saved to /tmp/test_output.json")
