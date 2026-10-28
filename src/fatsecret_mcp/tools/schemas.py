"""The shapes of the tools' inputs and results, as Claude sees them.

The MCP SDK publishes them as each tool's input and output schema. Diary totals are rounded for
reading; the Servings search_foods finds retain their precision. The `of` methods turn domain objects
into these shapes.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

from fatsecret_mcp.domain.models import (
    Confirmation,
    ConfirmedChange,
    ConfirmedEntry,
    DaySummary,
    DraftChange,
    DraftItem,
    DraftTotals,
    EntryChange,
    EntryRemoval,
    Food,
    FoodEntry,
    Meal,
    Nutrition,
    NutritionLabel,
    PeriodDaySummary,
    RepeatedFrom,
    Serving,
    StandIn,
)

ATTRIBUTION = "Powered by fatsecret Platform API"

# --- Inputs ----------------------------------------------------------------

# Lists and text are bounded, so one tool call makes a bounded number of FatSecret requests.
MAX_DRAFT_ITEMS = 20
# A search description or its English rendering.
MAX_SEARCH_LENGTH = 200
# A product name or a Stand-in's search phrase.
MAX_NAME_LENGTH = 100
MAX_ASSUMPTION_LENGTH = 300
MAX_ID_LENGTH = 20
# Room for twenty items with long, non-ASCII catalog names, sealed and Base64-encoded.
MAX_DRAFT_ID_LENGTH = 32768

DiaryDayOrToday = Annotated[
    date | None,
    Field(description="The Diary Day, as YYYY-MM-DD. Omit for today in the User's timezone."),
]

PeriodStart = Annotated[date, Field(description="First Diary Day in the period, inclusive, as YYYY-MM-DD.")]
PeriodEnd = Annotated[date, Field(description="Last Diary Day in the period, inclusive, as YYYY-MM-DD.")]

FoodDescription = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_SEARCH_LENGTH,
        description="The description of one food as the User said it.",
    ),
]
EnglishRendering = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=MAX_SEARCH_LENGTH,
        description="English rendering for the catalog. Pass this for non-English input.",
    ),
]
CatalogId = Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
Assumption = Annotated[str | None, Field(max_length=MAX_ASSUMPTION_LENGTH)]


class FoodDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    food_id: CatalogId
    serving_id: CatalogId
    number_of_units: float = Field(
        gt=0,
        le=10000,
        allow_inf_nan=False,
    )
    food_entry_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description="The entry's name in the User's own words and language, with a short note that it "
        'was matched in the US catalog, e.g. "Kajzerka (katalog USA)". Omit to use the catalog Food\'s name.',
    )
    assumption: Assumption = None


class NutritionLabelValues(BaseModel):
    """kcal and Macros per 100 g of the product."""

    model_config = ConfigDict(extra="forbid")

    kcal: float = Field(gt=0, le=900, allow_inf_nan=False)
    protein: float = Field(ge=0, le=100, allow_inf_nan=False)
    fat: float = Field(ge=0, le=100, allow_inf_nan=False)
    carbohydrate: float = Field(ge=0, le=100, allow_inf_nan=False)
    estimated: bool = Field(description="True when estimated without the packaging, not read from it.")

    def as_label(self) -> NutritionLabel:
        per_100_g = Nutrition(
            kcal=self.kcal,
            protein=self.protein,
            fat=self.fat,
            carbohydrate=self.carbohydrate,
        )

        return NutritionLabel(per_100_g=per_100_g, estimated=self.estimated)


class NutritionLabelDraftInput(BaseModel):
    """A product missing from the catalog, logged as a Stand-in under its own name."""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description="The product's name, given to its Food Entry.",
    )
    nutrition_label: NutritionLabelValues
    grams: float = Field(
        gt=0,
        le=10000,
        allow_inf_nan=False,
        description="Grams of the product eaten.",
    )
    search_phrase: str = Field(
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description='English catalog search for a similar generic Food, e.g. "protein bar".',
    )
    assumption: Assumption = None


class DiaryEntryDraftInput(BaseModel):
    """A Food Entry from the Food Diary, logged again under its own name."""

    model_config = ConfigDict(extra="forbid")

    diary_day: date = Field(description="The Diary Day get_diary_day showed it on, as YYYY-MM-DD.")
    food_entry_id: CatalogId = Field(description="The food_entry_id get_diary_day showed.")
    metric_amount: float | None = Field(
        default=None,
        gt=0,
        le=10000,
        allow_inf_nan=False,
        description=(
            "Grams eaten this time, or ml for an entry measured in ml. Omit for the same amount; "
            "the Draft shows it in grams or ml when its weight is known."
        ),
    )
    assumption: Assumption = None


ChangeKind = Literal["edit", "remove"]


class EntryChangeDraftInput(BaseModel):
    """A change to a Food Entry in the Food Diary: its removal, or a new amount, Meal or name."""

    model_config = ConfigDict(extra="forbid")

    change: ChangeKind = Field(
        description="remove takes the entry out of the Food Diary; edit changes the values given."
    )
    diary_day: date = Field(description="The Diary Day get_diary_day showed it on, as YYYY-MM-DD.")
    food_entry_id: CatalogId = Field(description="The food_entry_id get_diary_day or confirm_draft showed.")
    metric_amount: float | None = Field(
        default=None,
        gt=0,
        le=10000,
        allow_inf_nan=False,
        description="The new amount in grams, or ml for an entry measured in ml.",
    )
    meal: Meal | None = Field(default=None, description="The Meal to move the entry to, on its Diary Day.")
    food_entry_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description="A new name for the entry.",
    )

    @model_validator(mode="after")
    def _check_values(self) -> EntryChangeDraftInput:
        values = [self.metric_amount, self.meal, self.food_entry_name]
        given = [value for value in values if value is not None]

        if self.change == "remove" and given:
            raise ValueError("A removal takes no metric_amount, meal or food_entry_name.")
        if self.change == "edit" and not given:
            raise ValueError("An edit needs a new metric_amount, meal or food_entry_name.")

        return self


# Fields only a Nutrition Label item has, so one missing its label is still told what it lacks.
NUTRITION_LABEL_ITEM_FIELDS = {"product_name", "nutrition_label", "grams", "search_phrase"}


def _draft_item_kind(item: Any) -> str:
    """Tells the four kinds of item apart by the fields only one kind of item has."""
    if isinstance(item, NutritionLabelDraftInput):
        return "nutrition_label"

    if isinstance(item, EntryChangeDraftInput):
        return "entry_change"

    if isinstance(item, DiaryEntryDraftInput):
        return "diary_entry"

    if isinstance(item, dict):
        present = [field in item for field in NUTRITION_LABEL_ITEM_FIELDS]

        if any(present):
            return "nutrition_label"

        if "change" in item:
            return "entry_change"

        if "food_entry_id" in item:
            return "diary_entry"

    return "food"


DraftItemInput = Annotated[
    Annotated[FoodDraftInput, Tag("food")]
    | Annotated[NutritionLabelDraftInput, Tag("nutrition_label")]
    | Annotated[DiaryEntryDraftInput, Tag("diary_entry")]
    | Annotated[EntryChangeDraftInput, Tag("entry_change")],
    Discriminator(_draft_item_kind),
]

DraftItems = Annotated[
    list[DraftItemInput],
    Field(
        min_length=1,
        max_length=MAX_DRAFT_ITEMS,
        description=f"At most {MAX_DRAFT_ITEMS} items. Split a larger meal into several Drafts.",
    ),
]

DraftId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_DRAFT_ID_LENGTH,
        description="The draft_id returned by draft_entries.",
    ),
]


# --- Results ---------------------------------------------------------------


class Amounts(BaseModel):
    """kcal and Macros (grams)."""

    kcal: int
    protein: float
    fat: float
    carbohydrate: float

    @classmethod
    def of(cls, nutrition: Nutrition) -> Amounts:
        return cls(
            kcal=round(nutrition.kcal),
            protein=_rounded(nutrition.protein),
            fat=_rounded(nutrition.fat),
            carbohydrate=_rounded(nutrition.carbohydrate),
        )


class DiaryEntry(BaseModel):
    food_entry_id: str = Field(
        description="Pass it to draft_entries to log this Food Entry again, change or remove it."
    )
    name: str
    amount: str = Field(description='The amount in its Serving, e.g. "150 g" or "2 servings".')
    kcal: int
    protein: float
    fat: float
    carbohydrate: float

    @classmethod
    def of(cls, entry: FoodEntry) -> DiaryEntry:
        amounts = Amounts.of(entry.nutrition)

        return cls(
            food_entry_id=entry.id,
            name=entry.name,
            amount=entry.amount,
            kcal=amounts.kcal,
            protein=amounts.protein,
            fat=amounts.fat,
            carbohydrate=amounts.carbohydrate,
        )


class MealEntries(BaseModel):
    meal: Meal
    food_entries: list[DiaryEntry]

    @classmethod
    def of(cls, meal: Meal, entries: list[FoodEntry]) -> MealEntries:
        food_entries = [DiaryEntry.of(entry) for entry in entries]

        return cls(meal=meal, food_entries=food_entries)


class DiaryDay(BaseModel):
    diary_day: date
    meals: list[MealEntries] = Field(description="Meals that have Food Entries, in the order of the day.")
    totals: Amounts
    daily_targets: Amounts
    remaining: Amounts = Field(description="Daily Targets minus totals; negative once a target is exceeded.")

    @classmethod
    def of(cls, summary: DaySummary) -> DiaryDay:
        meals = [MealEntries.of(meal, entries) for meal, entries in summary.meals.items()]

        return cls(
            diary_day=summary.diary_day,
            meals=meals,
            totals=Amounts.of(summary.totals),
            daily_targets=Amounts.of(summary.daily_targets),
            remaining=Amounts.of(summary.remaining),
        )


class PeriodDay(BaseModel):
    diary_day: date
    has_food_entries: bool = Field(description="False when the Diary Day has no Food Entries.")
    totals: Amounts
    daily_targets: Amounts

    @classmethod
    def of(cls, summary: PeriodDaySummary) -> PeriodDay:
        return cls(
            diary_day=summary.diary_day,
            has_food_entries=summary.has_food_entries,
            totals=Amounts.of(summary.totals),
            daily_targets=Amounts.of(summary.daily_targets),
        )


class PeriodSummary(BaseModel):
    from_day: date
    to_day: date
    days: list[PeriodDay] = Field(description="Every Diary Day in the period, in chronological order.")


class FoodServing(BaseModel):
    serving_id: str
    description: str
    metric_amount: float | None
    metric_unit: str | None
    number_of_units: float = Field(
        description="FatSecret units in this described Serving, e.g. 100 for '100 g'."
    )
    kcal: float
    protein: float
    fat: float
    carbohydrate: float

    @classmethod
    def of(cls, serving: Serving) -> FoodServing:
        return cls(
            serving_id=serving.id,
            description=serving.description,
            metric_amount=serving.metric_amount,
            metric_unit=serving.metric_unit,
            number_of_units=serving.number_of_units,
            kcal=serving.nutrition.kcal,
            protein=serving.nutrition.protein,
            fat=serving.nutrition.fat,
            carbohydrate=serving.nutrition.carbohydrate,
        )


class FoodMatch(BaseModel):
    food_id: str
    name: str
    brand: str | None
    servings: list[FoodServing] = Field(
        description="Loggable Servings, with kcal and Macros per described Serving."
    )

    @classmethod
    def of(cls, food: Food) -> FoodMatch:
        food_servings = [FoodServing.of(serving) for serving in food.servings]

        return cls(
            food_id=food.id,
            name=food.name,
            brand=food.brand,
            servings=food_servings,
        )


class FoodSearch(BaseModel):
    status: Literal["found", "not_found"]
    matches: list[FoodMatch]
    attribution: str = ATTRIBUTION


class StandInDetails(BaseModel):
    food_name: str = Field(description="The catalog Food logged in the product's place.")
    grams_eaten: float
    nutrition_label: Amounts = Field(description="The product's Nutrition Label for the grams eaten.")
    difference: Amounts = Field(description="The logged values minus the Nutrition Label's.")
    estimated: bool = Field(description="True when the Nutrition Label was estimated, not read from it.")

    @classmethod
    def of(cls, stand_in: StandIn, logged: Nutrition) -> StandInDetails:
        return cls(
            food_name=stand_in.food_name,
            grams_eaten=stand_in.grams_eaten,
            nutrition_label=Amounts.of(stand_in.label_for_grams_eaten),
            difference=Amounts.of(logged - stand_in.label_for_grams_eaten),
            estimated=stand_in.estimated,
        )


class RepeatedFromDetails(BaseModel):
    diary_day: date
    meal: Meal

    @classmethod
    def of(cls, repeated_from: RepeatedFrom) -> RepeatedFromDetails:
        return cls(
            diary_day=repeated_from.diary_day,
            meal=repeated_from.meal,
        )


class ProposedEntry(BaseModel):
    food_id: str
    serving_id: str
    name: str
    serving: str
    number_of_units: float
    amount: str
    kcal: int
    protein: float
    fat: float
    carbohydrate: float
    assumption: str | None
    catalog_name: str | None = Field(
        description="The catalog Food's name, set when the entry is named in the User's own words."
    )
    stand_in: StandInDetails | None = Field(
        description="Set when the entry is a Stand-in for a product missing from the catalog."
    )
    repeated_from: RepeatedFromDetails | None = Field(
        description="Set when the entry repeats a Food Entry from the Food Diary, not a new match."
    )

    @classmethod
    def of(cls, item: DraftItem) -> ProposedEntry:
        amounts = Amounts.of(item.nutrition)
        stand_in = _stand_in_details(item)
        repeated_from = _repeated_from_details(item)

        return cls(
            food_id=item.entry.food_id,
            serving_id=item.entry.serving_id,
            name=item.entry.entry_name,
            serving=item.serving,
            number_of_units=item.entry.number_of_units,
            amount=item.amount,
            kcal=amounts.kcal,
            protein=amounts.protein,
            fat=amounts.fat,
            carbohydrate=amounts.carbohydrate,
            assumption=item.assumption,
            catalog_name=item.catalog_name,
            stand_in=stand_in,
            repeated_from=repeated_from,
        )


class EntryValues(BaseModel):
    name: str
    amount: str
    meal: Meal
    kcal: int
    protein: float
    fat: float
    carbohydrate: float

    @classmethod
    def of(cls, entry: FoodEntry) -> EntryValues:
        amounts = Amounts.of(entry.nutrition)

        return cls(
            name=entry.name,
            amount=entry.amount,
            meal=entry.meal,
            kcal=amounts.kcal,
            protein=amounts.protein,
            fat=amounts.fat,
            carbohydrate=amounts.carbohydrate,
        )


class ProposedChange(BaseModel):
    food_entry_id: str
    diary_day: date
    change: ChangeKind
    before: EntryValues
    after: EntryValues | None = Field(description="The entry once changed; null when it's removed.")

    @classmethod
    def of(cls, change: DraftChange) -> ProposedChange:
        after = EntryValues.of(change.after) if change.after is not None else None

        return cls(
            food_entry_id=change.before.id,
            diary_day=change.before.diary_day,
            change=_change_kind(change.change),
            before=EntryValues.of(change.before),
            after=after,
        )


class DayAfter(BaseModel):
    consumed: Amounts
    daily_targets: Amounts
    remaining: Amounts

    @classmethod
    def of(cls, totals: DraftTotals) -> DayAfter:
        return cls(
            consumed=Amounts.of(totals.consumed),
            daily_targets=Amounts.of(totals.daily_targets),
            remaining=Amounts.of(totals.remaining),
        )


class OtherDayAfter(DayAfter):
    diary_day: date

    @classmethod
    def on(cls, diary_day: date, totals: DraftTotals) -> OtherDayAfter:
        return cls(
            diary_day=diary_day,
            consumed=Amounts.of(totals.consumed),
            daily_targets=Amounts.of(totals.daily_targets),
            remaining=Amounts.of(totals.remaining),
        )


class FoodDraft(BaseModel):
    draft_id: str
    expires_at: datetime
    meal: Meal | None = Field(description="The Meal of the new items; null when the Draft only has changes.")
    meal_suggested: bool
    diary_day: date | None = Field(
        description="The Diary Day of the new items; null when the Draft only has changes."
    )
    previous_day_suggested: bool
    items: list[ProposedEntry] = Field(description="New Food Entries.")
    changes: list[ProposedChange] = Field(description="Changes to Food Entries, each before and after.")
    totals: Amounts = Field(description="The new items added up.")
    day_after: DayAfter | None = Field(
        description="The new items' Diary Day after the whole Draft; null when the Draft only has changes."
    )
    other_days_after: list[OtherDayAfter] = Field(
        description="Each other Diary Day the changes are on, after the whole Draft."
    )
    removal_note: str | None = Field(description="Set when the Draft removes a Food Entry; tell the User.")
    attribution: str = ATTRIBUTION


class ConfirmedFoodEntry(BaseModel):
    name: str
    number_of_units: float
    food_entry_id: str | None = Field(description="The created Food Entry; null when it wasn't written.")

    @classmethod
    def of(cls, confirmed: ConfirmedEntry) -> ConfirmedFoodEntry:
        return cls(
            name=confirmed.entry.entry_name,
            number_of_units=confirmed.entry.number_of_units,
            food_entry_id=confirmed.food_entry_id,
        )


class ConfirmedFoodEntryChange(BaseModel):
    food_entry_id: str
    diary_day: date
    change: ChangeKind
    written: bool = Field(description="False when writing stopped before this change.")

    @classmethod
    def of(cls, confirmed: ConfirmedChange) -> ConfirmedFoodEntryChange:
        return cls(
            food_entry_id=confirmed.change.food_entry_id,
            diary_day=confirmed.change.diary_day,
            change=_change_kind(confirmed.change),
            written=confirmed.written,
        )


class DraftConfirmation(BaseModel):
    status: Literal["confirmed", "partially_written", "already_confirmed"] = Field(
        description=(
            "partially_written: writing stopped at an item without a food_entry_id or a change not "
            "written. already_confirmed: an earlier call wrote this Draft; nothing new was written."
        )
    )
    meal: Meal | None = Field(
        description="The Meal of the new entries; null when the Draft only has changes."
    )
    diary_day: date | None = Field(
        description="The Diary Day of the new entries; null when the Draft only has changes."
    )
    entries: list[ConfirmedFoodEntry] = Field(description="New Food Entries, written first.")
    changes: list[ConfirmedFoodEntryChange] = Field(description="Changes, written after the new entries.")
    failure: str | None = Field(description="Why writing stopped part-way through.")
    attribution: str = ATTRIBUTION

    @classmethod
    def of(cls, confirmation: Confirmation, *, repeated: bool) -> DraftConfirmation:
        entries = [ConfirmedFoodEntry.of(confirmed) for confirmed in confirmation.entries]
        changes = [ConfirmedFoodEntryChange.of(confirmed) for confirmed in confirmation.changes]
        status = _confirmation_status(confirmation, repeated=repeated)
        additions = confirmation.draft.additions

        return cls(
            status=status,
            meal=additions.meal if additions is not None else None,
            diary_day=additions.diary_day if additions is not None else None,
            entries=entries,
            changes=changes,
            failure=confirmation.failure,
        )


def as_tool_result(result: BaseModel) -> CallToolResult:
    """`result` as the tool's answer: structured, plus the same JSON as compact text for the model.

    The SDK's own text copy is indented, which costs about 60% more tokens and carries nothing more.
    """
    structured = result.model_dump(mode="json")
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))

    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
    )


def _stand_in_details(item: DraftItem) -> StandInDetails | None:
    if item.stand_in is None:
        return None

    return StandInDetails.of(item.stand_in, item.nutrition)


def _repeated_from_details(item: DraftItem) -> RepeatedFromDetails | None:
    if item.repeated_from is None:
        return None

    return RepeatedFromDetails.of(item.repeated_from)


def _change_kind(change: EntryChange) -> ChangeKind:
    return "remove" if isinstance(change, EntryRemoval) else "edit"


def _confirmation_status(
    confirmation: Confirmation, *, repeated: bool
) -> Literal["confirmed", "partially_written", "already_confirmed"]:
    if repeated:
        return "already_confirmed"
    if confirmation.failure is not None:
        return "partially_written"

    return "confirmed"


def _rounded(grams: float) -> float:
    # Adding 0.0 turns -0.0 into 0.0.
    return round(grams, 1) + 0.0
