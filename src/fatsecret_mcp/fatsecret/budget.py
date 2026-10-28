"""Each User's share of the Household's daily FatSecret quota, counted in memory.

FatSecret allows 5,000 requests a day for the server's consumer key. Each User may make an equal
share of 4,500 of them in any 24 hours, which leaves the rest for the Operator's commands. A rolling
window keeps the Household under the quota whenever FatSecret's own day starts. The counts are
forgotten on restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fatsecret_mcp.domain.diary_day import Clock
from fatsecret_mcp.domain.models import CatalogFood, DraftEntry, EntryEdit, Food, FoodEntry, Meal, Nutrition
from fatsecret_mcp.fatsecret.errors import BudgetSpent, FatSecretError
from fatsecret_mcp.fatsecret.gateway import AccessToken, FatSecretGateway, RequestToken
from fatsecret_mcp.limits import RollingWindow

FATSECRET_DAILY_QUOTA = 5000
OPERATOR_RESERVE = 500
WINDOW = timedelta(hours=24)

logger = logging.getLogger(__name__)


def daily_budget(user_count: int) -> int:
    """Each User's requests in 24 hours, so the whole Household fits under FatSecret's quota."""
    return (FATSECRET_DAILY_QUOTA - OPERATOR_RESERVE) // user_count


class DailyBudget:
    """At most `requests` FatSecret requests in any 24 hours; each frees up a day after it was made."""

    def __init__(self, requests: int, timezone: ZoneInfo, clock: Clock) -> None:
        self._requests = requests
        self._timezone = timezone
        self._clock = clock
        self._spent = RollingWindow(
            limit=requests,
            seconds=WINDOW.total_seconds(),
        )

    def spend(self) -> float:
        """Count one request and return when it was counted, or refuse it once the budget is spent."""
        now = self._clock()
        counted_at = now.timestamp()
        wait = self._spent.admit(counted_at)

        if wait is not None:
            frees_up_at = (now + timedelta(seconds=wait)).astimezone(self._timezone)

            logger.warning(
                "FatSecret request not sent: BudgetSpent (a daily budget of %d requests)", self._requests
            )

            raise BudgetSpent(self._requests, frees_up_at)

        return counted_at

    def refund(self, counted_at: float) -> None:
        """Give back a request that was never sent."""
        self._spent.forget(counted_at)


class BudgetedGateway:
    """The FatSecret gateway as one User's tools see it: every request sent counts towards their budget."""

    def __init__(self, fatsecret: FatSecretGateway, budget: DailyBudget) -> None:
        self._fatsecret = fatsecret
        self._budget = budget

    async def search_catalog(self, query: str, *, max_results: int | None = None) -> list[CatalogFood]:
        return await self._counted(lambda: self._fatsecret.search_catalog(query, max_results=max_results))

    async def get_food(self, food_id: str) -> Food:
        return await self._counted(lambda: self._fatsecret.get_food(food_id))

    async def list_food_entries(self, user: AccessToken, day: date) -> list[FoodEntry]:
        return await self._counted(lambda: self._fatsecret.list_food_entries(user, day))

    async def get_month_totals(self, user: AccessToken, month: date) -> dict[date, Nutrition]:
        return await self._counted(lambda: self._fatsecret.get_month_totals(user, month))

    async def create_food_entry(self, user: AccessToken, entry: DraftEntry, meal: Meal, day: date) -> str:
        return await self._counted(lambda: self._fatsecret.create_food_entry(user, entry, meal, day))

    async def edit_food_entry(self, user: AccessToken, edit: EntryEdit) -> None:
        return await self._counted(lambda: self._fatsecret.edit_food_entry(user, edit))

    async def delete_food_entry(self, user: AccessToken, food_entry_id: str) -> None:
        return await self._counted(lambda: self._fatsecret.delete_food_entry(user, food_entry_id))

    async def request_token(self) -> RequestToken:
        return await self._counted(self._fatsecret.request_token)

    def authorize_url(self, request_token: RequestToken) -> str:
        return self._fatsecret.authorize_url(request_token)

    async def exchange_pin(self, request_token: RequestToken, pin: str) -> AccessToken:
        return await self._counted(lambda: self._fatsecret.exchange_pin(request_token, pin))

    async def _counted[T](self, request: Callable[[], Awaitable[T]]) -> T:
        """Send `request` within the budget; a request the server refused itself costs nothing."""
        counted_at = self._budget.spend()

        try:
            return await request()
        except FatSecretError as error:
            if not error.sent:
                self._budget.refund(counted_at)

            raise
