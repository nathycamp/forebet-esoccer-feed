# Querying the Forebet feed

Base URL: https://forebet-esoccer-feed-production.up.railway.app

`GET /predictions` keeps the full feed and its existing JSON shape. Queries
only filter the existing memory cache. They do not start a browser, fetch
Forebet, add background jobs, or store a database. Refresh remains every two
minutes. All responses remain grouped under `leagues`.

## Parameters

| Parameter | Values | Behavior |
|---|---|---|
| `league` | `esb8`, `volta6`, `h2hgg`, `gt12` | Comma-separated; default all four |
| `status` | `upcoming`, `live`, `played` | Comma-separated; default all statuses |
| `player` | Player tag | Exact match on either player, ignoring case and extra whitespace |
| `limit` | Integer 1–1000 | Maximum fixtures per selected league, after filtering |
| `fields` | Comma-separated fixture field names | Return only those fixture fields |

Filtered results sort by kickoff ascending. With only `status=played`, they
sort newest first. `status=upcoming` excludes elapsed kickoffs, including old
unresolved rows; it cannot eliminate delays in the underlying snapshot.
An unfiltered request retains the source's original fixture order. Empty
matches return `[]` for each selected league. Unavailable leagues retain
`{"status":"error"}` even when fields are selected. Invalid parameters return
HTTP 400 with an `error` message.

Examples:

```text
/predictions?status=live
/predictions?league=esb8,volta6
/predictions?status=upcoming&limit=5
/predictions?status=played&limit=20
/predictions?player=Scorpion
/predictions?league=h2hgg,gt12&status=live&fields=home_tag,away_tag,status,live_score,match_clock
```

Available fixture fields:

```text
home_tag,away_tag,home_club,away_club,kickoff_utc,
source_kickoff_utc,source_display_time,source_utc_offset_minutes,
kickoff_correction_minutes,kickoff_time_status,
p1,px,p2,pred,correct_score,avg_goals,status,actual_score,live_score,match_clock
```

Top-level freshness and source metadata remain present. Per-league metadata
includes only selected leagues. `stale` remains a conservative overall-reader
indicator, including failed reads and clock inconsistencies; filters do not
hide reader problems. The documented kickoff correction is already applied
in `kickoff_utc`; do not apply it again downstream.

## Picks

`GET /picks` returns goal calls for each league's next kickoff and any other
fixture starting within 20 minutes, computed once per feed refresh from the
same memory cache. Games in progress count as partial evidence for their
players' next fixtures: the score so far over the share of the match played
(warning `live_score_evidence_used`). It stores nothing and never fetches
Forebet. It accepts `league`, `player`, `limit` and `fields` exactly as above
(there is no `status`; every pick is upcoming), and uses the same compact JSON,
gzip and ETag behavior.

| Field | Meaning |
|---|---|
| `goals_line` | The league's usual line: mean total goals of this snapshot's results, rounded down to x.5 |
| `goals_call`, `goals_probability` | `over` or `under` that line, and the model's probability for the likelier side. `goals_call` is null below 55%, or when either player has fewer than 5 results in the snapshot |
| `expected_goals` | Model's expected total goals |
| `goals_confidence` | `trusted` for esb8 and volta6, where replays showed an edge; `low` otherwise |
| `favourite`, `favourite_probability` | Home tag, away tag or `draw`, from form probabilities pulled halfway to league outcome rates |
| `favourite_strength` | Always `weak`: winner forecasts did not beat league outcome rates in replays |
| `call_status` | `ok`; `no call: too close` or `no call: new players`; or why calls are withheld (`withheld: stale source`, `withheld: no league results`) |
| `warnings` | Model warnings, such as limited player history |
| `one_x_two`, `double_chance` | Home/draw/away and 1X/X2/12 probabilities, for information only (`winner_markets_trust`: not trusted, since they did not beat league outcome rates in replays) |

Each eligible game (fresh league, both players with at least 5 results) has a
`safest_option`: the over/under call at the league line when it has a proven
edge (esb8 and volta6 goal calls), otherwise its single likeliest selection
across 1X2, double chance and over/under, with `market`, `selection`, `probability`, the
`league_rate` for that selection in this snapshot, and `proven_edge`.

`goal_lines` gives every x.5 line from 1.5 to 9.5 with the model's `over` and
`under` chances and the matching `fair_over_odds` / `fair_under_odds` (1 ÷
chance). A bookmaker price above the fair odds is better value than the model
expects; only the league line itself has a replay-proven edge.

When esb8 or volta6 runs steadily late (at least 2 live clocks agreeing
within 3 minutes, delay up to 45 minutes), picks continue with kickoffs shifted
by the delay: `kickoff_utc` is the adjusted time, `listed_kickoff_utc` is
Forebet's, `kickoff_adjusted_minutes` gives the shift and a
`kickoff_adjusted_for_N_minute_delay` warning is added. Other mismatches still
withhold picks.

`top_picks` lists up to two games whose safest option is at least 60%, proven
over/under calls first and then the likeliest, each with its `league` and safest-option fields.
`top_picks_status` is `no top pick right now` when nothing qualifies.
`proven_edge` is true only for over/under calls in esb8 and volta6. Replays
found model 1X2 and double-chance choices landed slightly less often than the
league's most common result, so a likely double chance is not a proven edge.

`top=N` (1–10) returns only `top_picks`: the N best safest options (same order) within
the window, even below 60%. `league`, `player` and `fields` still apply.

```text
/picks?top=2
/picks?top=2&fields=home_tag,away_tag,kickoff_utc
```

```text
/picks?league=esb8,volta6&limit=3
/picks?player=Mani&fields=home_tag,away_tag,kickoff_utc,goals_line,goals_call,goals_probability,favourite
```

Calls are experimental and uncalibrated, not betting advice.

## Transfer size and conditional polling

Responses use compact UTF-8 JSON. Send `Accept-Encoding: gzip` to compress
responses of at least 512 bytes. Smaller responses stay uncompressed. For curl:

```sh
curl --compressed 'https://forebet-esoccer-feed-production.up.railway.app/predictions?status=live'
```

Save the response's `ETag` and send it on the next request for the same query:

```text
If-None-Match: W/"<value from the previous response>"
```

HTTP 304 has no response body: reuse the previously received JSON. A changed
response returns HTTP 200 and a new ETag. The validator covers freshness
metadata too, so a new read timestamp or a transition to stale produces an
updated response even when fixture scores have not changed. `Cache-Control:
no-cache` asks clients to revalidate each poll rather than silently reuse an
old snapshot. `Vary: Accept-Encoding` supports identity and gzip clients.

For a prediction engine, request completed history as well as upcoming games.
A scoreboard-only projection omits fields the prediction package needs.
