"""The MCP server: its name, its instructions for Claude, and the tools it offers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

from mcp.server import MCPServer
from pydantic import SecretStr

from fatsecret_mcp.config.models import User
from fatsecret_mcp.domain.diary_day import Clock, system_clock
from fatsecret_mcp.fatsecret.budget import BudgetedGateway, DailyBudget
from fatsecret_mcp.fatsecret.diary import UserDiary
from fatsecret_mcp.fatsecret.food_cache import FoodCache
from fatsecret_mcp.fatsecret.gateway import FatSecretGateway
from fatsecret_mcp.tools import (
    confirm_draft,
    draft_entries,
    get_diary_day,
    get_period_summary,
    search_foods,
)
from fatsecret_mcp.tools.schemas import ATTRIBUTION

DESCRIPTION = (
    "Unofficial MCP server for a FatSecret Food Diary, not affiliated with or endorsed by FatSecret. "
    f"{ATTRIBUTION}."
)

INSTRUCTIONS = f"""\
This server reads the User's own FatSecret Food Diary, the one they see in the FatSecret app.

- get_diary_day shows one Diary Day: Food Entries grouped by Meal, the day's totals, the User's Daily \
Targets and what remains. Omit diary_day for today: the server knows the User's timezone.
- get_period_summary shows daily totals and Daily Targets for an inclusive range of up to 31 Diary \
Days. Days with has_food_entries=false have no Food Entries. Compare periods with two calls.
- search_foods finds catalog Foods with loggable Servings. Search a composed dish as a whole first \
(e.g. "beef bibimbap"), and its components separately only when the dish has no good match. Pass \
an English rendering for non-English descriptions. A not_found result means there are no \
loggable matches.
- When a branded product isn't among the matches, ask for a photo of its Nutrition Label, unless \
the User already gave its values, and draft it as a Nutrition Label item: the server logs it as a \
Stand-in, the closest catalog Food with kcal matching the label, under the product's name. The \
server keeps no record of Stand-ins: for a product logged before, reuse its label from this \
conversation or your memory, or ask again. Estimate the label only when the User has no packaging. \
If the User dislikes the Stand-in, draft again with a different search phrase.
- For "the same as" an earlier meal, read that Diary Day with get_diary_day instead of searching \
the catalog. List its Food Entries with their amounts and ask whether everything was the same, or \
what to leave out or eat more or less of. Then draft the chosen entries as Diary Entry items by \
food_entry_id. They keep their names, so a product logged in the FatSecret app or as a Stand-in is \
repeated without its Nutrition Label.
- To change or remove logged Food Entries, draft change items by food_entry_id. For "undo that" \
after a confirm, remove the food_entry_ids confirm_draft returned; undoing a change is a change \
back. For any other change, read the Diary Day first, name the entries to change and ask when it's \
unclear which one is meant. A different Food or Diary Day is a removal plus a new item in the same \
Draft. A removed entry can't be restored, only logged again.
- draft_entries proposes Food Entries, assumptions, a Meal and Diary Day, plus the day after adding \
against Daily Targets, and changes before and after. Show the Draft and ask about a suggested \
Meal. When previous_day_suggested is true, ask about Snacks on the previous Diary Day. Name each \
catalog Food item in the User's own words and language, with a short note in that language that it \
was matched in the US catalog. Corrections need a new Draft. This tool does not write to the Food \
Diary.
- confirm_draft writes a Draft to the Food Diary: new entries first, then changes. Call it only \
after the User explicitly approved that Draft in their latest message.
- Macros are protein, fat and carbohydrate, in grams.
- Report the numbers neutrally and in the User's language. This server gives no diet, nutrition or \
health advice.

{ATTRIBUTION}.
"""


def build_server(
    user: User,
    fatsecret: FatSecretGateway,
    clock: Clock = system_clock,
    *,
    signing_key: SecretStr,
    daily_requests: int,
    food_cache: FoodCache,
) -> MCPServer:
    """The MCP server acting as `user`: it reads their diary and writes the Drafts they confirm.

    Every FatSecret request it makes counts towards the User's `daily_requests`. Foods come from
    `food_cache`, which the server's owner shares between Users and keeps running.
    """
    budget = DailyBudget(
        requests=daily_requests,
        timezone=user.timezone,
        clock=clock,
    )
    budgeted = BudgetedGateway(
        fatsecret=fatsecret,
        budget=budget,
    )
    diary = UserDiary(
        fatsecret=budgeted,
        user=user.fatsecret,
        timezone=user.timezone,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncGenerator[None]:
        async with diary.cache_lifespan():
            yield

    server = MCPServer(
        name="fatsecret-mcp",
        title="FatSecret",
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        version=version("fatsecret-mcp"),
        lifespan=lifespan,
    )

    get_diary_day.register(server, user, diary, clock)
    get_period_summary.register(server, user, diary)
    search_foods.register(server, budgeted, food_cache)
    draft_entries.register(server, user, budgeted, food_cache, diary, clock, signing_key)
    confirm_draft.register(server, user, budgeted, diary, clock, signing_key)

    return server
