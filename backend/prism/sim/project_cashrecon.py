"""CashRecon: nostro statements (MT940/MT950/camt.053) vs ledger, matching, breaks and their workflow."""
import random
from datetime import date, timedelta

from prism.sim.calendar import at
from prism.sim.canaries import plant_canaries
from prism.sim.keys import statement_format
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import CashAccount, Universe

MATCH_RULES = (
    ("MR01", "Exact 1:1 amount and reference", "1:1", 0.00, 0),
    ("MR02", "Amount tolerance 1:1", "1:1", 1.00, 0),
    ("MR03", "Date tolerance 1:1", "1:1", 0.00, 2),
    ("MR04", "Aggregate 1:N by reference", "1:N", 0.00, 1),
    ("MR05", "Manual match", "N:M", None, None),
)
OUTCOMES = ("matched", "missing_statement", "missing_ledger", "amount_diff", "date_diff")
OUTCOME_WEIGHTS = (965, 12, 10, 8, 5)
ROOT_CAUSES = ("timing", "bank_fee", "fx_rounding", "missing_booking", "duplicate_entry", "bank_error")
NARRATIVES = ("Incoming payment", "Outgoing payment", "FX settlement", "Coupon received",
              "Custody fee", "Margin call", "Dividend received", "Interest")


def ops_users(region: str) -> tuple[str, ...]:
    return tuple(f"ops.{region.lower()}{k:02d}" for k in (1, 2, 3))


def project_cashrecon(u: Universe) -> dict[str, TableData]:
    rng, nid = random.Random(u.cfg.seed + 3), id_sequence()
    t = {
        "private.cash_accounts": TableData(("account_id", "legal_entity_id", "bank_source_id", "bank_bic",
                                            "nostro_no", "ccy", "region")),
        "statements": TableData(("stmt_id", "account_id", "msg_type", "stmt_no", "value_date", "opening_bal",
                                 "closing_bal", "region")),
        "statement_entries": TableData(("entry_id", "stmt_id", "account_id", "value_date", "amount", "dc",
                                        "reference", "narrative", "region")),
        "ledger_entries": TableData(("entry_id", "account_id", "gl_ref", "amount", "dc", "booking_date",
                                     "reference", "region")),
        "match_rules": TableData(("rule_id", "name", "cardinality", "tol_amount", "tol_days")),
        "match_groups": TableData(("match_id", "rule_id", "status", "matched_at", "matched_by", "region")),
        "match_items": TableData(("match_id", "side", "entry_id", "region")),
        "breaks": TableData(("break_id", "account_id", "legal_entity_id", "break_type", "amount", "ccy",
                             "opened_on", "age_days", "status", "owner", "root_cause", "region", "bank_source_id")),
        "break_actions": TableData(("action_id", "break_id", "action", "actor", "ts", "comment", "region")),
    }
    for rule in MATCH_RULES:
        t["match_rules"].add(*rule)
    for a in u.cash_accounts:
        t["private.cash_accounts"].add(a.account_id, a.legal_entity_id, a.bank_source_id, a.bank_bic,
                                       a.nostro_no, a.ccy, a.region)
        _account_activity(rng, u, a, t, nid)
    _usd_break_story(rng, u, t, nid)
    plant_canaries(u, t, nid)   # last: no RNG draws after this
    return t


def _account_activity(rng, u: Universe, a: CashAccount, t, nid) -> None:
    users = ops_users(a.region)
    msg_type = statement_format(a.bank_source_id)
    balance = round(rng.uniform(1e6, 5e7), 2)
    for i, d in enumerate(u.days):
        stmt_id, lines = nid("ST"), []
        for _ in range(rng.randint(3, 10)):
            amount = round(rng.lognormvariate(9, 1.5), 2)
            dc, ref = rng.choice("CD"), nid("REF", 9)
            outcome = rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS)[0]
            ledger_id = stmt_entry_id = None
            stmt_amount = amount
            if outcome != "missing_ledger":
                ledger_id = nid("LGE")
                booking = u.days[i - 1] if outcome == "date_diff" and i > 0 else d
                t["ledger_entries"].add(ledger_id, a.account_id, f"GL-{a.ccy}-{rng.randint(1000, 1999)}",
                                        amount, dc, booking, ref, a.region)
            if outcome != "missing_statement":
                if outcome == "amount_diff":
                    stmt_amount = round(abs(amount + rng.choice((-1, 1)) * rng.uniform(0.5, 250)), 2)
                stmt_entry_id = nid("STE")
                lines.append((stmt_entry_id, stmt_amount, dc, ref))
            if outcome == "matched":
                _add_match(rng, u, t, nid, a, d, ledger_id, stmt_entry_id, users)
            else:
                diff = abs(amount - stmt_amount) if outcome == "amount_diff" else amount
                _add_break(rng, u, t, nid, a, d, outcome, max(diff, 0.01), users)
        opening = balance
        for entry_id, amt, dc, ref in lines:
            t["statement_entries"].add(entry_id, stmt_id, a.account_id, d, amt, dc, ref, rng.choice(NARRATIVES), a.region)
            balance += amt if dc == "C" else -amt
        balance = round(balance, 2)
        t["statements"].add(stmt_id, a.account_id, msg_type, i + 1, d, round(opening, 2), balance, a.region)


def _add_match(rng, u: Universe, t, nid, a: CashAccount, d: date, ledger_id: str, stmt_entry_id: str,
               users) -> None:
    rule = rng.choices(("MR01", "MR02", "MR04", "MR05"), weights=(88, 5, 2, 5))[0]
    manual = rule == "MR05"
    match_id = nid("M")
    matched_at = at(d, 19) + timedelta(days=1 if manual else 0, minutes=rng.randrange(120))
    matched_at = min(matched_at, at(u.cfg.as_of, 23, 59))  # manual next-day matches cannot happen after as-of
    t["match_groups"].add(match_id, rule, "manual" if manual else "auto", matched_at,
                          rng.choice(users) if manual else "system", a.region)
    t["match_items"].add(match_id, "ledger", ledger_id, a.region)
    t["match_items"].add(match_id, "statement", stmt_entry_id, a.region)


def _add_break(rng, u: Universe, t, nid, a: CashAccount, d: date, break_type: str, amount: float,
               users, force_open: bool = False) -> None:
    age = (u.cfg.as_of - d).days
    closed = False if force_open else rng.random() < (0.9 if age > 7 else 0.6 if age > 2 else 0.2)
    closed = closed and age >= 1  # a break opened today cannot already be closed
    break_id, owner = nid("BRK", 6), rng.choice(users)
    if closed:
        status, age_days, cause = "closed", min(rng.randint(1, 6), age), rng.choice(ROOT_CAUSES)
    else:
        status, age_days, cause = rng.choice(("open", "open", "investigating")), age, None
    t["breaks"].add(break_id, a.account_id, a.legal_entity_id, break_type, round(amount, 2), a.ccy, d, age_days,
                    status, owner, cause, a.region, a.bank_source_id)
    opened = at(d, 20)
    t["break_actions"].add(nid("BA"), break_id, "opened", "system", opened, None, a.region)
    t["break_actions"].add(nid("BA"), break_id, "assigned", "system", opened + timedelta(minutes=5),
                           f"Assigned to {owner}", a.region)
    if closed:
        t["break_actions"].add(nid("BA"), break_id, "commented", owner, opened + timedelta(hours=12),
                               f"Root cause: {cause}", a.region)
        t["break_actions"].add(nid("BA"), break_id, "closed", owner, opened + timedelta(days=age_days), None, a.region)


def _usd_break_story(rng, u: Universe, t, nid) -> None:
    st = u.stories
    accounts = {a.account_id: a for a in u.cash_accounts}
    eligible = [d for d in u.days if 6 <= (u.cfg.as_of - d).days <= 20]
    for k in range(st.usd_break_extra):
        a = accounts[st.usd_break_account_ids[k % len(st.usd_break_account_ids)]]
        _add_break(rng, u, t, nid, a, rng.choice(eligible), rng.choice(("amount_diff", "missing_statement")),
                   rng.uniform(5_000, 750_000), ops_users(a.region), force_open=True)
