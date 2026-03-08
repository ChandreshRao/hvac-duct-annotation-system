import os
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_annotation_endpoint_centerlines():
    """Test that the annotate endpoint can detect single centerlines from testset2.pdf"""
    pdf_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "sample", "testset2.pdf"
    )
    
    assert os.path.exists(pdf_path), f"Test PDF not found at {pdf_path}"
    
    with open(pdf_path, "rb") as f:
        response = client.post(
            "/api/v1/annotate",
            files={"file": ("testset2.pdf", f, "application/pdf")},
        )
        
    assert response.status_code == 200, f"Endpoint returned {response.status_code}: {response.text}"
    
    data = response.json()
    assert "annotations" in data
    
    annotations = data["annotations"]
    
    # Check if we have any auto-detected centerlines
    centerlines = [ann for ann in annotations if ann.get("source") == "auto_centerline"]
    
    assert len(centerlines) > 0, "No auto-detected centerlines found!"
    
    for cl in centerlines:
        assert "line" in cl and cl["line"], "Centerline annotation missing line coordinates"
        assert "x1" in cl["line"]
        assert "y1" in cl["line"]
        assert "x2" in cl["line"]
        assert "y2" in cl["line"]
        assert cl["orientation"] in ["horizontal", "vertical"]
        assert cl["dimension"], "Centerline missing dimension label"
        print(f"Bbox: {cl['bbox']}")
        print(f"Line Segment: {cl['line']}")
        print(f"Label: {cl['label']}")
        print(f"Dimension: {cl['dimension']}")
        
        # New checks to guarantee the tight-bound logic!
        assert cl['bbox']['x0'] == min(cl['line']['x1'], cl['line']['x2'])
        assert cl['bbox']['x1'] == max(cl['line']['x1'], cl['line']['x2'])
        assert cl['bbox']['y0'] == min(cl['line']['y1'], cl['line']['y2'])
        assert cl['bbox']['y1'] == max(cl['line']['y1'], cl['line']['y2'])
        assert cl['label'] == cl['dimension']
