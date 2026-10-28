"""Changes through MCP: "undo that", "it was 200 g", "move it to dinner", drafted, then confirmed."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from mcp import Client

from fatsecret_mcp.config.models import Household
from fatsecret_mcp.domain.models import Food, FoodEntry, Meal, Nutrition, Serving
from fatsecret_mcp.server import INSTRUCTIONS
from tests.fatsecret.fake import FakeFatSecret, food_entry
from tests.tools.test_draft_entries import decode_base64url, draft

pytestmark = pytest.mark.anyio
Connect = Callable[[str], Client]
TODAY = date(
    year=2026,
    month=10,
    day=14,
)
YESTERDAY = date(
    year=2026,
    month=10,
    day=13,
)
TWO_DAYS_AGO = date(
    year=2026,
    month=10,
    day=12,
)


@pytest.fixture
def thigh(fatsecret: FakeFatSecret) -> Food:
    serving = Serving(
        id="thigh-100g",
        description="100 g",
        metric_amount=100,
        metric_unit="g",
        number_of_units=100,
        nutrition=Nutrition(
            kcal=209,
            protein=26,
            fat=10.9,
            carbohydrate=0,
        ),
    )
    food = Food(
        id="thigh",
        name="Chicken Thigh",
        brand=None,
        servings=[serving],
    )

    fatsecret.add_food(food)

    return food


@pytest.fixture
def lunch(fatsecret: FakeFatSecret, household: Household) -> dict[str, FoodEntry]:
    """Yesterday's lunch: chicken breast in grams, a Brand yoghurt, bread in slices, a package of cheese.

    The bread's slice weight is in the catalog; the cheese was logged in the FatSecret app, so its
    weight is unknown.
    """
    slice_serving = Serving(
        id="rye-slice",
        description="1 slice",
        metric_amount=35,
        metric_unit="g",
        number_of_units=1,
        nutrition=Nutrition(
            kcal=90,
            protein=3,
            fat=1,
            carbohydrate=17,
        ),
    )
    rye_bread = Food(
        id="rye-bread",
        name="Rye Bread",
        brand=None,
        servings=[slice_serving],
    )
    breast = food_entry(
        "Pierś z kurczaka",
        Meal.LUNCH,
        YESTERDAY,
        kcal=248,
        protein=46.5,
        fat=5.4,
        amount="150 g",
        food_id="chicken-breast",
        serving_id="breast-100g",
        number_of_units=150,
    )
    yoghurt = food_entry(
        "Jogurt naturalny Piątnica",
        Meal.LUNCH,
        YESTERDAY,
        kcal=90,
        protein=6,
        fat=3,
        carbohydrate=9,
        amount="150 g",
        food_id="piatnica-yoghurt",
        serving_id="piatnica-100g",
        number_of_units=1.5,
    )
    bread = food_entry(
        "Chleb żytni",
        Meal.LUNCH,
        YESTERDAY,
        kcal=180,
        protein=6,
        fat=2,
        carbohydrate=34,
        amount="2 slices",
        food_id="rye-bread",
        serving_id="rye-slice",
        number_of_units=2,
    )
    cheese = food_entry(
        "Serek wiejski Piątnica",
        Meal.LUNCH,
        YESTERDAY,
        kcal=194,
        protein=22,
        fat=10,
        carbohydrate=4,
        amount="1 package",
        food_id="piatnica-cottage-cheese",
        serving_id="piatnica-package",
        number_of_units=1,
    )
    entries = {
        "breast": breast,
        "yoghurt": yoghurt,
        "bread": bread,
        "cheese": cheese,
    }

    fatsecret.add_food(rye_bread)

    for entry in entries.values():
        fatsecret.add_food_entry(household.user("jakub").fatsecret, entry)

    return entries


def remove(entry: FoodEntry) -> dict[str, Any]:
    return {
        "change": "remove",
        "diary_day": entry.diary_day.isoformat(),
        "food_entry_id": entry.id,
    }


def change(entry: FoodEntry, **values: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "change": "edit",
        "diary_day": entry.diary_day.isoformat(),
        "food_entry_id": entry.id,
    }

    item.update(values)

    return item


def add_to_diary(fatsecret: FakeFatSecret, household: Household, entry: FoodEntry) -> FoodEntry:
    fatsecret.add_food_entry(household.user("jakub").fatsecret, entry)

    return entry


def sealed_changes(proposed: dict[str, Any]) -> list[dict[str, Any]]:
    encoded, _ = proposed["draft_id"].split(".")

    return json.loads(decode_base64url(encoded))["changes"]


async def confirm(client: Client, draft_id: str) -> dict[str, Any]:
    result = await client.call_tool("confirm_draft", {"draft_id": draft_id})

    assert not result.is_error, result.content
    assert result.structured_content is not None

    return result.structured_content


async def diary_entries(client: Client, day: date) -> list[dict[str, Any]]:
    """The Diary Day's Food Entries as get_diary_day shows them, each with its Meal."""
    result = await client.call_tool("get_diary_day", {"diary_day": day.isoformat()})

    assert result.structured_content is not None

    entries: list[dict[str, Any]] = []

    for meal in result.structured_content["meals"]:
        for entry in meal["food_entries"]:
            with_meal = dict(entry)
            with_meal["meal"] = meal["meal"]

            entries.append(with_meal)

    return entries


async def draft_error(client: Client, items: list[dict[str, Any]], meal: str | None = None) -> str:
    arguments = {
        "items": items,
        "meal": meal,
    }
    result = await client.call_tool("draft_entries", arguments)

    assert result.is_error

    return str(result.content)


async def test_undo_of_a_three_entry_confirm_removes_them_with_one_read_and_three_deletes(
    connect: Connect, fatsecret: FakeFatSecret, thigh: Food
) -> None:
    thigh_100_g = {"food_id": thigh.id, "serving_id": "thigh-100g", "number_of_units": 100}

    async with connect("jakub") as client:
        added = await draft(client, [thigh_100_g] * 3, meal="dinner", diary_day=YESTERDAY.isoformat())
        confirmed = await confirm(client, added["draft_id"])
        written_ids = [entry["food_entry_id"] for entry in confirmed["entries"]]
        undo_items = [
            {"change": "remove", "diary_day": YESTERDAY.isoformat(), "food_entry_id": food_entry_id}
            for food_entry_id in written_ids
        ]

        before_undo = fatsecret.requests
        undo = await draft(client, undo_items)
        after_undo = fatsecret.requests
        undone = await confirm(client, undo["draft_id"])
        after_confirm = fatsecret.requests
        left = await diary_entries(client, YESTERDAY)

    removed = [proposed["after"] for proposed in undo["changes"]]
    removed_names = [proposed["before"]["name"] for proposed in undo["changes"]]
    written = [confirmed_change["written"] for confirmed_change in undone["changes"]]

    # Yesterday is read once for the Draft; the confirm sends one delete per entry.
    assert after_undo - before_undo == 1
    assert after_confirm - after_undo == 3
    assert removed == [None, None, None]
    assert removed_names == ["Chicken Thigh"] * 3
    assert undo["items"] == []
    assert undo["meal"] is None
    assert undo["meal_suggested"] is False
    assert undo["previous_day_suggested"] is False
    assert undo["diary_day"] is None
    assert undo["day_after"] is None
    assert undo["other_days_after"] == [
        {
            "diary_day": "2026-10-13",
            "consumed": {"kcal": 0, "protein": 0, "fat": 0, "carbohydrate": 0},
            "daily_targets": {"kcal": 2500, "protein": 160, "fat": 80, "carbohydrate": 280},
            "remaining": {"kcal": 2500, "protein": 160, "fat": 80, "carbohydrate": 280},
        }
    ]
    assert undo["removal_note"] == "A removed Food Entry can't be restored, only logged again."
    assert undone["status"] == "confirmed"
    assert undone["entries"] == []
    assert written == [True] * 3
    # The cached yesterday was dropped, so the removals show.
    assert left == []


async def test_an_amount_change_in_grams_scales_units_and_nutrition_without_another_request(
    connect: Connect, fatsecret: FakeFatSecret, lunch: dict[str, FoodEntry]
) -> None:
    breast = lunch["breast"]

    async with connect("jakub") as client:
        before = fatsecret.requests
        proposed = await draft(client, [change(breast, metric_amount=200)])
        after = fatsecret.requests

    [proposed_change] = proposed["changes"]

    assert after - before == 1
    assert proposed_change["before"] == {
        "name": "Pierś z kurczaka",
        "amount": "150 g",
        "meal": "lunch",
        "kcal": 248,
        "protein": 46.5,
        "fat": 5.4,
        "carbohydrate": 0,
    }
    assert proposed_change["after"] == {
        "name": "Pierś z kurczaka",
        "amount": "200 g",
        "meal": "lunch",
        "kcal": 331,
        "protein": 62,
        "fat": 7.2,
        "carbohydrate": 0,
    }
    assert sealed_changes(proposed) == [
        {
            "kind": "edit",
            "food_entry_id": breast.id,
            "diary_day": "2026-10-13",
            "number_of_units": 200,
            "meal": None,
            "entry_name": None,
        }
    ]
    assert proposed["removal_note"] is None


async def test_an_amount_in_portions_costs_one_lookup_and_none_once_the_food_is_cached(
    connect: Connect, fatsecret: FakeFatSecret, lunch: dict[str, FoodEntry]
) -> None:
    bread = lunch["bread"]

    async with connect("jakub") as client:
        before = fatsecret.requests
        first = await draft(client, [change(bread, metric_amount=35)])
        after_first = fatsecret.requests
        await draft(client, [change(bread, metric_amount=35)])
        after_second = fatsecret.requests

    after = first["changes"][0]["after"]

    # Yesterday and the bread's Servings, then neither: both are cached.
    assert after_first - before == 2
    assert after_second - after_first == 0
    assert (after["amount"], after["kcal"]) == ("35 g", 90)
    assert sealed_changes(first)[0]["number_of_units"] == 1


async def test_an_amount_without_a_known_weight_cannot_change(
    connect: Connect, lunch: dict[str, FoodEntry]
) -> None:
    async with connect("jakub") as client:
        error = await draft_error(client, [change(lunch["cheese"], metric_amount=100)])

    assert "weight" in error
    assert "Serek wiejski Piątnica" in error
    assert "Remove it" in error


async def test_a_meal_move_keeps_the_amount_and_nutrition(
    connect: Connect, lunch: dict[str, FoodEntry]
) -> None:
    breast = lunch["breast"]

    async with connect("jakub") as client:
        proposed = await draft(client, [change(breast, meal="dinner")])
        await confirm(client, proposed["draft_id"])
        entries = await diary_entries(client, YESTERDAY)

    [proposed_change] = proposed["changes"]
    moved = [entry for entry in entries if entry["food_entry_id"] == breast.id]

    moved_before = dict(proposed_change["before"])
    moved_before["meal"] = "dinner"

    assert proposed_change["after"] == moved_before
    assert sealed_changes(proposed)[0]["number_of_units"] is None
    assert [(entry["meal"], entry["amount"], entry["kcal"]) for entry in moved] == [("dinner", "150 g", 248)]


async def test_a_rename_shows_the_old_and_new_name_and_sends_only_the_name(
    connect: Connect, lunch: dict[str, FoodEntry]
) -> None:
    breast = lunch["breast"]

    async with connect("jakub") as client:
        proposed = await draft(client, [change(breast, food_entry_name="Grillowana pierś")])
        await confirm(client, proposed["draft_id"])
        entries = await diary_entries(client, YESTERDAY)

    [proposed_change] = proposed["changes"]
    renamed = [entry["name"] for entry in entries if entry["food_entry_id"] == breast.id]

    assert (proposed_change["before"]["name"], proposed_change["after"]["name"]) == (
        "Pierś z kurczaka",
        "Grillowana pierś",
    )
    assert sealed_changes(proposed)[0] == {
        "kind": "edit",
        "food_entry_id": breast.id,
        "diary_day": "2026-10-13",
        "number_of_units": None,
        "meal": None,
        "entry_name": "Grillowana pierś",
    }
    assert renamed == ["Grillowana pierś"]


async def test_a_brand_entrys_amount_changes_in_place_under_the_same_id(
    connect: Connect, lunch: dict[str, FoodEntry]
) -> None:
    yoghurt = lunch["yoghurt"]

    async with connect("jakub") as client:
        proposed = await draft(client, [change(yoghurt, metric_amount=300)])
        confirmed = await confirm(client, proposed["draft_id"])
        entries = await diary_entries(client, YESTERDAY)

    changed = [entry for entry in entries if entry["food_entry_id"] == yoghurt.id]

    assert sealed_changes(proposed)[0]["number_of_units"] == 3
    assert confirmed["changes"][0]["written"] is True
    assert [(entry["amount"], entry["kcal"]) for entry in changed] == [("300 g", 180)]


async def test_thigh_not_breast_is_one_draft_that_writes_the_thigh_before_removing_the_breast(
    connect: Connect, fatsecret: FakeFatSecret, lunch: dict[str, FoodEntry], thigh: Food
) -> None:
    breast = lunch["breast"]
    thigh_160_g = {"food_id": thigh.id, "serving_id": "thigh-100g", "number_of_units": 160}

    async with connect("jakub") as client:
        proposed = await draft(
            client, [remove(breast), thigh_160_g], meal="lunch", diary_day=YESTERDAY.isoformat()
        )

        # The thigh is written, then FatSecret refuses the removal.
        fatsecret.limit_requests_to(fatsecret.requests + 1)

        partial = await confirm(client, proposed["draft_id"])

        fatsecret.limit_requests_to(None)

        entries = await diary_entries(client, YESTERDAY)

    names = [entry["name"] for entry in entries]

    # 712 kcal yesterday, less the breast's 248, plus the thigh's 334.
    assert proposed["day_after"]["consumed"]["kcal"] == 798
    assert proposed["other_days_after"] == []
    assert partial["status"] == "partially_written"
    assert partial["entries"][0]["food_entry_id"] is not None
    assert partial["changes"][0]["written"] is False
    assert "error 12" in partial["failure"]
    assert "get_diary_day" in partial["failure"]
    # A duplicate the User can see and remove, never a gap.
    assert "Pierś z kurczaka" in names
    assert "Chicken Thigh" in names


async def test_that_was_yesterday_moves_an_entry_to_another_day_in_one_draft(
    connect: Connect, fatsecret: FakeFatSecret, household: Household, lunch: dict[str, FoodEntry]
) -> None:
    coffee = add_to_diary(
        fatsecret,
        household,
        food_entry(
            "Kawa z mlekiem",
            Meal.BREAKFAST,
            TODAY,
            kcal=60,
            amount="200 ml",
            food_id="coffee",
            serving_id="coffee-100ml",
            number_of_units=200,
        ),
    )
    repeat = {"diary_day": TODAY.isoformat(), "food_entry_id": coffee.id}

    async with connect("jakub") as client:
        proposed = await draft(
            client, [remove(coffee), repeat], meal="breakfast", diary_day=YESTERDAY.isoformat()
        )
        confirmed = await confirm(client, proposed["draft_id"])
        today = await diary_entries(client, TODAY)
        yesterday = await diary_entries(client, YESTERDAY)

    assert proposed["day_after"]["consumed"]["kcal"] == 772
    assert proposed["other_days_after"][0]["diary_day"] == "2026-10-14"
    assert proposed["other_days_after"][0]["consumed"]["kcal"] == 0
    assert confirmed["status"] == "confirmed"
    assert today == []
    assert [entry["name"] for entry in yesterday if entry["meal"] == "breakfast"] == ["Kawa z mlekiem"]


@pytest.mark.parametrize(
    ("items", "meal", "message"),
    [
        ([{"change": "remove", "diary_day": "2026-10-13", "food_entry_id": "999"}], None, "get_diary_day"),
        ([{"change": "remove", "diary_day": "2026-10-15", "food_entry_id": "1"}], None, "earlier Diary Day"),
        ([{"change": "remove", "diary_day": "2025-10-13", "food_entry_id": "1"}], None, "a year back"),
        ([{"change": "edit", "diary_day": "2026-10-13", "food_entry_id": "1"}], None, "An edit needs"),
        (
            [{"change": "remove", "diary_day": "2026-10-13", "food_entry_id": "1", "meal": "dinner"}],
            None,
            "A removal takes no",
        ),
        (
            [{"change": "remove", "diary_day": "2026-10-13", "food_entry_id": "1"}],
            "dinner",
            "meal and diary_day are for new Food Entries",
        ),
    ],
)
async def test_a_change_is_refused_before_any_request(
    connect: Connect,
    fatsecret: FakeFatSecret,
    items: list[dict[str, Any]],
    meal: str | None,
    message: str,
) -> None:
    async with connect("jakub") as client:
        before = fatsecret.requests
        error = await draft_error(client, items, meal)
        after = fatsecret.requests

    assert message in error
    # An unknown id needs its Diary Day read first; everything else is refused before that.
    assert after - before == (1 if message == "get_diary_day" else 0)


async def test_two_changes_to_one_entry_and_a_change_that_changes_nothing_are_refused(
    connect: Connect, lunch: dict[str, FoodEntry]
) -> None:
    breast = lunch["breast"]

    async with connect("jakub") as client:
        twice = await draft_error(client, [change(breast, meal="dinner"), remove(breast)])
        unchanged = await draft_error(client, [change(breast, meal="lunch", metric_amount=150)])

    assert "more than one change item" in twice
    assert "leaves Pierś z kurczaka as it is" in unchanged


async def test_a_partial_failure_reports_what_was_written_and_reads_every_changed_day_again(
    connect: Connect, fatsecret: FakeFatSecret, household: Household, lunch: dict[str, FoodEntry]
) -> None:
    banana = add_to_diary(
        fatsecret,
        household,
        food_entry(
            "Banan",
            Meal.SNACKS,
            TWO_DAYS_AGO,
            kcal=107,
            amount="120 g",
            number_of_units=120,
        ),
    )
    breast = lunch["breast"]

    async with connect("jakub") as client:
        await diary_entries(client, YESTERDAY)
        await diary_entries(client, TWO_DAYS_AGO)

        proposed = await draft(client, [remove(breast), change(banana, metric_amount=100)])

        # The removal is written, then FatSecret refuses the edit.
        fatsecret.limit_requests_to(fatsecret.requests + 1)

        partial = await confirm(client, proposed["draft_id"])

        fatsecret.limit_requests_to(None)

        before = fatsecret.requests
        yesterday = await diary_entries(client, YESTERDAY)
        two_days_ago = await diary_entries(client, TWO_DAYS_AGO)
        after = fatsecret.requests

    written = [confirmed["written"] for confirmed in partial["changes"]]
    yesterday_ids = [entry["food_entry_id"] for entry in yesterday]

    assert partial["status"] == "partially_written"
    assert written == [True, False]
    assert "2026-10-12" in partial["failure"]
    # Both days were dropped from memory and read again.
    assert after - before == 2
    assert breast.id not in yesterday_ids
    assert two_days_ago[0]["amount"] == "120 g"


async def test_an_entry_removed_in_the_app_meanwhile_asks_to_read_the_day_again(
    connect: Connect, fatsecret: FakeFatSecret, household: Household, lunch: dict[str, FoodEntry]
) -> None:
    breast = lunch["breast"]

    async with connect("jakub") as client:
        proposed = await draft(client, [change(breast, metric_amount=200)])

        await fatsecret.delete_food_entry(household.user("jakub").fatsecret, breast.id)

        result = await client.call_tool("confirm_draft", {"draft_id": proposed["draft_id"]})
        before = fatsecret.requests
        yesterday = await diary_entries(client, YESTERDAY)
        after = fatsecret.requests

    yesterday_ids = [entry["food_entry_id"] for entry in yesterday]

    assert result.is_error
    assert f"Food Entry {breast.id} is no longer in the Food Diary" in str(result.content)
    assert "refresh=true" in str(result.content)
    # Even a confirm that wrote nothing drops the day from memory, so it's read again.
    assert after - before == 1
    assert breast.id not in yesterday_ids


async def test_confirm_draft_asks_for_the_users_approval_in_every_mode(connect: Connect) -> None:
    async with connect("jakub") as client:
        listed = (await client.list_tools()).tools

    tool = next(tool for tool in listed if tool.name == "confirm_draft")

    assert tool.meta == {"anthropic/requiresUserInteraction": True}
    assert tool.annotations is not None
    assert tool.annotations.destructive_hint is True


async def test_the_tools_tell_claude_how_to_undo_and_change_entries(connect: Connect) -> None:
    async with connect("jakub") as client:
        listed = (await client.list_tools()).tools

    draft_tool = next(tool for tool in listed if tool.name == "draft_entries")
    description = draft_tool.description or ""

    assert '"undo that"' in description
    assert "read the Diary Day with get_diary_day first" in description
    assert "ask when it's unclear" in description
    assert '"chicken thigh, not breast"' in description
    assert '"that was yesterday"' in description
    assert '"undo that"' in INSTRUCTIONS
    assert "can't be restored" in INSTRUCTIONS
