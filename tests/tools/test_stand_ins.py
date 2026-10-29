"""Stand-ins through MCP: a product missing from the catalog, drafted from its Nutrition Label."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp import Client

from fatsecret_mcp.domain.models import CatalogFood, Food, Nutrition, Serving
from tests.fatsecret.fake import FakeFatSecret
from tests.tools.test_draft_entries import decode_base64url

pytestmark = pytest.mark.anyio
Connect = Callable[[str], Client]


def catalog_food(food_id: str, name: str, servings: list[Serving]) -> Food:
    return Food(
        id=food_id,
        name=name,
        brand=None,
        servings=servings,
    )


def serving(
    serving_id: str,
    description: str,
    metric_amount: float | None,
    nutrition: Nutrition,
    *,
    number_of_units: float = 1,
) -> Serving:
    return Serving(
        id=serving_id,
        description=description,
        metric_amount=metric_amount,
        metric_unit="g" if metric_amount is not None else None,
        number_of_units=number_of_units,
        nutrition=nutrition,
    )


def search_result(food: Food) -> CatalogFood:
    return CatalogFood(
        id=food.id,
        name=food.name,
        brand=food.brand,
    )


@pytest.fixture
def protein_bars(fatsecret: FakeFatSecret) -> list[Food]:
    """Catalog results for "protein bar"; the chocolate bar's Macros are closest to the DZIK bar."""
    peanut_butter = catalog_food(
        "peanut-butter-bar",
        "Peanut Butter Protein Bar",
        [
            serving(
                "peanut-butter-100g",
                "100 g",
                100,
                Nutrition(
                    kcal=420,
                    protein=25,
                    fat=20,
                    carbohydrate=35,
                ),
                number_of_units=100,
            )
        ],
    )
    # Its Macros match the label exactly, but a Serving without grams can't be scaled.
    bites = catalog_food(
        "protein-bites",
        "Protein Bites",
        [
            serving(
                "bites-1",
                "1 piece",
                None,
                Nutrition(
                    kcal=380,
                    protein=30,
                    fat=14,
                    carbohydrate=34,
                ),
            )
        ],
    )
    chocolate = catalog_food(
        "chocolate-bar",
        "Chocolate Protein Bar",
        [
            serving(
                "chocolate-1",
                "1 bar (60 g)",
                60,
                Nutrition(
                    kcal=240,
                    protein=20,
                    fat=8,
                    carbohydrate=24,
                ),
            )
        ],
    )
    foods = [peanut_butter, bites, chocolate]
    found = [search_result(food) for food in foods]

    for food in foods:
        fatsecret.add_food(food)

    fatsecret.set_search_results("protein bar", found)

    return foods


def dzik_label(**changes: Any) -> dict[str, Any]:
    """The DZIK protein bar's Nutrition Label: 380 kcal, 30/14/34 g per 100 g, read from the packaging."""
    label = {
        "kcal": 380,
        "protein": 30,
        "fat": 14,
        "carbohydrate": 34,
        "estimated": False,
    }

    label.update(changes)

    return label


def dzik_bar(**changes: Any) -> dict[str, Any]:
    """A Nutrition Label item for 50 g of a DZIK protein bar."""
    item = {
        "product_name": "Baton proteinowy DZIK",
        "nutrition_label": dzik_label(),
        "grams": 50,
        "search_phrase": "protein bar",
    }

    item.update(changes)

    return item


async def draft(client: Client, items: list[dict[str, Any]]) -> dict[str, Any]:
    result = await client.call_tool("draft_entries", {"items": items})

    assert not result.is_error, result.content
    assert result.structured_content is not None

    return result.structured_content


async def test_a_nutrition_label_picks_the_closest_catalog_food_and_matches_kcal_exactly(
    connect: Connect, protein_bars: list[Food]
) -> None:
    async with connect("jakub") as client:
        proposed = await draft(client, [dzik_bar()])

    assert proposed["items"] == [
        {
            "food_id": "chocolate-bar",
            "serving_id": "chocolate-1",
            "name": "Baton proteinowy DZIK",
            "serving": "1 bar (60 g)",
            "number_of_units": 0.792,
            "amount": "47.52 g",
            "kcal": 190,
            "protein": 15.8,
            "fat": 6.3,
            "carbohydrate": 19.0,
            "assumption": None,
            "catalog_name": None,
            "stand_in": {
                "food_name": "Chocolate Protein Bar",
                "grams_eaten": 50,
                "nutrition_label": {"kcal": 190, "protein": 15, "fat": 7, "carbohydrate": 17},
                "difference": {"kcal": 0, "protein": 0.8, "fat": -0.7, "carbohydrate": 2.0},
                "estimated": False,
            },
            "repeated_from": None,
        }
    ]
    assert proposed["totals"] == {"kcal": 190, "protein": 15.8, "fat": 6.3, "carbohydrate": 19.0}


async def test_the_first_of_equally_close_foods_is_chosen_every_time(
    connect: Connect, fatsecret: FakeFatSecret, protein_bars: list[Food]
) -> None:
    chocolate = protein_bars[-1]
    same_macros = catalog_food("other-chocolate-bar", "Other Chocolate Protein Bar", chocolate.servings)
    results = [search_result(same_macros), search_result(chocolate)]

    fatsecret.add_food(same_macros)
    fatsecret.set_search_results("chocolate protein bar", results)

    async with connect("jakub") as client:
        first = await draft(client, [dzik_bar(search_phrase="chocolate protein bar")])
        second = await draft(client, [dzik_bar(search_phrase="chocolate protein bar")])

    assert first["items"][0]["food_id"] == "other-chocolate-bar"
    assert second["draft_id"] == first["draft_id"]


async def test_a_stand_in_is_logged_in_grams_when_its_food_has_a_serving_in_grams(
    connect: Connect, fatsecret: FakeFatSecret
) -> None:
    bar = serving(
        "chocolate-grams-1",
        "1 bar (60 g)",
        60,
        Nutrition(
            kcal=240,
            protein=20,
            fat=8,
            carbohydrate=24,
        ),
    )
    hundred_grams = serving(
        "chocolate-grams-100g",
        "100 g",
        100,
        Nutrition(
            kcal=400,
            protein=33.3,
            fat=13.3,
            carbohydrate=42,
        ),
        number_of_units=100,
    )
    # The bar's Macros are closer to the label, so the bar is chosen and then logged in grams.
    chocolate = catalog_food("chocolate-grams", "Chocolate Protein Bar", [bar, hundred_grams])

    fatsecret.add_food(chocolate)
    fatsecret.set_search_results("chocolate protein bar", [search_result(chocolate)])

    async with connect("jakub") as client:
        proposed = await draft(client, [dzik_bar(search_phrase="chocolate protein bar")])

    [item] = proposed["items"]

    assert (item["serving_id"], item["number_of_units"], item["amount"]) == (
        "chocolate-grams-100g",
        47.5,
        "47.5 g",
    )
    assert item["kcal"] == item["stand_in"]["nutrition_label"]["kcal"] == 190


async def test_a_label_whose_kcal_are_lowered_by_polyols_is_accepted(
    connect: Connect, protein_bars: list[Food]
) -> None:
    # 20 g of polyols counted as carbohydrate give 2.4 kcal per gram, not 4.
    sugar_free = dzik_bar(nutrition_label=dzik_label(kcal=340, protein=30, fat=12, carbohydrate=40))

    async with connect("jakub") as client:
        proposed = await draft(client, [sugar_free])

    assert proposed["items"][0]["kcal"] == 170


async def test_an_estimated_nutrition_label_is_marked_as_estimated_in_the_draft(
    connect: Connect, protein_bars: list[Food]
) -> None:
    estimated = dzik_bar(nutrition_label=dzik_label(estimated=True))

    async with connect("jakub") as client:
        proposed = await draft(client, [estimated])

    assert proposed["items"][0]["stand_in"]["estimated"] is True


async def test_a_draft_with_a_catalog_food_and_a_stand_in_totals_the_logged_values(
    connect: Connect, fatsecret: FakeFatSecret, protein_bars: list[Food]
) -> None:
    rice = catalog_food(
        "rice",
        "Cooked Rice",
        [
            serving(
                "rice-100g",
                "100 g",
                100,
                Nutrition(
                    kcal=130,
                    protein=2.7,
                    fat=0.3,
                    carbohydrate=28.2,
                ),
                number_of_units=100,
            )
        ],
    )
    items = [
        {"food_id": "rice", "serving_id": "rice-100g", "number_of_units": 150},
        dzik_bar(),
    ]

    fatsecret.add_food(rice)

    async with connect("jakub") as client:
        proposed = await draft(client, items)

    assert proposed["items"][0]["stand_in"] is None
    assert proposed["totals"] == {"kcal": 385, "protein": 19.9, "fat": 6.8, "carbohydrate": 61.3}
    assert proposed["day_after"]["consumed"] == proposed["totals"]
    assert proposed["day_after"]["remaining"] == {
        "kcal": 2115,
        "protein": 140.1,
        "fat": 73.2,
        "carbohydrate": 218.7,
    }


async def test_a_confirmed_stand_in_is_written_under_the_products_name_and_the_food_keeps_its_own(
    connect: Connect, protein_bars: list[Food]
) -> None:
    async with connect("jakub") as client:
        proposed = await draft(client, [dzik_bar()])
        confirmed = await client.call_tool("confirm_draft", {"draft_id": proposed["draft_id"]})
        day = await client.call_tool("get_diary_day", {})
        search = await client.call_tool("search_foods", {"description": "protein bar"})

    encoded = proposed["draft_id"].split(".")[0]
    sealed = json.loads(decode_base64url(encoded))

    assert not confirmed.is_error, confirmed.content
    assert sealed["items"] == [
        {
            "food_id": "chocolate-bar",
            "serving_id": "chocolate-1",
            "number_of_units": 0.792,
            "entry_name": "Baton proteinowy DZIK",
        }
    ]
    assert confirmed.structured_content is not None
    assert day.structured_content is not None
    written_id = confirmed.structured_content["entries"][0]["food_entry_id"]

    assert day.structured_content["meals"] == [
        {
            "meal": "lunch",
            "food_entries": [
                {
                    "food_entry_id": written_id,
                    "name": "Baton proteinowy DZIK",
                    "amount": "0.792 units",
                    "kcal": 190,
                    "protein": 15.8,
                    "fat": 6.3,
                    "carbohydrate": 19.0,
                }
            ],
        }
    ]
    assert search.structured_content is not None
    assert "Chocolate Protein Bar" in [match["name"] for match in search.structured_content["matches"]]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "nutrition_label": {
                    "kcal": 380,
                    "protein": -1,
                    "fat": 14,
                    "carbohydrate": 34,
                    "estimated": False,
                }
            },
            "greater than or equal to 0",
        ),
        ({"grams": 0}, "greater than 0"),
        ({"grams": -50}, "greater than 0"),
        (
            {
                "nutrition_label": {
                    "kcal": 380,
                    "protein": 60,
                    "fat": 30,
                    "carbohydrate": 40,
                    "estimated": False,
                }
            },
            "don't agree with its Macros",
        ),
        ({"search_phrase": "protein bites"}, "No catalog Food with a Serving in grams"),
        ({"search_phrase": "nothing like it"}, "No catalog Food with a Serving in grams"),
    ],
)
async def test_an_implausible_label_or_a_search_without_gram_servings_is_rejected(
    connect: Connect,
    fatsecret: FakeFatSecret,
    protein_bars: list[Food],
    changes: dict[str, Any],
    message: str,
) -> None:
    bites = CatalogFood(
        id="protein-bites",
        name="Protein Bites",
        brand=None,
    )

    fatsecret.set_search_results("protein bites", [bites])

    async with connect("jakub") as client:
        result = await client.call_tool("draft_entries", {"items": [dzik_bar(**changes)]})

    assert result.is_error
    assert message in str(result.content)


async def test_the_tools_tell_claude_how_to_get_and_reuse_a_nutrition_label(connect: Connect) -> None:
    async with connect("jakub") as client:
        listed = (await client.list_tools()).tools
        instructions = client.instructions

    descriptions = {tool.name: tool.description or "" for tool in listed}
    guidance = descriptions["draft_entries"]

    assert "photo of its Nutrition Label" in descriptions["search_foods"]
    assert "photo of the Nutrition Label" in guidance
    assert "unless the User already gave its values" in guidance
    assert "reuse its label from this conversation or your memory" in guidance
    assert "only when the User has no packaging" in guidance
    assert "different search_phrase" in guidance
    assert instructions is not None
    assert "Nutrition Label" in instructions
    assert "Stand-in" in instructions
    assert "unless the User already gave its values" in instructions


async def test_a_nutrition_label_item_without_its_label_says_the_label_is_missing(
    connect: Connect, protein_bars: list[Food]
) -> None:
    without_label = dzik_bar()

    del without_label["nutrition_label"]

    async with connect("jakub") as client:
        result = await client.call_tool("draft_entries", {"items": [without_label]})

    assert result.is_error
    assert "nutrition_label" in str(result.content)
    assert "food_id" not in str(result.content)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_name", "x" * 101),
        ("search_phrase", "x" * 101),
        ("assumption", "x" * 301),
    ],
)
async def test_overlong_nutrition_label_text_is_refused_before_any_fatsecret_request(
    connect: Connect, fatsecret: FakeFatSecret, protein_bars: list[Food], field: str, value: str
) -> None:
    item = dzik_bar()

    item[field] = value

    async with connect("jakub") as client:
        result = await client.call_tool("draft_entries", {"items": [item]})

    assert result.is_error
    assert "at most" in str(result.content)
    assert fatsecret.requests == 0


async def test_a_product_name_and_search_phrase_of_a_hundred_characters_are_accepted(
    connect: Connect, protein_bars: list[Food]
) -> None:
    product_name = "Baton proteinowy DZIK " + "x" * 78

    async with connect("jakub") as client:
        result = await draft(client, [dzik_bar(product_name=product_name)])

    assert result["items"][0]["name"] == product_name
