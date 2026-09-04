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
      "booking_created": true
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

---

## Filters (query params — apply to `summary`, `calls`, `calls.csv`)
| Param     | Example                | Description |
|-----------|------------------------|-------------|
| `from`    | `from=2026-09-01`      | On/after this date (YYYY-MM-DD) |
| `to`      | `to=2026-09-30`        | On/before this date |
| `source`  | `source=twilio`        | `twilio` (phone) or `browser` |
| `booking` | `booking=true`         | Only calls that produced a booking |
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

> Note: This API intentionally does not expose any cost, pricing, or token-usage data.
