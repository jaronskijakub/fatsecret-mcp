# Security

## Trust model

A server is run by one Operator for their household. Everyone on it trusts the Operator:

- The Operator can read and write every diary on the server. The configuration holds each person's FatSecret access token and secret, which work without the server, from any machine with the Operator's FatSecret API key.
- The Operator can act as anyone on the server. The signing key signs connector tokens, so whoever holds it can make a valid token for any person. They can't read anyone's connector password, only its scrypt hash.
- The hosting provider sees the same environment variables as the Operator.
- The AI app you use, Claude or ChatGPT, sees what you ask it. Diary entries, totals and targets returned by the tools become part of your conversation with it.

Don't join a server whose Operator you wouldn't give your FatSecret password to.

## What is stored where

| What | Where | How long |
|---|---|---|
| FatSecret API key, signing key, each person's FatSecret token and secret, connector password hashes, daily targets | Environment variables on the host | Until the Operator changes them |
| Past diary days | Server memory | Up to twelve hours, or until restart |
| Catalog foods with their servings | Server memory, shared by everyone | One hour |
| Which drafts were confirmed | Server memory | Until the draft expires or the server restarts |
| Request counts, sign-in counters | Server memory | Until restart |
| Each connector app's client metadata document | Server memory | One hour |
| Drafts | Signed ids held by the app; the server stores nothing | Thirty minutes |
| Connector access and refresh tokens | Held by the app, signed by the server | One hour and thirty days |

There is no database and nothing is written to disk. Drafts carry entry names, ids, units, meal, day and person, but no nutrition values. HTTP access logs are off, because sign-in URLs carry one-time codes. The server's own log lines name FatSecret methods, error types and error codes, not diary contents or credentials.

## Revoking access

- Sign out one person's devices: increase their `session_generation` and redeploy. Their access and refresh tokens stop working; their password stays the same.
- Remove a person: delete their entry from `FATSECRET_MCP_USERS` and redeploy, then delete any other copy of it, such as a local `.env`. Their tokens stop working and they can no longer sign in.
- Sign everyone out: change `FATSECRET_MCP_SIGNING_KEY` and redeploy. Open drafts become invalid too.
- Cut off a FatSecret token: the server can't revoke it at FatSecret, so delete it from every copy of the configuration.

Details are in [docs/connector.md](docs/connector.md#sign-someone-out).

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository's **Security** tab, choose **Report a vulnerability**. Don't open a public issue. Include the version or commit, what an attacker could do, and steps to reproduce. I'll reply as soon as I can; this is a personal project with no guaranteed response time.

Only the latest commit on `main` and its published image are supported.
