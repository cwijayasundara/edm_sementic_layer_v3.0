import random

from prism.sim.ids import (
    cusip_check_digit,
    is_valid_isin,
    is_valid_lei,
    isin_check_digit,
    make_bic,
    make_isin,
    make_lei,
    sedol_check_digit,
)


def test_isin_check_digit_matches_published_examples():
    assert isin_check_digit("US037833100") == "5"  # US0378331005
    assert isin_check_digit("GB000263494") == "6"  # GB0002634946


def test_cusip_and_sedol_check_digits_match_published_examples():
    assert cusip_check_digit("03783310") == "0"
    assert sedol_check_digit("026349") == "4"


def test_generated_leis_validate_and_tampering_is_detected():
    rng = random.Random(1)
    for _ in range(200):
        lei = make_lei(rng)
        assert len(lei) == 20 and is_valid_lei(lei)
    bad = lei[:-1] + ("0" if lei[-1] != "0" else "1")
    assert not is_valid_lei(bad)


def test_make_isin_uses_national_identifier_for_us_and_gb():
    rng = random.Random(2)
    isin, cusip, sedol = make_isin(rng, "US")
    assert isin.startswith("US") and isin[2:11] == cusip and sedol is None and is_valid_isin(isin)
    isin, cusip, sedol = make_isin(rng, "GB")
    assert isin.startswith("GB00") and isin[4:11] == sedol and cusip is None and is_valid_isin(isin)
    isin, cusip, sedol = make_isin(rng, "JP")
    assert isin.startswith("JP") and cusip is None and sedol is None and is_valid_isin(isin)


def test_make_bic_shape():
    bic = make_bic(random.Random(3), "DE")
    assert len(bic) == 8 and bic[4:6] == "DE" and bic[:4].isalpha()
