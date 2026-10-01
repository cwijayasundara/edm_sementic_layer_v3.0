"""Generic invariants over every generated table of every platform."""
from datetime import UTC, date, datetime, time, timedelta

import pytest

from prism.sim.seed import PROJECTORS

# Columns that legitimately hold a FUTURE business date (bounded separately below).
FUTURE_DATE_COLUMNS = {
    "settle_date": "trades settle T+2 after the trade date",
    "pay_date": "corporate-action entitlements are paid days after the ex-date",
    "ex_date": "announced corporate actions can go ex after the as-of date",
    "sla_due": "an exception's SLA deadline lies after the day it was raised",
}
FUTURE_HORIZON = timedelta(days=30)


@pytest.fixture(scope="module")
def projections(universe):
    return {db: project(universe) for db, project in PROJECTORS.items()}


def _temporal_values(projections):
    for db, tables in projections.items():
        for name, table in tables.items():
            for row in table.rows:
                for column, value in zip(table.columns, row):
                    if isinstance(value, (date, datetime)):
                        yield f"{db}.{name}.{column}", column, value


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def test_nothing_happens_after_as_of(projections, universe):
    as_of = universe.cfg.as_of
    end_of_day = datetime.combine(as_of, time(23, 59, 59), tzinfo=UTC)
    violations: dict[str, int] = {}
    checked = 0
    for where, column, value in _temporal_values(projections):
        if column in FUTURE_DATE_COLUMNS:
            continue
        checked += 1
        late = value > end_of_day if isinstance(value, datetime) else value > as_of
        if late:
            violations[where] = violations.get(where, 0) + 1
    assert checked > 0
    assert not violations, violations


def test_future_business_dates_stay_within_horizon(projections, universe):
    limit = universe.cfg.as_of + FUTURE_HORIZON
    seen = set()
    for where, column, value in _temporal_values(projections):
        if column in FUTURE_DATE_COLUMNS:
            seen.add(column)
            assert _as_date(value) <= limit, (where, value)
    assert seen == FUTURE_DATE_COLUMNS.keys()
