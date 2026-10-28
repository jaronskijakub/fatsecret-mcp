"""The shape of the Household configuration, and the rules each field must follow."""

from __future__ import annotations

from datetime import time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    SecretStr,
    StringConstraints,
)

from fatsecret_mcp.domain.models import Meal, Nutrition
from fatsecret_mcp.fatsecret.gateway import AccessToken, ConsumerKey

DEFAULT_TIMEZONE = ZoneInfo("Europe/Warsaw")
DEFAULT_CONNECTOR_CLIENTS = ("claude",)


def _timezone(value: object) -> object:
    if not isinstance(value, str):
        return value

    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        # A fixed message: pydantic's own would quote the value, which might be a misplaced secret.
        raise ValueError("not a known IANA timezone name, such as Europe/Warsaw") from None


NonEmptySecret = Annotated[SecretStr, Field(min_length=1)]
UserId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Timezone = Annotated[ZoneInfo, BeforeValidator(_timezone)]
MealStart = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class ConfigModel(BaseModel):
    """Base for every configuration model.

    - `frozen`: read-only once loaded.
    - `extra="forbid"`: an unknown field, such as a typo, is an error rather than silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class DailyTargets(ConfigModel):
    """What the User aims to eat in one day: kcal, and Macros in grams."""

    kcal: PositiveFloat
    protein: NonNegativeFloat
    fat: NonNegativeFloat
    carbohydrate: NonNegativeFloat

    def as_nutrition(self) -> Nutrition:
        return Nutrition(
            kcal=self.kcal,
            protein=self.protein,
            fat=self.fat,
            carbohydrate=self.carbohydrate,
        )


class MealHours(ConfigModel):
    """Local start of each Meal, up to the next Meal start (including across midnight)."""

    breakfast: MealStart = "04:00"
    lunch: MealStart = "11:30"
    dinner: MealStart = "16:00"
    snacks: MealStart = "21:00"

    def as_starts(self) -> dict[Meal, time]:
        return {
            Meal.BREAKFAST: time.fromisoformat(self.breakfast),
            Meal.LUNCH: time.fromisoformat(self.lunch),
            Meal.DINNER: time.fromisoformat(self.dinner),
            Meal.SNACKS: time.fromisoformat(self.snacks),
        }


class User(ConfigModel):
    id: UserId
    display_name: DisplayName
    fatsecret_access_token: NonEmptySecret
    fatsecret_access_secret: NonEmptySecret
    daily_targets: DailyTargets
    timezone: Timezone = DEFAULT_TIMEZONE
    meal_hours: MealHours = Field(default_factory=MealHours)
    connector_password_hash: SecretStr | None = None
    session_generation: Annotated[int, Field(strict=True, ge=0)] = 0

    @property
    def fatsecret(self) -> AccessToken:
        return AccessToken(
            token=self.fatsecret_access_token.get_secret_value(),
            secret=self.fatsecret_access_secret.get_secret_value(),
        )


class Household(ConfigModel):
    consumer_key: SecretStr
    consumer_secret: SecretStr
    signing_key: Annotated[SecretStr, Field(min_length=32)]
    users: tuple[User, ...]
    public_base_url: str | None = None
    connector_clients: tuple[str, ...] = DEFAULT_CONNECTOR_CLIENTS

    @property
    def consumer(self) -> ConsumerKey:
        return ConsumerKey(
            key=self.consumer_key.get_secret_value(),
            secret=self.consumer_secret.get_secret_value(),
        )

    def user(self, user_id: str) -> User:
        for user in self.users:
            if user.id == user_id:
                return user

        raise KeyError(user_id)
