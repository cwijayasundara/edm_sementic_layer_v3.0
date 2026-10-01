"""ResultStore: per-sub isolation, TTL (injected clock), per-sub cap, byte cap, LRU, compact summaries."""
import re
import threading

import pytest

from prism.gateway.errors import GatewayError
from prism.gateway.results import ResultStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def rows(n, width=1):
    return [[i, "x" * width] for i in range(n)]


def _unknown(fn, *a, **k):
    with pytest.raises(GatewayError) as info:
        fn(*a, **k)
    assert info.value.code == "unknown_handle" and str(info.value) == "unknown handle"


def test_handle_format_and_round_trip():
    s = ResultStore()
    h = s.put("alice", ["n", "s"], rows(3), {"units": {"n": "breaks"}, "truncated": False})
    assert re.fullmatch(r"r_[0-9a-f]{12}", h)
    r = s.get("alice", h)
    assert r.columns == ("n", "s") and r.rows == ((0, "x"), (1, "x"), (2, "x")) and r.meta["units"] == {"n": "breaks"}
    assert len({s.put("alice", ["n"], [[1]]) for _ in range(40)}) == 40


def test_cross_sub_access_is_unknown_handle_never_forbidden():
    s = ResultStore()
    h = s.put("alice", ["n"], [[1]])
    for who in ("bob", "", "ALICE", "alice ", None, 7):
        _unknown(s.get, who, h)
        _unknown(s.summary, who, h)
        _unknown(s.page, who, h)
    assert s.get("alice", h).rows == ((1,),)  # bob's probing did not evict or touch alice's result


@pytest.mark.parametrize("bad", ["r_000000000000", "nope", "", None, 5, ["r_x"], "r_" + "a" * 500])
def test_missing_or_malformed_handle_is_unknown(bad):
    _unknown(ResultStore().get, "alice", bad)


def test_ttl_expiry_with_injected_clock():
    clock = Clock()
    s = ResultStore(ttl_s=900, clock=clock)
    h = s.put("alice", ["n"], [[1]])
    clock.t += 899
    s.get("alice", h)
    clock.t += 2  # TTL counts from creation, not last access: a result never lives longer than ttl_s
    _unknown(s.get, "alice", h)
    assert s.stats()["handles"] == 0


def test_per_sub_cap_evicts_only_that_subs_least_recently_used():
    s = ResultStore(max_handles_per_sub=3)
    bob = s.put("bob", ["n"], [[0]])
    a = [s.put("alice", ["n"], [[i]]) for i in range(3)]
    s.get("alice", a[0])                    # a[0] is now most recently used
    a.append(s.put("alice", ["n"], [[3]]))  # evicts a[1]
    _unknown(s.get, "alice", a[1])
    for h in (a[0], a[2], a[3]):
        s.get("alice", h)
    assert s.get("bob", bob).rows == ((0,),)


def _too_large(fn, *a, code="result_too_large", **k):
    with pytest.raises(GatewayError) as info:
        fn(*a, **k)
    assert info.value.code == code, info.value.code
    return info.value


def test_per_sub_byte_quota_evicts_only_that_subs_own_lru():
    s = ResultStore(max_bytes=10_000_000, max_bytes_per_sub=30_000)
    bob = s.put("bob", ["n", "s"], rows(10, 500))
    a = [s.put("alice", ["n", "s"], rows(10, 500)) for _ in range(3)]
    sizes = [s.get("alice", h).nbytes for h in a]
    assert sum(sizes) <= 30_000 < sum(sizes) + sizes[0]       # the next put needs room
    s.get("alice", a[0])                                      # a[1] is now alice's least recently used
    a.append(s.put("alice", ["n", "s"], rows(10, 500)))
    _unknown(s.get, "alice", a[1])
    for h in (a[0], a[2], a[3]):
        s.get("alice", h)
    assert s.get("bob", bob).rows[0] == (0, "x" * 500)
    assert s.stats()["bytes"] == sum(s.get(sub, h).nbytes for sub, h in (("bob", bob), *(("alice", h) for h in
                                                                                        (a[0], a[2], a[3]))))


def test_an_attacker_can_never_evict_a_victims_result():
    s = ResultStore(max_bytes=200_000, max_bytes_per_sub=60_000)
    victim = s.put("victim", ["n", "s"], rows(10, 500))
    for attacker in ("mallory", "eve", "trent", "oscar"):
        for _ in range(10):
            try:
                s.put(attacker, ["n", "s"], rows(10, 500))
            except GatewayError as e:
                assert e.code == "result_store_full"
    assert s.get("victim", victim).rows[0] == (0, "x" * 500)
    assert s.stats()["bytes"] <= 200_000
    full = _too_large(s.put, "zed", ["n", "s"], rows(40, 500), code="result_store_full")
    assert "try again" in str(full)
    s.get("victim", victim)


def test_a_refused_put_evicts_nothing():
    s = ResultStore(max_bytes=200_000, max_bytes_per_sub=150_000)
    keep = s.put("alice", ["n", "s"], rows(10, 500))
    other = s.put("bob", ["n", "s"], rows(200, 500))
    _too_large(s.put, "alice", ["n", "s"], rows(80, 500), code="result_store_full")   # even without its own
    s.get("alice", keep)
    s.get("bob", other)
    _too_large(s.put, "alice", ["n", "s"], rows(10, 50_000))   # over the cell cap
    _too_large(s.put, "alice", ["n", "s"], rows(400, 500))     # over the sub quota on its own
    s.get("alice", keep)


def test_a_20mb_logical_put_is_refused_before_freezing(monkeypatch):
    import prism.gateway.results as results

    monkeypatch.setattr(results, "jsonable", lambda v: pytest.fail("froze an oversized result"))
    s = ResultStore()
    _too_large(s.put, "alice", ["s"], [["y" * 4000] for _ in range(5000)])               # 20 MB of text
    _too_large(s.put, "alice", ["n"], [[i] for i in range(s.max_rows + 1)])              # too long
    _too_large(s.put, "alice", [f"c{i}" for i in range(s.max_columns + 1)], [[1] * (s.max_columns + 1)])
    _too_large(s.put, "alice", ["c" * 300], [[1]])                                       # column name
    _too_large(s.put, "alice", ["s"], [["z" * (s.max_cell_chars + 1)]])                  # one long cell
    _too_large(s.put, "alice", ["l"], [[["z" * 100] * 200]])                             # a long list cell
    _too_large(s.put, "alice", ["d"], [[{f"k{i}": "v" * 100 for i in range(200)}]])      # a long dict cell
    deep: list = []
    for _ in range(100):
        deep = [deep]
    _too_large(s.put, "alice", ["deep"], [[deep]])                                       # nesting
    assert s.stats() == {"handles": 0, "bytes": 0}


def test_row_cap_admits_a_full_combine_result():
    from prism.gateway.combine import MAX_ROWS

    s = ResultStore()
    assert s.max_rows >= MAX_ROWS
    h = s.put("alice", ["n", "region"], [[i, "EMEA"] for i in range(MAX_ROWS)])
    assert s.summary("alice", h)["row_count"] == MAX_ROWS


def test_byte_accounting_is_not_an_underestimate():
    """JSON length undercounts Python objects several times over; the store's account must cover real memory."""
    import tracemalloc
    from datetime import date
    from decimal import Decimal

    data = [[Decimal(i), date(2026, 9, 30), i * 1.5, f"r{i}", None] for i in range(5000)]
    s = ResultStore()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        h = s.put("alice", ["a", "b", "c", "d", "e"], data)
        grown = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert s.get("alice", h).nbytes >= grown > 0
    assert s.stats()["bytes"] == s.get("alice", h).nbytes


def test_summary_is_compact():
    s = ResultStore()
    h = s.put("alice", ["ccy", "value", "note"], [["EUR", i, "y" * 5000] for i in range(100)],
              {"units": {"value": "amount (transaction currency)"}, "truncated": True, "source": "cashrecon",
               "metric_id": "open_break_amount"})
    sm = s.summary("alice", h)
    assert sm["handle"] == h and sm["columns"] == ["ccy", "value", "note"] and sm["row_count"] == 100
    assert len(sm["sample_rows"]) == 5 and sm["truncated"] is True
    assert sm["units"] == {"value": "amount (transaction currency)"}
    assert sm["source"] == "cashrecon" and sm["metric_id"] == "open_break_amount"
    assert all(len(r[2]) <= 203 for r in sm["sample_rows"])
    assert len(str(sm)) < 2000


def test_summary_is_compact_for_any_shape():
    import json

    names = [f"col_{i:04d}_" + "n" * 200 for i in range(2000)]
    cell = ["z" * 100] * 40                                                  # list cells, each ~4 KB as JSON
    s = ResultStore(max_bytes=2**31, max_bytes_per_sub=2**31)
    h = s.put("alice", names, [[cell] * 2000 for _ in range(5)],
              {"units": {n: "u" * 300 for n in names}, "source": "combine"})
    sm = s.summary("alice", h)
    assert len(json.dumps(sm)) <= 8192 and len(json.dumps(sm, indent=None, ensure_ascii=True)) <= 8192
    wide = s.summary("alice", s.put("alice", [f"é{i}" for i in range(60)], [["é" * 150] * 60] * 5))
    assert len(json.dumps(wide)) <= 8192                                    # non-ASCII counted as escaped
    assert sm["columns_truncated"] is True and sm["column_count"] == 2000 and sm["row_count"] == 5
    assert 0 < len(sm["columns"]) <= 50 and all(len(c) <= 67 for c in sm["columns"])
    assert all(len(r) == len(sm["columns"]) for r in sm["sample_rows"])
    assert all(len(json.dumps(v)) <= 210 for r in sm["sample_rows"] for v in r)
    assert len(sm["units"]) <= len(sm["columns"]) and all(len(u) <= 67 for u in sm["units"].values())
    d = s.put("alice", ["k"], [[{f"key{i}": "v" * 50 for i in range(100)}]])
    (cellv,) = s.summary("alice", d)["sample_rows"][0]
    assert isinstance(cellv, str) and len(cellv) <= 203                     # dict cells are capped too
    small = s.summary("alice", s.put("alice", ["a"], [[[1, 2]]], {"units": {"a": "x"}}))
    assert small["columns_truncated"] is False and small["sample_rows"] == [[[1, 2]]] and small["units"] == {"a": "x"}


def test_page_bounds():
    s = ResultStore()
    h = s.put("alice", ["n"], [[i] for i in range(300)])
    p = s.page("alice", h)
    assert p["rows"] == [[i] for i in range(50)] and p["offset"] == 0 and p["row_count"] == 300
    assert s.page("alice", h, offset=290, limit=200)["rows"] == [[i] for i in range(290, 300)]
    assert s.page("alice", h, offset=1000)["rows"] == []
    for kw in ({"limit": 201}, {"limit": 0}, {"offset": -1}, {"limit": "5"}, {"offset": True}):
        with pytest.raises(GatewayError) as info:
            s.page("alice", h, **kw)
        assert info.value.code == "invalid_request"


def test_stored_rows_are_copies_and_jsonable():
    from datetime import date
    from decimal import Decimal

    src = [[Decimal("1.5"), date(2026, 9, 30), float("nan")]]
    meta = {"units": {"a": "x"}}
    s = ResultStore()
    h = s.put("alice", ["a", "b", "c"], src, meta)
    src[0][0] = 99
    meta["units"]["a"] = "changed"
    r = s.get("alice", h)
    assert r.rows == ((1.5, "2026-09-30", "NaN"),) and r.meta["units"] == {"a": "x"}
    with pytest.raises(TypeError):
        r.meta["units"] = {}


def test_put_validates_shape():
    s = ResultStore()
    for cols, data in ((["a"], [[1, 2]]), ([], [[]]), (["a", 1], [[1, 2]]), ("ab", [[1, 2]])):
        with pytest.raises(ValueError):
            s.put("alice", cols, data)
    for sub in ("", None, "x" * 257):
        with pytest.raises(ValueError):
            s.put(sub, ["a"], [[1]])


def test_threaded_puts_respect_caps():
    s = ResultStore(max_handles_per_sub=5)

    def work():
        for i in range(50):
            s.put("alice", ["n"], [[i]])

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.stats()["handles"] == 5
