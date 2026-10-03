"""The LangGraph pipeline: node order, the skip branch, and the published graph."""
import io
import zipfile

from tests.samples import logo_png

FULL = ["resolve_config", "read_input", "build_evidence", "extract", "verify", "route"]


def test_normal_run_visits_every_node(client):
    m = client.post("/extract", json={"file_location": "invoice.pdf", "extended": True}).json()["metadata"]
    assert m["graph"]["path"] == FULL
    assert set(m["timings_ms"]) == set(FULL)


def test_nothing_readable_skips_the_model(client, input_root):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("photo.png", logo_png())
    (input_root / "photos.zip").write_bytes(buf.getvalue())
    e = client.post("/extract", json={"file_location": "photos.zip", "extended": True}).json()
    assert e["metadata"]["graph"]["path"] == [n for n in FULL if n != "extract"]
    assert e["status"] == "review" and "no_readable_content" in e["review_reasons"]
    assert e["data"] == [] and e["records"] == []           # nothing to read: no records


def test_graph_endpoint_draws_the_pipeline(client):
    text = client.get("/graph").text
    for node in FULL:
        assert node in text
    assert "build_evidence -.-> extract" in text or "build_evidence -.->" in text
