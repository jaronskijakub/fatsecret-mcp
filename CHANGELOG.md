# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Changing and removing Food Entries through a draft: "undo that" right after a save, "it was 200 g, not 300", "move it to breakfast", a new name, or "delete yesterday's coffee". A draft shows each entry before and after and how each day looks afterwards, and can also add entries, so "chicken thigh, not breast" or "that was yesterday" is one draft. This works for any entry in your diary from the past year, including ones logged in the FatSecret app. A removed entry can't be restored, only logged again.
- `confirm_draft` asks Claude Code to get your approval before every call, even in its automatic modes.
- The connector has its own icon, and names the GitHub repository as its website. ChatGPT shows both; Claude still shows the favicon of the server's domain.
- ChatGPT as a second connector app, off by default. Set `FATSECRET_MCP_CLIENTS=claude,chatgpt` to let people sign in from ChatGPT with the same user `id` and connector password. Its client ID and callback are pinned like Claude's, and each app gets its own tokens.

### Changed

- The sign-in page names the app being connected and the host it returns to, and has a cleaner design. Its only stylesheet is allowed by its hash, so the page still runs no script.
- Catalog foods are saved under the name you used, in your language, with a short note that they were matched in the US catalog, such as "Kajzerka (katalog USA)" instead of "Hard Rolls (Includes Kaiser)". The draft shows the catalog's name beside it.
- A portion of known weight, such as "1 roll (57 g)", is saved in grams when the food has a serving in grams or ml, for catalog foods and stand-ins alike. The FatSecret app then shows the same amount as the draft, even where its own language lacks that portion. A changed or removed entry whose amount shows as a bare number, such as "1", appears in grams in the draft.

After upgrading, reconnect the connector in Claude, so that Claude sees the new tool options. In ChatGPT, remove the connector and add it again to see its icon.

## [0.2.0] - 2026-10-25

### Added

- Repeating earlier Food Entries: "the same as yesterday's breakfast" drafts entries from your diary by their `food_entry_id`, with their own food, serving, name and nutrition, and an amount in grams or ml that may change. This works for products the catalog can't return and for stand-ins.
- `get_diary_day` shows each entry's `food_entry_id`.

After upgrading, reconnect the connector in Claude, so that Claude sees the new tool options.

## [0.1.0] - 2026-10-20

The first release.

### Added

- Five MCP tools: `search_foods`, `draft_entries`, `confirm_draft`, `get_diary_day` and `get_period_summary`.
- Drafts that show amounts, calories, macros, assumptions, a suggested meal and the day after adding against daily targets. Nothing is written until a draft is confirmed.
- Stand-ins: a product missing from the catalog is logged from its nutrition label as the closest catalog food, with calories matching the label, under the product's own name.
- One server for a household: each person signs in to FatSecret once through `add-user` and to the connector with their own password.
- stdio for Claude Code, and HTTP with OAuth sign-in for Claude's web, desktop and mobile apps.
- Sign-in hardening: PKCE, refresh tokens, signing out by `session_generation`, throttling and pinned Claude client values.
- A daily share of the FatSecret quota for each person, paced requests and in-memory caching.
- The `add-user`, `check` and `serve` commands.
- A package on PyPI, so `uvx fatsecret-mcp` runs the server and the commands without a clone.
- A container image for Linux amd64 and arm64, published to GHCR by CI and tagged with each release version.

[0.1.0]: https://github.com/jaronskijakub/fatsecret-mcp/releases/tag/v0.1.0
