"""Run spool: item bytes stored once per run and referenced from the run state.

Intake writes each item's bytes here and keeps only a reference in the
submission, so checkpoints stay small whatever the attachment size (RT-34).
With STATE_KEY set (a Fernet key), bytes are encrypted at rest (RT-35, RT-51);
the spool of a finished run is deleted by the retention sweep.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


class Spool:
    def __init__(self, folder: Path, key: str | None = None) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._fernet = None
        if key:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def put(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()
        path = self.folder / ref
        if not path.exists():
            blob = self._fernet.encrypt(data) if self._fernet else data
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, path)
        return ref

    def get(self, ref: str) -> bytes:
        if not all(c in "0123456789abcdef" for c in ref) or len(ref) != 64:
            raise ValueError("bad spool reference")
        blob = (self.folder / ref).read_bytes()
        return self._fernet.decrypt(blob) if self._fernet else blob

    def delete(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)
