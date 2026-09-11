# Autoverse AI — Call Analytics API

Read-only API for pulling call activity and booking performance into your own
dashboard. Returns **usage and outcome data only** (no pricing/billing fields).

- **Base URL:** `https://aicalling.autoverseai.in`
- **Dashboard (optional):** open `/analytics` in a browser and sign in with your API key.
- **Auth:** send your API key as a Bearer token on every request:

  ```
  Authorization: Bearer YOUR_API_KEY
  ```

  (`X-API-Key: YOUR_API_KEY` and `?key=YOUR_API_KEY` are also accepted.)

- **Responses:** JSON, UTF-8. All timestamps are ISO-8601 UTC.
- **CORS:** enabled for all origins, so a browser dashboard can call it directly.

### Auth responses
| Status | Meaning |
|--------|---------|
| `200`  | OK |
| `401`  | Missing or wrong API key |
| `404`  | Call ID not found |
| `503`  | API key not configured on the server |

---

## Endpoints

### 1. `GET /api/v1/analytics/summary`
Aggregate metrics across all calls (respects the filters below).

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://aicalling.autoverseai.in/api/v1/analytics/summary
```

```json
{
  "total_calls": 128,
  "by_source": { "browser": 90, "twilio": 38 },
  "by_language": { "hi": 70, "gu": 33, "en": 25 },
  "total_minutes": 214.5,
  "total_seconds": 12870,
  "bookings": 41,
  "booking_conversion_rate": 0.3203,
  "by_day": [ { "date": "2026-09-01", "calls": 12 }, { "date": "2026-09-02", "calls": 9 } ],
  "this_month": { "calls": 128 }
}
```

### 2. `GET /api/v1/analytics/calls`
List calls, newest first. Supports the filters and pagination below.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  "https://aicalling.autoverseai.in/api/v1/analytics/calls?limit=50"
```

```json
{
  "total": 128,
  "items": [
    {
      "id": "a1b2c3",
      "started_at": "2026-09-03T06:12:44+00:00",
      "ended_at": "2026-09-03T06:15:02+00:00",
      "duration_seconds": 138,
      "language": "hi",
      "status": "completed",
      "source": "browser",
      "caller": "web-a1b2c3",
      "booking_created": true,
      "has_recording": true
    }
  ]
}
```

### 3. `GET /api/v1/analytics/calls/{id}`
Full detail for one call, including transcript and actions taken.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://aicalling.autoverseai.in/api/v1/analytics/calls/a1b2c3
```

```json
{
  "id": "a1b2c3",
  "started_at": "2026-09-03T06:12:44+00:00",
  "ended_at": "2026-09-03T06:15:02+00:00",
  "duration_seconds": 138,
  "language": "hi",
  "status": "completed",
  "source": "browser",
  "caller": "web-a1b2c3",
  "booking_created": true,
  "has_recording": true,
  "recording_url": "/api/v1/analytics/calls/a1b2c3/recording",
  "recording_duration_seconds": 138.4,
  "transcript": [
    { "role": "agent", "text": "Namaste! Main Rahul bol raha hoon...", "ts": "..." },
    { "role": "user", "text": "Haan boliye", "ts": "..." }
  ],
  "tool_calls": [
    { "name": "schedule_pickup", "args": { "date": "2026-09-10", "time": "10:00 AM" }, "result": { "success": true }, "ts": "..." }
  ]
}
```

### 4. `GET /api/v1/analytics/calls.csv`
Same list as #2 as a downloadable CSV.
Columns: `started_at, source, caller, duration_seconds, language, status, booking_created`.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  "https://aicalling.autoverseai.in/api/v1/analytics/calls.csv?from=2026-09-01" -o calls.csv
```

### 5. `GET /api/v1/analytics/calls/{id}/recording`
The call's audio recording (both sides mixed) as a WAV file, `audio/wav`, mono 16 kHz.
Only available when the call's `has_recording` is `true`; otherwise returns `404`.
Recordings are kept for a limited retention period (currently 30 days), after which
`has_recording` becomes `false`. Calls made before recording was enabled have no audio.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://aicalling.autoverseai.in/api/v1/analytics/calls/a1b2c3/recording -o call_a1b2c3.wav
```

To play it in a browser dashboard, fetch it with the `Authorization` header, create an
object URL from the blob, and set it as the `src` of an `<audio controls>` element.

### 6. `GET /api/v1/analytics/sessions/{session_id}`
**Everything for one session in a single request.** You create a session id in your own
system (for example after your user signs in) and put it on the demo link. Every call
made from that link is tagged with it. This endpoint returns all of those calls, newest
first, and each call carries its full detail: transcript, actions taken, and recording.
One session can have many calls.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://aicalling.autoverseai.in/api/v1/analytics/sessions/sess_8f2a91
```

```json
{
  "session_id": "sess_8f2a91",
  "total": 2,
  "calls": [
    {
      "id": "a1b2c3",
      "session_id": "sess_8f2a91",
      "started_at": "2026-09-03T06:12:44+00:00",
      "ended_at": "2026-09-03T06:15:02+00:00",
      "duration_seconds": 138,
      "language": "hi",
      "status": "completed",
      "source": "browser",
      "caller": null,
      "booking_created": true,
      "has_recording": true,
      "recording_url": "/api/v1/analytics/calls/a1b2c3/recording",
      "recording_duration_seconds": 138.4,
      "transcript": [ { "role": "agent", "text": "...", "ts": "..." }, { "role": "user", "text": "...", "ts": "..." } ],
      "tool_calls": [ { "name": "schedule_pickup", "args": { "date": "2026-09-10" }, "result": { "success": true }, "ts": "..." } ]
    }
  ]
}
```

Session ids may contain letters, digits and `_ - . : @` (up to 128 characters); anything
else returns `400`. An unknown session returns `200` with `"total": 0`. Accepts the same
`from`, `to`, `source`, and `booking` filters as the list endpoint, plus `limit`
(default 200, max 500) and `offset`.

**Tagging calls with your session id.** Put it on the link you open for the customer:

```
https://aicalling.autoverseai.in/?session_id=sess_8f2a91
```

The page passes it through automatically; nothing is shown to the customer. Phone
(Twilio) calls are not tagged this way; they carry the caller's number in `caller`.

---

## Filters (query params — apply to `summary`, `calls`, `calls.csv`)
| Param     | Example                | Description |
|-----------|------------------------|-------------|
| `from`    | `from=2026-09-01`      | On/after this date (YYYY-MM-DD) |
| `to`      | `to=2026-09-30`        | On/before this date |
| `source`  | `source=twilio`        | `twilio` (phone) or `browser` |
| `booking` | `booking=true`         | Only calls that produced a booking |
| `session_id` | `session_id=sess_8f2a91` | Only calls tagged with this session id (exact match) |
| `q`       | `q=chetan`             | Free-text match on caller / language / status / source |
| `limit`   | `limit=100`            | Max rows (`calls` only; default 500) |
| `offset`  | `offset=100`           | Skip N rows for pagination (`calls` only) |

## Field reference
| Field | Meaning |
|-------|---------|
| `duration_seconds` | Call length in seconds |
| `language` | Detected from the customer's speech: `hi` Hindi, `gu` Gujarati, `en` English, `mr` Marathi, `te` Telugu, `kn` Kannada, `ta` Tamil, `ml` Malayalam, `bn` Bengali, `pa` Punjabi, `or` Odia. `unknown` = customer spoke but the script wasn't recognised. `no_speech` = the customer never spoke (usually a blocked microphone or an immediate hang-up). |
| `status` | `completed`, `abandoned`, `in_progress` |
| `source` | `browser` (web demo) or `twilio` (phone) |
| `booking_created` | `true` if a service pickup was booked on the call |
| `booking_conversion_rate` | bookings ÷ total calls (0–1) |
| `session_id` | Your session id from the demo link; `null` for calls made without one |
| `transcript[].role` | `user` (the customer) or `agent` (the AI assistant) |
| `tool_calls[]` | Actions the assistant took during the call (e.g. `schedule_pickup`) |
| `has_recording` | `true` if an audio recording is available for this call |
| `recording_url` | Path of the recording endpoint (detail only; `null` when no recording) |
| `recording_duration_seconds` | Length of the audio file (detail only) |

> Note: This API intentionally does not expose any cost, pricing, or token-usage data.
