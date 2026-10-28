"""Reading the Household configuration from environment variables, once, at startup.

- `FATSECRET_CONSUMER_KEY`, `FATSECRET_CONSUMER_SECRET`: the server's OAuth 1.0 consumer key and secret.
- `FATSECRET_MCP_USERS`: a JSON list of Users, for example
  `[{"id": "ola", "display_name": "Ola", "fatsecret_access_token": "…", "fatsecret_access_secret": "…",
  "daily_targets": {"kcal": 2000, "protein": 120, "fat": 70, "carbohydrate": 220},
  "timezone": "Europe/Warsaw"}]`.
- `FATSECRET_MCP_USER`: over stdio, the id of the User the server acts as. Optional when there's only one.
- `FATSECRET_MCP_SIGNING_KEY`: a persistent, random instance key of at least 32 characters for signed values.
- `FATSECRET_MCP_BASE_URL`: the HTTPS origin for remote HTTP; loopback HTTP is allowed for development.
- `FATSECRET_MCP_CLIENTS`: over HTTP, the connector apps that may sign in, comma-separated, from `claude`
  and `chatgpt`. Defaults to `claude`.
- Over HTTP each User needs a `connector_password_hash` (scrypt); `session_generation` defaults to zero.
- Each User may set `meal_hours`: local start times as HH:MM for breakfast, lunch, dinner and snacks.

Errors name the bad variable or field but never print a value, since most values here are secrets.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from pydantic import AnyHttpUrl, SecretStr, TypeAdapter, ValidationError
from pydantic_core import ErrorDetails

from fatsecret_mcp.auth.client_metadata import KNOWN_CLIENTS
from fatsecret_mcp.auth.passwords import valid_password_hash
from fatsecret_mcp.config.models import DEFAULT_CONNECTOR_CLIENTS, Household, MealHours, User
from fatsecret_mcp.fatsecret.gateway import ConsumerKey

CONSUMER_KEY_VAR = "FATSECRET_CONSUMER_KEY"
CONSUMER_SECRET_VAR = "FATSECRET_CONSUMER_SECRET"
USERS_VAR = "FATSECRET_MCP_USERS"
ACTING_USER_VAR = "FATSECRET_MCP_USER"
SIGNING_KEY_VAR = "FATSECRET_MCP_SIGNING_KEY"
BASE_URL_VAR = "FATSECRET_MCP_BASE_URL"
CLIENTS_VAR = "FATSECRET_MCP_CLIENTS"

_USERS = TypeAdapter(list[User])


class HouseholdConfigError(Exception):
    """The Household configuration is missing or invalid. The message names the problem, never a value."""


def load_household(environ: Mapping[str, str]) -> Household:
    consumer = load_consumer_key(environ)
    signing_key = _required(environ, SIGNING_KEY_VAR)
    public_base_url = _public_base_url(environ)

    if len(signing_key) < 32:
        raise HouseholdConfigError(f"{SIGNING_KEY_VAR} must contain at least 32 characters")

    users = parse_users(_required(environ, USERS_VAR))
    connector_clients = _connector_clients(environ)

    return Household(
        consumer_key=SecretStr(consumer.key),
        consumer_secret=SecretStr(consumer.secret),
        signing_key=SecretStr(signing_key),
        users=tuple(users),
        public_base_url=public_base_url,
        connector_clients=connector_clients,
    )


def load_consumer_key(environ: Mapping[str, str]) -> ConsumerKey:
    """The server's FatSecret consumer key and secret, all that the PIN flow needs."""
    return ConsumerKey(
        key=_required(environ, CONSUMER_KEY_VAR),
        secret=_required(environ, CONSUMER_SECRET_VAR),
    )


def parse_users(value: str) -> list[User]:
    """The Users in a `FATSECRET_MCP_USERS` value, checked as the server checks them at startup."""
    try:
        users = _USERS.validate_json(value)
    except ValidationError as error:
        described = [_describe(details) for details in error.errors(include_input=False)]
        problems = "; ".join(described)

        raise HouseholdConfigError(f"Invalid Household configuration: {problems}") from None

    if not users:
        raise HouseholdConfigError(f"{USERS_VAR} must list at least one User")

    _validate_meal_hours(users)
    _validate_connector_passwords(users)

    repeated = _repeated_ids(users)

    if repeated:
        raise HouseholdConfigError(f"{USERS_VAR} repeats a User id: {', '.join(repeated)}")

    return users


def validate_http_household(household: Household) -> None:
    """Fail before binding a public socket if connector sign-in is not configured."""
    if household.public_base_url is None:
        raise HouseholdConfigError(f"{BASE_URL_VAR} is required over HTTP")

    for index, user in enumerate(household.users):
        if user.connector_password_hash is None:
            raise HouseholdConfigError(f"{USERS_VAR}[{index}].connector_password_hash is required over HTTP")


def _validate_connector_passwords(users: list[User]) -> None:
    for index, user in enumerate(users):
        stored = user.connector_password_hash

        if stored is not None and not valid_password_hash(stored.get_secret_value()):
            raise HouseholdConfigError(f"{USERS_VAR}[{index}].connector_password_hash: invalid scrypt hash")


def acting_user(household: Household, environ: Mapping[str, str]) -> User:
    """The User the server acts as over stdio, where there's no sign-in to tell Users apart."""
    user_id = environ.get(ACTING_USER_VAR, "").strip()

    if not user_id:
        if len(household.users) == 1:
            return household.users[0]
        raise HouseholdConfigError(f"{ACTING_USER_VAR} must name the User to act as: {_ids(household)}")

    try:
        return household.user(user_id)
    except KeyError:
        raise HouseholdConfigError(
            f"{ACTING_USER_VAR} names no User in {USERS_VAR}; the ids are: {_ids(household)}"
        ) from None


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()

    if not value:
        raise HouseholdConfigError(f"{name} is not set")

    return value


def _public_base_url(environ: Mapping[str, str]) -> str | None:
    if BASE_URL_VAR not in environ:
        return None

    try:
        url = AnyHttpUrl(environ[BASE_URL_VAR])
    except ValidationError:
        raise HouseholdConfigError(f"{BASE_URL_VAR} must be a public HTTPS origin") from None

    insecure = url.scheme != "https" and url.host not in ("localhost", "127.0.0.1", "[::1]")
    has_credentials = bool(url.username or url.password)
    beyond_origin = bool(url.query or url.fragment) or url.path not in (None, "/")

    if insecure or has_credentials or beyond_origin:
        raise HouseholdConfigError(
            f"{BASE_URL_VAR} must be an HTTPS origin without credentials, path or query"
        )

    return str(url).rstrip("/")


def _connector_clients(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The known connector apps the Operator turned on, in their order, each once."""
    if CLIENTS_VAR not in environ:
        return DEFAULT_CONNECTOR_CLIENTS

    parts = environ[CLIENTS_VAR].split(",")
    names = [part.strip() for part in parts]
    known = ", ".join(sorted(KNOWN_CLIENTS))

    if "" in names:
        raise HouseholdConfigError(f"{CLIENTS_VAR} must list connector clients by name, from: {known}")

    unknown = [name for name in names if name not in KNOWN_CLIENTS]

    if unknown:
        named = ", ".join(unknown)

        raise HouseholdConfigError(f"{CLIENTS_VAR} names an unknown client: {named}; known clients: {known}")

    unique = dict.fromkeys(names)

    return tuple(unique)


def _repeated_ids(users: list[User]) -> list[str]:
    ids = [user.id for user in users]
    counts = Counter(ids)
    repeated = [user_id for user_id, count in counts.items() if count > 1]

    return sorted(repeated)


def meal_starts_differ(meal_hours: MealHours) -> bool:
    """Whether every Meal starts at its own time, so each Meal lasts until the next one starts."""
    starts = list(meal_hours.as_starts().values())

    return len(set(starts)) == len(starts)


def _validate_meal_hours(users: list[User]) -> None:
    for index, user in enumerate(users):
        if not meal_starts_differ(user.meal_hours):
            raise HouseholdConfigError(f"{USERS_VAR}[{index}].meal_hours: Meal start times must differ")


def _describe(details: ErrorDetails) -> str:
    parts = [f"[{part}]" if isinstance(part, int) else f".{part}" for part in details["loc"]]
    path = "".join(parts)

    return f"{USERS_VAR}{path}: {details['msg']}"


def _ids(household: Household) -> str:
    return ", ".join(user.id for user in household.users)
