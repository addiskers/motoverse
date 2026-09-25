"""
Plivo Voice <-> Gemini Live bridge, plus the Plivo REST call that dials out.

Wire protocol (Plivo Audio Streaming, bidirectional):
  Plivo -> us   {"event":"start", "start":{"streamId","callId",...}}
                {"event":"media", "media":{"payload": <base64 mulaw 8 kHz>}}
                {"event":"stop"}
  us -> Plivo   {"event":"playAudio", "media":{"contentType":"audio/x-mulaw",
                                                "sampleRate":8000, "payload":...}}
                {"event":"clearAudio", "streamId": ...}

Audio is the same G.711 mulaw 8 kHz as Twilio, so the codec in twilio_handler is
reused unchanged; only the JSON envelope differs.

Unlike the Twilio bridge, this one ends the Gemini session as soon as the phone
side goes away, so a hung-up call never leaves an open Gemini session behind.
"""

import asyncio
import base64
import json
import logging
import os
from xml.sax.saxutils import escape

import httpx

from twilio_handler import mulaw_to_pcm16k, pcm24k_to_mulaw

logger = logging.getLogger(__name__)

PLIVO_API_BASE = (os.getenv("PLIVO_API_BASE") or "https://api.plivo.com").rstrip("/")
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN", "")
# Caller ID. PLIVO_FROM_NUMBER is accepted as an alias; spaces are fine ("+91 80 ...").
PLIVO_NUMBER = os.getenv("PLIVO_NUMBER") or os.getenv("PLIVO_FROM_NUMBER", "")

# Hard cap on a phone call (Plivo hangs up at this point) and when the agent is
# told to wind up, matching the 7-minute browser demo limit.
CALL_TIME_LIMIT = int(os.getenv("CALL_TIME_LIMIT_SECONDS", "420") or 420)
WRAPUP_AFTER = max(30, CALL_TIME_LIMIT - 25)
RING_TIMEOUT = int(os.getenv("PLIVO_RING_TIMEOUT_SECONDS", "45") or 45)
DIAL_TIMEOUT = float(os.getenv("PLIVO_DIAL_TIMEOUT_SECONDS", "7") or 7)

WRAPUP_TEXT = "[SYSTEM] wrap up the call now"

# Plivo recommends playAudio payloads of 16 KB base64 or less; 12000 raw mulaw
# bytes encodes to 16000 base64 characters (1.5 s of audio).
_MAX_MULAW_PER_MSG = 12000


def configured():
    return bool(PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN and PLIVO_NUMBER)


def _digits(number):
    """Plivo wants E.164 digits without the leading '+'."""
    return "".join(ch for ch in str(number or "") if ch.isdigit())


def answer_xml(stream_url):
    """Answer-URL response: stream the call's audio to our WebSocket, both ways.
    keepCallAlive holds the call open for the life of the stream; when our socket
    closes, the call ends."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '    <Stream bidirectional="true" keepCallAlive="true" '
        'contentType="audio/x-mulaw;rate=8000">'
        f"{escape(stream_url)}</Stream>\n"
        "</Response>"
    )


def hangup_xml():
    return '<?xml version="1.0" encoding="UTF-8"?>\n<Response><Hangup/></Response>'


async def place_call(to_number, answer_url, hangup_url):
    """Dial `to_number`. Returns (status, data):
         ("ok", {"request_uuid": ...})     Plivo accepted the call
         ("rejected", {"http", "detail"})  Plivo refused it (bad number, no balance...)
         ("timeout", {"detail"})           Plivo did not answer in DIAL_TIMEOUT
         ("error", {"detail"})             network or other failure
    Never raises.
    """
    url = f"{PLIVO_API_BASE}/v1/Account/{PLIVO_AUTH_ID}/Call/"
    body = {
        "from": _digits(PLIVO_NUMBER),
        "to": _digits(to_number),
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
        "ring_timeout": RING_TIMEOUT,
        "time_limit": CALL_TIME_LIMIT,
    }
    try:
        async with httpx.AsyncClient(timeout=DIAL_TIMEOUT) as client:
            resp = await client.post(url, json=body, auth=(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN))
    except httpx.TimeoutException as e:
        logger.warning(f"Plivo dial timed out: {e}")
        return "timeout", {"detail": "Plivo did not respond in time"}
    except Exception as e:
        logger.warning(f"Plivo dial failed: {type(e).__name__}: {e}")
        return "error", {"detail": f"{type(e).__name__}: {e}"}

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:300]}
    if resp.status_code in (200, 201, 202):
        uuid = data.get("request_uuid")
        if isinstance(uuid, list):          # multi-destination form returns a list
            uuid = uuid[0] if uuid else None
        return "ok", {"request_uuid": uuid, "api_id": data.get("api_id")}
    logger.warning(f"Plivo rejected dial: HTTP {resp.status_code} {str(data)[:200]}")
    return "rejected", {"http": resp.status_code,
                        "detail": data.get("error") or data.get("message") or str(data)[:200]}


class PlivoMediaBridge:
    """Bridges one Plivo audio stream with one Gemini Live session."""

    def __init__(self, websocket, gemini_client, text_trigger, on_event=None,
                 audio_recorder=None, wrapup_after=WRAPUP_AFTER):
        self.ws = websocket
        self.gemini = gemini_client
        self.text_trigger = text_trigger
        self.on_event = on_event
        self.audio_recorder = audio_recorder
        self.wrapup_after = wrapup_after

        self.stream_id = None
        self.call_id = None
        self.started = False       # Plivo 'start' received: the callee picked up
        self.wrapped_up = False
        self.gemini_error = None   # set if the AI session failed, so the call is
                                   # reported as 'failed' rather than 'no_answer'

        self.audio_input_queue = asyncio.Queue()
        self.video_input_queue = asyncio.Queue()
        self.text_input_queue = asyncio.Queue()
        self._wrapup_task = None

    # ---- Gemini -> phone ---------------------------------------------------

    async def audio_output_callback(self, data: bytes):
        if not self.stream_id:
            return
        if self.audio_recorder:
            self.audio_recorder.add_output(data)
        try:
            mulaw = pcm24k_to_mulaw(data)
            for i in range(0, len(mulaw), _MAX_MULAW_PER_MSG):
                await self.ws.send_json({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(mulaw[i:i + _MAX_MULAW_PER_MSG]).decode("ascii"),
                    },
                })
        except Exception as e:
            logger.debug(f"Error sending audio to Plivo: {e}")

    async def audio_interrupt_callback(self):
        """Customer barged in: drop audio Plivo has buffered but not yet played."""
        if self.audio_recorder:
            self.audio_recorder.on_interrupt()
        if not self.stream_id:
            return
        try:
            await self.ws.send_json({"event": "clearAudio", "streamId": self.stream_id})
        except Exception:
            pass

    # ---- phone -> Gemini ---------------------------------------------------

    async def _handle_messages(self):
        try:
            while True:
                data = json.loads(await self.ws.receive_text())
                event = data.get("event")

                if event == "start":
                    start = data.get("start") or {}
                    self.stream_id = start.get("streamId") or data.get("streamId")
                    self.call_id = start.get("callId") or data.get("callId")
                    self.started = True
                    logger.info(f"Plivo stream started: stream={self.stream_id} call={self.call_id}")
                    await self._emit({"type": "call_start", "call_sid": self.call_id or ""})
                    self._wrapup_task = asyncio.create_task(self._wrapup_timer())
                    await self.text_input_queue.put(self.text_trigger)

                elif event == "media":
                    payload = (data.get("media") or {}).get("payload")
                    if not payload:
                        continue
                    pcm_16k = mulaw_to_pcm16k(base64.b64decode(payload))
                    if self.audio_recorder:
                        self.audio_recorder.add_input(pcm_16k)
                    await self.audio_input_queue.put(pcm_16k)

                elif event == "stop":
                    logger.info("Plivo stream stopped")
                    return
        except Exception as e:
            # Socket closed by Plivo on hangup lands here too.
            logger.info(f"Plivo stream ended: {type(e).__name__}")

    async def _wrapup_timer(self):
        try:
            await asyncio.sleep(self.wrapup_after)
            self.wrapped_up = True
            await self.text_input_queue.put(WRAPUP_TEXT)
        except asyncio.CancelledError:
            pass

    async def _emit(self, event):
        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:
                pass

    async def _run_gemini(self):
        try:
            await self._consume_gemini()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.gemini_error = f"{type(e).__name__}: {e}"
            logger.error(f"Gemini session error during Plivo call: {self.gemini_error}")

    async def _consume_gemini(self):
        async for event in self.gemini.start_session(
            audio_input_queue=self.audio_input_queue,
            video_input_queue=self.video_input_queue,
            text_input_queue=self.text_input_queue,
            audio_output_callback=self.audio_output_callback,
            audio_interrupt_callback=self.audio_interrupt_callback,
        ):
            if event:
                await self._emit(event)
                if event.get("type") == "error":
                    self.gemini_error = str(event.get("error") or event)[:200]
                    logger.error(f"Gemini error during Plivo call: {event}")
                    return

    async def run(self):
        """Run until either side ends, then tear the other side down."""
        phone = asyncio.create_task(self._handle_messages())
        gemini = asyncio.create_task(self._run_gemini())
        try:
            await asyncio.wait({phone, gemini}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (phone, gemini, self._wrapup_task):
                if t and not t.done():
                    t.cancel()
            for t in (phone, gemini):
                try:
                    await t
                except BaseException:
                    pass
            if self.started:
                await self._emit({"type": "call_end"})
            logger.info("Plivo-Gemini bridge closed")
