"""The LangGraph pipeline: node order, the skip branch, and the published graph."""
import io
import zipfile

from tests.samples import logo_png

FULL = ["resolve_config", "ingest", "parse_item", "assemble", "extract", "verify", "route"]


def test_normal_run_visits_every_node(client):
    m = client.post("/extract", json={"file_location": "invoice.pdf", "extended": True}).json()["metadata"]
    assert m["graph"]["path"] == ["resolve_config", "ingest", "parse_item:invoice.pdf", "assemble",
                                  "extract", "verify", "route"]
    assert set(m["timings_ms"]) == set(m["graph"]["path"])


def test_items_are_parsed_in_parallel_branches(client):
    m = client.post("/extract", json={"file_location": "invoice-email.eml", "extended": True}).json()["metadata"]
    path = m["graph"]["path"]
    branches = [p for p in path if p.startswith("parse_item:")]
    assert sorted(branches) == ["parse_item:INV-20194.xlsx", "parse_item:email body"]
    assert path.index("assemble") > max(path.index(b) for b in branches)      # one join after all branches


def test_nothing_readable_skips_the_model(client, input_root):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("photo.png", logo_png())
    (input_root / "photos.zip").write_bytes(buf.getvalue())
    e = client.post("/extract", json={"file_location": "photos.zip", "extended": True}).json()
    assert e["metadata"]["graph"]["path"] == ["resolve_config", "ingest", "assemble", "verify", "route"]
    assert e["flagged"] is True and "no_readable_content" in e["flags"]
    assert e["data"] == [] and e["records"] == []           # nothing to read: no records


def test_graph_endpoint_draws_the_pipeline(client):
    text = client.get("/graph").text
    for node in FULL:
        assert node in text
    assert "assemble -.-> extract" in text or "assemble -.->" in text
