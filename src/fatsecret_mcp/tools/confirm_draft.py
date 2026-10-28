"""confirm_draft: write a Draft the User approved to their Food Diary."""

from __future__ import annotations

from datetime import date
from typing import Annotated

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import SecretStr

from fatsecret_mcp.config.models import User
from fatsecret_mcp.domain.confirmed_drafts import ConfirmedDrafts
from fatsecret_mcp.domain.diary_day import Clock
from fatsecret_mcp.domain.draft_token import InvalidDraft, open_draft
from fatsecret_mcp.domain.models import (
    Confirmation,
    ConfirmedChange,
    ConfirmedEntry,
    Draft,
    EntryChange,
    EntryRemoval,
)
from fatsecret_mcp.fatsecret.diary import UserDiary
from fatsecret_mcp.fatsecret.errors import FatSecretError, NotFound, UpstreamFailure
from fatsecret_mcp.fatsecret.gateway import AccessToken, FatSecretGateway
from fatsecret_mcp.tools.schemas import DraftConfirmation, DraftId, as_tool_result


def register(
    server: MCPServer,
    user: User,
    fatsecret: FatSecretGateway,
    diary: UserDiary,
    clock: Clock,
    signing_key: SecretStr,
) -> None:
    annotations = ToolAnnotations(
        title="Confirm Draft",
        read_only_hint=False,
        destructive_hint=True,
        # Confirmed Drafts are remembered in memory only, so a repeat after a restart writes again.
        idempotent_hint=False,
    )
    # Claude Code asks the User before every call, even when it otherwise runs tools on its own.
    meta = {"anthropic/requiresUserInteraction": True}
    confirmed_drafts = ConfirmedDrafts()
    # One confirm at a time, so a double tap can't write the same Draft twice.
    confirming = anyio.Lock()

    @server.tool(annotations=annotations, meta=meta)
    async def confirm_draft(draft_id: DraftId) -> Annotated[CallToolResult, DraftConfirmation]:
        """Write a Draft to the User's Food Diary: its new Food Entries first, then its changes.

        New entries go under the Draft's Meal and Diary Day. A removed entry can't be restored.
        Call this only after the User explicitly approved this Draft in their latest message.
        After a correction, draft again and confirm the new Draft instead.
        Confirm each Draft once: a repeat usually returns the earlier result, but after a server
        restart it would write the Draft again.
        """
        async with confirming:
            now = clock()

            try:
                draft = open_draft(
                    draft_id,
                    signing_key.get_secret_value(),
                    user_id=user.id,
                    now=now,
                )
            except InvalidDraft as error:
                raise ToolError(str(error)) from error

            earlier = confirmed_drafts.get(draft_id, now)

            if earlier is not None:
                return as_tool_result(DraftConfirmation.of(earlier, repeated=True))

            try:
                confirmation = await _write_draft(draft, user.fatsecret, fatsecret)
            finally:
                # Even a failed or cancelled write may have saved something, so each day is read afresh.
                with anyio.CancelScope(shield=True):
                    for day in _affected_days(draft):
                        await diary.invalidate(day)

            confirmed_drafts.remember(draft_id, confirmation)

        return as_tool_result(DraftConfirmation.of(confirmation, repeated=False))


async def _write_draft(draft: Draft, user: AccessToken, fatsecret: FatSecretGateway) -> Confirmation:
    """Write the new entries, then the changes, stopping at the first failure; fails when nothing was written.

    New entries go first, so a failure part-way leaves a duplicate the User can see, never a gap.
    """
    entries: list[ConfirmedEntry] = []
    changes: list[ConfirmedChange] = []
    failure: FatSecretError | None = None
    failed_change: EntryChange | None = None
    additions = draft.additions

    if additions is not None:
        for entry in additions.entries:
            food_entry_id: str | None = None

            if failure is None:
                try:
                    food_entry_id = await fatsecret.create_food_entry(
                        user, entry, additions.meal, additions.diary_day
                    )
                except FatSecretError as error:
                    failure = error

            confirmed = ConfirmedEntry(
                entry=entry,
                food_entry_id=food_entry_id,
            )

            entries.append(confirmed)

    for change in draft.changes:
        written = False

        if failure is None:
            try:
                await _make_change(change, user, fatsecret)
                written = True
            except FatSecretError as error:
                failure = error
                failed_change = change

        confirmed_change = ConfirmedChange(
            change=change,
            written=written,
        )

        changes.append(confirmed_change)

    if failure is not None and not _wrote_anything(entries, changes):
        raise ToolError(_nothing_written(failure, failed_change))

    return Confirmation(
        draft=draft,
        entries=tuple(entries),
        changes=tuple(changes),
        failure=_unwritten(failure, failed_change),
    )


async def _make_change(change: EntryChange, user: AccessToken, fatsecret: FatSecretGateway) -> None:
    if isinstance(change, EntryRemoval):
        await fatsecret.delete_food_entry(user, change.food_entry_id)
    else:
        await fatsecret.edit_food_entry(user, change)


def _affected_days(draft: Draft) -> list[date]:
    """The new entries' Diary Day and each Diary Day a change is on, each once."""
    days = [change.diary_day for change in draft.changes]

    if draft.additions is not None:
        days.insert(0, draft.additions.diary_day)

    return list(dict.fromkeys(days))


def _wrote_anything(entries: list[ConfirmedEntry], changes: list[ConfirmedChange]) -> bool:
    created = [entry for entry in entries if entry.food_entry_id is not None]
    changed = [change for change in changes if change.written]

    return bool(created or changed)


def _nothing_written(failure: FatSecretError, failed_change: EntryChange | None) -> str:
    if failed_change is not None:
        return f"{_change_failed(failure, failed_change)} Nothing else was written."
    if isinstance(failure, UpstreamFailure):
        # Without an answer, FatSecret may still have saved the first entry.
        return f"{failure} Check the Diary Day with get_diary_day before confirming this Draft again."

    return f"{failure} Nothing was written; the Draft can be confirmed again."


def _unwritten(failure: FatSecretError | None, failed_change: EntryChange | None) -> str | None:
    if failure is None:
        return None
    if failed_change is not None:
        return _change_failed(failure, failed_change)
    if isinstance(failure, UpstreamFailure):
        return f"{failure} The first entry without a food_entry_id may have been saved anyway."

    return str(failure)


def _change_failed(failure: FatSecretError, change: EntryChange) -> str:
    """Why a change failed, and that its Diary Day must be read again before drafting it anew."""
    read_again = (
        f"Read {change.diary_day.isoformat()} again with get_diary_day and refresh=true before "
        "drafting the change again."
    )

    if isinstance(failure, NotFound):
        return (
            f"Food Entry {change.food_entry_id} is no longer in the Food Diary: it was probably changed "
            f"or removed in the FatSecret app. {read_again}"
        )
    if isinstance(failure, UpstreamFailure):
        return f"{failure} The first change not written may have been made anyway. {read_again}"

    return f"{failure} {read_again}"
