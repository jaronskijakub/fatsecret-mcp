"""Logs never contain tokens, secrets or nutrition data, even at DEBUG level."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from mcp import Client
from pydantic import SecretStr

from fatsecret_mcp.config.loader import load_household
from fatsecret_mcp.domain.draft_token import seal_draft
from fatsecret_mcp.domain.models import Additions, Draft, DraftEntry, Meal
from fatsecret_mcp.fatsecret.budget import daily_budget
from fatsecret_mcp.fatsecret.client import FatSecretApi
from fatsecret_mcp.fatsecret.food_cache import FoodCache
from fatsecret_mcp.fatsecret.request_limits import RequestLimits
from fatsecret_mcp.server import build_server
from tests.conftest import FakeClock
from tests.fatsecret.recorded import CONSUMER, USER, RecordedFatSecret, fixture

pytestmark = pytest.mark.anyio

SECRETS = [CONSUMER.key, CONSUMER.secret, USER.token, USER.secret, "test-signing-key-0123456789abcdef"]
NUTRITION = ["Skyr Waniliowy", "Chicken Breast", "18.4", "39.89", "11.58", "97.01", "24.6"]

# Noon in Warsaw on the last recorded test Diary Day.
CLOCK = FakeClock(datetime(2015, 1, 14, 11, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("tool", "recorded"),
    [
        ("get_diary_day", "food_entries_get.json"),
        ("get_diary_day", "error_invalid_access_token.json"),
        ("get_period_summary", "food_entries_get_month.json"),
        ("get_period_summary", "error_invalid_access_token.json"),
        ("search_foods", "foods_search_single.json"),
        ("search_foods", "error_daily_quota.json"),
        ("draft_entries", "food_get_generic.json"),
        ("draft_entries", "error_invalid_access_token.json"),
        ("confirm_draft", "food_entry_create.json"),
        ("confirm_draft", "error_invalid_access_token.json"),
    ],
)
async def test_a_tool_call_logs_no_secrets_and_no_nutrition(
    tool: str, recorded: str, caplog: pytest.LogCaptureFixture, request_limits: RequestLimits
) -> None:
    caplog.set_level(logging.DEBUG)
    household = load_household(
        {
            "FATSECRET_CONSUMER_KEY": CONSUMER.key,
            "FATSECRET_MCP_SIGNING_KEY": "test-signing-key-0123456789abcdef",
            "FATSECRET_CONSUMER_SECRET": CONSUMER.secret,
            "FATSECRET_MCP_USERS": json.dumps(
                [
                    {
                        "id": "jakub",
                        "display_name": "Jakub",
                        "fatsecret_access_token": USER.token,
                        "fatsecret_access_secret": USER.secret,
                        "daily_targets": {"kcal": 2500, "protein": 160, "fat": 80, "carbohydrate": 280},
                    }
                ]
            ),
        }
    )
    fatsecret = RecordedFatSecret()

    if tool == "confirm_draft" and not recorded.startswith("error_"):
        fatsecret.serve(recorded)
    elif tool == "draft_entries" and not recorded.startswith("error_"):
        fatsecret.serve(recorded)
        fatsecret.serve("food_entries_get_empty.json")
    elif recorded == "foods_search_single.json":
        # Same recorded response, requested with the tool's three-result limit.
        exchange = fixture(recorded)
        exchange["request"]["params"]["max_results"] = "3"

        fatsecret.serve(exchange)
        fatsecret.serve("food_get_generic.json")
    else:
        fatsecret.answer_everything_with(recorded)

    async with fatsecret.client() as http:
        gateway = FatSecretApi(household.consumer, http, request_limits=request_limits)
        server = build_server(
            user=household.user("jakub"),
            fatsecret=gateway,
            clock=CLOCK,
            signing_key=household.signing_key,
            daily_requests=daily_budget(len(household.users)),
            food_cache=FoodCache(clock=CLOCK),
        )

        async with Client(server) as client:
            arguments = {
                "get_diary_day": {"diary_day": "2015-01-12"},
                "get_period_summary": {"from_day": "2015-01-12", "to_day": "2015-01-13"},
                "search_foods": {"description": "chicken breast"},
                "draft_entries": {
                    "items": [{"food_id": "1641", "serving_id": "50321", "number_of_units": 150}],
                },
                "confirm_draft": {"draft_id": recorded_skyr_draft(household.signing_key)},
            }

            result = await client.call_tool(tool, arguments[tool])

            assert result.is_error == recorded.startswith("error_")

    assert caplog.records, "expected the call to log something, or this test proves nothing"
    for leak in SECRETS + NUTRITION:
        assert leak not in caplog.text


def recorded_skyr_draft(signing_key: SecretStr) -> str:
    """A Draft of the recorded `food_entry.create`: two units of Skyr for lunch on 2015-01-12."""
    entry = DraftEntry(
        food_id="51467582",
        serving_id="43596665",
        number_of_units=2,
        entry_name="Skyr Waniliowy",
    )
    additions = Additions(
        entries=(entry,),
        meal=Meal.LUNCH,
        diary_day=date(2015, 1, 12),
    )
    draft = Draft(
        user_id="jakub",
        additions=additions,
        changes=(),
        expires_at=CLOCK.now + timedelta(minutes=30),
    )

    return seal_draft(draft, signing_key.get_secret_value())
