"""Food Diary and catalog concepts as data: Foods, Servings, Food Entries, and summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Meal(StrEnum):
    """The fixed slots a Diary Day is divided into, in the order they're shown."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACKS = "snacks"


@dataclass(frozen=True)
class Nutrition:
    """kcal plus Macros (protein, fat and carbohydrate, in grams)."""

    kcal: float = 0
    protein: float = 0
    fat: float = 0
    carbohydrate: float = 0

    def __add__(self, other: Nutrition) -> Nutrition:
        return Nutrition(
            kcal=self.kcal + other.kcal,
            protein=self.protein + other.protein,
            fat=self.fat + other.fat,
            carbohydrate=self.carbohydrate + other.carbohydrate,
        )

    def __sub__(self, other: Nutrition) -> Nutrition:
        return Nutrition(
            kcal=self.kcal - other.kcal,
            protein=self.protein - other.protein,
            fat=self.fat - other.fat,
            carbohydrate=self.carbohydrate - other.carbohydrate,
        )

    def scaled(self, factor: float) -> Nutrition:
        return Nutrition(
            kcal=self.kcal * factor,
            protein=self.protein * factor,
            fat=self.fat * factor,
            carbohydrate=self.carbohydrate * factor,
        )


@dataclass(frozen=True)
class FoodEntry:
    """One item recorded in a User's Food Diary."""

    id: str
    food_id: str
    serving_id: str
    name: str
    amount: str
    """The amount in its Serving, as FatSecret describes it: "150 g", "2 servings"."""
    number_of_units: float
    meal: Meal
    diary_day: date
    nutrition: Nutrition


@dataclass(frozen=True)
class CatalogFood:
    id: str
    name: str
    brand: str | None


@dataclass(frozen=True)
class Serving:
    id: str
    description: str
    metric_amount: float | None
    metric_unit: str | None
    number_of_units: float
    """FatSecret units represented by the description and nutrition (e.g. 100 for '100 g')."""
    nutrition: Nutrition


@dataclass(frozen=True)
class Food(CatalogFood):
    servings: list[Serving]


@dataclass(frozen=True)
class NutritionLabel:
    """A product's kcal and Macros per 100 g, read from its packaging or estimated by Claude."""

    per_100_g: Nutrition
    estimated: bool


@dataclass(frozen=True)
class DaySummary:
    diary_day: date
    meals: dict[Meal, list[FoodEntry]]
    """Only the Meals that have Food Entries, in the order of the day."""
    totals: Nutrition
    daily_targets: Nutrition
    remaining: Nutrition
    """Daily Targets minus totals; negative once a target is exceeded."""


@dataclass(frozen=True)
class PeriodDaySummary:
    diary_day: date
    has_food_entries: bool
    totals: Nutrition
    daily_targets: Nutrition


@dataclass(frozen=True)
class DraftEntry:
    """Only the values needed to write one confirmed Food Entry; no nutrition."""

    food_id: str
    serving_id: str
    number_of_units: float
    entry_name: str


@dataclass(frozen=True)
class EntryEdit:
    """Only the values needed to change one Food Entry in place; None leaves a value as it is."""

    food_entry_id: str
    diary_day: date
    number_of_units: float | None
    meal: Meal | None
    entry_name: str | None


@dataclass(frozen=True)
class EntryRemoval:
    food_entry_id: str
    diary_day: date


EntryChange = EntryEdit | EntryRemoval


@dataclass(frozen=True)
class Additions:
    """New Food Entries for one Meal of one Diary Day."""

    entries: tuple[DraftEntry, ...]
    meal: Meal
    diary_day: date


@dataclass(frozen=True)
class Draft:
    """The self-contained proposal that is signed and returned as a Draft id."""

    user_id: str
    additions: Additions | None
    """None when the Draft only changes or removes Food Entries."""
    changes: tuple[EntryChange, ...]
    expires_at: datetime


@dataclass(frozen=True)
class DraftTotals:
    """A Draft's Food Entries added up, and the Diary Day once they're added."""

    added: Nutrition
    consumed: Nutrition
    daily_targets: Nutrition
    remaining: Nutrition


@dataclass(frozen=True)
class DraftItem:
    """One proposed entry, with the values shown to the User before confirmation."""

    entry: DraftEntry
    serving: str
    amount: str
    nutrition: Nutrition
    assumption: str | None
    stand_in: StandIn | None = None
    """Set when the entry logs a catalog Food in place of a product missing from the catalog."""
    repeated_from: RepeatedFrom | None = None
    """Set when the entry logs a Food Entry from the Food Diary again."""


@dataclass(frozen=True)
class DraftChange:
    """One proposed change, with the Food Entry before and after, shown before confirmation."""

    change: EntryChange
    before: FoodEntry
    after: FoodEntry | None
    """The entry once changed; None when it's removed."""


@dataclass(frozen=True)
class RepeatedFrom:
    """Where in the Food Diary a repeated Food Entry was taken from."""

    diary_day: date
    meal: Meal


@dataclass(frozen=True)
class MetricAmount:
    """An amount in grams or millilitres."""

    amount: float
    unit: str


@dataclass(frozen=True)
class StandIn:
    """The catalog Food a Stand-in logs, beside its product's Nutrition Label for the grams eaten."""

    food_name: str
    grams_eaten: float
    label_for_grams_eaten: Nutrition
    """The product's Nutrition Label scaled from 100 g to the grams eaten."""
    estimated: bool


@dataclass(frozen=True)
class ConfirmedEntry:
    entry: DraftEntry
    food_entry_id: str | None
    """The created Food Entry's id; None when this item wasn't written."""


@dataclass(frozen=True)
class ConfirmedChange:
    change: EntryChange
    written: bool


@dataclass(frozen=True)
class Confirmation:
    """What confirming a Draft wrote to the Food Diary."""

    draft: Draft
    entries: tuple[ConfirmedEntry, ...]
    changes: tuple[ConfirmedChange, ...]
    failure: str | None
    """Why writing stopped part-way through; None when every item was written."""
