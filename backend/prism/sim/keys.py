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
