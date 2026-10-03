"""Thread Decomposer: split an email body into segments, newest first (RT-12).

Segment 0 is what the sender wrote; segment 1 is the message they replied to
or forwarded, and so on. A segment starts at a reply or forward marker:

    -----Original Message-----          ----- Forwarded message -----
    Begin forwarded message:            On <date>, <someone> wrote:
    a From:/Sent:/To: header block      a long underscore rule (Outlook)
    a run of ">"-quoted lines

Only line numbers are produced, so locators stay ``body#L<n>`` and every
block can say which segment it came from. When no marker is found the whole
body is segment 0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_MARKERS = [
    re.compile(r"^\s*-{2,}\s*(original message|forwarded message)\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*begin forwarded message:?\s*$", re.I),
    re.compile(r"^\s*_{10,}\s*$"),
]
_ON_WROTE = re.compile(r"^\s*on\b.{4,200}\bwrote:\s*$", re.I)
_ON_START = re.compile(r"^\s*on\b.{4,200}$", re.I)
_HEADER = re.compile(r"^\s*\*?(from|sent|date|to|cc|subject)\s*:\*?\s", re.I)


@dataclass(frozen=True)
class Segment:
    index: int
    first_line: int          # 1-based, inclusive
    last_line: int


def _header_block(lines: list[str], i: int) -> bool:
    """A From: line followed within four lines by Sent:/Date: and To:/Subject:."""
    if not re.match(r"^\s*\*?from\s*:", lines[i], re.I):
        return False
    window = [ln for ln in lines[i + 1:i + 5]]
    names = {m.group(1).lower() for ln in window if (m := _HEADER.match(ln))}
    return bool(names & {"sent", "date"}) and bool(names & {"to", "subject"})


def split(body: str) -> list[Segment]:
    lines = body.splitlines()
    if not lines:
        return []
    starts = [0]
    content = False              # has the current segment any text beyond markers and headers?
    in_quote = False
    for i, ln in enumerate(lines):
        quoted = ln.lstrip().startswith(">")
        boundary = bool(any(rx.match(ln) for rx in _MARKERS) or _ON_WROTE.match(ln)
                        or (_ON_START.match(ln) and i + 1 < len(lines)
                            and lines[i + 1].strip().endswith("wrote:"))
                        or _header_block(lines, i)
                        or (quoted and not in_quote))
        if boundary and content:
            starts.append(i)
            content = False
        structural = boundary or bool(_HEADER.match(ln)) or ln.strip().endswith("wrote:")
        if ln.strip() and not structural:
            content = True
        in_quote = quoted if ln.strip() else in_quote
    segs = []
    for n, s in enumerate(starts):
        end = (starts[n + 1] if n + 1 < len(starts) else len(lines))
        segs.append(Segment(n, s + 1, end))
    return segs


def segment_of(segments: list[Segment], line: int) -> int:
    for s in segments:
        if s.first_line <= line <= s.last_line:
            return s.index
    return 0
