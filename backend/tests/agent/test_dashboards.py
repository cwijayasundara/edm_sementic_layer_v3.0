import uuid

import pytest
from pydantic import ValidationError

from prism.agent.dashboards import (
    MAX_DASHBOARDS,
    LimitReached,
    MemoryDashboardStore,
    PgDashboardStore,
    SaveDashboard,
)

METRIC = {"tool": "run_metric", "args": {"metric_id": "open_breaks"}}
WIDGET = {"id": "w1", "type": "bar", "title": "Open breaks", "handle": "r_aaaaaaaaaaaa",
          "encoding": {"x": "region", "y": "value"}}


def body(title="My board", n=1):
    return SaveDashboard.model_validate({"title": title, "items": [{"widget": WIDGET, "recipe": METRIC}] * n})


def test_save_body_normalises_recipe_and_drops_the_handle():
    b = body()
    assert b.items[0].widget.handle == "" and b.items[0].recipe["args"]["dimensions"] == []
    assert body("  padded  ").title == "padded"


@pytest.mark.parametrize("raw", [
    {"title": "", "items": [{"widget": WIDGET, "recipe": METRIC}]},
    {"title": "x" * 81, "items": [{"widget": WIDGET, "recipe": METRIC}]},
    {"title": "t", "items": []},
    {"title": "t", "items": [{"widget": WIDGET, "recipe": METRIC}] * 9},
    {"title": "t", "items": [{"widget": WIDGET, "recipe": {"tool": "get_rows", "args": {}}}]},
    {"title": "t", "items": [{"widget": WIDGET, "recipe": METRIC}], "sub": "someone"},
])
def test_save_body_rejects(raw):
    with pytest.raises(ValidationError):
        SaveDashboard.model_validate(raw)


async def test_memory_store_is_per_sub_and_limited():
    s = MemoryDashboardStore()
    a = await s.create("alice", body("A"))
    assert [d["title"] for d in await s.list("alice")] == ["A"] and await s.list("bob") == []
    assert await s.get("bob", a) is None and await s.delete("bob", a) is False
    got = await s.get("alice", a)
    assert got["title"] == "A" and got["items"][0]["recipe"]["tool"] == "run_metric"
    assert (await s.list("alice"))[0]["widget_count"] == 1
    for i in range(MAX_DASHBOARDS - 1):
        await s.create("alice", body(f"d{i}"))
    with pytest.raises(LimitReached):
        await s.create("alice", body("one too many"))
    assert await s.delete("alice", a) is True and await s.get("alice", a) is None


@pytest.mark.db
async def test_pg_store_roundtrip_scoping_and_limit():
    from psycopg_pool import AsyncConnectionPool

    from prism.config import Settings
    from prism.db.app_migrate import migrate_app

    settings = Settings()
    migrate_app(settings)
    sub, other = f"agent-test-{uuid.uuid4().hex}", f"agent-test-{uuid.uuid4().hex}"
    pool = AsyncConnectionPool(settings.app_dsn(), min_size=1, max_size=2, open=False)
    await pool.open()
    store = PgDashboardStore(pool)
    try:
        first = await store.create(sub, body("First"))
        listed = await store.list(sub)
        assert [(d["id"], d["title"], d["widget_count"]) for d in listed] == [(first, "First", 1)]
        got = await store.get(sub, first)
        assert got["title"] == "First" and got["items"][0]["recipe"]["tool"] == "run_metric"
        assert got["items"][0]["widget"]["handle"] == ""
        assert await store.get(other, first) is None and await store.delete(other, first) is False
        assert await store.get(sub, "not-a-uuid") is None and await store.delete(sub, "not-a-uuid") is False
        for i in range(MAX_DASHBOARDS - 1):
            await store.create(sub, body(f"d{i}"))
        with pytest.raises(LimitReached):
            await store.create(sub, body("one too many"))
        assert await store.delete(sub, first) is True and await store.get(sub, first) is None
        await store.create(sub, body("fits again"))
    finally:
        for d in await store.list(sub):
            await store.delete(sub, d["id"])
        await pool.close()
