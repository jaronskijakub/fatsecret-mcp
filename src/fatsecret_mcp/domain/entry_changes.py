"""Changes to Food Entries in the Food Diary: a new amount, Meal or name, or their removal.

The entry's current values come from the Food Diary, never from Claude. A new amount in grams or ml
scales its FatSecret units and nutrition alike, as a repeated entry's does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from fatsecret_mcp.domain.drafts import MAX_DAYS_BACK, InvalidDraftItem, UnlikelyDiaryDay, check_plausible
from fatsecret_mcp.domain.entry_amounts import amount_text, positive, scaled_units
from fatsecret_mcp.domain.models import DraftChange, EntryEdit, EntryRemoval, FoodEntry, Meal, MetricAmount

REMOVAL_NOTE = "A removed Food Entry can't be restored, only logged again."


def check_changed_day(diary_day: date, today: date) -> None:
    """Allow changes from today back to a year ago."""
    days_back = (today - diary_day).days

    if days_back < 0:
        raise UnlikelyDiaryDay("Only Food Entries from today or an earlier Diary Day can be changed.")

    if days_back > MAX_DAYS_BACK:
        raise UnlikelyDiaryDay("Only Food Entries from at most a year back can be changed.")


def check_one_change_per_entry(food_entry_ids: Sequence[str]) -> None:
    """Refuse two change items for one Food Entry, whose order FatSecret would decide."""
    repeated = _repeated_ids(food_entry_ids)

    if repeated:
        raise InvalidDraftItem(
            f"Food Entry {repeated[0]} has more than one change item. Put all of its changes in one item."
        )


def proposed_removal(entry: FoodEntry) -> DraftChange:
    """The change taking `entry` out of the Food Diary."""
    change = EntryRemoval(
        food_entry_id=entry.id,
        diary_day=entry.diary_day,
    )

    return DraftChange(
        change=change,
        before=entry,
        after=None,
    )


def proposed_edit(
    entry: FoodEntry,
    entry_amount: MetricAmount | None,
    *,
    metric_amount: float | None,
    meal: Meal | None,
    name: str | None,
) -> DraftChange:
    """`entry` with a new amount in grams or ml, Meal or name; a value equal to the current one is kept.

    `entry_amount` is the entry's amount in grams or ml; None when its weight is unknown.
    """
    after = entry

    if metric_amount is not None:
        after = _with_amount(entry, entry_amount, metric_amount)

    if meal is not None:
        after = replace(after, meal=meal)

    if name is not None:
        after = replace(after, name=name)

    change = EntryEdit(
        food_entry_id=entry.id,
        diary_day=entry.diary_day,
        number_of_units=_changed(entry.number_of_units, after.number_of_units),
        meal=_changed(entry.meal, after.meal),
        entry_name=_changed(entry.name, after.name),
    )
    values = [change.number_of_units, change.meal, change.entry_name]

    if all(value is None for value in values):
        raise InvalidDraftItem(
            f"This change leaves {entry.name} as it is. Change something, or leave it out."
        )

    return DraftChange(
        change=change,
        before=entry,
        after=after,
    )


def entries_after(entries: Sequence[FoodEntry], changes: Sequence[DraftChange]) -> list[FoodEntry]:
    """A Diary Day's entries once the changes are made: changed ones replaced, removed ones left out."""
    changed = {change.before.id: change.after for change in changes}
    after: list[FoodEntry] = []

    for entry in entries:
        if entry.id not in changed:
            after.append(entry)
            continue

        changed_entry = changed[entry.id]

        if changed_entry is not None:
            after.append(changed_entry)

    return after


def changed_days(changes: Sequence[DraftChange]) -> list[date]:
    """The Diary Days the changes are on, each once, in the order of the changes."""
    days = [change.before.diary_day for change in changes]

    return list(dict.fromkeys(days))


def _with_amount(entry: FoodEntry, entry_amount: MetricAmount | None, metric_amount: float) -> FoodEntry:
    if entry_amount is None or not positive(entry.number_of_units):
        raise InvalidDraftItem(
            f"The weight of {entry.name} ({entry.amount}) is unknown, so its amount can't change in grams. "
            "Remove it and log the right amount as a new item instead."
        )

    units, nutrition = scaled_units(entry, metric_amount / entry_amount.amount)

    if units == entry.number_of_units:
        return entry

    amount = MetricAmount(
        amount=metric_amount,
        unit=entry_amount.unit,
    )

    check_plausible(nutrition, metric_amount)

    return replace(
        entry,
        amount=amount_text(entry, amount),
        number_of_units=units,
        nutrition=nutrition,
    )


def _changed[T](before: T, after: T) -> T | None:
    """`after` when it differs from `before`, so an edit sends only what changes."""
    return after if after != before else None


def _repeated_ids(food_entry_ids: Sequence[str]) -> list[str]:
    counts = Counter(food_entry_ids)
    repeated = [food_entry_id for food_entry_id, count in counts.items() if count > 1]

    return repeated
