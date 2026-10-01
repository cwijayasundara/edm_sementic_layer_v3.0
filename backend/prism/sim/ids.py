"""Realistic financial identifiers with valid check digits (ISIN, CUSIP, SEDOL, LEI) and BICs."""
import random
import string

_ALNUM = string.digits + string.ascii_uppercase
_SEDOL_CHARS = "0123456789BCDFGHJKLMNPQRSTVWXYZ"
_SEDOL_WEIGHTS = (1, 3, 1, 7, 3, 9)


def _value(ch: str) -> int:
    return int(ch) if ch.isdigit() else ord(ch) - 55  # A=10 … Z=35


def isin_check_digit(body11: str) -> str:
    """Luhn over the digit expansion of the 11-character ISIN body."""
    digits = "".join(str(_value(c)) for c in body11)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def cusip_check_digit(body8: str) -> str:
    total = 0
    for i, ch in enumerate(body8):
        v = _value(ch)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def sedol_check_digit(body6: str) -> str:
    total = sum(_value(c) * w for c, w in zip(body6, _SEDOL_WEIGHTS))
    return str((10 - total % 10) % 10)


def lei_check_digits(base18: str) -> str:
    """ISO 17442 / ISO 7064 MOD 97-10."""
    n = int("".join(str(_value(c)) for c in base18 + "00"))
    return f"{98 - n % 97:02d}"


def is_valid_isin(isin: str) -> bool:
    return len(isin) == 12 and isin_check_digit(isin[:11]) == isin[11]


def is_valid_lei(lei: str) -> bool:
    return len(lei) == 20 and int("".join(str(_value(c)) for c in lei)) % 97 == 1


def make_cusip(rng: random.Random) -> str:
    body = "".join(rng.choice(string.digits) for _ in range(6)) + "".join(rng.choice(_ALNUM) for _ in range(2))
    return body + cusip_check_digit(body)


def make_sedol(rng: random.Random) -> str:
    body = "".join(rng.choice(_SEDOL_CHARS) for _ in range(6))
    return body + sedol_check_digit(body)


def make_isin(rng: random.Random, country: str) -> tuple[str, str | None, str | None]:
    """Return (isin, cusip, sedol); CUSIP only for US, SEDOL only for GB."""
    if country == "US":
        cusip = make_cusip(rng)
        body = "US" + cusip
        return body + isin_check_digit(body), cusip, None
    if country == "GB":
        sedol = make_sedol(rng)
        body = "GB00" + sedol
        return body + isin_check_digit(body), None, sedol
    body = country + "".join(rng.choice(_ALNUM) for _ in range(9))
    return body + isin_check_digit(body), None, None


def make_lei(rng: random.Random) -> str:
    base = "".join(rng.choice(string.digits) for _ in range(4)) + "00" + "".join(rng.choice(_ALNUM) for _ in range(12))
    return base + lei_check_digits(base)


def make_bic(rng: random.Random, country: str) -> str:
    return (
        "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        + country
        + "".join(rng.choice(_ALNUM) for _ in range(2))
    )
