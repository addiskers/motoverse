"""
AutoServe Voice Agent API client.

All access to the dealership's booking system lives here. The voice agent decides
what to say; AutoServe decides what is true — booking references, statuses, slots
and charges all come back from this API and are never invented locally.

Switched off by default. `enabled()` is False unless AUTOSERVE_ENABLED is truthy
AND an API key is present, so the existing demo keeps running on its mock data
until the flag is explicitly set on the server.

Nothing in here raises: every call returns (ok: bool, data_or_error: dict) so a
network failure degrades to a spoken apology instead of a dropped phone call.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = (os.getenv("AUTOSERVE_BASE_URL")
            or "https://autoverseai-autoserve.vercel.app/api/v1").rstrip("/")
API_KEY = os.getenv("AUTOSERVE_API_KEY", "")
DEFAULT_MOBILE = os.getenv("AUTOSERVE_DEFAULT_MOBILE", "")
TIMEOUT = float(os.getenv("AUTOSERVE_TIMEOUT_SECONDS", "6") or 6)
_FLAG = (os.getenv("AUTOSERVE_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")

# Asia/Kolkata — the API's local wall clock for dates/times.
IST = timezone(timedelta(hours=5, minutes=30))

_client = None
_client_lock = asyncio.Lock()

# reference() is stable config; cache it briefly so we don't refetch mid-call.
_ref_cache = {"at": 0.0, "data": None}
_REF_TTL = 300

# After repeated transport failures, stop trying for a while: on a live phone
# call it is better to fall back instantly than to make the agent wait again.
_BREAKER_THRESHOLD = 2
_BREAKER_COOLDOWN = 60
_breaker = {"fails": 0, "until": 0.0}


def enabled():
    """True only when explicitly switched on AND configured."""
    return bool(_FLAG and API_KEY)


def configured():
    """An API key is present. Dispatch reporting only needs this: AutoServe must
    hear the outcome of calls it asked for, whether or not the agent's booking
    tools are switched on."""
    return bool(API_KEY)


def today_ist():
    return datetime.now(IST).date()


async def _get_client():
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    base_url=BASE_URL,
                    timeout=TIMEOUT,
                    headers={"Authorization": f"Bearer {API_KEY}",
                             "Content-Type": "application/json"},
                )
    return _client


async def aclose():
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _unwrap(resp):
    """AutoServe envelope -> (ok, data | error dict). Never raises."""
    try:
        body = resp.json()
    except Exception:
        return False, {"reason": "BAD_RESPONSE", "message": f"HTTP {resp.status_code}"}
    if isinstance(body, dict) and body.get("ok"):
        return True, body.get("data") or {}
    err = (body or {}).get("error") or {}
    return False, {
        "code": err.get("code") or "INTERNAL",
        "reason": err.get("reason") or "UNKNOWN",
        "message": err.get("message") or f"HTTP {resp.status_code}",
        "http": resp.status_code,
    }


async def _request(method, path, *, params=None, json=None, force=False):
    if not (enabled() or (force and configured())):
        return False, {"reason": "DISABLED", "message": "AutoServe is not enabled"}
    if time.time() < _breaker["until"]:
        return False, {"code": "INTERNAL", "reason": "UNREACHABLE",
                       "message": "AutoServe temporarily unavailable"}
    try:
        client = await _get_client()
        resp = await client.request(method, path, params=params, json=json)
    except Exception as e:
        # Transport-level failure: timeout, DNS, TLS, connection refused.
        _breaker["fails"] += 1
        if _breaker["fails"] >= _BREAKER_THRESHOLD:
            _breaker["until"] = time.time() + _BREAKER_COOLDOWN
            logger.warning(f"AutoServe unreachable; pausing calls for {_BREAKER_COOLDOWN}s")
        logger.warning(f"AutoServe {method} {path} failed: {type(e).__name__}: {e}")
        return False, {"code": "INTERNAL", "reason": "UNREACHABLE", "message": str(e)}
    _breaker["fails"] = 0
    _breaker["until"] = 0.0
    ok, data = _unwrap(resp)
    if not ok:
        logger.warning(f"AutoServe {method} {path} -> {data.get('reason')} ({data.get('http')})")
    return ok, data


async def _get(path, params=None):
    return await _request("GET", path, params=params)


async def _post(path, payload, force=False):
    return await _request("POST", path, json=payload, force=force)


def idem(call_ref, kind, n=1):
    """Stable idempotency key: a retry of the same action reuses the same key,
    so AutoServe's dedup returns the original booking instead of a duplicate."""
    return f"{(call_ref or 'call')[:60]}_{kind}_{n}"


# ---- endpoints -------------------------------------------------------------

async def health():
    return await _get("/health")


async def reference(force=False):
    """Outlets + services. Cached briefly — it only changes when the dealer edits setup."""
    now = time.time()
    if not force and _ref_cache["data"] is not None and now - _ref_cache["at"] < _REF_TTL:
        return True, _ref_cache["data"]
    ok, data = await _get("/reference")
    if ok:
        _ref_cache["at"] = now
        _ref_cache["data"] = data
    return ok, data


async def lookup_customer(mobile):
    return await _get("/customers/lookup", {"mobile": mobile})


async def vehicle(reg_no):
    return await _get(f"/vehicles/{reg_no}")


async def service_due(reg_no):
    return await _get(f"/vehicles/{reg_no}/service-due")


async def service_history(reg_no, limit=5):
    return await _get(f"/vehicles/{reg_no}/service-history", {"limit": limit})


async def slots(outlet_id=None, service_id=None, days=14, limit=50):
    """Available appointment slots.

    A date range is REQUIRED in practice: /slots with no from/to returns an empty
    list, which would leave the agent with nothing to offer.
    """
    start = today_ist()
    params = {"from": start.isoformat(),
              "to": (start + timedelta(days=days)).isoformat(),
              "limit": limit}
    if outlet_id:
        params["outlet_id"] = outlet_id
    if service_id:
        params["service_id"] = service_id
    return await _get("/slots", params)


async def pickup_slots(area=None, pincode=None, address_id=None, days=14, limit=50):
    """Slots that can actually be honoured door-to-door, plus the pickup charge."""
    start = today_ist()
    params = {"from": start.isoformat(),
              "to": (start + timedelta(days=days)).isoformat(),
              "limit": limit}
    if address_id:
        params["address_id"] = address_id
    elif pincode:
        params["pincode"] = pincode
    elif area:
        params["area"] = area
    return await _get("/pickup-slots", params)


async def pickup_serviceable(area=None, pincode=None):
    params = {"pincode": pincode} if pincode else {"area": area}
    return await _get("/pickup/serviceable", params)


async def create_booking(payload):
    return await _post("/bookings", payload)


async def get_booking(ref):
    return await _get(f"/bookings/{ref}")


async def cancel_booking(ref, idempotency_key, reason="other", note=""):
    return await _post(f"/bookings/{ref}/cancel",
                       {"idempotency_key": idempotency_key, "reason": reason, "note": note})


async def create_callback(payload):
    return await _post("/callbacks", payload)


async def log_call(payload, force=False):
    """POST /calls — dedups on external_ref, so a repeat updates in place.
    Note: this endpoint rejects idempotency_key, and rejects the whole record
    if dispatch_id is unknown (DISPATCH_NOT_FOUND)."""
    return await _post("/calls", payload, force=force)
