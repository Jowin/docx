"""Test-only writer for Outlook .msg files (OLE compound file, version 3).

Python has no permissively licensed .msg writer, so the tests build their own
fixtures. This writes the structures Outlook uses: property streams named
``__substg1.0_<tag>``, a ``__properties_version1.0`` stream of fixed-size
properties, one ``__attach_version1.0_#<n>`` storage per attachment, and
embedded messages as ``__substg1.0_3701000D`` storages. The container follows
[MS-CFB]: 512-byte sectors, a 64-byte-sector mini stream for small streams.
"""
from __future__ import annotations

import struct
from typing import Union

Tree = dict[str, Union[bytes, "Tree"]]

SECTOR, MINI, CUTOFF = 512, 64, 4096
ENDOFCHAIN, FREESECT, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def _sort_key(name: str) -> tuple[int, str]:
    return (len(name), name.upper())


def build_cfb(tree: Tree) -> bytes:
    # 1. flatten into directory entries (0 = root)
    entries: list[dict] = [{"name": "Root Entry", "type": 5, "children": [], "data": b""}]

    def add(parent: int, subtree: Tree) -> None:
        for name in sorted(subtree, key=_sort_key):
            value = subtree[name]
            idx = len(entries)
            if isinstance(value, dict):
                entries.append({"name": name, "type": 1, "children": [], "data": b""})
                entries[parent]["children"].append(idx)
                add(idx, value)
            else:
                entries.append({"name": name, "type": 2, "children": [], "data": value})
                entries[parent]["children"].append(idx)

    add(0, tree)

    # 2. small streams into the mini stream
    mini_data = bytearray()
    minifat: list[int] = []
    for e in entries:
        if e["type"] == 2 and len(e["data"]) < CUTOFF:
            if not e["data"]:
                e["start"] = ENDOFCHAIN
                continue
            n = -(-len(e["data"]) // MINI)
            start = len(minifat)
            minifat.extend(range(start + 1, start + n))
            minifat.append(ENDOFCHAIN)
            mini_data += e["data"] + b"\0" * (n * MINI - len(e["data"]))
            e["start"] = start

    # 3. regular sectors: large streams, mini stream, mini FAT, directory, FAT
    sectors: list[bytes] = []
    fat: list[int] = []

    def place(data: bytes) -> int:
        if not data:
            return ENDOFCHAIN
        n = -(-len(data) // SECTOR)
        start = len(sectors)
        for i in range(n):
            sectors.append(data[i * SECTOR:(i + 1) * SECTOR].ljust(SECTOR, b"\0"))
            fat.append(start + i + 1 if i < n - 1 else ENDOFCHAIN)
        return start

    for e in entries:
        if e["type"] == 2 and len(e["data"]) >= CUTOFF:
            e["start"] = place(e["data"])
    root_start = place(bytes(mini_data))
    minifat_bytes = b"".join(struct.pack("<I", x) for x in minifat)
    if minifat:
        minifat_bytes += struct.pack("<I", FREESECT) * ((-len(minifat)) % (SECTOR // 4))
    first_minifat = place(minifat_bytes) if minifat else ENDOFCHAIN
    n_minifat = len(minifat_bytes) // SECTOR

    # siblings: a right-leaning chain in sorted order (valid, if unbalanced)
    for e in entries:
        kids = e["children"]
        e["child"] = kids[0] if kids else NOSTREAM
        for a, b in zip(kids, kids[1:] + [None]):
            entries[a]["right"] = b if b is not None else NOSTREAM

    dir_bytes = bytearray()
    for i, e in enumerate(entries):
        name = e["name"].encode("utf-16-le") + b"\0\0"
        size = len(mini_data) if i == 0 else len(e["data"])
        start = root_start if i == 0 else e.get("start", 0) if e["type"] == 2 else 0
        dir_bytes += struct.pack("<64sHBBIII16sIQQIQ", name, len(name), e["type"], 1,
                                 NOSTREAM, e.get("right", NOSTREAM), e["child"],
                                 b"\0" * 16, 0, 0, 0, start, size)
    while len(dir_bytes) % SECTOR:
        dir_bytes += struct.pack("<64sHBBIII16sIQQIQ", b"", 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM,
                                 b"\0" * 16, 0, 0, 0, 0, 0)
    first_dir = place(bytes(dir_bytes))

    # 4. FAT sectors (grow until they cover themselves)
    n_fat = 1
    while (len(sectors) + n_fat) > n_fat * (SECTOR // 4):
        n_fat += 1
    if n_fat > 109:
        raise ValueError("fixture too large for a header-only DIFAT")
    fat_ids = list(range(len(sectors), len(sectors) + n_fat))
    fat.extend([FATSECT] * n_fat)
    fat.extend([FREESECT] * (n_fat * (SECTOR // 4) - len(fat)))
    fat_bytes = b"".join(struct.pack("<I", x) for x in fat)
    for i in range(n_fat):
        sectors.append(fat_bytes[i * SECTOR:(i + 1) * SECTOR])

    difat = fat_ids + [FREESECT] * (109 - n_fat)
    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\0" * 16,
                         0x3E, 3, 0xFFFE, 9, 6, b"\0" * 6, 0, n_fat, first_dir, 0, CUTOFF,
                         first_minifat, n_minifat, ENDOFCHAIN, 0)
    header += b"".join(struct.pack("<I", x) for x in difat)
    return header + b"".join(sectors)


# ------------------------------------------------------------------ MSG layout

def _u(text: str) -> bytes:
    return text.encode("utf-16-le")


def _props(header: bytes, fixed: dict[int, int]) -> bytes:
    """__properties_version1.0: header, then 16-byte entries for fixed-size properties."""
    out = bytearray(header)
    for tag, value in fixed.items():
        out += struct.pack("<IIQ", tag, 0x6, value)          # flags: readable | writable
    return bytes(out)


def build_msg(subject: str | None = None, sender_name: str | None = None,
              sender_email: str | None = None, body: str | None = None,
              html: bytes | None = None, rtf_compressed: bytes | None = None,
              attachments: list[dict] | None = None, ansi_codepage: int | None = None,
              embedded: bool = False) -> bytes | Tree:
    """attachments: [{"name": str, "data": bytes}] or [{"name": str, "msg": <tree from build_msg(embedded=True)>}].
    ansi_codepage writes string properties as 8-bit (type 001E) in that code page."""
    attachments = attachments or []
    tree: Tree = {}

    def string(propid: int, text: str | None) -> None:
        if text is None:
            return
        if ansi_codepage:
            tree[f"__substg1.0_{propid:04X}001E"] = text.encode(f"cp{ansi_codepage}")
        else:
            tree[f"__substg1.0_{propid:04X}001F"] = _u(text)

    string(0x0037, subject)
    string(0x0C1A, sender_name)
    string(0x5D01, sender_email)
    string(0x1000, body)
    if html is not None:
        tree["__substg1.0_10130102"] = html
    if rtf_compressed is not None:
        tree["__substg1.0_10090102"] = rtf_compressed
    fixed = {0x3FFD0003: ansi_codepage} if ansi_codepage else {}
    for i, att in enumerate(attachments):
        a: Tree = {"__substg1.0_3707001F": _u(att["name"]), "__substg1.0_3704001F": _u(att["name"][:12])}
        if "msg" in att:
            a["__substg1.0_3701000D"] = att["msg"]
            method = 5
        else:
            a["__substg1.0_37010102"] = att["data"]
            method = 1
        a["__properties_version1.0"] = _props(b"\0" * 8, {0x37050003: method})
        tree[f"__attach_version1.0_#{i:08X}"] = a
    count = len(attachments)
    header = (struct.pack("<8sIIII8s", b"\0" * 8, 0, count, 0, count, b"\0" * 8) if not embedded
              else struct.pack("<8sIIII", b"\0" * 8, 0, count, 0, count))
    tree["__properties_version1.0"] = _props(header, fixed)
    if not embedded:
        tree["__nameid_version1.0"] = {"__substg1.0_00020102": b"", "__substg1.0_00030102": b"",
                                       "__substg1.0_00040102": b""}
        return build_cfb(tree)
    return tree
