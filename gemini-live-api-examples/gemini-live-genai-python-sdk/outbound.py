"""
Outbound calls dispatched by AutoServe (POST /call-me).

Lifecycle of one dispatch:
  1. AutoServe POSTs a signed `call.requested` event. We verify the signature,
     dedupe on dispatch_id, dial through Plivo, and answer within seconds.
  2. Plivo fetches our answer URL and streams the call audio to our WebSocket.
  3. When the call ends (or never connects), we report the outcome back on
     POST /api/v1/calls with the dispatch_id, exactly once. That frees one of
     AutoServe's concurrent call slots. A watchdog guarantees a report inside
     their 15-minute window even if Plivo never calls us back.

The registry is in-memory: the app runs as a single uvicorn process. A restart
mid-call loses pending dispatches, and AutoServe then treats them as unreported.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone

import autoserve

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.getenv("AUTOSERVE_WEBHOOK_SECRET", "")
CALL_ME_API_KEY = os.getenv("CALL_ME_API_KEY", "")
SIGNATURE_TOLERANCE = int(os.getenv("AUTOSERVE_SIGNATURE_TOLERANCE_SECONDS", "300") or 300)
WATCHDOG_SECONDS = int(os.getenv("DISPATCH_WATCHDOG_SECONDS", "720") or 720)

# Signs the per-call answer / hangup / stream URLs we hand to Plivo. Only Plivo ever
# sees them, so knowing our domain is not enough to open a media stream.
_URL_SECRET = (os.getenv("OUTBOUND_URL_SECRET")
               or (hashlib.sha256(("outbound:" + os.getenv("PLIVO_AUTH_TOKEN", "")).encode()).hexdigest()
                   if os.getenv("PLIVO_AUTH_TOKEN") else secrets.token_hex(32)))

_registry = {}        # key -> dispatch record
_by_dispatch = {}     # dispatch_id -> key
_REGISTRY_TTL = 3600


# ---- inbound authentication -----------------------------------------------

def auth_mode():
    """How /call-me authenticates, or None when it is not configured (fail closed)."""
    if WEBHOOK_SECRET:
        return "signature"
    if CALL_ME_API_KEY:
        return "api_key"
    return None


def _parse_timestamp(raw):
    """Epoch seconds, epoch milliseconds or ISO-8601 -> epoch seconds (float)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v / 1000.0 if v > 1e12 else v
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def verify_autoserve_signature(headers, raw_body: bytes):
    """HMAC-SHA256 over "<timestamp>.<body>" with the shared secret.

    Tolerant of the header shape until AutoServe confirms it: the timestamp may
    arrive in X-AutoServe-Timestamp or inside the signature header as
    "t=...,v1=..."; the signature may be hex, "sha256=<hex>" or base64.
    Returns (ok, reason).
    """
    if not WEBHOOK_SECRET:
        return False, "signing secret not configured"
    sig_header = (headers.get("x-autoserve-signature") or "").strip()
    if not sig_header:
        return False, "missing X-AutoServe-Signature"

    ts_raw = (headers.get("x-autoserve-timestamp") or "").strip()
    candidates = []
    if "=" in sig_header and ("t=" in sig_header or "v1=" in sig_header):
        for part in sig_header.split(","):
            k, _, v = part.strip().partition("=")
            if k == "t" and not ts_raw:
                ts_raw = v.strip()
            elif k in ("v1", "sha256", "sig", "signature"):
                candidates.append(v.strip())
    else:
        candidates.append(sig_header[7:] if sig_header.lower().startswith("sha256=") else sig_header)

    ts = _parse_timestamp(ts_raw)
    if ts is None:
        return False, "missing or unreadable timestamp"
    if abs(time.time() - ts) > SIGNATURE_TOLERANCE:
        return False, "timestamp outside tolerance"

    mac = hmac.new(WEBHOOK_SECRET.encode(), ts_raw.encode() + b"." + raw_body, hashlib.sha256)
    expected_hex = mac.hexdigest()
    expected_b64 = base64.b64encode(mac.digest()).decode()
    for c in candidates:
        if hmac.compare_digest(c.lower(), expected_hex) or hmac.compare_digest(c, expected_b64):
            return True, "ok"
    return False, "signature mismatch"


def verify_api_key(headers):
    if not CALL_ME_API_KEY:
        return False
    auth = headers.get("authorization") or ""
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    key = headers.get("x-api-key") or bearer
    return bool(key) and hmac.compare_digest(key, CALL_ME_API_KEY)


# ---- per-call URL tokens ----------------------------------------------------

def url_sig(key):
    return hmac.new(_URL_SECRET.encode(), key.encode(), hashlib.sha256).hexdigest()[:32]


def check_url_sig(key, sig):
    return bool(key and sig) and key in _registry and hmac.compare_digest(url_sig(key), sig)


# ---- registry ---------------------------------------------------------------

def _prune():
    cutoff = time.time() - _REGISTRY_TTL
    for k in [k for k, r in _registry.items() if r["created_ts"] < cutoff]:
        rec = _registry.pop(k)
        if rec.get("dispatch_id"):
            _by_dispatch.pop(rec["dispatch_id"], None)


def existing(dispatch_id):
    key = _by_dispatch.get(dispatch_id) if dispatch_id else None
    return _registry.get(key) if key else None


def new_dispatch(payload, base_url):
    _prune()
    key = secrets.token_urlsafe(12)
    rec = {
        "key": key,
        "dispatch_id": payload.get("dispatch_id"),
        "phone": payload.get("phone"),
        "customer": payload.get("customer") or {},
        "vehicle": payload.get("vehicle") or {},
        "outlet": payload.get("outlet") or {},
        "reason": payload.get("reason"),
        "base_url": base_url,
        "created_ts": time.time(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request_uuid": None,
        "status": "dialling",
        "connected": False,
        "reported": False,
        "call_id": None,
    }
    _registry[key] = rec
    if rec["dispatch_id"]:
        _by_dispatch[rec["dispatch_id"]] = key
    return rec


def get(key):
    return _registry.get(key)


def forget(key):
    """Drop a dispatch that never dialled, so AutoServe can retry the same id."""
    rec = _registry.pop(key, None)
    if rec and rec.get("dispatch_id"):
        _by_dispatch.pop(rec["dispatch_id"], None)


def urls(rec):
    key, sig, base = rec["key"], url_sig(rec["key"]), rec["base_url"]
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return {
        "answer": f"{base}/plivo/answer/{key}/{sig}",
        "hangup": f"{base}/plivo/hangup/{key}/{sig}",
        "stream": f"{ws_base}/plivo/media-stream/{key}/{sig}",
    }


# ---- outcome reporting --------------------------------------------------------

_NO_ANSWER_STATUSES = {"busy", "no-answer", "timeout", "cancel", "canceled", "cancelled", "rejected"}


def outcome_for_telephony(status):
    return "no_answer" if (status or "").lower() in _NO_ANSWER_STATUSES else "failed"


def outcome_for_call(call, wrapped_up=False):
    """Map a finished conversation to AutoServe's outcome vocabulary."""
    call = call or {}
    if call.get("booking_created"):
        return "booked"
    tools = [t.get("name") for t in (call.get("tool_calls") or [])]
    if "raise_callback" in tools or wrapped_up:
        return "callback"
    if any(m.get("role") == "user" for m in (call.get("transcript") or [])):
        return "not_interested"
    return "no_answer"


def _summary(outcome, call, detail=None):
    call = call or {}
    if outcome == "booked":
        for t in reversed(call.get("tool_calls") or []):
            r = t.get("result") or {}
            if t.get("name") == "schedule_pickup" and isinstance(r, dict) and r.get("success"):
                bits = [f"Booked {r.get('booking_ref')}", f"for {r.get('date')}" if r.get("date") else "",
                        f"at {r.get('outlet')}" if r.get("outlet") else "",
                        "with pickup" if r.get("pickup") else ""]
                return " ".join(b for b in bits if b) + "."
        return "Booking made during the call."
    if outcome == "callback":
        for t in reversed(call.get("tool_calls") or []):
            if t.get("name") == "raise_callback":
                note = (t.get("args") or {}).get("context_note")
                if note:
                    return f"Callback requested: {note}"[:500]
        return "Call reached the time limit; customer handed to the team for a callback."
    if outcome == "not_interested":
        return "Customer spoke with the agent; no booking was made."
    if outcome == "no_answer":
        return f"Customer did not answer{f' ({detail})' if detail else ''}."
    return f"Call could not be completed{f': {detail}' if detail else ''}."


def _transcript(call):
    out = []
    for m in (call or {}).get("transcript") or []:
        role = "customer" if m.get("role") == "user" else "agent"
        if m.get("text"):
            out.append({"role": role, "text": m["text"]})
    return out


def _call_payload(call, *, outcome, external_ref, started_at, duration,
                  dispatch_id=None, customer_id=None, recording_url=None, detail=None):
    payload = {
        "external_ref": external_ref,
        "dispatch_id": dispatch_id,
        "customer_id": customer_id,
        "direction": "outgoing",
        "started_at": started_at,
        "duration_sec": int(float(duration or 0)),
        "outcome": outcome,
        "sentiment": "neutral",
        "summary": _summary(outcome, call, detail),
        "recording_url": recording_url,
        "transcript": _transcript(call) or None,
    }
    return {k: v for k, v in payload.items() if v not in (None, "", [])}


async def _send(payload, label):
    """POST /api/v1/calls with retries on transport errors. The API dedups on
    external_ref, so a retry updates in place rather than duplicating."""
    backoff = [5, 20, 60]
    while True:
        ok, data = await autoserve.log_call(payload, force=True)
        if ok:
            logger.info(f"{label}: reported {payload.get('outcome')}")
            return True
        reason = data.get("reason")
        if reason == "DISPATCH_NOT_FOUND" and "dispatch_id" in payload:
            # Keep the call record even if AutoServe no longer knows the dispatch.
            # Retried at once: this is a data problem, not a transient one.
            payload = {k: v for k, v in payload.items() if k != "dispatch_id"}
            continue
        if reason not in ("UNREACHABLE", "INTERNAL", "BAD_RESPONSE", "UNKNOWN"):
            logger.warning(f"{label}: report rejected ({reason}): {data.get('message')}")
            return False
        if not backoff:
            logger.warning(f"{label}: report failed after retries")
            return False
        await asyncio.sleep(backoff.pop(0))


async def report(key, outcome, *, call=None, recording_url=None, detail=None,
                 telephony_duration=None):
    """Report a dispatched call's outcome on POST /api/v1/calls, exactly once.
    Never raises."""
    rec = _registry.get(key)
    if not rec or rec["reported"]:
        return
    rec["reported"] = True
    rec["status"] = f"reported:{outcome}"
    call = call or {}
    duration = call.get("duration_seconds")
    if duration is None:
        duration = telephony_duration
    payload = _call_payload(
        call, outcome=outcome, detail=detail, recording_url=recording_url,
        external_ref=call.get("id") or (f"plivo_{rec['request_uuid']}" if rec.get("request_uuid")
                                        else f"dispatch_{key}"),
        started_at=call.get("started_at") or rec["created_at"],
        duration=duration,
        dispatch_id=rec.get("dispatch_id"),
        customer_id=(rec.get("customer") or {}).get("customer_id"),
    )
    try:
        await _send(payload, f"Dispatch {rec.get('dispatch_id')}")
    except Exception as e:
        logger.warning(f"Dispatch report error: {e}")


async def log_finished_call(call, *, customer_id=None, recording_url=None, wrapped_up=False):
    """Final upsert for a non-dispatched call that already logged itself (for
    booking attribution), so the provisional outcome is replaced by the real one."""
    call = call or {}
    if not call.get("id"):
        return
    payload = _call_payload(
        call, outcome=outcome_for_call(call, wrapped_up), recording_url=recording_url,
        external_ref=call["id"], started_at=call.get("started_at"),
        duration=call.get("duration_seconds"), customer_id=customer_id,
    )
    try:
        await _send(payload, f"Call {call['id']}")
    except Exception as e:
        logger.warning(f"Call log error: {e}")


def start_watchdog(key):
    """Guarantee a report inside AutoServe's 15-minute window."""
    async def _watch():
        try:
            await asyncio.sleep(WATCHDOG_SECONDS)
            rec = _registry.get(key)
            if rec and not rec["reported"]:
                logger.warning(f"Dispatch {rec.get('dispatch_id')}: no outcome in "
                               f"{WATCHDOG_SECONDS}s, reporting failed")
                await report(key, "failed", detail="no outcome received from the phone network")
        except asyncio.CancelledError:
            pass
    return asyncio.create_task(_watch())
