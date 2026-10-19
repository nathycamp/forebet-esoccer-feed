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
