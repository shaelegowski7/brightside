"""Estimated monthly sales from Amazon sales rank, for products without a
"bought in past month" figure -- the same idea as SellerAmp's Est. Sales.

A power law per root category (log10 sales = a + b * log10 rank) fitted by
tools/build_sales_model.py; categories without enough data use the
all-category fit. An estimate, not a count: used for storage and
competition maths and shown in reports, never as the only sales evidence
for a pass.
"""
import json
from functools import lru_cache
from pathlib import Path

_MODEL_PATH = Path(__file__).resolve().parent / "data" / "sales_model.json"


@lru_cache(maxsize=1)
def _model() -> dict | None:
    return json.loads(_MODEL_PATH.read_text(encoding="utf-8")) if _MODEL_PATH.exists() else None


def estimate_with(model: dict | None, category: str | None, rank: float | None) -> float | None:
    if not model or not rank or rank <= 0:
        return None
    fit = (model.get("categories") or {}).get(category or "") or model.get("all")
    if not fit:
        return None
    import math
    return round(10 ** (fit["a"] + fit["b"] * math.log10(rank)), 1)


def estimate(category: str | None, rank: float | None) -> float | None:
    """Monthly sales for a 30-day average root rank in `category`."""
    return estimate_with(_model(), category, rank)
