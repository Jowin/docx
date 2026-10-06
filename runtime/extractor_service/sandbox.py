"""Parse a document in a separate process, under a time and memory limit.

Every run opens files from outside senders, so parsing is contained (RT-65):
the parser runs in a child process with an address-space cap (Linux:
SANDBOX_MEMORY_MB on top of what the process already maps), and is
killed when it overruns its time budget (RT-39). A kill or crash comes back as
a failed document with a typed reason (``agent_timeout:parse:<name>``,
``attachment_parse_failed:<name>``), so one bad file degrades the run rather
than ending it (RT-10).

The child cannot reach the run's state; it gets one item and returns one
document. Network isolation needs OS support (a network namespace or a
container without egress) and is left to the deployment.

PARSE_SANDBOX=off parses in-process (for platforms without fork, or debugging).
"""
from __future__ import annotations

import multiprocessing as mp
import os
import traceback
from typing import Any

from .evidence import Doc, build_doc
from .intake import Item


def _child(conn, item: Item, ev: dict[str, Any], spool: Any, memory_mb: int) -> None:
    try:
        if memory_mb and hasattr(os, "fork"):
            import resource
            # headroom on top of what the forked process already maps, so the cap bounds the
            # parse itself however large the parent is
            limit = _mapped_bytes() + memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        conn.send(("ok", build_doc(item, ev, spool)))
    except MemoryError:
        conn.send(("error", "memory_limit"))
    except BaseException:                                      # noqa: BLE001 - reported to the parent
        conn.send(("error", traceback.format_exc(limit=3)[-500:]))
    finally:
        conn.close()


def _mapped_bytes() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _failed(item: Item, reason: str, note: str) -> Doc:
    name = "email body" if item.source_prefix == "body" else item.name
    return Doc("", item.source_prefix, name, item.kind, item.sha256, status="failed", reason=reason,
               notes=[note], order=item.order)


def parse(item: Item, ev: dict[str, Any], spool: Any = None, *, mode: str = "process",
          timeout_s: float = 120, memory_mb: int = 1024) -> Doc:
    if mode == "off":
        return build_doc(item, ev, spool)
    if item.data is None:
        # the parent reads the bytes: a database connection must not cross into the child
        from dataclasses import replace
        try:
            item = replace(item, data=item.read(spool))
        except Exception as exc:                               # noqa: BLE001 - an unreadable item is a flag
            return _failed(item, f"attachment_parse_failed:{item.name}", f"spool: {exc}"[:300])
        spool = None
    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(child, item, ev, spool, memory_mb), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout_s):
            proc.kill()
            proc.join(5)
            return _failed(item, f"agent_timeout:parse:{item.name}", f"parse timed out after {timeout_s}s")
        status, payload = parent.recv()
    except EOFError:
        status, payload = "error", f"parser process died (exit {proc.exitcode})"
    finally:
        parent.close()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
    if status == "ok":
        return payload
    return _failed(item, f"attachment_parse_failed:{item.name}", f"sandbox: {payload}"[:500])


class SandboxError(RuntimeError):
    """A sandboxed call timed out, ran out of memory or raised."""


def _call_child(conn, fn, args, memory_mb: int) -> None:
    try:
        if memory_mb and hasattr(os, "fork"):
            import resource
            limit = _mapped_bytes() + memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        conn.send(("ok", fn(*args)))
    except MemoryError:
        conn.send(("error", "memory_limit"))
    except BaseException:                                      # noqa: BLE001 - reported to the parent
        conn.send(("error", traceback.format_exc(limit=3)[-500:]))
    finally:
        conn.close()


def call(fn, *args, mode: str = "process", timeout_s: float = 120, memory_mb: int = 1024):
    """``fn(*args)`` in a child process under the same limits as ``parse``; raises SandboxError.

    Used by expand.py to read the rest of a large file. Arguments and the result cross a pipe,
    so they must pickle; nothing the child does reaches the parent's state.
    """
    if mode == "off":
        return fn(*args)
    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_call_child, args=(child, fn, args, memory_mb), daemon=True)
    proc.start()
    child.close()
    try:
        # a large result arrives in pieces; poll only for the first byte
        if not parent.poll(timeout_s):
            proc.kill()
            proc.join(5)
            raise SandboxError(f"timed out after {timeout_s:.0f}s")
        status, payload = parent.recv()
    except EOFError:
        status, payload = "error", f"process died (exit {proc.exitcode})"
    finally:
        parent.close()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
    if status != "ok":
        raise SandboxError(str(payload)[:500])
    return payload
