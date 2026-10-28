"""Repeated Food Entries through MCP: "the same as yesterday's breakfast", drafted from the Food Diary."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from mcp import Client

from fatsecret_mcp.config.models import Household
from fatsecret_mcp.domain.models import CatalogFood, Food, FoodEntry, Meal, Nutrition, Serving
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
TOMORROW = date(
    year=2026,
    month=10,
    day=15,
)


@pytest.fixture
def breakfast(fatsecret: FakeFatSecret, household: Household) -> dict[str, FoodEntry]:
    """Yesterday's breakfast: oats and milk from the catalog, a Stand-in and a Polish yoghurt.

    The yoghurt was logged in the FatSecret app, so the catalog can't return its Food, and its
    FatSecret units aren't its grams.
    """
    oats = food_entry(
        "Owsianka górska",
        Meal.BREAKFAST,
        YESTERDAY,
        kcal=300,
        protein=10,
        fat=6,
        carbohydrate=50,
        amount="80 g",
        food_id="oats",
        serving_id="oats-100g",
        number_of_units=80,
    )
    milk = food_entry(
        "Mleko 2%",
        Meal.BREAKFAST,
        YESTERDAY,
        kcal=125,
        protein=8.5,
        fat=5,
        carbohydrate=12,
        amount="250 ml",
        food_id="milk",
        serving_id="milk-100ml",
        number_of_units=250,
    )
    dzik = food_entry(
        "Baton proteinowy DZIK",
        Meal.BREAKFAST,
        YESTERDAY,
        kcal=190,
        protein=15,
        fat=7,
        carbohydrate=17,
        amount="50 g",
        food_id="chocolate-bar",
        serving_id="chocolate-100g",
        number_of_units=50,
    )
    yoghurt = food_entry(
        "Jogurt naturalny Piątnica",
        Meal.BREAKFAST,
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
    entries = {
        "oats": oats,
        "milk": milk,
        "dzik": dzik,
        "yoghurt": yoghurt,
    }

    for entry in entries.values():
        fatsecret.add_food_entry(household.user("jakub").fatsecret, entry)

    return entries


def repeat(entry: FoodEntry, **changes: Any) -> dict[str, Any]:
    """A Diary Entry item repeating `entry` from its Diary Day."""
    item: dict[str, Any] = {
        "diary_day": entry.diary_day.isoformat(),
        "food_entry_id": entry.id,
    }

    item.update(changes)

    return item


def add_to_diary(fatsecret: FakeFatSecret, household: Household, entry: FoodEntry) -> FoodEntry:
    fatsecret.add_food_entry(household.user("jakub").fatsecret, entry)

    return entry


async def draft_error(client: Client, items: list[dict[str, Any]]) -> str:
    result = await client.call_tool("draft_entries", {"items": items})

    assert result.is_error

    return str(result.content)


async def test_repeating_yesterdays_breakfast_reads_yesterday_once_and_searches_nothing(
    connect: Connect, fatsecret: FakeFatSecret, breakfast: dict[str, FoodEntry]
) -> None:
    items = [repeat(entry) for entry in breakfast.values()]

    async with connect("jakub") as client:
        before = fatsecret.requests
        first = await draft(client, items)
        after_first = fatsecret.requests
        await draft(client, items)
        after_second = fatsecret.requests

    # Yesterday and today, then only today: yesterday is cached.
    assert after_first - before == 2
    assert after_second - after_first == 1
    assert first["items"][0] == {
        "food_id": "oats",
        "serving_id": "oats-100g",
        "name": "Owsianka górska",
        "serving": "80 g",
        "number_of_units": 80,
        "amount": "80 g",
        "kcal": 300,
        "protein": 10,
        "fat": 6,
        "carbohydrate": 50,
        "assumption": None,
        "catalog_name": None,
        "stand_in": None,
        "repeated_from": {"diary_day": "2026-10-13", "meal": "breakfast"},
    }
    assert first["totals"] == {"kcal": 705, "protein": 39.5, "fat": 21, "carbohydrate": 88}
    # The source's Meal isn't copied: noon suggests lunch, and Claude asks about it.
    assert first["meal"] == "lunch"
    assert first["meal_suggested"] is True
    assert first["diary_day"] == "2026-10-14"


async def test_a_draft_after_reading_the_source_day_reads_only_today(
    connect: Connect, fatsecret: FakeFatSecret, breakfast: dict[str, FoodEntry]
) -> None:
    items = [repeat(entry) for entry in breakfast.values()]

    async with connect("jakub") as client:
        source = await client.call_tool("get_diary_day", {"diary_day": "2026-10-13"})
        before = fatsecret.requests
        await draft(client, items)
        after = fatsecret.requests

    assert source.structured_content is not None
    shown = source.structured_content["meals"][0]["food_entries"]
    shown_ids = [entry["food_entry_id"] for entry in shown]

    assert shown_ids == [entry.id for entry in breakfast.values()]
    assert after - before == 1


async def test_a_food_the_catalog_cannot_return_is_repeated_with_the_entrys_own_nutrition(
    connect: Connect, breakfast: dict[str, FoodEntry]
) -> None:
    yoghurt = breakfast["yoghurt"]

    async with connect("jakub") as client:
        proposed = await draft(client, [repeat(yoghurt)])

    [item] = proposed["items"]

    assert item["food_id"] == "piatnica-yoghurt"
    assert item["serving_id"] == "piatnica-100g"
    assert item["number_of_units"] == 1.5
    assert item["amount"] == "150 g"
    assert (item["kcal"], item["protein"], item["fat"], item["carbohydrate"]) == (90, 6, 3, 9)


@pytest.mark.parametrize(
    ("entry", "metric_amount", "units", "amount", "kcal"),
    [
        ("yoghurt", 75, 0.75, "75 g", 45),
        ("milk", 125, 125, "125 ml", 62),
        ("oats", 120, 120, "120 g", 450),
    ],
)
async def test_a_changed_metric_amount_scales_nutrition_units_and_amount(
    connect: Connect,
    breakfast: dict[str, FoodEntry],
    entry: str,
    metric_amount: float,
    units: float,
    amount: str,
    kcal: int,
) -> None:
    source = breakfast[entry]

    async with connect("jakub") as client:
        proposed = await draft(client, [repeat(source, metric_amount=metric_amount)])

    [item] = proposed["items"]

    assert item["number_of_units"] == units
    assert item["amount"] == amount
    assert item["serving"] == source.amount
    assert item["kcal"] == kcal


async def test_an_amount_in_portions_is_shown_in_grams_after_one_lookup_of_its_serving(
    connect: Connect, fatsecret: FakeFatSecret, household: Household
) -> None:
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
    bread = food_entry(
        "Chleb żytni",
        Meal.BREAKFAST,
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

    fatsecret.add_food(rye_bread)
    add_to_diary(fatsecret, household, bread)

    async with connect("jakub") as client:
        before = fatsecret.requests
        unchanged = await draft(client, [repeat(bread)])
        after = fatsecret.requests
        halved = await draft(client, [repeat(bread, metric_amount=35)])
        uneven = await draft(client, [repeat(bread, metric_amount=50)])

    # Yesterday, the bread's Servings and today.
    assert after - before == 3
    assert unchanged["items"][0]["amount"] == "70 g"
    assert unchanged["items"][0]["serving"] == "70 g"
    assert unchanged["items"][0]["number_of_units"] == 2
    assert halved["items"][0]["amount"] == "35 g"
    assert halved["items"][0]["number_of_units"] == 1
    assert halved["items"][0]["kcal"] == 90
    # 50/70 of 2 units is rounded to 1.429 units, and the amount still reads as asked.
    assert uneven["items"][0]["number_of_units"] == 1.429
    assert uneven["items"][0]["amount"] == "50 g"


async def test_an_amount_without_a_known_weight_is_repeated_as_written_and_cannot_change(
    connect: Connect, fatsecret: FakeFatSecret, household: Household
) -> None:
    # Logged in the FatSecret app: the catalog can't return its Food (error 106).
    cottage_cheese = add_to_diary(
        fatsecret,
        household,
        food_entry(
            "Serek wiejski Piątnica",
            Meal.BREAKFAST,
            YESTERDAY,
            kcal=194,
            protein=22,
            fat=10,
            carbohydrate=4,
            amount="1 package",
            food_id="piatnica-cottage-cheese",
            serving_id="piatnica-package",
            number_of_units=1,
        ),
    )

    async with connect("jakub") as client:
        proposed = await draft(client, [repeat(cottage_cheese)])
        error = await draft_error(client, [repeat(cottage_cheese, metric_amount=100)])

    [item] = proposed["items"]

    assert item["amount"] == "1 package"
    assert item["serving"] == "1 package"
    assert item["number_of_units"] == 1
    assert item["kcal"] == 194
    assert "weight" in error
    assert "Serek wiejski Piątnica" in error


async def test_a_stand_in_is_repeated_and_confirmed_under_its_product_name(
    connect: Connect, breakfast: dict[str, FoodEntry]
) -> None:
    dzik = breakfast["dzik"]

    async with connect("jakub") as client:
        proposed = await draft(client, [repeat(dzik)], meal="snacks")
        await client.call_tool("confirm_draft", {"draft_id": proposed["draft_id"]})
        today = await client.call_tool("get_diary_day", {})

    [item] = proposed["items"]
    encoded = proposed["draft_id"].split(".")[0]
    payload = json.loads(decode_base64url(encoded))

    assert item["name"] == "Baton proteinowy DZIK"
    assert item["food_id"] == "chocolate-bar"
    assert item["stand_in"] is None
    assert payload["items"] == [
        {
            "food_id": "chocolate-bar",
            "serving_id": "chocolate-100g",
            "number_of_units": 50,
            "entry_name": "Baton proteinowy DZIK",
        }
    ]
    assert today.structured_content is not None
    [snacks] = today.structured_content["meals"]

    assert snacks["meal"] == "snacks"
    assert [entry["name"] for entry in snacks["food_entries"]] == ["Baton proteinowy DZIK"]
    assert snacks["food_entries"][0]["kcal"] == 190


async def test_an_id_not_on_the_source_diary_day_is_refused_with_a_request_to_read_it_again(
    connect: Connect, fatsecret: FakeFatSecret, household: Household, breakfast: dict[str, FoodEntry]
) -> None:
    todays = food_entry(
        "Kawa",
        Meal.BREAKFAST,
        TODAY,
        kcal=5,
    )
    on_another_day = repeat(todays, diary_day="2026-10-13")

    fatsecret.add_food_entry(household.user("jakub").fatsecret, todays)

    async with connect("jakub") as client:
        unknown = await draft_error(client, [repeat(breakfast["oats"], food_entry_id="999999")])
        elsewhere = await draft_error(client, [on_another_day])

    for error in (unknown, elsewhere):
        assert "get_diary_day" in error
        assert "2026-10-13" in error


@pytest.mark.parametrize("diary_day", ["2026-10-15", "2025-10-13"])
async def test_a_source_diary_day_in_the_future_or_over_a_year_back_is_refused_before_any_request(
    connect: Connect, fatsecret: FakeFatSecret, diary_day: str
) -> None:
    item = {"diary_day": diary_day, "food_entry_id": "1"}

    async with connect("jakub") as client:
        error = await draft_error(client, [item])

    assert "today or an earlier Diary Day" in error or "a year back" in error
    assert fatsecret.requests == 0


async def test_a_draft_mixes_food_nutrition_label_and_diary_entry_items(
    connect: Connect, fatsecret: FakeFatSecret, breakfast: dict[str, FoodEntry]
) -> None:
    gram_serving = Serving(
        id="chocolate-100g",
        description="100 g",
        metric_amount=100,
        metric_unit="g",
        number_of_units=100,
        nutrition=Nutrition(
            kcal=400,
            protein=33,
            fat=13,
            carbohydrate=40,
        ),
    )
    chocolate_bar = Food(
        id="chocolate-bar",
        name="Chocolate Protein Bar",
        brand=None,
        servings=[gram_serving],
    )
    found = CatalogFood(
        id=chocolate_bar.id,
        name=chocolate_bar.name,
        brand=None,
    )
    food_item = {"food_id": "chocolate-bar", "serving_id": "chocolate-100g", "number_of_units": 30}
    label_item = {
        "product_name": "Baton proteinowy DZIK",
        "nutrition_label": {"kcal": 380, "protein": 30, "fat": 14, "carbohydrate": 34, "estimated": False},
        "grams": 50,
        "search_phrase": "protein bar",
    }

    fatsecret.add_food(chocolate_bar)
    fatsecret.set_search_results("protein bar", [found])

    async with connect("jakub") as client:
        proposed = await draft(client, [food_item, label_item, repeat(breakfast["oats"])])

    food, stand_in, repeated = proposed["items"]

    assert (food["stand_in"], food["repeated_from"]) == (None, None)
    assert stand_in["stand_in"] is not None
    assert stand_in["repeated_from"] is None
    assert repeated["repeated_from"] == {"diary_day": "2026-10-13", "meal": "breakfast"}


@pytest.mark.parametrize(
    ("metric_amount", "message"),
    [
        (10000, "implausibly large"),
        (10001, "10000"),
        (0, "greater than 0"),
    ],
)
async def test_an_implausible_or_invalid_repeated_amount_is_refused(
    connect: Connect, breakfast: dict[str, FoodEntry], metric_amount: float, message: str
) -> None:
    async with connect("jakub") as client:
        error = await draft_error(client, [repeat(breakfast["oats"], metric_amount=metric_amount)])

    assert message in error


async def test_the_tools_tell_claude_to_list_the_source_entries_and_ask_before_repeating(
    connect: Connect,
) -> None:
    async with connect("jakub") as client:
        listed = (await client.list_tools()).tools
        instructions = client.instructions

    draft_tool = next(tool for tool in listed if tool.name == "draft_entries")
    description = draft_tool.description or ""

    assert instructions is not None

    for guidance in (description, instructions):
        assert "the same as" in guidance
        assert "food_entry_id" in guidance
        assert "ask" in guidance
