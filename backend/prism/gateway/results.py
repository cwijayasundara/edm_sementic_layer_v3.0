"""In-memory result store: results live behind opaque handles so only compact summaries reach the LLM context.

A handle is readable only by the `sub` that created it; any other caller, an expired handle, an evicted one and a
made-up one all get the same "unknown handle" error (never "forbidden", which would confirm the handle exists).
Bounds: a TTL counted from creation, a per-sub handle cap and a per-sub byte quota (both make room by evicting that
sub's OWN least recently used results), and a global byte cap that is never met by evicting anyone: when the store is
full the put is refused (`result_store_full`), so one sub can never push another sub's results out. A result over the
column / row / cell / column-name caps or over the sub's quota is refused (`result_too_large`) by a bounded pre-pass
over the raw input BEFORE anything is converted or copied; a refused put evicts nothing. Bytes are a conservative
estimate of the Python objects held (sys.getsizeof for text, fixed per-object costs otherwise), not JSON length.
Thread-safe: combine() reads it from a worker thread.
"""
from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from prism.gateway.audit import valid_sub
from prism.gateway.errors import GatewayError
from prism.mcp.results import jsonable

SAMPLE_ROWS = 5
SAMPLE_CELL_CHARS = 200      # a sample cell, as JSON text, whatever its type
SUMMARY_COLUMNS = 50
SUMMARY_NAME_CHARS = 64      # column names, units and meta strings shown in a summary
SUMMARY_MAX_CHARS = 8192     # the whole summary as JSON
PAGE_DEFAULT = 50
PAGE_MAX = 200
HANDLE_HEX = 6  # bytes -> 12 hex characters
MAX_COLUMNS = 2048
MAX_ROWS = 50_000             # >= combine's 10,000-row output cap
MAX_CELL_CHARS = 10_000       # text in one cell, nested values included
MAX_COLUMN_CHARS = 256
MAX_DEPTH = 16                # nesting of list / dict cells
MAX_META_CHARS = 2_000_000   # units / lineage per column; bytes still count against the quota
# Conservative per-object costs (CPython, 64-bit) for the frozen copy: a scalar that is converted (Decimal -> float,
# date -> str, ...) becomes a new object; containers cost a header plus one pointer per item.
_SCALAR_COST = 100
_CONTAINER_COST = 72
_POINTER = 8
_ENTRY_COST = 1024


@dataclass(frozen=True)
class StoredResult:
    handle: str
    sub: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    meta: Mapping[str, Any]   # read-only: units {column: unit}, truncated, source, metric_id, ...
    nbytes: int
    created: float


def _unknown() -> GatewayError:
    return GatewayError("unknown_handle", "unknown handle")


def _sample_cell(value: Any) -> Any:
    if isinstance(value, str):
        return value[:SAMPLE_CELL_CHARS] + "..." if len(value) > SAMPLE_CELL_CHARS else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = json.dumps(value, separators=(",", ":"))   # lists / dicts: capped by serialised length
    return value if len(text) <= SAMPLE_CELL_CHARS else text[:SAMPLE_CELL_CHARS] + "..."


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > SUMMARY_NAME_CHARS:
        return value[:SUMMARY_NAME_CHARS] + "..."
    return value


def _size(obj: Any) -> int:
    """Default json.dumps form (spaces, ASCII escapes): never smaller than any other serialisation a caller uses."""
    return len(json.dumps(obj))


def _too_large(what: str) -> GatewayError:
    return GatewayError("result_too_large", f"the result is too large to keep ({what}); narrow the request")


def _cost(value: Any, max_chars: int) -> int:
    """Estimated bytes of the frozen copy of one cell (iterative, bounded: aborts past `max_chars` of content or
    MAX_DEPTH of nesting without touching the rest)."""
    total, chars, stack = 0, 0, [(value, 0)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, (str, bytes)):
            chars += len(v)
            total += sys.getsizeof(v) * (1 if isinstance(v, str) else 4)   # bytes are decoded to str
        elif isinstance(v, (list, tuple, set, frozenset, dict)):
            if depth >= MAX_DEPTH:
                raise _too_large("nesting too deep")
            chars += 2 + len(v)
            total += _CONTAINER_COST + _POINTER * len(v)
            if chars > max_chars:
                raise _too_large(f"a cell over {max_chars} characters")
            if isinstance(v, dict):
                total += _CONTAINER_COST * 2 + _POINTER * 2 * len(v)
                stack.extend((k, depth + 1) for k in v)
                stack.extend((x, depth + 1) for x in v.values())
            else:
                stack.extend((x, depth + 1) for x in v)
        else:
            chars += 8
            total += _SCALAR_COST if not isinstance(v, int) else max(_SCALAR_COST, sys.getsizeof(v))
        if chars > max_chars:
            raise _too_large(f"a cell over {max_chars} characters")
    return total


def _check_int(name: str, value: Any, lo: int, hi: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < lo or (hi is not None and value > hi):
        bound = f"{lo}..{hi}" if hi is not None else f">= {lo}"
        raise GatewayError("invalid_request", f"{name} must be an integer {bound}")
    return value


class ResultStore:
    def __init__(self, max_bytes: int = 128 * 2**20, ttl_s: float = 900, max_handles_per_sub: int = 50, *,
                 max_bytes_per_sub: int = 16 * 2**20, max_columns: int = MAX_COLUMNS, max_rows: int = MAX_ROWS,
                 max_cell_chars: int = MAX_CELL_CHARS, clock: Callable[[], float] = time.monotonic):
        self.max_bytes = max_bytes
        self.max_bytes_per_sub = min(max_bytes_per_sub, max_bytes)
        self.ttl_s = ttl_s
        self.max_handles_per_sub = max_handles_per_sub
        self.max_columns, self.max_rows, self.max_cell_chars = max_columns, max_rows, max_cell_chars
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, StoredResult] = OrderedDict()  # LRU order: oldest first
        self._bytes = 0
        self._sub_bytes: dict[str, int] = {}

    # ------------------------------------------------------------------------------------------------- write
    def put(self, sub: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
            meta: Mapping[str, Any] | None = None) -> str:
        if not valid_sub(sub):
            raise ValueError("sub must be a non-empty string of at most 256 characters")
        if isinstance(columns, str) or not columns or not all(isinstance(c, str) for c in columns):
            raise ValueError("columns must be a non-empty list of strings")
        nbytes = self._estimate(columns, rows, meta)          # bounded; raises before anything is copied
        frozen = tuple(tuple(jsonable(list(r))) for r in rows)
        clean_meta = jsonable(dict(meta or {}))
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            mine = [h for h, e in self._entries.items() if e.sub == sub]
            drop = mine[:max(0, len(mine) - self.max_handles_per_sub + 1)]
            freed = sum(self._entries[h].nbytes for h in drop)
            rest = iter(mine[len(drop):])
            while self._sub_bytes.get(sub, 0) - freed + nbytes > self.max_bytes_per_sub or \
                    self._bytes - freed + nbytes > self.max_bytes:
                h = next(rest, None)
                if h is None:   # only other subs' results stand in the way: refuse, evict nothing
                    raise GatewayError("result_store_full", "the result store is full; try again shortly")
                drop.append(h)
                freed += self._entries[h].nbytes
            for h in drop:
                self._drop(h)
            handle = self._new_handle()
            self._entries[handle] = StoredResult(handle, sub, tuple(columns), frozen, MappingProxyType(clean_meta),
                                                 nbytes, now)
            self._bytes += nbytes
            self._sub_bytes[sub] = self._sub_bytes.get(sub, 0) + nbytes
            return handle

    def _estimate(self, columns: Sequence[str], rows: Any, meta: Mapping[str, Any] | None) -> int:
        """Shape checks and a conservative byte estimate on the raw input, aborting as soon as a cap is passed."""
        width = len(columns)
        if width > self.max_columns:
            raise _too_large(f"over {self.max_columns} columns")
        if any(len(c) > MAX_COLUMN_CHARS for c in columns):
            raise _too_large(f"a column name over {MAX_COLUMN_CHARS} characters")
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ValueError("rows must be a list of rows")
        if len(rows) > self.max_rows:
            raise _too_large(f"over {self.max_rows} rows")
        limit = self.max_bytes_per_sub
        total = _ENTRY_COST + sum(sys.getsizeof(c) for c in columns) + _cost(dict(meta or {}), MAX_META_CHARS)
        row_cost = _CONTAINER_COST + _POINTER * width
        for r in rows:
            if isinstance(r, (str, bytes, Mapping)) or not isinstance(r, Sequence) or len(r) != width:
                raise ValueError("every row must have one value per column")
            total += row_cost + sum(_cost(v, self.max_cell_chars) for v in r)
            if total > limit:
                raise _too_large(f"over the {limit // 2**20} MB per-caller quota" if limit >= 2**20
                                 else f"over the {limit} byte per-caller quota")
        return total

    def _new_handle(self) -> str:
        while (handle := "r_" + secrets.token_hex(HANDLE_HEX)) in self._entries:
            pass
        return handle

    def _drop(self, handle: str) -> None:
        e = self._entries.pop(handle)
        self._bytes -= e.nbytes
        left = self._sub_bytes[e.sub] - e.nbytes
        if left:
            self._sub_bytes[e.sub] = left
        else:
            del self._sub_bytes[e.sub]

    def _purge_expired(self, now: float) -> None:
        for h in [h for h, e in self._entries.items() if now - e.created > self.ttl_s]:
            self._drop(h)

    # -------------------------------------------------------------------------------------------------- read
    def get(self, sub: Any, handle: Any) -> StoredResult:
        if not isinstance(handle, str) or not valid_sub(sub):
            raise _unknown()
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.sub != sub:
                raise _unknown()
            if self._clock() - entry.created > self.ttl_s:
                self._drop(handle)
                raise _unknown()
            self._entries.move_to_end(handle)
            return entry

    def summary(self, sub: Any, handle: Any) -> dict[str, Any]:
        """Compact description for the LLM, at most SUMMARY_MAX_CHARS of JSON whatever the result's shape: the first
        SUMMARY_COLUMNS columns (names capped; `columns_truncated` / `column_count` say so), row count, at most 5
        sample rows of those columns (every cell capped by its serialised length), and their units."""
        e = self.get(sub, handle)
        shown = min(len(e.columns), SUMMARY_COLUMNS)
        units = e.meta.get("units") or {}
        out = {"handle": e.handle, "columns": [_short(c) for c in e.columns[:shown]], "column_count": len(e.columns),
               "columns_truncated": shown < len(e.columns), "row_count": len(e.rows),
               "sample_rows": [[_sample_cell(v) for v in r[:shown]] for r in e.rows[:SAMPLE_ROWS]],
               "units": {_short(c): _short(units[c]) for c in e.columns[:shown]
                         if isinstance(units, Mapping) and c in units},
               "truncated": bool(e.meta.get("truncated", False))}
        for key in ("source", "metric_id", "partial"):
            if key in e.meta:
                out[key] = _short(e.meta[key])
        while _size(out) > SUMMARY_MAX_CHARS and out["sample_rows"]:
            out["sample_rows"].pop()
        if _size(out) > SUMMARY_MAX_CHARS:
            out["units"] = {}
        while _size(out) > SUMMARY_MAX_CHARS and len(out["columns"]) > 1:
            out["columns"].pop()
            out["columns_truncated"] = True
        return out

    def page(self, sub: Any, handle: Any, offset: Any = 0, limit: Any = PAGE_DEFAULT) -> dict[str, Any]:
        e = self.get(sub, handle)
        offset = _check_int("offset", offset, 0)
        limit = _check_int("limit", limit, 1, PAGE_MAX)
        return {"handle": e.handle, "columns": list(e.columns), "offset": offset, "row_count": len(e.rows),
                "rows": [list(r) for r in e.rows[offset:offset + limit]]}

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"handles": len(self._entries), "bytes": self._bytes}


__all__ = ["ResultStore", "StoredResult"]
