"""
Post-call safety net for the call outcome.

The agent tags "call me later" and "stop calling me" with tools during the call.
If it forgets, the rule-based outcome would say "not_interested", which tells
AutoServe the customer is done and they are never called again. So when that is
the verdict, this reads the transcript once more with a plain text model and
asks one question: what did the customer actually ask for?

It is advisory only: any error, timeout or unclear answer returns None and the
rule-based outcome stands. It never blocks or fails a report.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

MODEL = os.getenv("OUTCOME_CHECK_MODEL", "gemini-flash-latest")
TIMEOUT = float(os.getenv("OUTCOME_CHECK_TIMEOUT_SECONDS", "8") or 8)
ENABLED = (os.getenv("OUTCOME_CHECK_ENABLED", "true") or "").strip().lower() in ("1", "true", "yes", "on")

INTENTS = ("call_back_later", "stop_calling", "asked_for_human", "none")

_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "callback_in_minutes": {"type": "integer"},
        "callback_at_local": {"type": "string"},
    },
    "required": ["intent"],
}

_PROMPT = """You review the transcript of a phone call between an AI service advisor
("agent") and a car owner ("customer"). Decide what the CUSTOMER asked for.

intent:
- "call_back_later": the customer asked to be called again later, in any language
  (e.g. "1-2 ghante baad call kijiye", "kal call karna", "abhi busy hoon, baad mein",
  "call me in the evening").
- "stop_calling": the customer asked not to be called or contacted again.
- "asked_for_human": the customer asked to speak to a person or a senior.
- "none": anything else, including simply not being interested right now.

For "call_back_later" also give EITHER callback_in_minutes (for a relative time; use
the LATER end of a range, so "1-2 hours" is 120; use 120 if no time was given) OR
callback_at_local as "YYYY-MM-DD HH:MM" in India time (for a named day or clock time).

The call ended at {ended} India time.

Transcript:
{transcript}
"""

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def _render(transcript):
    lines = []
    for m in transcript or []:
        text = (m.get("text") or "").strip()
        if text:
            who = "customer" if m.get("role") == "user" else "agent"
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


async def classify(transcript, ended_local):
    """Returns {"intent", "callback_in_minutes"?, "callback_at_local"?} or None."""
    if not ENABLED or not os.getenv("GEMINI_API_KEY"):
        return None
    rendered = _render(transcript)
    if "customer:" not in rendered:
        return None
    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_SCHEMA,
            temperature=0,
        )
        resp = await asyncio.wait_for(
            _get_client().aio.models.generate_content(
                model=MODEL,
                contents=_PROMPT.format(ended=ended_local, transcript=rendered),
                config=config,
            ),
            timeout=TIMEOUT,
        )
        data = json.loads((resp.text or "").strip().strip("`").removeprefix("json").strip())
    except Exception as e:
        logger.warning(f"Outcome check skipped: {type(e).__name__}: {e}")
        return None
    if not isinstance(data, dict) or data.get("intent") not in INTENTS:
        return None
    logger.info(f"Outcome check: {data}")
    return data
