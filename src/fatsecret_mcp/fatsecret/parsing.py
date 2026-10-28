"""FatSecret's JSON turned into domain objects, and domain values written as FatSecret expects them.

This is where FatSecret's quirks are smoothed over: numbers arrive as strings, a list with one item
may arrive as a bare object, an empty list as `null` or a missing key, and a Diary Day is a number.
Anything that doesn't look as expected becomes an `UpstreamFailure`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, cast
from urllib.parse import parse_qs

from fatsecret_mcp.domain.models import CatalogFood, Food, FoodEntry, Meal, Nutrition, Serving
from fatsecret_mcp.fatsecret.errors import UpstreamFailure

EPOCH = date(1970, 1, 1)

CUSTOM_AMOUNT = ":custom:"
STRAY_PLURAL = re.compile(r"^(\d+)s\s+")
LEADING_FRACTION = re.compile(r"^(?:(?P<whole>\d+) )?(?P<numerator>\d+)/(?P<denominator>\d+)\b")

MEAL_NAMES = {Meal.BREAKFAST: "breakfast", Meal.LUNCH: "lunch", Meal.DINNER: "dinner", Meal.SNACKS: "other"}
MEALS = {name: meal for meal, name in MEAL_NAMES.items()}


def day_number(day: date) -> int:
    """FatSecret identifies a Diary Day by the number of days since 1970-01-01."""
    return (day - EPOCH).days


def meal_text(meal: Meal) -> str:
    """FatSecret calls Snacks "other"."""
    return MEAL_NAMES[meal]


def number_text(value: float) -> str:
    """A whole number without a trailing ".0", as FatSecret sends numbers itself: 2.0 is "2"."""
    if value.is_integer():
        return str(int(value))

    return str(value)


def created_food_entry_id(body: dict[str, Any]) -> str:
    """The id in a `food_entry.create` response: `{"food_entry_id": {"value": "…"}}`."""
    return _text(_object(body, "food_entry_id"), "value")


def check_success(body: dict[str, Any]) -> None:
    """Accept a `food_entry.edit` or `food_entry.delete` response: `{"success": {"value": "1"}}`."""
    if _text(_object(body, "success"), "value") != "1":
        raise UpstreamFailure("a change FatSecret didn't confirm")


def token_and_secret(text: str) -> tuple[str, str]:
    """The token and secret in a PIN flow response, form-encoded: `oauth_token=…&oauth_token_secret=…`."""
    fields = parse_qs(text, strict_parsing=False)
    token = fields.get("oauth_token", [""])[0]
    secret = fields.get("oauth_token_secret", [""])[0]

    if not token or not secret:
        raise UpstreamFailure("a sign-in response without a token")

    return token, secret


def error_code(error: object) -> int | None:
    """The code from FatSecret's `{"error": {"code": …}}`. It's an integer, though the docs show a string."""
    raw = cast(dict[str, Any], error).get("code") if isinstance(error, dict) else None

    try:
        return int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None


def food_entries(body: dict[str, Any]) -> list[FoodEntry]:
    """The Food Entries in a `food_entries.get.v2` response."""
    food_entries = _object(body, "food_entries")
    raw_entries = _as_list(food_entries.get("food_entry"))

    return [_food_entry(raw) for raw in raw_entries]


def catalog_foods(body: dict[str, Any]) -> list[CatalogFood]:
    foods = _object(body, "foods")
    raw_foods = _as_list(foods.get("food"))
    matches = [_catalog_food(raw) for raw in raw_foods]

    return matches


def _catalog_food(raw: dict[str, Any]) -> CatalogFood:
    return CatalogFood(
        id=_text(raw, "food_id"),
        name=_text(raw, "food_name"),
        brand=_optional_text(raw, "brand_name"),
    )


def food(body: dict[str, Any]) -> Food:
    raw = _object(body, "food")
    raw_servings = _as_list(_object(raw, "servings").get("serving"))
    servings = [_serving(serving) for serving in raw_servings]

    return Food(
        id=_text(raw, "food_id"),
        name=_text(raw, "food_name"),
        brand=_optional_text(raw, "brand_name"),
        servings=servings,
    )


def _serving(raw: dict[str, Any]) -> Serving:
    metric_amount = _number(raw, "metric_serving_amount") if raw.get("metric_serving_amount") else None

    return Serving(
        id=_text(raw, "serving_id"),
        description=_text(raw, "serving_description"),
        metric_amount=metric_amount,
        metric_unit=_optional_text(raw, "metric_serving_unit"),
        number_of_units=_number(raw, "number_of_units"),
        nutrition=_nutrition(raw),
    )


def _nutrition(raw: dict[str, Any]) -> Nutrition:
    """kcal and Macros as FatSecret names them in Servings, Food Entries and month totals."""
    return Nutrition(
        kcal=_number(raw, "calories", default=0),
        protein=_number(raw, "protein", default=0),
        fat=_number(raw, "fat", default=0),
        carbohydrate=_number(raw, "carbohydrate", default=0),
    )


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    if raw.get(key) is None:
        return None

    return _text(raw, key)


def month_totals(body: dict[str, Any]) -> dict[date, Nutrition]:
    """Per-day totals in a `food_entries.get_month.v2` response, including retained all-zero days."""
    month = _object(body, "month")
    raw_days = _as_list(month.get("day"))
    totals: dict[date, Nutrition] = {}

    for raw in raw_days:
        try:
            day = EPOCH + timedelta(days=int(_text(raw, "date_int")))
        except (ValueError, OverflowError):
            raise UpstreamFailure("an invalid Diary Day") from None

        totals[day] = _nutrition(raw)

    return totals


def _food_entry(raw: dict[str, Any]) -> FoodEntry:
    name = _text(raw, "food_entry_name")

    return FoodEntry(
        id=_text(raw, "food_entry_id"),
        food_id=_text(raw, "food_id"),
        serving_id=_text(raw, "serving_id"),
        name=name,
        amount=_amount(_text(raw, "food_entry_description"), name),
        number_of_units=_number(raw, "number_of_units"),
        meal=_meal(_text(raw, "meal")),
        diary_day=EPOCH + timedelta(days=int(_number(raw, "date_int"))),
        nutrition=_nutrition(raw),
    )


def _amount(description: str, name: str) -> str:
    """The amount from an entry's description, as the FatSecret app shows it.

    FatSecret describes an entry as "<amount> <name>", e.g. "150 g Chicken Breast".
    """
    amount = description.removesuffix(f" {name}")

    if CUSTOM_AMOUNT in amount:
        amount = _typed_amount(amount)

    amount = " ".join(amount.split())

    return _decimal_fraction(amount)


def _typed_amount(amount: str) -> str:
    """An amount the User typed in the app: "0.1 :custom:10 g" is shown as "10 g".

    Above one serving FatSecret adds a stray "s": "2 1/2 :custom:250s  g" is shown as "250 g".
    """
    typed = amount.split(CUSTOM_AMOUNT, 1)[1]

    return STRAY_PLURAL.sub(r"\1 ", typed)


def _decimal_fraction(amount: str) -> str:
    """A leading fraction as a decimal: "187 1/2 g" is "187.5 g", "1/3 cup" is "0.33 cup"."""
    match = LEADING_FRACTION.match(amount)

    if match is None or match["denominator"] == "0":
        return amount

    whole = int(match["whole"] or 0)
    value = whole + int(match["numerator"]) / int(match["denominator"])
    decimal = f"{value:.2f}".rstrip("0").rstrip(".")

    return decimal + amount[match.end() :]


def _meal(raw: str) -> Meal:
    try:
        return MEALS[raw.lower()]
    except KeyError:
        raise UpstreamFailure("an unknown Meal") from None


def _as_list(value: object) -> list[dict[str, Any]]:
    """FatSecret sends one item as a bare object and no items as `null` or not at all."""
    if value is None:
        return []

    if isinstance(value, dict):
        return [cast(dict[str, Any], value)]

    if isinstance(value, list) and all(isinstance(item, dict) for item in cast(list[object], value)):
        return cast(list[dict[str, Any]], value)

    raise UpstreamFailure("unexpected response")


def _object(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)

    if value is None:
        return {}

    if not isinstance(value, dict):
        raise UpstreamFailure("unexpected response")

    return cast(dict[str, Any], value)


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)

    if not isinstance(value, str):
        raise UpstreamFailure("unexpected response")

    return value


def _number(raw: dict[str, Any], key: str, *, default: float | None = None) -> float:
    """FatSecret sends numbers as strings ("148", "2.000"). A nutrient it doesn't know is left out."""
    value = raw.get(key)

    if value is None and default is not None:
        return default

    try:
        return float(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        raise UpstreamFailure("unexpected response") from None
