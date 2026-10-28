# Setup

This page covers the configuration every server needs, adding people to it, and running it locally with Claude Code. To put it online, continue with [deploy](deploy.md).

## Configuration

The server reads its configuration only from environment variables. Locally, keep them in a `.env` file (in a clone of the repository it's git-ignored):

```sh
FATSECRET_CONSUMER_KEY=...
FATSECRET_CONSUMER_SECRET=...
FATSECRET_MCP_SIGNING_KEY=...
FATSECRET_MCP_USERS='[{"id": "me", "display_name": "Me", ...}]'
```

| Variable | What it holds |
|---|---|
| `FATSECRET_CONSUMER_KEY`, `FATSECRET_CONSUMER_SECRET` | Your OAuth 1.0 consumer key and secret from the [FatSecret Platform API](https://platform.fatsecret.com). The free Basic plan works. |
| `FATSECRET_MCP_SIGNING_KEY` | A random key of at least 32 characters that signs drafts and connector credentials. |
| `FATSECRET_MCP_USERS` | Everyone the server serves, as one JSON line. `add-user` writes it for you. |
| `FATSECRET_MCP_USER` | Over stdio with more than one person: the `id` the server acts as. |
| `FATSECRET_MCP_BASE_URL` | Over HTTP only: the server's public origin. See [connector](connector.md). |
| `FATSECRET_MCP_CLIENTS` | Over HTTP only, optional: the apps that may sign in, `claude` by default. Add `chatgpt` to turn ChatGPT on. See [connector](connector.md#add-the-connector-in-chatgpt). |

Generate the signing key once:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Keep it across restarts and deploys. Changing it invalidates every open draft and signs everyone out of their connectors. Draft ids are signed, self-contained values holding entry names, ids, units, meal, diary day, user id and expiry; they contain no nutrition values.

## Add a person

You need [uv](https://docs.astral.sh/uv/) or Docker, and no clone of the repository. With uv, which also fetches Python 3.13 if you don't have it:

```sh
uvx --env-file .env fatsecret-mcp add-user
```

With Docker:

```sh
docker run -it --rm --env-file .env ghcr.io/jaronskijakub/fatsecret-mcp fatsecret-mcp add-user
```

Docker's `--env-file` keeps quotes as part of the value, so in a `.env` used with Docker write `FATSECRET_MCP_USERS` without the surrounding single quotes.

It takes about two minutes per person:

1. It shows a FatSecret link. The new person opens it, signs in with their own FatSecret account (the one in the FatSecret app, not your developer account), allows access, and reads out the PIN that FatSecret shows.
2. You type their display name, a user `id` for signing in, their daily targets, their timezone (Europe/Warsaw by default) and, optionally, meal hours.
3. It generates a connector password and shows it once, a short line like `xxxx-xxxx-xxxx-xxxx-xxxx`. Give it to the person straight away. The configuration keeps only its scrypt hash in `connector_password_hash`; that hash is not the password.
4. It prints the complete new `FATSECRET_MCP_USERS` value. Replace the whole value in `.env` or on your host with it.

When `FATSECRET_MCP_USERS` is already set, the new person is added and everyone else is kept exactly as written. An `id` that's already taken is refused. To sign an existing person in to FatSecret again, remove them from the value first.

## A person's settings

Each entry in `FATSECRET_MCP_USERS` looks like this (shown across lines; the variable holds it on one):

```json
{
  "id": "anna",
  "display_name": "Anna",
  "fatsecret_access_token": "...",
  "fatsecret_access_secret": "...",
  "connector_password_hash": "...",
  "session_generation": 0,
  "daily_targets": {"kcal": 2200, "protein": 140, "fat": 70, "carbohydrate": 250},
  "timezone": "Europe/Warsaw",
  "meal_hours": {"breakfast": "06:00", "lunch": "12:00", "dinner": "18:00", "snacks": "22:00"}
}
```

Daily targets are in kcal and grams. They live here, not in the FatSecret app, and you can edit them by hand.

`meal_hours` is optional and sets local meal start times for suggesting a meal. A meal lasts until the next start, including across midnight; times must differ. Omitted starts default to 04:00, 11:30, 16:00 and 21:00. Between midnight and 04:00, drafts ask whether to use Snacks on the previous day. A meal or day named in the conversation always wins over the suggestion.

`connector_password_hash` and `session_generation` matter only over HTTP; see [connector](connector.md).

## Check the configuration

```sh
uvx --env-file .env fatsecret-mcp check
```

`check` validates the configuration the way `serve` does at startup: the HTTP settings when `FATSECRET_MCP_BASE_URL` is set, otherwise the acting person for stdio. Then it reads each person's current diary day, one FatSecret request per person, and reports whether the connection works. A revoked access token, an exhausted daily quota and an IP restriction each get their own message. It prints no tokens, secrets or passwords, and exits with status 1 if anything failed.

## Run it with Claude Code

Without a command, `fatsecret-mcp` serves over stdio, the same as `fatsecret-mcp serve`. Add it to Claude Code:

```sh
claude mcp add fatsecret -- uvx --env-file /path/to/.env fatsecret-mcp
```

With more than one person in `FATSECRET_MCP_USERS`, set `FATSECRET_MCP_USER` to the `id` to act as. A local stdio copy counts its FatSecret requests separately from a deployed server with the same key, so the two can together go over the daily quota described in [limits](limits.md).
