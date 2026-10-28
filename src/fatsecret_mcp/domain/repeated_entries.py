"""Repeated Food Entries: a Food Entry from the Food Diary, proposed again in grams or millilitres.

The Food, Serving, name and nutrition come from the source entry, so a Food the catalog can't
return can still be logged. A changed amount scales its FatSecret units and nutrition alike.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from fatsecret_mcp.domain.drafts import MAX_DAYS_BACK, InvalidDraftItem, UnlikelyDiaryDay, check_plausible
from fatsecret_mcp.domain.entry_amounts import amount_text, positive, scaled_units
from fatsecret_mcp.domain.models import DraftEntry, DraftItem, FoodEntry, MetricAmount, RepeatedFrom


def check_source_day(source_day: date, today: date) -> None:
    """Allow repeating from today back to a year ago."""
    days_back = (today - source_day).days

    if days_back < 0:
        raise UnlikelyDiaryDay("A Food Entry can only be repeated from today or an earlier Diary Day.")

    if days_back > MAX_DAYS_BACK:
        raise UnlikelyDiaryDay("A Food Entry can only be repeated from at most a year back.")


def entry_on_day(entries: Sequence[FoodEntry], food_entry_id: str, diary_day: date) -> FoodEntry:
    """The Food Entry with this id among a Diary Day's entries, to repeat or to change."""
    for entry in entries:
        if entry.id == food_entry_id:
            return entry

    raise InvalidDraftItem(
        f"Food Entry {food_entry_id} isn't on {diary_day.isoformat()}. Read that Diary Day again with "
        "get_diary_day and refresh=true, and use a food_entry_id it shows."
    )


def repeated_item(
    source: FoodEntry,
    source_amount: MetricAmount | None,
    eaten_amount: float | None,
    assumption: str | None,
) -> DraftItem:
    """The Draft item logging `source` again: `eaten_amount` grams or ml, or its own amount when None.

    `source_amount` is the source's amount in grams or ml; None when its weight is unknown.
    """
    if not positive(source.number_of_units):
        raise InvalidDraftItem(f"{source.name} has no valid unit count in the Food Diary to repeat.")

    amount = _eaten_metric_amount(source, source_amount, eaten_amount)
    units, nutrition = scaled_units(source, _ratio(amount, source_amount))
    grams_or_ml = amount.amount if amount is not None else None

    check_plausible(nutrition, grams_or_ml)

    entry = DraftEntry(
        food_id=source.food_id,
        serving_id=source.serving_id,
        number_of_units=units,
        entry_name=source.name,
    )
    repeated_from = RepeatedFrom(
        diary_day=source.diary_day,
        meal=source.meal,
    )

    return DraftItem(
        entry=entry,
        serving=amount_text(source, source_amount),
        amount=amount_text(source, amount),
        nutrition=nutrition,
        assumption=assumption,
        repeated_from=repeated_from,
    )


def _eaten_metric_amount(
    source: FoodEntry, source_amount: MetricAmount | None, eaten_amount: float | None
) -> MetricAmount | None:
    """The amount eaten this time in grams or ml; None when the source's weight is unknown."""
    if eaten_amount is None:
        return source_amount

    if source_amount is None:
        raise InvalidDraftItem(
            f"The weight of {source.name} ({source.amount}) is unknown, so it can only be repeated in "
            "its own amount. Omit metric_amount, or search the catalog for it in grams."
        )

    return MetricAmount(
        amount=eaten_amount,
        unit=source_amount.unit,
    )


def _ratio(amount: MetricAmount | None, source_amount: MetricAmount | None) -> float:
    if amount is None or source_amount is None:
        return 1

    return amount.amount / source_amount.amount
