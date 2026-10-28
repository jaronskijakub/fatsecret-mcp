# Limits

How the server stays within FatSecret's quota, what it keeps in memory, and how large a request to a tool can be.

## Daily quota

FatSecret's free plan allows 5,000 requests a day for the whole household. Each person gets an equal share of 4,500 of them, for example 2,250 each for two people; the remaining 500 are left for `add-user` and `check`. Every request sent counts, catalog searches included, and each one frees up 24 hours after it was made; a food served from memory costs nothing.

When a person's share is spent, their next request is refused before it reaches FatSecret, with the time it frees up again; others keep working. The counts are kept in memory by one running server and reset when it restarts. A second server with the same FatSecret key, such as a local stdio copy, counts separately.

## Pacing

All requests are paced together, at most one a second. The server holds up to ten requests in reserve and earns one back every three seconds; once the reserve is used, a request waits for its turn instead of failing. After a quiet spell about fourteen requests go a second apart, then one every three seconds, so no minute holds more than thirty and the steady rate is twenty a minute. A search waits at most about twelve seconds, unless other searches are queued ahead of it.

FatSecret's own limit is undocumented: it refused about 38 requests in 50 seconds, so these numbers are this server's policy, not a guarantee. After FatSecret reports too many actions, the server sends nothing for thirty seconds; after it reports the daily quota used up, nothing for ten minutes. Requests the server refuses itself cost nothing, and the logs mark them as `not sent`, unlike errors FatSecret returned.

## Searching

Search uses only the FatSecret catalog a free key can reach: US foods, searched in English. Claude passes your description along with an English rendering. Search never reads your diary. Each search costs one catalog request plus one per food shown, up to three.

A food fetched with its servings is kept in memory for an hour and shared by everyone on the server. Repeating a search costs only the catalog request, and drafting a food just found costs nothing. For a dish on a plate, Claude searches the whole dish first and its components only when the dish has no good match.

## Reading the diary

Today is always read fresh. Past days are kept in memory until the server stops or twelve hours pass from fetching them. If you edit a past day in the FatSecret app, ask Claude to refresh it: `get_diary_day` and `get_period_summary` accept `refresh=true`. Period summaries reuse cached entries and fetch month totals for days not yet loaded.

## Drafts

Drafts expire after thirty minutes. Confirming one writes each new item as a food entry under the draft's name, meal and day, then makes its changes and removals. A draft can be confirmed only by the person it was made for. Confirming it again writes nothing and returns the earlier result, until the draft expires or the server restarts. If FatSecret fails part-way through, the result lists which entries were saved and which changes were made; draft the rest again.

A draft's day can be at most a year back or a week ahead. An entry can be changed or removed from today back to a year ago, and a removed entry can't be restored.

## Input sizes

`draft_entries` takes items with `food_id`, `serving_id`, `number_of_units` and an optional `assumption`. Units follow the serving returned by `search_foods`: a serving described as "100 g" with 100 units needs 150 units for 150 g.

| Limit | Value |
|---|---|
| Items in one draft | 20; Claude splits a larger meal into several drafts |
| Amount per item | positive and finite, at most 10,000 units, 10,000 g or ml where weight or volume is known, and 20,000 kcal |
| Search text | 200 characters |
| Product name, search phrase, new entry name | 100 characters |
| Assumption | 300 characters |
