"""Saved dashboards: a caller's pinned widgets with the recipes that produced them, re-run later with the current
caller's token. Rows are always scoped by `sub`; another caller's dashboard is indistinguishable from a missing one."""
import json
import time
import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prism.agent.recipes import parse_recipe
from prism.agent.spec import Widget

MAX_DASHBOARDS = 20
MAX_ITEMS = 8


class LimitReached(Exception):
    pass


class SaveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    widget: Widget
    recipe: dict

    @field_validator("recipe")
    @classmethod
    def _recipe(cls, v: dict) -> dict:
        return parse_recipe(v)

    @field_validator("widget")
    @classmethod
    def _no_handle(cls, w: Widget) -> Widget:
        return w.model_copy(update={"handle": ""})   # handles expire; the recipe is what is saved


class SaveDashboard(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)
    items: list[SaveItem] = Field(min_length=1, max_length=MAX_ITEMS)

    def items_json(self) -> list[dict]:
        return [{"widget": i.widget.model_dump(), "recipe": i.recipe} for i in self.items]


class DashboardStore(Protocol):
    async def list(self, sub: str) -> list[dict]: ...
    async def create(self, sub: str, body: SaveDashboard) -> str: ...
    async def get(self, sub: str, dashboard_id: str) -> dict | None: ...
    async def delete(self, sub: str, dashboard_id: str) -> bool: ...


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


class MemoryDashboardStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._seq = 0

    async def list(self, sub: str) -> list[dict]:
        mine = sorted((r for r in self._rows.values() if r["sub"] == sub), key=lambda r: r["seq"], reverse=True)
        return [{"id": r["id"], "title": r["title"], "created_at": r["created_at"],
                 "widget_count": len(r["items"])} for r in mine]

    async def create(self, sub: str, body: SaveDashboard) -> str:
        if sum(r["sub"] == sub for r in self._rows.values()) >= MAX_DASHBOARDS:
            raise LimitReached()
        new_id = str(uuid.uuid4())
        self._seq += 1   # monotonic: deletes never make two rows share a position
        self._rows[new_id] = {"id": new_id, "sub": sub, "title": body.title, "items": body.items_json(),
                              "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "seq": self._seq}
        return new_id

    async def get(self, sub: str, dashboard_id: str) -> dict | None:
        r = self._rows.get(_uuid(dashboard_id) or "")
        return {"id": r["id"], "title": r["title"], "items": r["items"]} if r and r["sub"] == sub else None

    async def delete(self, sub: str, dashboard_id: str) -> bool:
        key = _uuid(dashboard_id) or ""
        if key in self._rows and self._rows[key]["sub"] == sub:
            del self._rows[key]
            return True
        return False


class PgDashboardStore:
    """app.saved_dashboards through the agent's app-role pool. The per-sub limit is checked inside the insert
    transaction under a per-sub advisory lock, so concurrent saves cannot exceed it."""

    def __init__(self, pool, timeout_s: float = 3.0):
        self._pool, self._timeout = pool, timeout_s

    async def list(self, sub: str) -> list[dict]:
        async with self._pool.connection(timeout=self._timeout) as conn:
            cur = await conn.execute(
                "SELECT id::text, title, created_at, jsonb_array_length(items) FROM app.saved_dashboards "
                "WHERE sub = %s ORDER BY created_at DESC, id", (sub,))
            return [{"id": i, "title": t, "created_at": c.isoformat(), "widget_count": n}
                    for i, t, c, n in await cur.fetchall()]

    async def create(self, sub: str, body: SaveDashboard) -> str:
        new_id = str(uuid.uuid4())
        async with self._pool.connection(timeout=self._timeout) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('saved_dashboards:' || %s))", (sub,))
                cur = await conn.execute("SELECT count(*) FROM app.saved_dashboards WHERE sub = %s", (sub,))
                if (await cur.fetchone())[0] >= MAX_DASHBOARDS:
                    raise LimitReached()
                await conn.execute("INSERT INTO app.saved_dashboards (id, sub, title, items) VALUES (%s, %s, %s, %s)",
                                   (new_id, sub, body.title, json.dumps(body.items_json())))
        return new_id

    async def get(self, sub: str, dashboard_id: str) -> dict | None:
        if (key := _uuid(dashboard_id)) is None:
            return None
        async with self._pool.connection(timeout=self._timeout) as conn:
            cur = await conn.execute("SELECT id::text, title, items FROM app.saved_dashboards WHERE id = %s AND sub = %s",
                                     (key, sub))
            row = await cur.fetchone()
        return {"id": row[0], "title": row[1], "items": row[2]} if row else None

    async def delete(self, sub: str, dashboard_id: str) -> bool:
        if (key := _uuid(dashboard_id)) is None:
            return False
        async with self._pool.connection(timeout=self._timeout) as conn:
            cur = await conn.execute("DELETE FROM app.saved_dashboards WHERE id = %s AND sub = %s", (key, sub))
            return cur.rowcount == 1
