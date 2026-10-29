"""Stand-ins: a catalog Food logged in place of a product missing from the catalog.

The closest Food is the one whose Macros differ least once it's scaled to the label's kcal. Its
amount is then set so that the logged kcal equal the label's kcal for the grams eaten.
"""

from __future__ import annotations

import math
from dataclasses import replace

from fatsecret_mcp.domain.drafts import UNIT_DECIMALS, InvalidDraftItem, draft_item, metric_serving
from fatsecret_mcp.domain.models import DraftItem, Food, NutritionLabel, Serving, StandIn

KCAL_PER_GRAM_PROTEIN = 4
KCAL_PER_GRAM_FAT = 9
KCAL_PER_GRAM_CARBOHYDRATE = 4
# Fibre, polyols and rounding make a label's kcal differ from 4/9/4 kcal per gram of Macros.
KCAL_TOLERANCE = 25
KCAL_TOLERANCE_SHARE = 0.2


def check_label(label: NutritionLabel) -> None:
    """Reject a Nutrition Label whose kcal don't roughly agree with its Macros."""
    per_100_g = label.per_100_g
    macro_kcal = (
        KCAL_PER_GRAM_PROTEIN * per_100_g.protein
        + KCAL_PER_GRAM_FAT * per_100_g.fat
        + KCAL_PER_GRAM_CARBOHYDRATE * per_100_g.carbohydrate
    )
    tolerance = KCAL_TOLERANCE + KCAL_TOLERANCE_SHARE * macro_kcal

    if abs(per_100_g.kcal - macro_kcal) > tolerance:
        raise InvalidDraftItem(
            f"The Nutrition Label's {per_100_g.kcal:g} kcal per 100 g don't agree with its Macros, "
            f"which give about {macro_kcal:.0f} kcal. Check the label's values."
        )


def stand_in_item(
    product_name: str,
    label: NutritionLabel,
    grams: float,
    foods: list[Food],
    assumption: str | None,
) -> DraftItem:
    """The Draft item logging `grams` of the product as the closest of `foods`, under its own name.

    `foods` are the catalog search results in their order, which settles ties.
    """
    candidates = _gram_servings(foods)

    if not candidates:
        raise InvalidDraftItem(
            "No catalog Food with a Serving in grams matches this search_phrase. "
            "Draft again with a different, more generic English search_phrase."
        )

    food, closest = _closest(candidates, label)
    # Matched in the Serving it's logged in, so the logged kcal equal the label's.
    serving = metric_serving(food, closest)
    eaten = label.per_100_g.scaled(grams / 100)
    kcal_per_unit = serving.nutrition.kcal / serving.number_of_units
    units = round(eaten.kcal / kcal_per_unit, UNIT_DECIMALS)

    if units <= 0:
        raise InvalidDraftItem("Amount is too small to log.")

    item = draft_item(food, product_name, serving, units, assumption)
    stand_in = StandIn(
        food_name=food.name,
        grams_eaten=grams,
        label_for_grams_eaten=eaten,
        estimated=label.estimated,
    )

    return replace(item, stand_in=stand_in)


def _gram_servings(foods: list[Food]) -> list[tuple[Food, Serving]]:
    """Every Serving with a metric amount in grams and some kcal, in catalog order."""
    candidates: list[tuple[Food, Serving]] = []

    for food in foods:
        for serving in food.servings:
            if _scalable(serving):
                candidates.append((food, serving))

    return candidates


def _scalable(serving: Serving) -> bool:
    in_grams = serving.metric_unit == "g" and serving.metric_amount is not None and serving.metric_amount > 0
    has_units = math.isfinite(serving.number_of_units) and serving.number_of_units > 0

    return in_grams and has_units and serving.nutrition.kcal > 0


def _closest(candidates: list[tuple[Food, Serving]], label: NutritionLabel) -> tuple[Food, Serving]:
    """The first candidate with the smallest Macro difference, so the same results give the same choice."""

    def difference(candidate: tuple[Food, Serving]) -> float:
        _, serving = candidate

        return _macro_difference(serving, label)

    return min(candidates, key=difference)


def _macro_difference(serving: Serving, label: NutritionLabel) -> float:
    """Grams of protein, fat and carbohydrate apart, weighted equally, at the label's kcal per 100 g."""
    # Scaling to equal kcal makes the Serving's own size cancel out.
    at_label_kcal = serving.nutrition.scaled(label.per_100_g.kcal / serving.nutrition.kcal)
    per_100_g = label.per_100_g
    protein = abs(at_label_kcal.protein - per_100_g.protein)
    fat = abs(at_label_kcal.fat - per_100_g.fat)
    carbohydrate = abs(at_label_kcal.carbohydrate - per_100_g.carbohydrate)

    return protein + fat + carbohydrate
