"""FeedHub: custodian/bank/prime-broker feeds, daily delivery monitoring and support tickets."""
import itertools
import random
from datetime import time, timedelta

from prism.sim.calendar import at
from prism.sim.keys import delivery_id, feed_id, statement_format
from prism.sim.model import TableData
from prism.sim.universe import Universe

FEED_SPECS = {
    "custodian": (("positions", "MT535", time(6, 0)), ("transactions", "CSV", time(6, 0))),
    "bank": (("cash", None, time(7, 0)),),  # format per bank: keys.statement_format
    "prime_broker": (("positions", "CSV", time(5, 30)), ("transactions", "CSV", time(5, 30))),
}
RECORD_RANGES = {"positions": (50, 5000), "transactions": (10, 800), "cash": (10, 400)}
ERROR_CODES = ("PARSE_ERROR", "SCHEMA_MISMATCH", "AUTH_FAILED")
TICKET_CATEGORIES = ("credential_change", "format_change", "late", "missing_file")


def project_feedhub(u: Universe) -> dict[str, TableData]:
    rng, st = random.Random(u.cfg.seed + 5), u.stories
    t = {
        "sources": TableData(("source_id", "name", "source_type", "bic", "country")),
        "feeds": TableData(("feed_id", "source_id", "data_type", "format", "frequency", "expected_by_utc", "source_type")),
        "feed_deliveries": TableData(("delivery_id", "feed_id", "source_id", "business_date", "status", "received_at",
                                      "latency_min", "record_count", "error_code", "source_type", "feed_type")),
        "support_tickets": TableData(("ticket_id", "feed_id", "source_id", "category", "status", "opened_at",
                                      "closed_at", "source_type")),
    }
    feeds = []
    for s in u.sources:
        t["sources"].add(s.source_id, s.name, s.source_type, s.bic, s.country)
        for data_type, fmt, expected in FEED_SPECS[s.source_type]:
            if s.source_type == "bank":
                fmt = statement_format(s.source_id)  # must match the statements CashRecon receives
            fid = feed_id(s.source_id, data_type)
            feeds.append((fid, s, data_type, expected))
            t["feeds"].add(fid, s.source_id, data_type, fmt, "daily", expected, s.source_type)

    for fid, s, data_type, expected in feeds:
        story = s.source_id == st.late_custodian_source_id
        # AssetRecon custodian positions cite these deliveries, so outside the story window they always arrive.
        cited_by_assetrecon = s.source_type == "custodian" and data_type == "positions"
        for i, d in enumerate(u.days):
            if story and i >= st.late_start_idx:
                status, late_range = rng.choices(("late", "missing", "failed"), weights=(70, 20, 10))[0], (90, 600)
            else:
                status = rng.choices(("on_time", "late", "missing", "failed"), weights=(94, 4, 1, 1))[0]
                late_range = (15, 240)
                if cited_by_assetrecon and status in ("missing", "failed"):
                    status = "on_time"
                if story and i == st.late_start_idx - 1:
                    status = "on_time"  # the last good delivery before the credential change
            due = at(d, expected.hour, expected.minute)
            received = latency = count = error = None
            if status == "on_time":
                received, latency = due - timedelta(minutes=rng.randint(5, 120)), 0
            elif status == "late":
                latency = rng.randint(*late_range)
                received = due + timedelta(minutes=latency)
            elif status == "failed":
                received = due + timedelta(minutes=rng.randint(0, 60))
                error = "AUTH_FAILED" if story else rng.choice(ERROR_CODES)
            if status in ("on_time", "late"):
                count = rng.randint(*RECORD_RANGES[data_type])
            t["feed_deliveries"].add(delivery_id(s.source_id, data_type, d), fid, s.source_id, d, status, received,
                                     latency, count, error, s.source_type, data_type)
    _tickets(rng, u, feeds, t["support_tickets"])
    return t


def _tickets(rng: random.Random, u: Universe, feeds, table: TableData) -> None:
    st, seq = u.stories, itertools.count(1)
    story_source = next(s for s in u.sources if s.source_id == st.late_custodian_source_id)
    table.add(f"TK{next(seq):05d}", feed_id(story_source.source_id, "positions"), story_source.source_id,
              "credential_change", "open", at(u.days[st.late_start_idx - 1], 9, 15), None, story_source.source_type)
    for d in u.days:
        if rng.random() >= 0.3:
            continue
        fid, s, _, _ = rng.choice(feeds)
        category = rng.choice(TICKET_CATEGORIES)
        if s.source_id == st.late_custodian_source_id and category == "credential_change":
            category = "format_change"  # keep the story ticket unique
        opened = at(d, 8) + timedelta(minutes=rng.randrange(540))
        closed = (u.cfg.as_of - d).days > 3 and rng.random() < 0.9
        table.add(f"TK{next(seq):05d}", fid, s.source_id, category, "closed" if closed else "open", opened,
                  opened + timedelta(hours=rng.uniform(1, 72)) if closed else None, s.source_type)
    # Guarantee coverage: every source type has at least one ticket (appended after the loop so the
    # RNG stream of the draws above is unchanged).
    present = {row["source_type"] for row in table.dicts()}
    for source_type in sorted({s.source_type for _, s, _, _ in feeds} - present):
        fid, s, _, _ = rng.choice([f for f in feeds if f[1].source_type == source_type])
        d = rng.choice(u.days)
        category = rng.choice([c for c in TICKET_CATEGORIES
                               if not (s.source_id == st.late_custodian_source_id and c == "credential_change")])
        opened = at(d, 8) + timedelta(minutes=rng.randrange(540))
        closed = (u.cfg.as_of - d).days > 3
        table.add(f"TK{next(seq):05d}", fid, s.source_id, category, "closed" if closed else "open", opened,
                  opened + timedelta(hours=rng.uniform(1, 72)) if closed else None, s.source_type)
