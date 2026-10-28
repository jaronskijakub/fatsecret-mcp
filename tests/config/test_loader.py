"""Household configuration: loaded once from the environment, failing fast without printing secrets."""

from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from fatsecret_mcp.auth.passwords import hash_password
from fatsecret_mcp.config.loader import HouseholdConfigError, acting_user, load_household

SECRETS = [
    "consumer-key-0123",
    "consumer-secret-4567",
    "ola-token-89ab",
    "ola-secret-cdef",
    "test-signing-key-0123456789abcdef",
]


def ola(**overrides: Any) -> dict[str, Any]:
    user: dict[str, Any] = {
        "id": "ola",
        "display_name": "Ola",
        "fatsecret_access_token": "ola-token-89ab",
        "fatsecret_access_secret": "ola-secret-cdef",
        "daily_targets": {"kcal": 1900, "protein": 110, "fat": 65, "carbohydrate": 210},
    }

    user.update(overrides)
    return user


def environ(*users: dict[str, Any], **variables: str) -> dict[str, str]:
    return {
        "FATSECRET_CONSUMER_KEY": "consumer-key-0123",
        "FATSECRET_CONSUMER_SECRET": "consumer-secret-4567",
        "FATSECRET_MCP_SIGNING_KEY": "test-signing-key-0123456789abcdef",
        "FATSECRET_MCP_USERS": json.dumps(list(users)),
        **variables,
    }


def config_error(env: dict[str, str]) -> str:
    with pytest.raises(HouseholdConfigError) as raised:
        load_household(env)
    message = str(raised.value)

    for secret in SECRETS:
        assert secret not in message
    return message


def test_loads_the_consumer_key_and_each_user() -> None:
    household = load_household(environ(ola(), ola(id="jakub", display_name="Jakub", timezone="UTC")))

    assert household.consumer.key == "consumer-key-0123"
    assert household.consumer.secret == "consumer-secret-4567"
    assert [user.id for user in household.users] == ["ola", "jakub"]
    user = household.user("ola")

    assert user.display_name == "Ola"
    assert user.fatsecret.token == "ola-token-89ab"
    assert user.fatsecret.secret == "ola-secret-cdef"
    assert user.daily_targets.kcal == 1900
    assert user.daily_targets.carbohydrate == 210
    assert household.user("jakub").timezone == ZoneInfo("UTC")


def test_a_users_timezone_defaults_to_europe_warsaw() -> None:
    household = load_household(environ(ola()))

    assert household.user("ola").timezone == ZoneInfo("Europe/Warsaw")


def test_loads_the_public_url_and_session_generation() -> None:
    household = load_household(
        environ(ola(session_generation=3), FATSECRET_MCP_BASE_URL="https://diary.example.com")
    )

    assert household.public_base_url == "https://diary.example.com"
    assert household.user("ola").session_generation == 3


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "http://diary.example.com",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?secret=value",
        "https://example.com#fragment",
        "",
    ],
)
def test_an_invalid_public_url_is_rejected_without_printing_it(url: str) -> None:
    message = config_error(environ(ola(), FATSECRET_MCP_BASE_URL=url))

    assert "FATSECRET_MCP_BASE_URL" in message
    assert "secret" not in message


@pytest.mark.parametrize("generation", [-1, "1", True, 1.5])
def test_session_generation_is_a_nonnegative_integer(generation: object) -> None:
    assert "session_generation" in config_error(environ(ola(session_generation=generation)))


def test_loads_a_connector_password_hash_without_showing_it() -> None:
    password_hash = hash_password("my-connector-password")
    household = load_household(environ(ola(connector_password_hash=password_hash)))
    stored = household.user("ola").connector_password_hash

    assert stored is not None
    assert stored.get_secret_value() == password_hash
    assert password_hash not in repr(household)


@pytest.mark.parametrize(
    "password_hash",
    ["", "plaintext-password", "scrypt$1073741824$8$1$salt$hash", "scrypt$32768$8$1$aa$bb"],
)
def test_a_malformed_or_unbounded_password_hash_is_rejected(password_hash: str) -> None:
    message = config_error(environ(ola(connector_password_hash=password_hash)))

    assert "[0].connector_password_hash" in message
    assert "plaintext-password" not in message


def test_secrets_never_show_in_a_printed_household() -> None:
    household = load_household(environ(ola()))

    printed = f"{household!r} {household} {household.consumer!r} {household.user('ola').fatsecret!r}"

    for secret in SECRETS:
        assert secret not in printed


@pytest.mark.parametrize(
    "variable",
    [
        "FATSECRET_CONSUMER_KEY",
        "FATSECRET_CONSUMER_SECRET",
        "FATSECRET_MCP_USERS",
        "FATSECRET_MCP_SIGNING_KEY",
    ],
)
def test_a_missing_variable_is_named(variable: str) -> None:
    env = environ(ola())

    del env[variable]

    assert variable in config_error(env)


@pytest.mark.parametrize(
    ("user", "bad_field"),
    [
        (
            ola(daily_targets={"kcal": 0, "protein": 110, "fat": 65, "carbohydrate": 210}),
            "[0].daily_targets.kcal",
        ),
        (
            ola(daily_targets={"kcal": 1900, "protein": -1, "fat": 65, "carbohydrate": 210}),
            "[0].daily_targets.protein",
        ),
        (ola(daily_targets={"kcal": 1900, "protein": 110, "fat": 65}), "[0].daily_targets.carbohydrate"),
        (ola(fatsecret_access_secret=""), "[0].fatsecret_access_secret"),
        (ola(id="Ola Nowak"), "[0].id"),
        (ola(display_name=" "), "[0].display_name"),
        (ola(timezone_="Europe/Warsaw"), "[0].timezone_"),
        # An access token pasted into the wrong field must not be echoed back.
        (ola(timezone="ola-token-89ab"), "[0].timezone"),
    ],
)
def test_an_invalid_user_field_is_named_without_its_value(user: dict[str, Any], bad_field: str) -> None:
    assert f"FATSECRET_MCP_USERS{bad_field}" in config_error(environ(user))


def test_a_user_without_an_access_token_is_rejected() -> None:
    user = ola()

    del user["fatsecret_access_token"]

    assert "FATSECRET_MCP_USERS[0].fatsecret_access_token" in config_error(environ(user))


def test_users_that_arent_valid_json_are_rejected_without_echoing_them() -> None:
    env = environ(ola())
    env["FATSECRET_MCP_USERS"] = '[{"id": "ola", "fatsecret_access_token": "ola-token-89ab",'

    assert "FATSECRET_MCP_USERS" in config_error(env)


def test_a_household_needs_at_least_one_user() -> None:
    assert "at least one User" in config_error(environ())


def test_user_ids_are_unique() -> None:
    assert "ola" in config_error(environ(ola(), ola(display_name="Ola again")))


def test_over_stdio_the_server_acts_as_the_user_named_in_the_environment() -> None:
    env = environ(ola(), ola(id="jakub"), FATSECRET_MCP_USER="jakub")

    assert acting_user(load_household(env), env).id == "jakub"


def test_over_stdio_a_household_of_one_needs_no_acting_user() -> None:
    env = environ(ola())

    assert acting_user(load_household(env), env).id == "ola"


@pytest.mark.parametrize("acting", [None, "kasia"])
def test_over_stdio_the_acting_user_must_be_one_of_several(acting: str | None) -> None:
    env = environ(ola(), ola(id="jakub"))

    if acting:
        env["FATSECRET_MCP_USER"] = acting

    with pytest.raises(HouseholdConfigError) as raised:
        acting_user(load_household(env), env)

    assert "FATSECRET_MCP_USER" in str(raised.value)
    assert "ola, jakub" in str(raised.value)


@pytest.mark.parametrize("value", ["", "short", "x" * 31])
def test_the_instance_needs_a_persistent_signing_key_of_at_least_32_characters(value: str) -> None:
    env = environ(ola(), FATSECRET_MCP_SIGNING_KEY=value)
    message = config_error(env)

    assert "FATSECRET_MCP_SIGNING_KEY" in message

    if value:
        assert value not in message


def test_the_signing_key_is_loaded_from_household_configuration() -> None:
    env = environ(ola(), FATSECRET_MCP_SIGNING_KEY="s" * 32)
    household = load_household(env)

    assert household.signing_key.get_secret_value() == "s" * 32


@pytest.mark.parametrize(
    "hours",
    [
        {"breakfast": "24:00"},
        {"lunch": "12:60"},
        {"dinner": "16:00:01"},
        {"breakfast": "ola-token-89ab"},
        {"breakfast": "11:30"},  # Collides with the default Lunch start.
    ],
)
def test_invalid_or_colliding_meal_hours_are_rejected_without_echoing_values(hours: dict[str, str]) -> None:
    assert "meal_hours" in config_error(environ(ola(meal_hours=hours)))


def test_partial_meal_hours_keep_the_defaults_for_other_meals() -> None:
    household = load_household(environ(ola(meal_hours={"breakfast": "06:00"})))
    hours = household.user("ola").meal_hours

    assert hours.breakfast == "06:00"
    assert hours.lunch == "11:30"
    assert hours.dinner == "16:00"
    assert hours.snacks == "21:00"


def test_only_claudes_connector_is_on_by_default() -> None:
    household = load_household(environ(ola()))

    assert household.connector_clients == ("claude",)


@pytest.mark.parametrize(
    ("value", "clients"),
    [
        ("claude,chatgpt", ("claude", "chatgpt")),
        (" chatgpt ", ("chatgpt",)),
        ("chatgpt, claude, chatgpt", ("chatgpt", "claude")),
    ],
)
def test_the_operator_turns_known_connector_clients_on(value: str, clients: tuple[str, ...]) -> None:
    household = load_household(environ(ola(), FATSECRET_MCP_CLIENTS=value))

    assert household.connector_clients == clients


def test_an_unknown_connector_client_is_named() -> None:
    message = config_error(environ(ola(), FATSECRET_MCP_CLIENTS="claude,gemini"))

    assert "FATSECRET_MCP_CLIENTS" in message
    assert "gemini" in message
    assert "chatgpt, claude" in message


@pytest.mark.parametrize("value", ["", " ", ",", "claude,"])
def test_an_empty_connector_client_name_is_rejected(value: str) -> None:
    message = config_error(environ(ola(), FATSECRET_MCP_CLIENTS=value))

    assert "FATSECRET_MCP_CLIENTS" in message
