# Connector

Over HTTP, each person adds the server to their own Claude account as a custom connector, or to ChatGPT when the Operator turns it on, and signs in with their user `id` and connector password. This page explains how that sign-in works and how to sign someone out.

## Add the connector

1. Set `FATSECRET_MCP_BASE_URL` to the server's public HTTPS origin, for example `https://diary.example.com`, with no path, query or credentials. For local development use `http://127.0.0.1:8000`.
2. In Claude, add a custom connector with the URL `<origin>/mcp`. Leave the OAuth client ID and secret empty: Claude detects sign-in from the server's metadata and uses its own published identity.
3. Sign in with the user `id` and the connector password from `add-user`. The sign-in page names the app and the host your browser returns to, which is always `claude.ai` for Claude.
4. The connector appears in Claude on the web, desktop and phone. In its tool settings, the four reading tools can be set to "Always allow". Keep `confirm_draft` on "Needs approval", so every write asks you first.

## Add the connector in ChatGPT

ChatGPT is off by default. The Operator turns it on with `FATSECRET_MCP_CLIENTS`, a comma-separated list of the apps that may sign in:

| Value | Who can sign in |
|---|---|
| unset, or `claude` | Claude only |
| `claude,chatgpt` | Claude and ChatGPT |
| `chatgpt` | ChatGPT only |

An unknown name or an empty list stops the server at startup. Existing Claude connectors keep working when ChatGPT is turned on. Turning an app off and redeploying signs everyone out of it at once.

1. Set `FATSECRET_MCP_CLIENTS=claude,chatgpt` and redeploy.
2. In ChatGPT, turn on developer mode and add a custom connector with the URL `<origin>/mcp`, choosing OAuth. Leave any client ID and secret empty: ChatGPT uses its own published identity. For the icon, upload `<origin>/fs-256.png` (see [The connector's icon](#the-connectors-icon)). Which plans offer this is listed in OpenAI's [developer mode](https://help.openai.com/en/articles/12584461) article.
3. Sign in with the same user `id` and connector password as in Claude. The sign-in page names ChatGPT and `chatgpt.com`.

Each app gets its own tokens, so someone can use both at once. Signing a person out, as described below, ends their sessions in both.

## The connector's icon

The server names its icon and website in the details an MCP app reads when it connects: PNG icons served from the server's own address (`/fs-512.png`, `/fs-256.png` and `/fs-64.png`, plus `/favicon.ico`), and the GitHub repository as its website. They're public and need no sign-in, as the MCP specification asks. Apps that follow the specification show them; the two below don't yet.

- ChatGPT, for a connector added in developer mode, shows only what its form was given: the name, the description and an uploaded icon (PNG, at most 10 KB). Download `<origin>/fs-256.png` and upload it there. The version (1.0.0) and the empty website on the connector's page are ChatGPT's own.
- Claude ignores icons from MCP servers for now and shows the favicon of the server's domain instead. On a host such as `*.up.railway.app` that's the host's own logo; a custom domain with its own favicon changes it.

`GET /health` returns only `{"status":"ok"}`. An unauthenticated request to `/mcp` returns `401` and the address of the OAuth metadata.

## Only known connector apps can sign in

The server offers no dynamic client registration. For each app it pins a client ID and the callback that app's codes may go to, all in `src/fatsecret_mcp/auth/client_metadata.py`:

| App | Client ID | Callback |
|---|---|---|
| Claude (web, desktop and mobile) | `https://claude.ai/oauth/mcp-oauth-client-metadata` | `https://claude.ai/api/mcp/auth_callback` |
| ChatGPT | `https://chatgpt.com/oauth/client.json` | `https://chatgpt.com/connector_platform_oauth_redirect` |

Any other client ID, or the ID of an app that isn't turned on, is refused before any request is made, so a link to the sign-in page can't send your code anywhere else. A callback must belong to the same app as the client ID: Claude's ID with ChatGPT's callback is refused, and the other way round.

The server fetches the app's client metadata document and requires it to list the callback as well, to have no secret, and to allow a public client, which is the only kind the server offers. ChatGPT's document prefers `private_key_jwt` but also lists `none`, so ChatGPT signs in as a public client with PKCE, as Claude does; the server never checks client assertions. The fetch uses its own two connections, separate from FatSecret calls, and each app's document is kept for an hour; a failed fetch isn't kept. At most ten fetches run in a minute across all apps; beyond that, opening the sign-in page returns `503` with `Retry-After`.

If an app changes either value, adding the connector fails with `invalid_request` before the sign-in page appears. To find the new values, open the failed sign-in page's address: its `client_id` and `redirect_uri` parameters are what the app sent. Update the constants and redeploy. Claude's callback is listed in Anthropic's [Authentication for connectors](https://claude.com/docs/connectors/building/authentication), and ChatGPT's values in OpenAI's [Authentication](https://developers.openai.com/apps-sdk/build/auth) page. ChatGPT uses these stable values because the server supports client metadata documents and returns the issuer with each code; without that it would switch to per-connector values, which the server doesn't accept.

## Codes and tokens

Authorization uses PKCE S256. Signed codes expire after sixty seconds and can be exchanged once per running instance. Signed access tokens last one hour and are bound to this server, person and app. They survive a restart with the same configuration; the record of used codes is kept in memory. Connector credentials are signed with `FATSECRET_MCP_SIGNING_KEY`, in a separate signing context from drafts. HTTP access logs are disabled because authorization URLs contain credentials.

The app renews access with a signed refresh token, which only the app it was issued to can use. Each refresh returns a new refresh token valid for thirty days, so a connector used at least once a month stays signed in. Nothing is stored server-side, so an earlier refresh token keeps working until it expires.

## Sign someone out

- Every device of one person, for example after losing a phone: increase their `session_generation` in `FATSECRET_MCP_USERS` and redeploy. All their access and refresh tokens stop working, in Claude and in ChatGPT, and they sign in again with the same password.
- Remove a person: delete their entry from `FATSECRET_MCP_USERS` and redeploy, then delete any other copy of their entry, such as a local `.env`. Their tokens stop working and they can't sign in.
- Everyone at once: change `FATSECRET_MCP_SIGNING_KEY` and redeploy. This also invalidates every open draft.

## Sign-in throttling

Sign-in allows at most sixty attempts across the server in a rolling minute and two password checks at a time. Extra attempts get HTTP 429 with `Retry-After`; rejected attempts don't extend the minute. Each sign-in form accepts at most five submissions, then asks you to start sign-in again from your connector.

After five failed sign-ins for one user name, sign-in with that name is paused for fifteen minutes, even with the correct password. The limit applies to every typed name in the same way, so it doesn't reveal which names exist. Someone who knows a person's `id` can therefore delay their next sign-in, but connectors that are already signed in keep working. The counters remember at most 1,024 names and 1,024 forms. They are kept in memory and reset when the server restarts.
