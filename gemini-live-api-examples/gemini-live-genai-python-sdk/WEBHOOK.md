# Autoverse AI — Dispatch Webhook

How AutoServe asks the Autoverse voice agent to ring a customer, how each request is
verified, and how the outcome of every call comes back.

- **Endpoint:** `POST https://aicalling.autoverseai.in/call-me`
- **Auth:** HMAC-SHA256 signature (below). No bearer token or API key.
- **Responds within:** 10 s, usually under 2 s. The response only means "received".

> Until the signing secret is configured on our side, the endpoint answers
> `503 NOT_CONFIGURED` and dials no one.

## How a dispatch works

1. You send a signed `call.requested` event. We verify it and answer at once. `202` means
   the call is being placed, not that the customer has picked up.
2. We ring the customer for up to 45 seconds.
3. The agent opens with your context: name, car, due service and outlet. It introduces
   itself as calling from that outlet, without calling your API first.
4. During the call, the agent books through your `POST /bookings` with
   `call_external_ref`. It logs the call with you first, so the booking is attributed
   to the voice agent.
5. The call is capped at 7 minutes. Near the limit, the agent hands the customer to your
   team with a callback.
6. We report the outcome on your `POST /api/v1/calls` with the `dispatch_id`, usually
   seconds after hang-up. If we never hear from the phone network, we report `failed`
   no later than 12 minutes after your request, inside your 15-minute window.

## Request

```json
{
  "event": "call.requested",
  "dispatch_id": "068c2a9b-224c-453b-9efd-134c0cd8427e",
  "phone": "+919820012345",
  "reason": "service_due",
  "customer": { "customer_id": "…", "name": "Priya Sharma", "mobile": "9820012345",
                "language": "hi", "area": "Andheri" },
  "vehicle":  { "reg_no": "MH02AB1234", "model": "XUV 7XO",
                "next_service_label": "2nd Free Service", "next_service_due_date": "2026-11-01",
                "service_status": "due_soon", "free_services_left": 2 },
  "outlet":   { "outlet_id": "…", "name": "Jubilant Mahindra - Main" }
}
```

| Field | | Use |
|---|---|---|
| `phone` | required | Number to dial, E.164 (`+91…`). Placeholders such as `+910000000000` are refused with `400`. |
| `dispatch_id` | required | Echoed back on the outcome report. A repeat is never dialled twice. |
| `customer.customer_id` | optional | Used for bookings and callbacks; sent back on the report. |
| `customer.name`, `.area`, `.language` | optional | Greeting by name; area used for pickup slots. |
| `vehicle.*` | optional | What the agent says about the car and the due service; `reg_no` identifies it when booking. |
| `outlet.outlet_id`, `.name` | optional | The agent introduces itself as this outlet and offers slots there. |
| `reason` | optional | Passed to the agent as the purpose of the call. |
| `event`, `report_by`, `campaign`, `callback_at`, `tries_today` | ignored | Safe to keep sending. |

## Signing

| Header | Value |
|---|---|
| `X-AutoServe-Timestamp` | Unix seconds (milliseconds and ISO-8601 also accepted) |
| `X-AutoServe-Signature` | hex HMAC-SHA256 of `<timestamp>.<raw request body>` with the shared secret |

Also accepted: `sha256=<hex>`, base64, or `t=<timestamp>,v1=<hex>` in one header. Sign
the exact bytes you send. Requests more than 5 minutes old are refused.

```js
const crypto = require("crypto");
const body = JSON.stringify(payload);                  // send exactly this string
const ts   = Math.floor(Date.now() / 1000).toString();
const sig  = crypto.createHmac("sha256", process.env.AUTOSERVE_WEBHOOK_SECRET)
                   .update(`${ts}.${body}`).digest("hex");
await fetch("https://aicalling.autoverseai.in/call-me", {
  method: "POST",
  headers: { "Content-Type": "application/json",
             "X-AutoServe-Timestamp": ts, "X-AutoServe-Signature": sig },
  body,
});
```

```bash
BODY='{"dispatch_id":"test-001","phone":"+919820012345"}'
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST https://aicalling.autoverseai.in/call-me \
  -H "Content-Type: application/json" \
  -H "X-AutoServe-Timestamp: $TS" -H "X-AutoServe-Signature: $SIG" -d "$BODY"
```

## Responses

Only a `2xx` means the call is being placed.

| Status | `error` | Meaning | Retry? |
|---|---|---|---|
| `202` | — | Accepted; ringing the customer | No |
| `200` | — | Duplicate `dispatch_id`; already placed, nothing new dialled | No |
| `400` | `MISSING_PHONE`, `INVALID_PHONE`, `INVALID_JSON` | Malformed request | No, fix it |
| `401` | `UNAUTHORIZED` | Missing, wrong or expired signature | No, fix it |
| `502` | `DIAL_REJECTED`, `DIAL_FAILED` | Phone network refused or unreachable; nothing dialled | Yes, same `dispatch_id` |
| `503` | `NOT_CONFIGURED`, `TELEPHONY_NOT_CONFIGURED` | Calling not switched on for our side yet | Later |
| `504` | `DIAL_TIMEOUT` | Phone network did not answer in time; nothing dialled | Yes, same `dispatch_id` |

```json
{ "accepted": true, "dispatch_id": "068c2a9b-…", "request_uuid": "9834029e-…" }
{ "accepted": true, "duplicate": true, "dispatch_id": "068c2a9b-…", "status": "ringing" }
{ "accepted": false, "error": "INVALID_PHONE", "message": "'+910000000000' is not a dialable phone number." }
```

## Outcome report

For every accepted dispatch we call your `POST /api/v1/calls` exactly once with
`dispatch_id`, `external_ref` (our call id, also used as `call_external_ref` on
bookings), `customer_id`, `direction: "outgoing"`, `started_at`, `duration_sec`,
`outcome`, a one-line `summary`, the `transcript` (`agent` / `customer`) and a
`recording_url` (WAV, no header needed, valid 30 days).

| `outcome` | When |
|---|---|
| `booked` | A booking was confirmed during the call |
| `callback` | The agent raised a callback, including at the 7-minute limit |
| `not_interested` | The customer spoke with the agent but did not book |
| `no_answer` | Not picked up, busy, or picked up without the customer speaking |
| `failed` | Could not be completed, or no signal from the phone network within 12 minutes |

If your API answers `DISPATCH_NOT_FOUND`, we resend without `dispatch_id` so the call is
still on record. Transient errors are retried for about a minute and a half.
