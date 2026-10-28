"""Seal a self-contained Draft with the instance's persistent signing key, and open it again."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from fatsecret_mcp.domain.models import (
    Additions,
    Draft,
    DraftEntry,
    EntryChange,
    EntryEdit,
    EntryRemoval,
    Meal,
)
from fatsecret_mcp.signing import from_base64url, signature, signature_matches, to_base64url


class InvalidDraft(ValueError):
    """A Draft id that can't be confirmed: tampered with, expired, or another User's."""


def seal_draft(draft: Draft, signing_key: str) -> str:
    additions = draft.additions
    entries = additions.entries if additions is not None else ()
    items = [asdict(entry) for entry in entries]
    changes = [_change_payload(change) for change in draft.changes]
    # A Draft without changes keeps the shape it had before changes existed, so either opens.
    payload: dict[str, Any] = {
        "user_id": draft.user_id,
        "items": items,
        "meal": additions.meal.value if additions is not None else None,
        "diary_day": additions.diary_day.isoformat() if additions is not None else None,
        "expires_at": draft.expires_at.isoformat(),
    }

    if changes:
        payload["changes"] = changes

    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = to_base64url(serialized.encode())
    signed = signature(encoded.encode(), signing_key)

    return f"{encoded}.{signed}"


def open_draft(draft_id: str, signing_key: str, *, user_id: str, now: datetime) -> Draft:
    """The Draft sealed in `draft_id`, once its signature, User and expiry check out."""
    encoded, _, signed = draft_id.partition(".")

    if not signature_matches(signed, encoded.encode(), signing_key):
        raise InvalidDraft("This isn't a valid Draft id: it was changed or not issued by this server.")

    draft = _draft(json.loads(from_base64url(encoded)))

    if draft.user_id != user_id:
        raise InvalidDraft("This Draft belongs to another User. Draft the Food Entries again.")
    if draft.expires_at <= now:
        raise InvalidDraft("This Draft has expired. Draft the Food Entries again.")

    return draft


def _change_payload(change: EntryChange) -> dict[str, Any]:
    if isinstance(change, EntryRemoval):
        return {
            "kind": "remove",
            "food_entry_id": change.food_entry_id,
            "diary_day": change.diary_day.isoformat(),
        }

    return {
        "kind": "edit",
        "food_entry_id": change.food_entry_id,
        "diary_day": change.diary_day.isoformat(),
        "number_of_units": change.number_of_units,
        "meal": change.meal.value if change.meal is not None else None,
        "entry_name": change.entry_name,
    }


def _draft(payload: dict[str, Any]) -> Draft:
    # The valid signature proves seal_draft wrote this payload, so its shape needs no checks.
    changes = [_change(change) for change in payload.get("changes", [])]

    return Draft(
        user_id=payload["user_id"],
        additions=_additions(payload),
        changes=tuple(changes),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


def _additions(payload: dict[str, Any]) -> Additions | None:
    if payload["meal"] is None:
        return None

    entries = [_draft_entry(item) for item in payload["items"]]

    return Additions(
        entries=tuple(entries),
        meal=Meal(payload["meal"]),
        diary_day=date.fromisoformat(payload["diary_day"]),
    )


def _draft_entry(item: dict[str, Any]) -> DraftEntry:
    return DraftEntry(
        food_id=item["food_id"],
        serving_id=item["serving_id"],
        number_of_units=item["number_of_units"],
        entry_name=item["entry_name"],
    )


def _change(change: dict[str, Any]) -> EntryChange:
    diary_day = date.fromisoformat(change["diary_day"])

    if change["kind"] == "remove":
        return EntryRemoval(
            food_entry_id=change["food_entry_id"],
            diary_day=diary_day,
        )

    meal = change["meal"]

    return EntryEdit(
        food_entry_id=change["food_entry_id"],
        diary_day=diary_day,
        number_of_units=change["number_of_units"],
        meal=Meal(meal) if meal is not None else None,
        entry_name=change["entry_name"],
    )
