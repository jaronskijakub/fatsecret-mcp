"""Amounts of logged Food Entries in grams or millilitres, and FatSecret units scaled to a new amount.

Shared by repeated entries and changed entries, which both start from an entry in the Food Diary.
"""

from __future__ import annotations

import math
import re

from fatsecret_mcp.domain.drafts import METRIC_UNITS, UNIT_DECIMALS, InvalidDraftItem
from fatsecret_mcp.domain.models import FoodEntry, MetricAmount, Nutrition, Serving

WRITTEN_METRIC_AMOUNT = re.compile(rf"(?P<amount>\d+(?:\.\d+)?) ?(?P<unit>{'|'.join(METRIC_UNITS)})")
BARE_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def written_metric_amount(entry: FoodEntry) -> MetricAmount | None:
    """The entry's amount when FatSecret wrote it in grams or ml, e.g. "150 g"."""
    match = WRITTEN_METRIC_AMOUNT.fullmatch(entry.amount)

    if match is None:
        return None

    amount = float(match["amount"])

    if amount <= 0:
        return None

    return MetricAmount(
        amount=amount,
        unit=match["unit"],
    )


def has_bare_number_amount(entry: FoodEntry) -> bool:
    """True when FatSecret left the Serving's measure out of the amount: "1" for "1 roll (3-1/2" dia)"."""
    return BARE_NUMBER.fullmatch(entry.amount) is not None


def serving_metric_amount(entry: FoodEntry, serving: Serving) -> MetricAmount | None:
    """The entry's amount in grams or ml, from the weight of its Serving."""
    if serving.metric_amount is None or serving.metric_unit not in METRIC_UNITS:
        return None

    if not positive(serving.metric_amount) or not positive(serving.number_of_units):
        return None

    amount = serving.metric_amount * entry.number_of_units / serving.number_of_units

    return MetricAmount(
        amount=amount,
        unit=serving.metric_unit,
    )


def scaled_units(entry: FoodEntry, ratio: float) -> tuple[float, Nutrition]:
    """The FatSecret units for `ratio` times the entry's amount, rounded as sent, and their nutrition."""
    units = round(entry.number_of_units * ratio, UNIT_DECIMALS)

    if units <= 0:
        raise InvalidDraftItem("Amount is too small to log.")

    # FatSecret logs nutrition for the units it's given, so the rounded units scale it.
    nutrition = entry.nutrition.scaled(units / entry.number_of_units)

    return units, nutrition


def amount_text(entry: FoodEntry, amount: MetricAmount | None) -> str:
    """`amount` as "75 g", or the entry's own amount when its weight is unknown."""
    if amount is None:
        return entry.amount

    return f"{amount.amount:g} {amount.unit}"


def positive(value: float) -> bool:
    """True for a finite number above zero."""
    return math.isfinite(value) and value > 0
