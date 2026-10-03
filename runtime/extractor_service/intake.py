"""Intake: a file location -> one submission made of readable items.

The input file can be an email (.eml or Outlook .msg), a zip, or a single CSV,
Excel or PDF file. The kind is decided from the bytes, never the extension (RT-14).
Emails and zips are unpacked into items; anything that cannot be read
(images, unknown types, encrypted members, a zip over its limits) is
recorded in ``skipped`` with a reason, so nothing disappears silently.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from extractor_tools.common import sniff_kind

from .errors import ServiceError
from .msg import MsgError, is_msg, read_msg

READABLE = ("csv", "excel", "pdf")
_SYSTEM_FILES = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")


@dataclass
class Item:
    name: str            # display path inside the submission, e.g. "bundle.zip/inv.xlsx"
    data: bytes
    kind: str
    source_prefix: str   # "file:inv.csv", "attachment:inv.xlsx", "attachment:b.zip/inv.xlsx"


@dataclass
class Submission:
    location: str
    name: str
    data_sha256: str
    size: int
    kind: str
    subject: str | None = None
    sender: str | None = None
    body_text: str | None = None
    items: list[Item] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ kind detection

def detect_kind(data: bytes) -> str:
    """csv | excel | pdf | email | msg | zip | image | docx | ole2 | text | unknown"""
    kind = sniff_kind(data)
    if kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "xl/workbook.xml" in names:
            return "excel"
        if "word/document.xml" in names:
            return "docx"
        return "zip"
    if kind == "ole2":
        if is_msg(data):
            return "msg"
        try:
            import xlrd
            xlrd.open_workbook(file_contents=data, on_demand=True).release_resources()
            return "excel"
        except Exception:
            return "ole2"          # .doc, an encrypted workbook, ...
    if kind in ("pdf", "image", "email"):
        return kind
    if _looks_like_email(data):
        return "email"
    if _looks_like_csv(data):
        return "csv"
    return "text" if _is_text(data) else "unknown"


_EMAIL_HEADERS = {"from", "to", "cc", "subject", "date", "mime-version", "content-type",
                  "message-id", "received", "return-path", "reply-to", "delivered-to"}
_HEADER_LINE = re.compile(rb"^([A-Za-z][A-Za-z0-9-]*):[ \t]")


def _looks_like_email(data: bytes) -> bool:
    """A header block (Name: value lines, then a blank line) with 2+ known email headers."""
    head = data[:16384].replace(b"\r\n", b"\n")
    block, _, rest = head.partition(b"\n\n")
    if not rest and len(head) == len(block):
        return False
    names = set()
    for line in block.split(b"\n"):
        if line[:1] in (b" ", b"\t"):
            continue                       # folded header continuation
        m = _HEADER_LINE.match(line)
        if not m:
            return False
        names.add(m.group(1).decode().lower())
    return len(names & _EMAIL_HEADERS) >= 2


def _is_text(data: bytes) -> bool:
    head = data[:65536]
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _looks_like_csv(data: bytes) -> bool:
    if not _is_text(data):
        return False
    head = data[:65536]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = head.decode("utf-16", errors="ignore")
    else:
        text = head.decode("utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if len(lines) < 2:
        return False
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|")
    except csv.Error:
        return False
    widths = [len(r) for r in csv.reader(lines, dialect)]
    return max(widths) >= 2


# ------------------------------------------------------------------ email

_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_BLOCKS = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", re.I)


def html_to_text(raw: str) -> str:
    text = _BLOCKS.sub("\n", raw)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return "\n".join(" ".join(ln.split()) for ln in text.splitlines())


def _unique_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        taken.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        cand = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        if cand not in taken:
            taken.add(cand)
            return cand
        n += 1


def _read_email(sub: Submission, data: bytes, limits: dict[str, Any]) -> None:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(data)  # type: ignore[assignment]
    sub.subject = str(msg.get("subject", "") or "") or None
    sub.sender = str(msg.get("from", "") or "") or None
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError):
            content = body.get_payload(decode=True).decode("utf-8", errors="replace")
        sub.body_text = html_to_text(content) if body.get_content_subtype() == "html" else content

    taken: set[str] = set()
    for i, part in enumerate(msg.iter_attachments(), start=1):
        name = _unique_name(part.get_filename() or f"attachment-{i}", taken)
        if part.get_content_type() == "message/rfc822":
            sub.skipped.append({"item": name, "reason": "embedded_email"})
            sub.reasons.append("embedded_email_not_supported")
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            sub.skipped.append({"item": name, "reason": "empty_attachment"})
            continue
        _add_item(sub, name, payload, f"attachment:{name}", limits, depth=0)


def _read_msg(sub: Submission, data: bytes, limits: dict[str, Any]) -> None:
    try:
        msg = read_msg(data)
    except MsgError as exc:
        raise ServiceError(422, "malformed_email", f"cannot read the .msg file: {exc}") from exc
    sub.subject, sub.sender, sub.body_text = msg.subject, msg.sender, msg.body_text
    taken: set[str] = set()
    for att in msg.attachments:
        name = _unique_name(att.name, taken)
        if att.embedded_message:
            sub.skipped.append({"item": name, "reason": "embedded_email"})
            sub.reasons.append("embedded_email_not_supported")
            continue
        if not att.data:
            reason = "attachment_by_reference" if att.method in (2, 3, 4, 7) else "empty_attachment"
            sub.skipped.append({"item": name, "reason": reason})
            continue
        _add_item(sub, name, att.data, f"attachment:{name}", limits, depth=0)


# ------------------------------------------------------------------ zip

def _read_zip(sub: Submission, zip_name: str, data: bytes, prefix_kind: str,
              limits: dict[str, Any], depth: int) -> None:
    max_members = int(limits["max_zip_members"])
    max_total = int(limits["max_zip_uncompressed_mb"]) * 1024 * 1024
    max_ratio = float(limits["max_compression_ratio"])
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        sub.skipped.append({"item": zip_name, "reason": "archive_corrupt"})
        sub.reasons.append(f"attachment_parse_failed:{zip_name}")
        return
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        claimed = sum(i.file_size for i in infos)
        if len(infos) > max_members or claimed > max_total:
            sub.skipped.append({"item": zip_name, "reason": "archive_limit_exceeded",
                                "members": len(infos), "uncompressed_bytes": claimed})
            sub.reasons.append(f"archive_limit_exceeded:{zip_name}")
            return
        total = 0
        taken: set[str] = set()
        for info in infos:
            raw_name = info.filename.replace("\\", "/")
            clean = posixpath.normpath(raw_name).lstrip("/")
            label = f"{zip_name}/{clean}"
            if clean.startswith("..") or "/../" in f"/{clean}/":
                sub.skipped.append({"item": label, "reason": "unsafe_path"})
                continue
            if any(s in raw_name for s in _SYSTEM_FILES):
                continue
            if info.flag_bits & 0x1:
                sub.skipped.append({"item": label, "reason": "archive_encrypted"})
                sub.reasons.append(f"archive_encrypted:{zip_name}")
                continue
            if info.compress_size and info.file_size / info.compress_size > max_ratio:
                sub.skipped.append({"item": label, "reason": "archive_limit_exceeded",
                                    "ratio": round(info.file_size / info.compress_size)})
                sub.reasons.append(f"archive_limit_exceeded:{zip_name}")
                continue
            # read with a hard cap: header sizes can lie
            with zf.open(info) as fh:
                member = fh.read(max_total - total + 1)
            total += len(member)
            if total > max_total:
                sub.skipped.append({"item": label, "reason": "archive_limit_exceeded"})
                sub.reasons.append(f"archive_limit_exceeded:{zip_name}")
                return
            clean = _unique_name(clean, taken)
            _add_item(sub, f"{zip_name}/{clean}", member, f"{prefix_kind}:{zip_name}/{clean}",
                      limits, depth=depth + 1)


# ------------------------------------------------------------------ items

def _add_item(sub: Submission, name: str, data: bytes, source_prefix: str,
              limits: dict[str, Any], depth: int) -> None:
    kind = detect_kind(data)
    if kind in READABLE:
        sub.items.append(Item(name=name, data=data, kind=kind, source_prefix=source_prefix))
    elif kind == "zip":
        # depth = zip levels already entered; max_zip_depth = nested levels allowed
        if depth > int(limits["max_zip_depth"]):
            sub.skipped.append({"item": name, "reason": "archive_depth_exceeded"})
            sub.reasons.append(f"archive_limit_exceeded:{name}")
            return
        prefix_kind = source_prefix.split(":", 1)[0]
        _read_zip(sub, name, data, prefix_kind, limits, depth)
    elif kind == "image":
        sub.skipped.append({"item": name, "reason": "image_not_supported"})
    elif kind in ("email", "msg"):
        sub.skipped.append({"item": name, "reason": "embedded_email"})
        sub.reasons.append("embedded_email_not_supported")
    else:
        sub.skipped.append({"item": name, "reason": f"unsupported_attachment:{kind}"})
        sub.reasons.append(f"unsupported_attachment:{kind}")


def open_submission(location: str, name: str, data: bytes, limits: dict[str, Any]) -> Submission:
    kind = detect_kind(data)
    sub = Submission(location=location, name=name, data_sha256=hashlib.sha256(data).hexdigest(),
                     size=len(data), kind=kind)
    if kind == "email":
        _read_email(sub, data, limits)
    elif kind == "msg":
        _read_msg(sub, data, limits)
    elif kind == "zip":
        _read_zip(sub, name, data, "file", limits, depth=0)
    elif kind in READABLE:
        sub.items.append(Item(name=name, data=data, kind=kind, source_prefix=f"file:{name}"))
    return sub
