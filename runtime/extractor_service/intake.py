"""Intake: a file location -> one submission made of readable items.

The input file can be an email (.eml or Outlook .msg), a zip, or a single CSV,
Excel, PDF or DOCX file. The kind is decided from the bytes, never the
extension (RT-14). Emails and zips are unpacked into items:

* every item is first checked by the ingestion filter (ingest_filter.py); an
  ignored item is recorded in ``skipped`` with its rule and never parsed;
* an attached email (.eml, .msg, or an Outlook item) is read recursively as an
  ``embedded:<n>:<name>`` source, its body becoming an item of its own; the
  depth is checked *before* recursing (RT-18, RT-36);
* an encrypted PDF, Office file or zip member is opened with keys found in the
  email it came with (crypto.py); the key's location is recorded, never the key;
* images are read by OCR only when the config turns ``intake.ocr_images`` on.

Anything that cannot be read is recorded in ``skipped`` with a reason and, when
it matters, a flag in ``reasons``, so nothing disappears silently.

Item bytes go to the run's spool (spool.py) when there is one, so the run state
(and its checkpoints) holds references, not attachment bytes (RT-34).
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

from . import crypto, threads
from .errors import ServiceError
from .ingest_filter import IngestionFilter, item_facts
from .msg import MsgError, MsgMessage, is_msg, read_msg

READABLE = ("csv", "excel", "pdf", "docx")
_SYSTEM_FILES = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")
DEFAULT_EMAIL_DEPTH = 3


@dataclass
class Item:
    name: str            # display path inside the submission, e.g. "bundle.zip/inv.xlsx"
    kind: str            # csv | excel | pdf | docx | image | email_body
    source_prefix: str   # "file:inv.csv", "attachment:inv.xlsx", "body", "embedded:1:fwd.eml"
    sha256: str
    size: int
    order: int = 0
    ref: str | None = None          # spool reference, when bytes were spooled
    data: bytes | None = None       # bytes held in memory, when there is no spool
    meta: dict[str, Any] = field(default_factory=dict)

    def read(self, spool: Any = None) -> bytes:
        if self.data is not None:
            return self.data
        if self.ref is None or spool is None:
            raise ServiceError(500, "item_unavailable", f"no bytes for {self.name}")
        return spool.get(self.ref)


@dataclass
class Submission:
    location: str
    name: str
    data_sha256: str
    size: int
    kind: str
    subject: str | None = None
    sender: str | None = None
    message_id: str | None = None
    body_text: str | None = None
    items: list[Item] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: The rule that ignored the input file itself, when one did.
    ignored: str | None = None
    #: Where each decryption key was found: {item name: locator}.
    keys_used: dict[str, str] = field(default_factory=dict)


@dataclass
class IntakeContext:
    limits: dict[str, Any]
    filter: IngestionFilter | None = None
    client: str = ""
    usecase: str = ""
    spool: Any = None
    embedded_count: int = 0

    @property
    def max_email_depth(self) -> int:
        return int(self.limits.get("max_email_depth", DEFAULT_EMAIL_DEPTH))

    @property
    def ocr_images(self) -> bool:
        return bool(self.limits.get("ocr_images", False))


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
            return "ole2"          # .doc, an encrypted workbook or document, ...
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


# ------------------------------------------------------------------ helpers

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


def _body_lines_newest_first(body: str | None, locator_prefix: str = "body#") -> list[tuple[str, str]]:
    """(locator, line) pairs for key search, segment 0 first."""
    if not body:
        return []
    lines = body.splitlines()
    segs = threads.split(body)
    out = []
    for seg in segs:
        for n in range(seg.first_line, seg.last_line + 1):
            if lines[n - 1].strip():
                out.append((f"{locator_prefix}L{n}", lines[n - 1]))
    return out


@dataclass
class _Email:
    """The parts of one email (top level or attached) that intake needs."""
    subject: str | None
    sender: str | None
    message_id: str | None
    body: str | None
    attachments: list[tuple[str, bytes | None, str]]     # (name, data, how) how: data|email|msg|ref|empty
    nested: dict[str, Any] = field(default_factory=dict)   # name -> EmailMessage | MsgMessage


def _parse_eml(data: bytes) -> _Email:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(data)  # type: ignore[assignment]
    return _from_message(msg)


def _from_message(msg: EmailMessage) -> _Email:
    body_text = None
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError):
            content = body.get_payload(decode=True).decode("utf-8", errors="replace")
        body_text = html_to_text(content) if body.get_content_subtype() == "html" else content
    out = _Email(str(msg.get("subject", "") or "") or None, str(msg.get("from", "") or "") or None,
                 str(msg.get("message-id", "") or "") or None, body_text, [])
    taken: set[str] = set()
    for i, part in enumerate(msg.iter_attachments(), start=1):
        name = _unique_name(part.get_filename() or f"attachment-{i}", taken)
        if part.get_content_type() == "message/rfc822":
            inner = part.get_content()
            if isinstance(inner, EmailMessage):
                if not part.get_filename():
                    name = _unique_name(f"{(inner.get('subject') or 'message')[:60]}.eml", taken)
                out.attachments.append((name, None, "email"))
                out.nested[name] = inner
            continue
        payload = part.get_payload(decode=True)
        out.attachments.append((name, payload or None, "data" if payload else "empty"))
    return out


def _from_msg(msg: MsgMessage) -> _Email:
    out = _Email(msg.subject, msg.sender, msg.message_id, msg.body_text, [])
    taken: set[str] = set()
    for att in msg.attachments:
        name = _unique_name(att.name, taken)
        if att.embedded_message and att.message is not None:
            out.attachments.append((name, None, "msg"))
            out.nested[name] = att.message
        elif not att.data:
            how = "ref" if att.method in (2, 3, 4, 7) else "empty"
            out.attachments.append((name, None, how))
        else:
            out.attachments.append((name, att.data, "data"))
    return out


# ------------------------------------------------------------------ the reader

class _Reader:
    def __init__(self, sub: Submission, ctx: IntakeContext) -> None:
        self.sub, self.ctx = sub, ctx
        self.order = 0

    # -------------------------------------------------------------- items

    def _store(self, name: str, data: bytes, kind: str, prefix: str, meta: dict[str, Any] | None = None) -> None:
        self.order += 1
        item = Item(name=name, kind=kind, source_prefix=prefix, sha256=hashlib.sha256(data).hexdigest(),
                    size=len(data), order=self.order, meta=dict(meta or {}))
        if self.ctx.spool is not None:
            item.ref = self.ctx.spool.put(data)
        else:
            item.data = data
        self.sub.items.append(item)

    def _skip(self, name: str, reason: str, flag: str | None = None, **extra: Any) -> None:
        self.sub.skipped.append({"item": name, "reason": reason, **extra})
        if flag:
            self.sub.reasons.append(flag)

    def _filtered(self, name: str, data: bytes, kind: str, container: str, depth: int,
                  sender: str | None, subject: str | None) -> bool:
        f = self.ctx.filter
        if f is None or not f.active:
            return False
        verdict = f.check(item_facts(name, name, data, kind, container, depth, sender, subject,
                                     self.ctx.client, self.ctx.usecase))
        if verdict.error:
            self.sub.reasons.append("ingestion_rule_error")
            self.sub.skipped.append({"item": name, "reason": "ingestion_rule_error", "detail": verdict.error,
                                     "kept": True})
        if verdict.ignore:
            self._skip(name, "ignored_by_filter", rule=verdict.rule)
            return True
        return False

    def add(self, name: str, data: bytes, prefix: str, *, container: str, depth: int, zip_depth: int,
            email: _Email | None) -> None:
        kind = detect_kind(data)
        sender = email.sender if email else self.sub.sender
        subject = email.subject if email else self.sub.subject
        if self._filtered(name, data, kind, container, depth, sender, subject):
            return
        if kind in ("pdf", "ole2", "docx") and crypto.is_encrypted(data, kind):
            keys = crypto.find_keys(subject, _body_lines_newest_first(email.body if email else self.sub.body_text,
                                                                      _body_prefix(prefix, email)))
            opened = crypto.try_keys(data, "pdf" if kind == "pdf" else "office", keys)
            if opened is None:
                self._skip(name, "encrypted_no_key", "encrypted_no_key", candidates_tried=len(keys))
                return
            data, cand = opened
            self.sub.keys_used[name] = cand.locator
            kind = detect_kind(data)
        if kind in READABLE:
            self._store(name, data, kind, prefix)
        elif kind == "zip":
            if zip_depth > int(self.ctx.limits["max_zip_depth"]):
                self._skip(name, "archive_depth_exceeded", f"archive_limit_exceeded:{name}")
                return
            self.read_zip(name, data, prefix, depth=depth, zip_depth=zip_depth, email=email)
        elif kind in ("email", "msg"):
            self.embedded(name, data, kind, prefix, depth)
        elif kind == "image":
            if self.ctx.ocr_images:
                self._store(name, data, "image", prefix)
            else:
                self._skip(name, "image_not_supported")
        else:
            self._skip(name, f"unsupported_attachment:{kind}", f"unsupported_attachment:{kind}")

    # -------------------------------------------------------------- emails

    def email(self, mail: _Email, body_prefix: str, attach_prefix: str, depth: int, label: str) -> None:
        """The body as an item, then every attachment (recursively)."""
        body = (mail.body or "").strip("﻿")
        if mail.subject or body.strip():
            text = (mail.body or "")
            self._store(label, text.encode("utf-8"), "email_body", body_prefix,
                        {"subject": mail.subject, "sender": mail.sender, "depth": depth})
        for name, data, how in mail.attachments:
            path = f"{label}/{name}" if depth else name
            prefix = f"{attach_prefix}{name}"
            if how in ("email", "msg"):
                nested = mail.nested[name]
                self.nested_email(path, nested, how, depth + 1, prefix_parent=attach_prefix)
            elif how == "ref":
                self._skip(path, "attachment_by_reference")
            elif how == "empty" or not data:
                self._skip(path, "empty_attachment")
            else:
                self.add(path, data, prefix, container="email" if depth == 0 else "embedded",
                         depth=depth, zip_depth=0, email=mail)

    def nested_email(self, name: str, obj: Any, how: str, depth: int, prefix_parent: str) -> None:
        """An email attached as a message part (.eml rfc822 part, or an Outlook item)."""
        if depth > self.ctx.max_email_depth:                 # checked before recursing (RT-36)
            self._skip(name, "embedded_depth_exceeded", "embedded_depth_exceeded", depth=depth)
            return
        if isinstance(obj, EmailMessage):
            raw, sender, subject = obj.as_bytes(), str(obj.get("from") or ""), str(obj.get("subject") or "")
        else:
            raw, sender, subject = (obj.body_text or "").encode(), obj.sender or "", obj.subject or ""
        if self._filtered(name, raw, "email" if how == "email" else "msg", "embedded", depth, sender, subject):
            return
        mail = _from_message(obj) if isinstance(obj, EmailMessage) else _from_msg(obj)
        self.ctx.embedded_count += 1
        n = self.ctx.embedded_count
        base = name.rsplit("/", 1)[-1]
        self.email(mail, f"embedded:{n}:{base}", f"embedded:{n}:{base}/", depth, name)

    def embedded(self, name: str, data: bytes, kind: str, prefix: str, depth: int) -> None:
        """An attached .eml or .msg file."""
        if depth + 1 > self.ctx.max_email_depth:
            self._skip(name, "embedded_depth_exceeded", "embedded_depth_exceeded", depth=depth + 1)
            return
        try:
            mail = _parse_eml(data) if kind == "email" else _from_msg(read_msg(data))
        except (MsgError, Exception) as exc:                  # noqa: BLE001 - any parse failure is a flag
            self._skip(name, "malformed_email", f"attachment_parse_failed:{name}", detail=str(exc)[:200])
            return
        self.ctx.embedded_count += 1
        n = self.ctx.embedded_count
        base = name.rsplit("/", 1)[-1]
        self.email(mail, f"embedded:{n}:{base}", f"embedded:{n}:{base}/", depth + 1, name)

    # -------------------------------------------------------------- zip

    def read_zip(self, zip_name: str, data: bytes, prefix: str, *, depth: int, zip_depth: int,
                 email: _Email | None) -> None:
        limits = self.ctx.limits
        max_members = int(limits["max_zip_members"])
        max_total = int(limits["max_zip_uncompressed_mb"]) * 1024 * 1024
        max_ratio = float(limits["max_compression_ratio"])
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            self._skip(zip_name, "archive_corrupt", f"attachment_parse_failed:{zip_name}")
            return
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            claimed = sum(i.file_size for i in infos)
            if len(infos) > max_members or claimed > max_total:
                self._skip(zip_name, "archive_limit_exceeded", f"archive_limit_exceeded:{zip_name}",
                           members=len(infos), uncompressed_bytes=claimed)
                return
            keys: list[crypto.KeyCandidate] | None = None
            total = 0
            taken: set[str] = set()
            for info in infos:
                raw_name = info.filename.replace("\\", "/")
                clean = posixpath.normpath(raw_name).lstrip("/")
                label = f"{zip_name}/{clean}"
                if clean.startswith("..") or "/../" in f"/{clean}/":
                    self._skip(label, "unsafe_path")
                    continue
                if any(s in raw_name for s in _SYSTEM_FILES):
                    continue
                if info.compress_size and info.file_size / info.compress_size > max_ratio:
                    self._skip(label, "archive_limit_exceeded", f"archive_limit_exceeded:{zip_name}",
                               ratio=round(info.file_size / info.compress_size))
                    continue
                pwd_from = None
                if info.flag_bits & 0x1:
                    if keys is None:
                        keys = crypto.find_keys(email.subject if email else self.sub.subject,
                                                _body_lines_newest_first(
                                                    email.body if email else self.sub.body_text,
                                                    _body_prefix(prefix, email)))
                    member = None
                    for cand in keys:
                        try:
                            with zf.open(info, pwd=cand.key.encode()) as fh:
                                member = fh.read(max_total - total + 1)
                            pwd_from = cand.locator
                            break
                        except (RuntimeError, NotImplementedError, zipfile.BadZipFile, ValueError):
                            continue
                    if member is None:
                        self._skip(label, "encrypted_no_key", "encrypted_no_key")
                        continue
                else:
                    with zf.open(info) as fh:        # read with a hard cap: header sizes can lie
                        member = fh.read(max_total - total + 1)
                total += len(member)
                if total > max_total:
                    self._skip(label, "archive_limit_exceeded", f"archive_limit_exceeded:{zip_name}")
                    return
                clean = _unique_name(clean, taken)
                if pwd_from:
                    self.sub.keys_used[f"{zip_name}/{clean}"] = pwd_from
                self.add(f"{zip_name}/{clean}", member, f"{prefix}/{clean}", container="zip", depth=depth,
                         zip_depth=zip_depth + 1, email=email)


def _body_prefix(prefix: str, email: _Email | None) -> str:
    """Locator prefix of the body an item's keys are searched in: "body#" or "embedded:<n>:<name>#"."""
    if prefix.startswith("embedded:"):
        head = prefix.split("/", 1)[0]
        return f"{head}#"
    return "body#"


def open_submission(location: str, name: str, data: bytes, ctx_or_limits: IntakeContext | dict[str, Any]
                    ) -> Submission:
    ctx = ctx_or_limits if isinstance(ctx_or_limits, IntakeContext) else IntakeContext(limits=ctx_or_limits)
    kind = detect_kind(data)
    sub = Submission(location=location, name=name, data_sha256=hashlib.sha256(data).hexdigest(),
                     size=len(data), kind=kind)
    reader = _Reader(sub, ctx)
    if ctx.filter is not None and ctx.filter.active:
        verdict = ctx.filter.check(item_facts(name, name, data, kind, "file", 0, None, None,
                                              ctx.client, ctx.usecase))
        if verdict.ignore:
            sub.ignored = verdict.rule
            sub.skipped.append({"item": name, "reason": "ignored_by_filter", "rule": verdict.rule})
            return sub
    if kind in ("email", "msg"):
        try:
            mail = _parse_eml(data) if kind == "email" else _from_msg(read_msg(data))
        except MsgError as exc:
            raise ServiceError(422, "malformed_email", f"cannot read the .msg file: {exc}") from exc
        sub.subject, sub.sender, sub.message_id, sub.body_text = mail.subject, mail.sender, mail.message_id, mail.body
        reader.email(mail, "body", "attachment:", 0, "email body")
    elif kind == "zip":
        reader.read_zip(name, data, f"file:{name}", depth=0, zip_depth=0, email=None)
    elif kind in ("pdf", "ole2") and crypto.is_encrypted(data, kind):
        sub.skipped.append({"item": name, "reason": "encrypted_no_key"})
        sub.reasons.append("encrypted_no_key")
    elif kind in READABLE:
        reader._store(name, data, kind, f"file:{name}")
    elif kind == "image" and ctx.ocr_images:
        reader._store(name, data, "image", f"file:{name}")
    return sub
