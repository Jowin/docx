"""Reader for Outlook .msg files.

A .msg file is an OLE compound file holding MAPI properties ([MS-OXMSG]):
  __substg1.0_<id><type>           one stream per variable-size property
  __properties_version1.0          fixed-size properties (integers, times)
  __attach_version1.0_#<n>         one storage per attachment
  __substg1.0_3701000D             an attached message, as a storage

Built on olefile (BSD) and compressed_rtf (MIT). The GPL-licensed extract-msg
package is deliberately not used, so the image carries no copyleft code.

What is read: subject, sender, body (plain text, else HTML, else RTF), and
each attachment's name and bytes. An attached message (Outlook "item"
attachment) is read recursively as its own message (``MsgAttachment.message``);
intake applies the depth limit.
"""
from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass, field

PT_UNICODE, PT_STRING8, PT_BINARY, PT_OBJECT = 0x001F, 0x001E, 0x0102, 0x000D

PR_SUBJECT = 0x0037
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_SENDER_SMTP = 0x5D01
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENT_REPRESENTING_SMTP = 0x5D02
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013
PR_INTERNET_MESSAGE_ID = 0x1035
PR_ATTACH_DATA = 0x3701
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
PR_DISPLAY_NAME = 0x3001
PR_ATTACH_METHOD = 0x3705
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD

ATTACH_BY_VALUE, ATTACH_EMBEDDED_MSG, ATTACH_OLE = 1, 5, 6


class MsgError(Exception):
    pass


@dataclass
class MsgAttachment:
    name: str
    data: bytes | None
    method: int | None
    embedded_message: bool = False
    #: The attached Outlook item itself, read the same way, when it is one.
    message: "MsgMessage | None" = None


@dataclass
class MsgMessage:
    subject: str | None = None
    sender: str | None = None
    message_id: str | None = None
    body_text: str | None = None
    attachments: list[MsgAttachment] = field(default_factory=list)


# ------------------------------------------------------------------ detection

def is_msg(data: bytes) -> bool:
    import olefile
    try:
        # always a file object: given bytes under 1,536 long, olefile treats them as a path
        if not olefile.isOleFile(io.BytesIO(data)):
            return False
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            names = {"/".join(e) for e in ole.listdir(streams=True, storages=False)}
    except Exception:
        return False
    return "__properties_version1.0" in names and any(n.startswith("__substg1.0_") for n in names)


# ------------------------------------------------------------------ reading

class _Props:
    """Properties of one object (the message, or one attachment) inside the file."""

    def __init__(self, ole, prefix: str, header_size: int) -> None:
        self.ole, self.prefix = ole, prefix
        self.fixed: dict[int, int] = {}
        path = f"{prefix}__properties_version1.0"
        if ole.exists(path):
            raw = ole.openstream(path).read()
            for off in range(header_size, len(raw) - 15, 16):
                tag, _flags, value = struct.unpack_from("<IIQ", raw, off)
                self.fixed[tag] = value
        cp = self.int(PR_INTERNET_CPID) or self.int(PR_MESSAGE_CODEPAGE)
        self.codepage = _codec(cp) if cp else "cp1252"

    def int(self, propid: int) -> int | None:
        v = self.fixed.get((propid << 16) | 0x0003)
        return None if v is None else v & 0xFFFFFFFF

    def raw(self, propid: int, ptype: int) -> bytes | None:
        path = f"{self.prefix}__substg1.0_{propid:04X}{ptype:04X}"
        return self.ole.openstream(path).read() if self.ole.exists(path) else None

    def text(self, propid: int) -> str | None:
        b = self.raw(propid, PT_UNICODE)
        if b is not None:
            return b.decode("utf-16-le", errors="replace").rstrip("\x00") or None
        b = self.raw(propid, PT_STRING8)
        if b is not None:
            return b.decode(self.codepage, errors="replace").rstrip("\x00") or None
        return None


def _codec(cp: int) -> str:
    names = {65001: "utf-8", 1200: "utf-16-le", 20127: "ascii", 28591: "latin-1", 50220: "iso2022_jp",
             51932: "euc_jp", 936: "gbk", 950: "big5", 949: "cp949", 932: "cp932"}
    name = names.get(cp, f"cp{cp}")
    try:
        "".encode(name)
        return name
    except LookupError:
        return "cp1252"


def read_msg(data: bytes) -> MsgMessage:
    import olefile
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as exc:
        raise MsgError(f"not a readable .msg file: {exc}") from exc
    with ole:
        try:
            return _read(ole)
        except MsgError:
            raise
        except Exception as exc:
            raise MsgError(f"cannot read .msg content: {exc}") from exc


PT_OBJECT_STORAGE = "__substg1.0_3701000D"


def _read(ole, prefix: str = "", header_size: int = 32, depth: int = 0) -> MsgMessage:
    """Read the message at ``prefix``; an attached Outlook item is read recursively.

    The top-level property stream has a 32-byte header, an embedded message's
    has 24, and an attachment's has 8 ([MS-OXMSG] 2.4).
    """
    top = _Props(ole, prefix, header_size=header_size)
    msg = MsgMessage()
    msg.subject = top.text(PR_SUBJECT)
    name = top.text(PR_SENDER_NAME) or top.text(PR_SENT_REPRESENTING_NAME)
    email = top.text(PR_SENDER_SMTP) or top.text(PR_SENT_REPRESENTING_SMTP)
    if not email:
        candidate = top.text(PR_SENDER_EMAIL) or ""
        email = candidate if "@" in candidate else None     # skip Exchange X.500 addresses
    msg.sender = f"{name} <{email}>" if name and email else (email or name)
    msg.message_id = top.text(PR_INTERNET_MESSAGE_ID)
    msg.body_text = _body(top)

    parts = [p for p in prefix.split("/") if p]
    storages = sorted({e[len(parts)] for e in ole.listdir(streams=False, storages=True)
                       if len(e) > len(parts) and list(e[:len(parts)]) == parts
                       and e[len(parts)].startswith("__attach_version1.0_#")})
    for storage in storages:
        path = f"{prefix}{storage}/"
        a = _Props(ole, path, header_size=8)
        a.codepage = top.codepage if a.codepage == "cp1252" else a.codepage
        name = (a.text(PR_ATTACH_LONG_FILENAME) or a.text(PR_ATTACH_FILENAME)
                or a.text(PR_DISPLAY_NAME) or storage.rsplit("#", 1)[-1])
        method = a.int(PR_ATTACH_METHOD)
        embedded = ole.exists(f"{path}{PT_OBJECT_STORAGE}")
        payload = None if embedded else a.raw(PR_ATTACH_DATA, PT_BINARY)
        inner = None
        if embedded and depth < 8:          # hard stop; the configured depth limit is applied by intake
            inner = _read(ole, f"{path}{PT_OBJECT_STORAGE}/", header_size=24, depth=depth + 1)
        msg.attachments.append(MsgAttachment(name=name, data=payload, method=method,
                                             embedded_message=embedded or method == ATTACH_EMBEDDED_MSG,
                                             message=inner))
    return msg


def _body(p: _Props) -> str | None:
    text = p.text(PR_BODY)
    if text and text.strip():
        return text.replace("\r\n", "\n")
    html = p.raw(PR_HTML, PT_BINARY)
    if html is None:
        h = p.text(PR_HTML)
        html = h.encode("utf-8") if h else None
    if html:
        from .intake import html_to_text
        return html_to_text(_decode_html(html, p.codepage))
    rtf = p.raw(PR_RTF_COMPRESSED, PT_BINARY)
    if rtf:
        import compressed_rtf
        try:
            return rtf_to_text(compressed_rtf.decompress(rtf))
        except Exception:
            return None
    return None


def _decode_html(raw: bytes, fallback: str) -> str:
    m = re.search(rb'charset\s*=\s*["\']?([A-Za-z0-9_\-]+)', raw[:2048], re.I)
    for enc in ((m.group(1).decode("ascii") if m else None), fallback, "utf-8"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ RTF

_RTF_TOKEN = re.compile(rb"\\([a-z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\([^a-z])|([{}])|[\r\n]+|([^\\{}\r\n]+)")
_SKIP_DESTINATIONS = {b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"header", b"footer",
                      b"listtable", b"listoverridetable", b"rsidtbl", b"generator", b"xmlnstbl",
                      b"themedata", b"colorschememapping", b"latentstyles", b"datastore", b"object"}


_BLOCK_TAG = re.compile(r"<\s*/?\s*(br|p|div|tr|li|h[1-6]|table)\b", re.I)


def rtf_to_text(rtf: bytes) -> str:
    """Plain text from RTF, including Outlook's HTML-in-RTF (\\fromhtml1) bodies.

    Skips ignorable destinations ({\\* ...}), font and colour tables, and the
    RTF-only runs Outlook marks with \\htmlrtf ... \\htmlrtf0. In HTML-in-RTF,
    the original tags sit in {\\*\\htmltag ...} groups; block tags such as
    </p> and <br> become line breaks.
    """
    out: list[str] = []
    stack: list[tuple[bool, bool, str, int]] = []
    skip, htmlrtf, codec, uc, pending_skip = False, False, "cp1252", 1, 0
    star_next = False
    tag_depth, tag_buf = 0, bytearray()
    for m in _RTF_TOKEN.finditer(rtf):
        word, arg, hexbyte, sym, brace, text = m.groups()
        if brace == b"{":
            stack.append((skip, htmlrtf, codec, uc))
            star_next = False
            continue
        if brace == b"}":
            if stack:
                skip, htmlrtf, codec, uc = stack.pop()
            if tag_depth and len(stack) < tag_depth:
                if _BLOCK_TAG.search(tag_buf.decode("latin-1")):
                    out.append("\n")
                tag_depth, tag_buf = 0, bytearray()
            continue
        if tag_depth:
            if text:
                tag_buf += text
            continue
        if pending_skip and (text or hexbyte):
            if text:
                n = min(pending_skip, len(text))
                text, pending_skip = text[n:], pending_skip - n
                if not text:
                    continue
            else:
                pending_skip -= 1
                continue
        if sym is not None:
            if sym == b"*":
                star_next = True
            elif not (skip or htmlrtf):
                out.append({b"~": "\u00a0", b"-": "", b"_": "-", b"\\": "\\", b"{": "{", b"}": "}"}
                           .get(sym, ""))
            continue
        if word is not None:
            if star_next and word.startswith(b"htmltag"):
                tag_depth, tag_buf, star_next = len(stack), bytearray(), False
                continue
            if star_next or word in _SKIP_DESTINATIONS:
                skip, star_next = True, False
                continue
            if word == b"htmlrtf":
                htmlrtf = arg != b"0"
                continue
            if word == b"ansicpg" and arg:
                codec = _codec(int(arg))
            elif word == b"uc" and arg:
                uc = int(arg)
            elif skip or htmlrtf:
                continue
            elif word in (b"par", b"line", b"row"):
                out.append("\n")
            elif word in (b"tab", b"cell"):
                out.append("\t")
            elif word == b"u" and arg:
                out.append(chr(int(arg) % 0x10000))
                pending_skip = uc
            continue
        if skip or htmlrtf:
            continue
        if hexbyte is not None:
            out.append(bytes([int(hexbyte, 16)]).decode(codec, errors="replace"))
        elif text:
            out.append(text.decode(codec, errors="replace"))
    lines = (" ".join(ln.split()) for ln in "".join(out).split("\n"))
    return "\n".join(ln for ln in lines if ln)
