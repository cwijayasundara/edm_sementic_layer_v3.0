"""Identifier conventions shared across platforms (FeedHub deliveries referenced by AssetRecon)."""
from datetime import date


def feed_id(source_id: str, data_type: str) -> str:
    return f"FD-{source_id}-{data_type}"


def delivery_id(source_id: str, data_type: str, d: date) -> str:
    return f"DLV-{feed_id(source_id, data_type)}-{d:%Y%m%d}"


STATEMENT_FORMATS = ("MT940", "MT950", "CAMT053")


def statement_format(source_id: str) -> str:
    """The one statement format a bank sends: CashRecon statements and the FeedHub bank feed must agree."""
    return STATEMENT_FORMATS[int(source_id[3:]) % len(STATEMENT_FORMATS)]


def fund_entity_id(n_entities: int, index: int) -> str:
    """Legal entity of the portfolio at `index` (0-based) in the universe: appended after the universe's entities by
    the incident pass (LE00401... by default); AssetRecon portfolios cite it."""
    return f"LE{n_entities + index + 1:05d}"


def fund_cash_account_id(n_cash_accounts: int, index: int) -> str:
    """Cash account of the portfolio at `index` (0-based), owned by its fund entity; appended by the incident pass."""
    return f"CA{n_cash_accounts + index + 1:04d}"
