"""draft_entries: show proposed Food Entries before the User confirms anything."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import SecretStr

from fatsecret_mcp.config.models import User
from fatsecret_mcp.domain.diary_day import Clock, today
from fatsecret_mcp.domain.draft_token import seal_draft
from fatsecret_mcp.domain.drafts import (
    InvalidDraftItem,
    UnlikelyDiaryDay,
    check_diary_day,
    draft_expiry,
    draft_item,
    draft_totals,
)
from fatsecret_mcp.domain.entry_amounts import (
    amount_text,
    has_bare_number_amount,
    serving_metric_amount,
    written_metric_amount,
)
from fatsecret_mcp.domain.entry_changes import (
    REMOVAL_NOTE,
    changed_days,
    check_changed_day,
    check_one_change_per_entry,
    entries_after,
    proposed_edit,
    proposed_removal,
)
from fatsecret_mcp.domain.meal_suggestion import ask_about_previous_day, suggest_meal
from fatsecret_mcp.domain.models import (
    Additions,
    Draft,
    DraftChange,
    DraftItem,
    EntryRemoval,
    Food,
    FoodEntry,
    Meal,
    MetricAmount,
    Nutrition,
    Serving,
)
from fatsecret_mcp.domain.repeated_entries import check_source_day, entry_on_day, repeated_item
from fatsecret_mcp.domain.stand_ins import check_label, stand_in_item
from fatsecret_mcp.fatsecret.catalog import loggable_foods
from fatsecret_mcp.fatsecret.diary import UserDiary
from fatsecret_mcp.fatsecret.errors import FatSecretError, NotFound
from fatsecret_mcp.fatsecret.food_cache import FoodCache
from fatsecret_mcp.fatsecret.gateway import FatSecretGateway
from fatsecret_mcp.tools.schemas import (
    MAX_DRAFT_ID_LENGTH,
    Amounts,
    DayAfter,
    DiaryDayOrToday,
    DiaryEntryDraftInput,
    DraftItems,
    EntryChangeDraftInput,
    FoodDraft,
    FoodDraftInput,
    NutritionLabelDraftInput,
    OtherDayAfter,
    ProposedChange,
    ProposedEntry,
    as_tool_result,
)

# The entries of each Diary Day that a Draft's items repeat from or change.
DiaryDays = dict[date, list[FoodEntry]]
AdditionInput = FoodDraftInput | NutritionLabelDraftInput | DiaryEntryDraftInput


def register(
    server: MCPServer,
    user: User,
    fatsecret: FatSecretGateway,
    food_cache: FoodCache,
    diary: UserDiary,
    clock: Clock,
    signing_key: SecretStr,
) -> None:
    annotations = ToolAnnotations(
        title="Draft Entries",
        read_only_hint=True,
    )

    @server.tool(annotations=annotations)
    async def draft_entries(
        items: DraftItems, meal: Meal | None = None, diary_day: DiaryDayOrToday = None
    ) -> Annotated[CallToolResult, FoodDraft]:
        """Propose new Food Entries, and changes to logged ones, without writing to the Food Diary.

        Show the Draft to the User.

        A Food item uses Food and Serving ids from search_foods. number_of_units is the FatSecret unit
        count, not necessarily grams: scale it from the Serving's number_of_units. Name it with
        food_entry_name the way the User said it, in their language, followed by a short note in
        the same language that it was matched in the US catalog, e.g. "Kajzerka (katalog USA)" or
        "Kaiser roll (US catalog)". The Draft shows the catalog's name beside it. A Serving of known
        weight, such as "1 roll (57 g)", is logged as that weight in the Food's Serving in grams or
        ml when it has one, so the Draft may show that Serving instead.

        A Nutrition Label item logs a branded product missing from the catalog as a Stand-in: the
        closest catalog Food for search_phrase, in the amount whose kcal match the label, under the
        product's name. Ask for a photo of the Nutrition Label unless the User already gave its values.
        For a product logged before as a Stand-in,
        reuse its label from this conversation or your memory, and ask again otherwise.
        Estimate the label, with estimated=true, only when the User has no packaging. Show the
        label values, the logged values and the difference. If the User dislikes the Stand-in,
        draft again with a different search_phrase.

        A Diary Entry item logs a Food Entry from the Food Diary again, under its own name, with its
        Food, Serving and nutrition, so no catalog search is needed. For "the same as yesterday's
        breakfast", read that Diary Day with get_diary_day, list its Food Entries with their amounts
        to the User and ask whether everything was the same, or what to leave out or eat more or less
        of. Then draft the chosen entries by diary_day and food_entry_id, with metric_amount for a
        changed amount in grams or ml.

        A change item changes or removes a Food Entry already in the Food Diary, by diary_day and
        food_entry_id: change="remove", or change="edit" with a new metric_amount in grams or ml,
        a meal on the same Diary Day or a food_entry_name. For "undo that" after a confirm_draft,
        remove the food_entry_ids it returned; undoing a change is a change back. For any other
        change, read the Diary Day with get_diary_day first, name the entries to change,
        and ask when it's unclear which one is meant. The Food or Diary Day of an entry can't change:
        for "chicken thigh, not breast", remove the entry and add a Food item; for
        "that was yesterday", remove it and add a Diary Entry item with the same food_entry_id, in
        one Draft whose diary_day is yesterday. Show each change before and after, and the
        removal_note when there is one.

        A Draft holds at most 20 items: split a larger meal into several Drafts.

        Include assumptions about estimated amounts. When the Draft has new items, always
        ask the User whether the suggested Meal is correct. If previous_day_suggested is true, ask whether to
        use Snacks on the previous Diary Day instead. meal and diary_day are for new items only. A
        correction needs a new Draft with the explicit Meal or Diary Day.
        Nothing is written by this tool; a Draft expires after thirty minutes.
        """
        now = clock()
        user_today = today(now, user.timezone)
        additions_input, changes_input = _additions_and_changes(items)
        has_additions = bool(additions_input)

        if not has_additions and (meal is not None or diary_day is not None):
            raise ToolError(
                "meal and diary_day are for new Food Entries. To move an entry, set meal on its change item."
            )

        day = diary_day or user_today
        meal_suggested = has_additions and meal is None
        previous_day_suggested = (
            meal_suggested and diary_day is None and ask_about_previous_day(now, user.timezone)
        )
        prepared: list[DraftItem] = []
        changes: list[DraftChange] = []

        try:
            if has_additions:
                check_diary_day(day, user_today)

            changed_ids = [item.food_entry_id for item in changes_input]

            check_one_change_per_entry(changed_ids)

            diary_days = await _diary_days(items, diary, user_today)

            for item in additions_input:
                prepared.append(await _prepared_item(item, fatsecret, food_cache, diary_days))

            for item in changes_input:
                changes.append(await _prepared_change(item, fatsecret, food_cache, diary_days))

            if has_additions and day not in diary_days:
                diary_days[day] = await diary.entries(day)
        except (FatSecretError, InvalidDraftItem, UnlikelyDiaryDay) as error:
            raise ToolError(str(error)) from error

        additions = None

        if has_additions:
            selected_meal = meal or suggest_meal(now, user.timezone, user.meal_hours.as_starts())
            entries_to_seal = [item.entry for item in prepared]
            additions = Additions(
                entries=tuple(entries_to_seal),
                meal=selected_meal,
                diary_day=day,
            )

        sealed_changes = [change.change for change in changes]
        sealed = Draft(
            user_id=user.id,
            additions=additions,
            changes=tuple(sealed_changes),
            expires_at=draft_expiry(now),
        )
        draft_id = seal_draft(sealed, signing_key.get_secret_value())

        # confirm_draft would refuse a longer id, so the User couldn't confirm this Draft.
        if len(draft_id) > MAX_DRAFT_ID_LENGTH:
            raise ToolError("These Food names are too long for one Draft. Split it into several Drafts.")

        daily_targets = user.daily_targets.as_nutrition()
        added = draft_totals([], prepared, daily_targets).added
        day_after = None

        if additions is not None:
            day_entries = entries_after(diary_days[day], changes)
            day_after = DayAfter.of(draft_totals(day_entries, prepared, daily_targets))

        proposed_entries = [ProposedEntry.of(item) for item in prepared]
        proposed_changes = [ProposedChange.of(change) for change in changes]
        result = FoodDraft(
            draft_id=draft_id,
            expires_at=sealed.expires_at,
            meal=additions.meal if additions is not None else None,
            meal_suggested=meal_suggested,
            diary_day=additions.diary_day if additions is not None else None,
            previous_day_suggested=previous_day_suggested,
            items=proposed_entries,
            changes=proposed_changes,
            totals=Amounts.of(added),
            day_after=day_after,
            other_days_after=_other_days_after(changes, diary_days, additions, daily_targets),
            removal_note=_removal_note(changes),
        )

        return as_tool_result(result)


def _additions_and_changes(items: DraftItems) -> tuple[list[AdditionInput], list[EntryChangeDraftInput]]:
    """The items that add Food Entries, and the ones that change or remove logged entries."""
    additions: list[AdditionInput] = []
    changes: list[EntryChangeDraftInput] = []

    for item in items:
        if isinstance(item, EntryChangeDraftInput):
            changes.append(item)
        else:
            additions.append(item)

    return additions, changes


async def _diary_days(items: DraftItems, diary: UserDiary, user_today: date) -> DiaryDays:
    """The entries of each Diary Day that items repeat from or change, each day read once.

    Every such day is checked before the first read, so a refused one costs no request.
    """
    repeated_days = [item.diary_day for item in items if isinstance(item, DiaryEntryDraftInput)]
    changed = [item.diary_day for item in items if isinstance(item, EntryChangeDraftInput)]
    days = list(dict.fromkeys(repeated_days + changed))
    entries: DiaryDays = {}

    for day in repeated_days:
        check_source_day(day, user_today)

    for day in changed:
        check_changed_day(day, user_today)

    for day in days:
        entries[day] = await diary.entries(day)

    return entries


def _other_days_after(
    changes: list[DraftChange],
    diary_days: DiaryDays,
    additions: Additions | None,
    daily_targets: Nutrition,
) -> list[OtherDayAfter]:
    """Each Diary Day the changes are on, besides the new items' own day, once they're made."""
    days_after: list[OtherDayAfter] = []

    for day in changed_days(changes):
        if additions is not None and day == additions.diary_day:
            continue

        entries = entries_after(diary_days[day], changes)
        totals = draft_totals(entries, [], daily_targets)

        days_after.append(OtherDayAfter.on(day, totals))

    return days_after


def _removal_note(changes: list[DraftChange]) -> str | None:
    removals = [change for change in changes if isinstance(change.change, EntryRemoval)]

    return REMOVAL_NOTE if removals else None


async def _prepared_item(
    item: AdditionInput,
    fatsecret: FatSecretGateway,
    food_cache: FoodCache,
    diary_days: DiaryDays,
) -> DraftItem:
    if isinstance(item, NutritionLabelDraftInput):
        return await _stand_in(item, fatsecret, food_cache)

    if isinstance(item, DiaryEntryDraftInput):
        return await _repeated(item, fatsecret, food_cache, diary_days)

    return await _food_item(item, fatsecret, food_cache)


async def _food_item(item: FoodDraftInput, fatsecret: FatSecretGateway, food_cache: FoodCache) -> DraftItem:
    """A catalog Food under the User's name for it, or under the catalog's name when none was given."""
    catalog_food, serving = await _resolve_serving(item, fatsecret, food_cache)
    name = item.food_entry_name or catalog_food.name
    drafted = draft_item(catalog_food, name, serving, item.number_of_units, item.assumption)

    if name == catalog_food.name:
        return drafted

    return replace(drafted, catalog_name=catalog_food.name)


async def _repeated(
    item: DiaryEntryDraftInput,
    fatsecret: FatSecretGateway,
    food_cache: FoodCache,
    diary_days: DiaryDays,
) -> DraftItem:
    source = entry_on_day(diary_days[item.diary_day], item.food_entry_id, item.diary_day)
    source_amount = await _metric_amount(source, fatsecret, food_cache)

    return repeated_item(
        source=source,
        source_amount=source_amount,
        eaten_amount=item.metric_amount,
        assumption=item.assumption,
    )


async def _prepared_change(
    item: EntryChangeDraftInput,
    fatsecret: FatSecretGateway,
    food_cache: FoodCache,
    diary_days: DiaryDays,
) -> DraftChange:
    entry = entry_on_day(diary_days[item.diary_day], item.food_entry_id, item.diary_day)
    bare_number = has_bare_number_amount(entry)

    # Only a new amount, or an amount shown as a bare number, needs the entry's weight.
    entry_amount = None

    if item.metric_amount is not None or bare_number:
        entry_amount = await _metric_amount(entry, fatsecret, food_cache)

    if bare_number:
        entry = replace(entry, amount=amount_text(entry, entry_amount))

    if item.change == "remove":
        return proposed_removal(entry)

    return proposed_edit(
        entry,
        entry_amount,
        metric_amount=item.metric_amount,
        meal=item.meal,
        name=item.food_entry_name,
    )


async def _metric_amount(
    entry: FoodEntry, fatsecret: FatSecretGateway, food_cache: FoodCache
) -> MetricAmount | None:
    """The entry's amount in grams or ml; None when its weight is unknown."""
    written = written_metric_amount(entry)

    # Only an amount not written in grams or ml costs a lookup of its Serving's weight.
    if written is not None:
        return written

    return await _looked_up_metric_amount(entry, fatsecret, food_cache)


async def _looked_up_metric_amount(
    source: FoodEntry, fatsecret: FatSecretGateway, food_cache: FoodCache
) -> MetricAmount | None:
    """The source's amount from its catalog Serving's weight; None when the catalog can't tell."""
    try:
        catalog_food = await food_cache.get_food(source.food_id, fatsecret)
    except NotFound:
        return None

    serving = _serving_of(catalog_food, source.serving_id)

    if serving is None:
        return None

    return serving_metric_amount(source, serving)


async def _stand_in(
    item: NutritionLabelDraftInput, fatsecret: FatSecretGateway, food_cache: FoodCache
) -> DraftItem:
    label = item.nutrition_label.as_label()

    # An implausible label is rejected before it costs any catalog requests.
    check_label(label)

    foods = await loggable_foods(fatsecret, food_cache, item.search_phrase)

    return stand_in_item(
        product_name=item.product_name,
        label=label,
        grams=item.grams,
        foods=foods,
        assumption=item.assumption,
    )


async def _resolve_serving(
    item: FoodDraftInput, fatsecret: FatSecretGateway, food_cache: FoodCache
) -> tuple[Food, Serving]:
    if item.serving_id == "0":
        raise ToolError("Serving 0 is derived and cannot be logged. Choose a Serving from search_foods.")

    try:
        catalog_food = await food_cache.get_food(item.food_id, fatsecret)
    except NotFound:
        raise ToolError("Unknown Food: use the ids of a match from search_foods.") from None

    serving = _serving_of(catalog_food, item.serving_id)

    if serving is None:
        raise ToolError("Serving does not belong to this Food.")

    return catalog_food, serving


def _serving_of(food: Food, serving_id: str) -> Serving | None:
    servings = [serving for serving in food.servings if serving.id == serving_id]

    return servings[0] if servings else None
