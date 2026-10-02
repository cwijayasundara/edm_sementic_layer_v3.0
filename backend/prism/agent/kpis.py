"""Dashboard KPI tiles: a fixed per-role metric list (kpis.yaml), each tile fetched through the gateway as the
caller and cached per role for a short TTL. A failing tile degrades alone and never carries an error message."""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from prism.agent.auth import UserContext
from prism.agent.gateway_client import GatewayError, GatewayPort

MAX_TILES = 4
_KEYS = {"metric_id", "label", "unit", "dimensions", "filters", "scale"}


@dataclass(frozen=True)
class KpiDef:
    metric_id: str
    label: str
    unit: str = ""
    dimensions: tuple[str, ...] = ()
    filters: tuple[tuple[str, object], ...] = ()   # passed to run_metric as {name: value}; sorted for hashing
    scale: float = 1   # multiplies the numeric value (a 0..1 fraction shown as a percentage uses 100)


def _scalar(v: object) -> bool:
    return isinstance(v, (str, int, float, bool))


def _scalar_or_list(v: object) -> bool:
    return _scalar(v) or (isinstance(v, list) and bool(v) and all(_scalar(x) for x in v))


def _parse(role: str, entry: object) -> KpiDef:
    if not isinstance(entry, dict) or not _KEYS >= entry.keys() >= {"metric_id", "label"}:
        raise ValueError(f"kpis.yaml: bad tile for {role!r}: {entry!r}")
    dims = entry.get("dimensions", [])
    if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
        raise ValueError(f"kpis.yaml: bad dimensions for {role!r}")
    if not all(isinstance(entry[k], str) for k in ("metric_id", "label")) or not isinstance(entry.get("unit", ""), str):
        raise ValueError(f"kpis.yaml: bad tile for {role!r}: {entry!r}")
    filters = entry.get("filters", {})
    if not isinstance(filters, dict) or not all(isinstance(k, str) and _scalar_or_list(v) for k, v in filters.items()):
        raise ValueError(f"kpis.yaml: bad filters for {role!r}")
    scale = entry.get("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale <= 0:
        raise ValueError(f"kpis.yaml: bad scale for {role!r}")
    return KpiDef(entry["metric_id"], entry["label"], entry.get("unit", ""), tuple(dims),
                  tuple(sorted(filters.items())), scale)


def load_kpis(path: Path) -> dict[str, list[KpiDef]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("kpis.yaml must map a role to a list of tiles")
    out: dict[str, list[KpiDef]] = {}
    for role, entries in raw.items():
        if not isinstance(entries, list) or len(entries) > MAX_TILES:
            raise ValueError(f"kpis.yaml: {role!r} needs a list of at most {MAX_TILES} tiles")
        out[str(role)] = [_parse(str(role), e) for e in entries]
    return out


KPIS: dict[str, list[KpiDef]] = load_kpis(Path(__file__).with_name("kpis.yaml"))


class KpiService:
    def __init__(self, ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._clock = ttl_s, clock
        self._cache: dict[tuple[tuple[str, ...], str], tuple[float, list[dict]]] = {}

    async def tiles(self, user: UserContext, gateway: GatewayPort) -> list[dict]:
        if (hit := self.cached(user)) is not None:
            return hit
        key = self._key(user)
        defs = next((KPIS[r] for r in key[0] if r in KPIS), [])
        tiles = list(await asyncio.gather(*(self._tile(d, gateway) for d in defs)))
        if not tiles or any(t["status"] == "ok" for t in tiles):   # never pin a total outage for the TTL
            self._cache[key] = (self._clock(), tiles)
        return tiles

    @staticmethod
    def _key(user: UserContext) -> tuple[tuple[str, ...], str]:
        """Roles pick the tile list; the scope digest (scopes, row grants, metrics_only) is what the gateway
        enforces, so two callers share an entry only when they would get identical rows."""
        return tuple(sorted(user.roles)), user.scope_digest

    def cached(self, user: UserContext) -> list[dict] | None:
        hit = self._cache.get(self._key(user))
        return hit[1] if hit and self._clock() - hit[0] < self._ttl else None

    @staticmethod
    async def _tile(d: KpiDef, gateway: GatewayPort) -> dict:
        tile = {"label": d.label, "metric_id": d.metric_id, "unit": d.unit, "status": "unavailable"}
        try:
            args = {"metric_id": d.metric_id, "dimensions": list(d.dimensions), "limit": 1}
            if d.filters:
                args["filters"] = dict(d.filters)
            h = await gateway.call("run_metric", args)
            rows = await gateway.call("get_rows", {"handle": h["handle"], "offset": 0, "limit": 1})
            value = rows["rows"][0][-1]
            if d.scale != 1:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError("scaled tile needs a number")
                value = round(value * d.scale, 4)
            tile["value"] = value
            tile["status"] = "ok"
            if isinstance(source := h.get("summary", {}).get("source"), str):
                tile["source"] = source
        except (GatewayError, KeyError, IndexError, TypeError):
            tile.pop("value", None)
            tile.pop("source", None)
        return tile
