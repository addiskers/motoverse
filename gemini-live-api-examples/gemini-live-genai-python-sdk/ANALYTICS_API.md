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

### 6. `GET /api/v1/analytics/search?phone={number}`
**Everything for one customer in a single request.** Returns every call made with that
phone number, newest first, and each call carries its full detail: transcript, actions
taken, and recording. Country code and spacing don't matter: `9876543210`,
`+919876543210`, and `+91 98765 43210` all match the same customer.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  "https://aicalling.autoverseai.in/api/v1/analytics/search?phone=9876543210"
```

```json
{
  "phone": "9876543210",
  "total": 2,
  "calls": [
    {
      "id": "a1b2c3",
      "started_at": "2026-09-03T06:12:44+00:00",
      "ended_at": "2026-09-03T06:15:02+00:00",
      "duration_seconds": 138,
      "language": "hi",
      "status": "completed",
      "source": "browser",
      "caller": "+919876543210",
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

Returns `400` if `phone` is missing or shorter than 8 digits. Accepts the same `from`,
`to`, `source`, and `booking` filters as the list endpoint, plus `limit` (default 100,
max 200) and `offset`. An unknown number returns `200` with `"total": 0`.

**Linking browser calls to a phone number.** Phone (Twilio) calls carry the caller's
number automatically. Browser demo calls don't collect a number, so pass it on the
link you send the customer and it is stored on the call:

```
https://aicalling.autoverseai.in/?phone=9876543210
```

`?ref=YOUR-OWN-ID` also works if you prefer to tag calls with your own customer or
lead id; search for it the same way with `?phone=YOUR-OWN-ID`.

---

## Filters (query params — apply to `summary`, `calls`, `calls.csv`)
| Param     | Example                | Description |
|-----------|------------------------|-------------|
| `from`    | `from=2026-09-01`      | On/after this date (YYYY-MM-DD) |
| `to`      | `to=2026-09-30`        | On/before this date |
| `source`  | `source=twilio`        | `twilio` (phone) or `browser` |
| `booking` | `booking=true`         | Only calls that produced a booking |
| `phone`   | `phone=9876543210`     | Only calls from this number (digits compared, prefix-insensitive) |
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
| `transcript[].role` | `user` (the customer) or `agent` (the AI assistant) |
| `tool_calls[]` | Actions the assistant took during the call (e.g. `schedule_pickup`) |
| `has_recording` | `true` if an audio recording is available for this call |
| `recording_url` | Path of the recording endpoint (detail only; `null` when no recording) |
| `recording_duration_seconds` | Length of the audio file (detail only) |

> Note: This API intentionally does not expose any cost, pricing, or token-usage data.
