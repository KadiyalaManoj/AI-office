import csv
import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import NoReturn

COLUMNS = ["variety", "origin_country", "origin_region", "price_usd_kg",
           "price_basis", "source", "date_checked"]
OPTIONAL = "origin_note"  # non-empty = disputed/multi-region origin
PRICE_RE = re.compile(r"\d+(\.\d+)?")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    variety: str
    origin_country: str
    origin_region: str
    price_usd_kg: float
    price_basis: str
    source: str
    date_checked: dt.date
    origin_note: str = ""


def parse_row(raw: dict, line: int) -> Row:
    def fail(msg) -> NoReturn:
        raise ValidationError(f"row {line} ({raw.get('variety') or '?'}): {msg}")

    for col in COLUMNS:
        if not (raw.get(col) or "").strip():
            fail(f"'{col}' is empty")
    text = raw["price_usd_kg"].strip()
    if not PRICE_RE.fullmatch(text):
        fail("price_usd_kg is not numeric (plain digits, '.' decimal)")
    price = float(text)
    if not math.isfinite(price) or price <= 0:
        fail("price_usd_kg must be > 0")
    try:
        day = dt.date.fromisoformat(raw["date_checked"].strip())
    except ValueError:
        fail("date_checked must be YYYY-MM-DD")
    vals = {c: raw[c].strip() for c in COLUMNS[:3] + COLUMNS[4:6]}
    note = (raw.get(OPTIONAL) or "").strip()
    return Row(price_usd_kg=price, date_checked=day, origin_note=note, **vals)


def validate(rows: list) -> list:
    seen = set()
    for r in rows:
        key = r.variety.casefold()
        if key in seen:
            raise ValidationError(f"duplicate variety: {r.variety}")
        seen.add(key)
    if not 15 <= len(rows) <= 20:
        raise ValidationError(f"need 15-20 varieties, got {len(rows)}")
    return rows


def load_rows(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        names = reader.fieldnames or []
        if names not in (COLUMNS, COLUMNS + [OPTIONAL]):
            raise ValidationError(f"header must be {COLUMNS} (+ optional {OPTIONAL}), got {names}")
        rows = [parse_row(d, i) for i, d in enumerate(reader, start=2)]
    return validate(rows)
