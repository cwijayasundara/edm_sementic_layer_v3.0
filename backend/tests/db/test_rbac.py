import time

import psycopg
import pytest
from psycopg import sql

from prism.config import LOGICAL_DBS
from prism.db.policies import ROW_SCOPES, physical_name
from prism.db.session import ctx_from_claims, sign_ctx, scoped_sync
from prism.security.personas import PERSONAS, claims_for

pytestmark = pytest.mark.db


def expected(persona: str, db: str, table: str) -> str:
    """The access specification, written independently of the policy code."""
    if persona in ("head_data", "bi_analyst"):
        return "all"
    if persona == "steward":
        return "all" if db in ("refmaster", "marketmaster") else "none"
    if persona == "cash_ops_emea":
        if db == "cashrecon":
            return "all" if table == "match_rules" else "some"
        return "some" if db == "feedhub" else "none"
    if persona == "invest_ops_growth":
        if db == "assetrecon":
            return "all" if table == "custodians" else "some"
        if db == "feedhub":
            return "some"
        return "all" if (db, table) in {("refmaster", "securities"), ("refmaster", "legal_entities")} else "none"
    raise AssertionError(persona)


@pytest.fixture(scope="module")
def totals(seeded):
    out = {}
    for db in LOGICAL_DBS:
        with psycopg.connect(seeded.dsn(db, admin=True)) as conn:
            for table in ROW_SCOPES[db]:
                ident = sql.Identifier(*physical_name(db, table).split("."))
                out[(db, table)] = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident)).fetchone()[0]
                assert out[(db, table)] > 0, f"{db}.{table} is empty; matrix would be ambiguous"
    return out


@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_visibility_matrix(seeded, totals, persona):
    ctx = ctx_from_claims(claims_for(persona), seeded.ctx_hmac_key.get_secret_value())
    failures = []
    for db in LOGICAL_DBS:
        with scoped_sync(seeded, db, ctx) as conn:
            for table in ROW_SCOPES[db]:
                n = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"]
                total = totals[(db, table)]
                got = "none" if n == 0 else "all" if n == total else "some"
                want = expected(persona, db, table)
                if got != want:
                    failures.append(f"{db}.{table}: expected {want}, got {got} ({n}/{total})")
    assert not failures, "\n".join(failures)


def test_row_scopes_are_exact(seeded):
    ctx = ctx_from_claims(claims_for("cash_ops_emea"), seeded.ctx_hmac_key.get_secret_value())
    with scoped_sync(seeded, "cashrecon", ctx) as conn:
        assert {r["region"] for r in conn.execute("SELECT DISTINCT region FROM breaks")} == {"EMEA"}
    with scoped_sync(seeded, "feedhub", ctx) as conn:
        assert {r["source_type"] for r in conn.execute("SELECT DISTINCT source_type FROM feeds")} == {"bank"}
    ctx = ctx_from_claims(claims_for("invest_ops_growth"), seeded.ctx_hmac_key.get_secret_value())
    with scoped_sync(seeded, "assetrecon", ctx) as conn:
        assert {r["fund_group"] for r in conn.execute("SELECT DISTINCT fund_group FROM portfolios")} == {"Growth"}


def test_account_numbers_are_masked_without_pii_scope(seeded):
    for persona, masked in (("cash_ops_emea", True), ("head_data", False)):
        ctx = ctx_from_claims(claims_for(persona), seeded.ctx_hmac_key.get_secret_value())
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            values = [r["nostro_no"] for r in conn.execute("SELECT nostro_no FROM cash_accounts")]
        assert values and all(v.startswith("****") == masked for v in values)


def test_base_table_behind_masking_view_is_not_directly_readable(seeded):
    ctx = ctx_from_claims(claims_for("head_data"), seeded.ctx_hmac_key.get_secret_value())
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT nostro_no FROM private.cash_accounts")


def test_forged_or_expired_context_is_rejected(seeded):
    forged = sign_ctx({"sub": "x", "scopes": ["cashrecon"], "rows": {"region": ["*"]},
                       "exp": int(time.time()) + 60}, "not-the-real-key")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="invalid security context"):
        with scoped_sync(seeded, "cashrecon", forged) as conn:
            conn.execute("SELECT count(*) FROM breaks")
    expired = ctx_from_claims(claims_for("head_data", ttl_s=-5), seeded.ctx_hmac_key.get_secret_value())
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="expired"):
        with scoped_sync(seeded, "cashrecon", expired) as conn:
            conn.execute("SELECT count(*) FROM breaks")


def test_query_cannot_escalate_by_resetting_context(seeded):
    ctx = ctx_from_claims(claims_for("cash_ops_emea"), seeded.ctx_hmac_key.get_secret_value())
    forged = sign_ctx({"sub": "x", "scopes": ["cashrecon"], "rows": {"region": ["*"]},
                       "exp": int(time.time()) + 60}, "guess")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT set_config('app.ctx', %s, true)", (forged,))
            conn.execute("SELECT count(*) FROM breaks")


def test_writes_and_key_reads_are_denied(seeded):
    ctx = ctx_from_claims(claims_for("head_data"), seeded.ctx_hmac_key.get_secret_value())
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("DELETE FROM breaks")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT k FROM prism_sec.hmac_key")
