"""Evaluate with the real runtime (DT-29): the engine that will serve a config is the one that scores it.

The candidate is a runtime config version folder (configwriter.py). It is
placed in a scratch config root, next to the lookups that apply to its client
and use case, and every corpus sample is rebuilt as an ``.eml`` (subject, body,
attachments) in a scratch input root. One isolated runtime process
(learning/runtime.py) extracts the whole batch; each answer is mapped onto the
outcome shape the evaluation agent scores (a result when the run was clean, a
review entry when it was flagged), so the metrics, gates and threshold tuning
are computed exactly as before, from what the runtime actually did.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..config import Settings
from ..contracts.corpus import Sample
from ..learning.runtime import IsolatedRuntime

ENGINE_NAME = "runtime"
EVAL_VERSION = "0.0.0"
SENDER = "sender@corpus.invalid"


def sample_eml(sample: Sample, corpus_root: Path | None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = sample.subject or ""
    msg["From"], msg["To"] = SENDER, "inbox@corpus.invalid"
    msg["Message-ID"] = f"<{sample.sample_id}@corpus.invalid>"
    msg.set_content(sample.body or "")
    for a in sample.attachments:
        if not a.path:
            continue
        path = Path(a.path)
        if not path.is_absolute() and corpus_root is not None:
            path = Path(corpus_root) / path
        if not path.is_file():
            continue
        msg.add_attachment(path.read_bytes(), maintype="application", subtype="octet-stream", filename=a.filename)
    return bytes(msg)


@dataclass
class RuntimeOutcome:
    """The evaluation agent's view of one runtime answer."""

    result: Any
    review: Any
    audit: Any
    classification: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None


def _outcome(answer: dict[str, Any]) -> RuntimeOutcome:
    if not answer.get("ok"):
        err = answer.get("error") or {}
        audit = SimpleNamespace(audit_id="error", outcome="error")
        review = SimpleNamespace(type=None, confidence=0.0, partial_fields={},
                                 review_reason=[f"error:{err.get('error', 'runtime_failed')}"])
        return RuntimeOutcome(result=None, review=review, audit=audit, flags=review.review_reason)
    out = answer["output"]
    c = out.get("metadata", {}).get("classification") or {}
    etype = c.get("type") if c.get("status") != "out_of_scope" else None
    etype = etype or out.get("metadata", {}).get("config", {}).get("email_type")
    if c.get("status") == "out_of_scope":
        etype = None
    rec = (out.get("records") or [{}])[0]
    fields = {name: SimpleNamespace(value=v.get("value"), source=v.get("source"))
              for name, v in (rec.get("fields") or {}).items() if v.get("value") is not None}
    conf = float(out.get("confidence") or 0.0)
    audit = SimpleNamespace(audit_id=out.get("metadata", {}).get("audit_id", ""),
                            outcome="review_queued" if out.get("flagged") else "extracted")
    if out.get("flagged"):
        review = SimpleNamespace(type=etype, confidence=conf, partial_fields=fields,
                                 review_reason=list(out.get("flags") or []))
        return RuntimeOutcome(result=None, review=review, audit=audit, classification=c,
                              flags=review.review_reason, output=out)
    result = SimpleNamespace(type=etype, confidence=conf, fields=fields)
    return RuntimeOutcome(result=result, review=None, audit=audit, classification=c, output=out)


class RuntimeEngine:
    """Run one candidate config folder over many samples in the isolated runtime."""

    def __init__(self, settings: Settings, *, client: str, usecase: str, folder: Path,
                 live_root: Path | None = None) -> None:
        self.settings = settings
        self.client, self.usecase = client, usecase
        self.folder = Path(folder)
        self.live_root = Path(live_root or settings.runtime_config_root)

    def _scratch_root(self, tmp: Path) -> Path:
        root = tmp / "configs"
        dst = root / self.client / self.usecase / EVAL_VERSION
        shutil.copytree(self.folder, dst)
        for rel in ("lookups", f"{self.client}/lookups", f"{self.client}/{self.usecase}/lookups"):
            src = self.live_root / rel
            if src.is_dir():
                shutil.copytree(src, root / rel, dirs_exist_ok=True)
        return root

    def run(self, samples: list[Sample], corpus_root: Path | None) -> dict[str, RuntimeOutcome]:
        if not samples:
            return {}
        with tempfile.TemporaryDirectory(prefix="dt-eval-") as t:
            tmp = Path(t)
            root = self._scratch_root(tmp)
            inputs = tmp / "inputs"
            inputs.mkdir()
            runs = []
            for s in samples:
                name = f"{s.sample_id}.eml"
                (inputs / name).write_bytes(sample_eml(s, corpus_root))
                runs.append({"file_location": name, "client": self.client, "usecase": self.usecase,
                             "version": EVAL_VERSION})
            rt = IsolatedRuntime(self.settings.runtime_dir, python=self.settings.runtime_python, input_root=inputs,
                                 timeout_s=self.settings.runtime_timeout_s,
                                 model_provider=self.settings.learning_model_provider)
            answers = rt.run(root, runs)
        return {s.sample_id: _outcome(a) for s, a in zip(samples, answers)}
