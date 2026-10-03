"""Encryption Agent tools: Key Search over the email, and decryption.

Keys come only from the email the file arrived with (RT-15): the subject and
the body, newest thread segment first. Nothing is brute-forced and no stored
key list exists. The audit records *where* a working key was found (its
locator), never the key itself.

Decryptable: PDFs with a user password (pypdf), password-protected Office
files (OOXML and legacy, msoffcrypto-tool), and ZipCrypto/AES zip members are
handled by the zip reader with the same candidates.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

#: "password: X", "pwd is X", "the passcode for the attachment is X", "PIN - X"
_KEY_PATTERNS = [
    re.compile(r"(?i)\b(?:password|passwd|pwd|passcode|pass\s*code|passphrase|pin|open(?:ing)?\s+code|key)\b"
               r"[^:\n=]{0,40}?(?:\bis\b|:|=|-|–)\s*[\"'“‘]?([^\s\"'”’]{3,64})"),
]
_TRAILING = ".,;:)]}"


@dataclass(frozen=True)
class KeyCandidate:
    key: str
    locator: str              # where it was found, e.g. "body#L4" or "subject"


def find_keys(subject: str | None, body_lines: list[tuple[str, str]]) -> list[KeyCandidate]:
    """Candidate keys from the email context, in the order to try them.

    ``body_lines`` is ``[(locator, text)]`` newest segment first.
    """
    out: list[KeyCandidate] = []
    seen: set[str] = set()
    sources = ([("subject", subject)] if subject else []) + body_lines
    for locator, text in sources:
        for rx in _KEY_PATTERNS:
            for m in rx.finditer(text or ""):
                key = m.group(1).rstrip(_TRAILING)
                if len(key) >= 3 and key.lower() not in ("is", "the", "below", "attached") and key not in seen:
                    seen.add(key)
                    out.append(KeyCandidate(key, locator))
    return out


def is_encrypted(data: bytes, kind: str) -> bool:
    if kind == "pdf":
        try:
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(data))
            return bool(r.is_encrypted) and r.decrypt("") == 0
        except Exception:
            return False
    if kind in ("ole2", "excel", "docx"):
        try:
            import msoffcrypto
            return msoffcrypto.OfficeFile(io.BytesIO(data)).is_encrypted()
        except Exception:
            return False
    return False


def decrypt(data: bytes, kind: str, key: str) -> bytes | None:
    """The decrypted bytes, or None when ``key`` does not open the file."""
    if kind == "pdf":
        from pypdf import PdfReader, PdfWriter
        try:
            r = PdfReader(io.BytesIO(data))
            if r.decrypt(key) == 0:
                return None
            w = PdfWriter(clone_from=r)
            buf = io.BytesIO()
            w.write(buf)
            return buf.getvalue()
        except Exception:
            return None
    try:
        import msoffcrypto
        f = msoffcrypto.OfficeFile(io.BytesIO(data))
        f.load_key(password=key)
        buf = io.BytesIO()
        f.decrypt(buf)
        return buf.getvalue()
    except Exception:
        return None


def try_keys(data: bytes, kind: str, keys: list[KeyCandidate]) -> tuple[bytes, KeyCandidate] | None:
    for cand in keys:
        out = decrypt(data, kind, cand.key)
        if out is not None:
            return out, cand
    return None
