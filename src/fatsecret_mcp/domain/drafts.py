"""Proposed Food Entries: their amounts and nutrition, the Diary Day they're for, and their totals."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from fatsecret_mcp.domain.day_summary import total_nutrition
from fatsecret_mcp.domain.models import (
    DraftEntry,
    DraftItem,
    DraftTotals,
    Food,
    FoodEntry,
    Nutrition,
    Serving,
)

METRIC_UNITS = ("g", "ml")
UNIT_DECIMALS = 3
MAX_METRIC_AMOUNT = 10000
MAX_KCAL = 20000
DRAFT_LIFETIME = timedelta(minutes=30)
MAX_DAYS_BACK = 365
MAX_DAYS_AHEAD = 7


class InvalidDraftItem(ValueError):
    """A proposed amount or its Serving cannot be used in a Draft."""


class UnlikelyDiaryDay(ValueError):
    """A Diary Day too far from today to be meant."""


def check_diary_day(diary_day: date, today: date) -> None:
    """Allow Diary Days from a year back to a week ahead."""
    days_ahead = (diary_day - today).days

    if days_ahead < -MAX_DAYS_BACK or days_ahead > MAX_DAYS_AHEAD:
        raise UnlikelyDiaryDay(
            "A Draft's Diary Day must be at most a year back or a week ahead. Check the date with the User."
        )


def draft_totals(
    entries: Sequence[FoodEntry], items: Sequence[DraftItem], daily_targets: Nutrition
) -> DraftTotals:
    """The items added up, and the Diary Day's totals and what remains once they're added."""
    item_nutrition = [item.nutrition for item in items]
    added = sum(item_nutrition, Nutrition())
    consumed = total_nutrition(entries) + added

    return DraftTotals(
        added=added,
        consumed=consumed,
        daily_targets=daily_targets,
        remaining=daily_targets - consumed,
    )


def draft_expiry(now: datetime) -> datetime:
    return now.astimezone(UTC) + DRAFT_LIFETIME


def draft_item(
    food: Food, name: str, serving: Serving, number_of_units: float, assumption: str | None
) -> DraftItem:
    """The item logging `number_of_units` of `serving`, in the Food's grams or ml when its weight is known."""
    if not math.isfinite(serving.number_of_units) or serving.number_of_units <= 0:
        raise InvalidDraftItem("Serving has no valid positive unit count. Choose another Serving.")

    serving, number_of_units = _in_metric_serving(food, serving, number_of_units)

    if number_of_units <= 0:
        raise InvalidDraftItem("Amount is too small to log.")

    ratio = number_of_units / serving.number_of_units
    nutrition = serving.nutrition.scaled(ratio)
    metric_amount = serving.metric_amount * ratio if serving.metric_amount is not None else None

    check_plausible(nutrition, metric_amount)

    amount = _amount(serving, ratio)
    entry = DraftEntry(
        food_id=food.id,
        serving_id=serving.id,
        number_of_units=number_of_units,
        entry_name=name,
    )

    return DraftItem(
        entry=entry,
        serving=serving.description,
        amount=amount,
        nutrition=nutrition,
        assumption=assumption,
    )


def metric_serving(food: Food, serving: Serving) -> Serving:
    """The Food's Serving of 1 g or ml per unit for a Serving of known weight; else `serving` itself.

    The FatSecret app can't always show a count Serving such as "1 roll": in another language the
    Food may lack it. Grams or ml always show, so a weighed portion is logged in them.
    """
    if serving.metric_amount is None or serving.metric_unit not in METRIC_UNITS:
        return serving

    if _one_unit_per_gram_or_ml(serving):
        return serving

    candidates: list[Serving] = []

    for candidate in food.servings:
        if candidate.metric_unit == serving.metric_unit and _one_unit_per_gram_or_ml(candidate):
            candidates.append(candidate)

    if not candidates:
        return serving

    # FatSecret rounds a Serving's kcal, so "100 g" is more exact than "1 g".
    return max(candidates, key=_units)


def check_plausible(nutrition: Nutrition, metric_amount: float | None) -> None:
    """Reject an item over 20000 kcal, or over 10000 g or ml when its weight is known."""
    if not math.isfinite(nutrition.kcal) or nutrition.kcal > MAX_KCAL:
        raise InvalidDraftItem("Amount is implausibly large: at most 20000 kcal per item.")

    if metric_amount is not None and (not math.isfinite(metric_amount) or metric_amount > MAX_METRIC_AMOUNT):
        raise InvalidDraftItem("Amount is implausibly large: at most 10000 g or ml per item.")


def _one_unit_per_gram_or_ml(serving: Serving) -> bool:
    """True for a Serving such as "100 g", whose units are grams or ml."""
    if serving.metric_amount is None or serving.metric_amount <= 0:
        return False

    # A Food's own gram Serving is described by its amount alone; "1 slice (1 g)" is not one.
    in_units = f"{serving.number_of_units:g} {serving.metric_unit}"

    return serving.number_of_units == serving.metric_amount and serving.description == in_units


def _units(serving: Serving) -> float:
    return serving.number_of_units


def _in_metric_serving(food: Food, serving: Serving, number_of_units: float) -> tuple[Serving, float]:
    """The Serving to log and its units: the same weight in the Food's grams or ml when known."""
    logged = metric_serving(food, serving)

    if logged is serving or serving.metric_amount is None:
        return serving, number_of_units

    metric_amount = serving.metric_amount * number_of_units / serving.number_of_units

    return logged, round(metric_amount, UNIT_DECIMALS)


def _amount(serving: Serving, ratio: float) -> str:
    if serving.metric_amount is not None and serving.metric_unit is not None:
        metric_amount = serving.metric_amount * ratio

        return f"{metric_amount:g} {serving.metric_unit}"

    return f"{ratio:g} x ({serving.description})"
