import asyncio
import base64
import csv
import io
import json
import logging
import os
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from gemini_live import GeminiLive
from twilio_handler import TwilioMediaBridge

import pricing
import store
from recorder import CallRecorder

# Load environment variables
load_dotenv()

# Configure logging - DEBUG for our modules, INFO for everything else
logging.basicConfig(level=logging.INFO)
logging.getLogger("gemini_live").setLevel(logging.INFO)
logging.getLogger(__name__).setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.1-flash-live-preview")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+19785715824")
ANALYTICS_SECRET = os.getenv("ANALYTICS_SECRET", "kataria2026")
# Separate client-facing API key (Bearer token) for /api/v1/analytics/* and the
# /analytics dashboard. NO default on purpose — if unset, the client API returns
# 503 so it can never ship open. This key is safe to share with the client's
# developers; it exposes only cost-free usage data, never our internal /admin.
ANALYTICS_API_KEY = os.getenv("ANALYTICS_API_KEY", "")

# ============ MOCK BACKEND DATA ============

VEHICLES = {
    "default": {
        "vehicle_number": "GJ05GT0903",
        "owner_name": "Chetan Seth",
        "phone": "+919876543210",
        "model": "Maruti Suzuki Baleno",
        "year": 2024,
        "purchase_date": "2024-10-30",
        "warranty_expiry": "2026-10-30",
        "warranty_active": True,
        "current_km_system": 8604,
        "service_history": [
            {
                "service_number": 1,
                "date": "2025-02-15",
                "km": 1023,
                "workshop": "Autoverse Motors, Ahmedabad",
                "type": "First Free Service",
                "cost": 0
            },
            {
                "service_number": 2,
                "date": "2025-08-20",
                "km": 5111,
                "workshop": "Karodhra Workshop",
                "type": "Second Service",
                "cost": 2200
            }
        ],
        "next_service": {
            "service_number": 3,
            "type": "Third Service",
            "due_km": 10000,
            "estimated_cost_min": 2500,
            "estimated_cost_max": 3000
        },
        "pickup_drop_free": True,
        "address": "B-101, Sterling City , Ahmedabad"
    }
}

def handle_get_vehicle_info(**kwargs):
    return VEHICLES["default"]

def handle_schedule_pickup(**kwargs):
    return {
        "success": True,
        "booking_id": "BK-20260413-001",
        "vehicle_number": kwargs.get("vehicle_number", "GJ05GT0903"),
        "pickup_date": kwargs.get("date", "2026-04-13"),
        "pickup_time": kwargs.get("time", "9:30 AM"),
        "driver_name": "Rajesh Kumar",
        "driver_phone": "+919876500001",
        "pickup_address": kwargs.get("pickup_address", "B-101, Sterling City, Bopal, Ahmedabad"),
        "workshop": "Autoverse Motors, S.G. Highway, Ahmedabad",
        "special_instructions": kwargs.get("special_instructions", ""),
        "note": "Driver details will be sent via SMS on the morning of pickup."
    }

def handle_get_service_cost_estimate(**kwargs):
    estimates = {
        "Third Service": {"min": 2500, "max": 3000, "includes": "Oil change, filter replacement, brake inspection, general checkup"},
        "Second Service": {"min": 2000, "max": 2500, "includes": "Oil change, filter check, general inspection"},
        "First Free Service": {"min": 0, "max": 0, "includes": "General inspection, fluid top-up (free under warranty)"},
    }
    service_type = kwargs.get("service_type", "Third Service")
    return estimates.get(service_type, {"min": 2000, "max": 4000, "includes": "General service"})


# Live transcript watchers (browser WebSockets watching phone calls)
live_watchers: set = set()

# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.on_event("startup")
async def _startup():
    """Initialize the call store and clean up any calls orphaned by a crash."""
    try:
        await store.init()
        await store.sweep_stale()
    except Exception as e:
        logger.error(f"Call store init failed: {e}")


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Gemini Live."""
    await websocket.accept()

    logger.info("WebSocket connection accepted")

    recorder = CallRecorder(model=MODEL)
    await recorder.open(source="browser")

    audio_input_queue = asyncio.Queue()
    video_input_queue = asyncio.Queue()
    text_input_queue = asyncio.Queue()

    client_disconnected = False

    async def audio_output_callback(data):
        if not client_disconnected:
            try:
                await websocket.send_bytes(data)
            except Exception:
                pass

    async def audio_interrupt_callback():
        pass

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY,
        model=MODEL,
        input_sample_rate=16000,
        tool_mapping={
            "get_vehicle_info": handle_get_vehicle_info,
            "schedule_pickup": handle_schedule_pickup,
            "get_service_cost_estimate": handle_get_service_cost_estimate,
        }
    )

    session_task = None

    async def receive_from_client():
        nonlocal client_disconnected
        try:
            while True:
                message = await websocket.receive()

                if message.get("bytes"):
                    await audio_input_queue.put(message["bytes"])
                elif message.get("text"):
                    text = message["text"]
                    try:
                        payload = json.loads(text)
                        if isinstance(payload, dict) and payload.get("type") == "image":
                            logger.info(f"Received image chunk from client: {len(payload['data'])} base64 chars")
                            image_data = base64.b64decode(payload["data"])
                            await video_input_queue.put(image_data)
                            continue
                    except json.JSONDecodeError:
                        pass

                    await text_input_queue.put(text)
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error receiving from client: {e}")
        finally:
            client_disconnected = True
            if session_task and not session_task.done():
                session_task.cancel()

    receive_task = asyncio.create_task(receive_from_client())

    MAX_RETRIES = 3
    RETRY_DELAYS = [2, 4, 8]

    async def run_session_with_retry():
        for attempt in range(MAX_RETRIES + 1):
            should_retry = False
            try:
                async for event in gemini_client.start_session(
                    audio_input_queue=audio_input_queue,
                    video_input_queue=video_input_queue,
                    text_input_queue=text_input_queue,
                    audio_output_callback=audio_output_callback,
                    audio_interrupt_callback=audio_interrupt_callback,
                ):
                    if event:
                        if event.get("type") == "error" and attempt < MAX_RETRIES:
                            error_msg = event.get("error", "")
                            if "exhausted" in error_msg or "quota" in error_msg.lower():
                                delay = RETRY_DELAYS[attempt]
                                logger.warning(f"Quota error, retrying in {delay}s (attempt {attempt+1}/{MAX_RETRIES})")
                                try:
                                    await websocket.send_json({"type": "status", "text": "Reconnecting..."})
                                except RuntimeError:
                                    return
                                await asyncio.sleep(delay)
                                should_retry = True
                                break
                        if event.get("type") == "go_away" and attempt < MAX_RETRIES:
                            logger.info(f"GoAway received, reconnecting (attempt {attempt+1}/{MAX_RETRIES})")
                            try:
                                await websocket.send_json({"type": "status", "text": "Reconnecting..."})
                            except RuntimeError:
                                return
                            await asyncio.sleep(1)
                            should_retry = True
                            break
                        await recorder.on_event(event)
                        try:
                            await websocket.send_json(event)
                        except RuntimeError:
                            return
                if not should_retry:
                    return
            except Exception as e:
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(f"Session error, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    raise

    try:
        session_task = asyncio.create_task(run_session_with_retry())
        await session_task
    except asyncio.CancelledError:
        logger.info("Gemini session cancelled due to client disconnect")
    except Exception as e:
        import traceback
        logger.error(f"Error in Gemini session: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        receive_task.cancel()
        await recorder.close()
        try:
            await websocket.close()
        except:
            pass
        logger.info("connection closed")


# ============ TWILIO VOICE ENDPOINTS ============

@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def twilio_voice(request: Request):
    """Twilio webhook: when someone calls your Twilio number, this answers."""
    host = request.headers.get("host", "localhost")
    protocol = "wss" if request.url.scheme == "https" or "onrender.com" in host or "globalvoxinc.ai" in host else "ws"
    ws_url = f"{protocol}://{host}/twilio/media-stream"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller" value="{{{{From}}}}" />
        </Stream>
    </Connect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    """WebSocket endpoint for Twilio Media Streams."""
    await websocket.accept()
    logger.info("Twilio Media Stream WebSocket accepted")

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY,
        model=MODEL,
        input_sample_rate=16000,
        tool_mapping={
            "get_vehicle_info": handle_get_vehicle_info,
            "schedule_pickup": handle_schedule_pickup,
            "get_service_cost_estimate": handle_get_service_cost_estimate,
        }
    )

    recorder = CallRecorder(model=MODEL)

    async def broadcast_event(event):
        """Send transcript events to all live watchers AND record the call."""
        # Persist (never let a recorder failure affect the live broadcast).
        etype = event.get("type")
        if etype == "call_start":
            await recorder.open(source="twilio", call_sid=event.get("call_sid") or None,
                                caller=event.get("caller"))
        elif etype == "call_end":
            await recorder.close()
        else:
            await recorder.on_event(event)

        dead = set()
        for watcher in live_watchers:
            try:
                await watcher.send_json(event)
            except Exception:
                dead.add(watcher)
        live_watchers.difference_update(dead)

    bridge = TwilioMediaBridge(
        websocket=websocket,
        gemini_client=gemini_client,
        text_trigger="Hi, I have picked up the phone. Please start the call.",
        on_event=broadcast_event,
    )

    try:
        await bridge.run()
    except Exception as e:
        import traceback
        logger.error(f"Twilio bridge error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        try:
            await websocket.close()
        except:
            pass


@app.post("/call-me")
async def call_me(request: Request):
    """Make Twilio call a phone number and connect to the AI agent."""
    from twilio.rest import Client

    body = await request.json()
    to_number = body.get("phone")
    if not to_number:
        return {"error": "Missing 'phone' field. Send {\"phone\": \"+91XXXXXXXXXX\"}"}

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured"}

    # Use PUBLIC_URL env var or Render URL — Twilio can't reach localhost
    public_url = os.getenv("PUBLIC_URL", "")
    if public_url:
        webhook_url = f"{public_url}/twilio/voice"
    else:
        host = request.headers.get("host", "localhost")
        protocol = "https" if "onrender.com" in host else request.url.scheme
        webhook_url = f"{protocol}://{host}/twilio/voice"

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url,
        )
        logger.info(f"Outbound call initiated: {call.sid} to {to_number}")
        return {"success": True, "call_sid": call.sid, "to": to_number}
    except Exception as e:
        logger.error(f"Failed to initiate call: {e}")
        return {"error": str(e)}


# ============ LIVE TRANSCRIPT DASHBOARD ============

@app.get("/live")
async def live_dashboard():
    """Live transcript dashboard — watch phone calls in real-time."""
    return HTMLResponse(LIVE_DASHBOARD_HTML)


@app.websocket("/live/ws")
async def live_ws(websocket: WebSocket):
    """WebSocket for live transcript watchers."""
    await websocket.accept()
    live_watchers.add(websocket)
    logger.info(f"Live watcher connected ({len(live_watchers)} total)")
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except:
        pass
    finally:
        live_watchers.discard(websocket)
        logger.info(f"Live watcher disconnected ({len(live_watchers)} total)")


LIVE_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Call Transcript</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0e17;
  --card: rgba(17,24,39,0.75);
  --border: rgba(255,255,255,0.08);
  --cyan: #00d4ff;
  --purple: #7c3aed;
  --green: #10b981;
  --red: #ef4444;
  --text: #f1f5f9;
  --muted: #64748b;
  --secondary: #94a3b8;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(0,212,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
}
.top-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 24px;
  background: rgba(10,14,23,0.9);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--cyan);
}
.status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 600;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted);
}
.dot.live {
  background: var(--green);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(16,185,129,0.4); }
  50% { opacity:0.7; box-shadow: 0 0 0 4px rgba(16,185,129,0); }
}
.container {
  flex: 1;
  max-width: 700px;
  width: 100%;
  margin: 0 auto;
  padding: 20px;
  position: relative;
  z-index: 1;
}
.waiting {
  text-align: center;
  padding: 60px 20px;
  color: var(--muted);
}
.waiting h2 { font-size: 1.1rem; margin-bottom: 8px; color: var(--secondary); }
.waiting p { font-size: 0.8rem; }
#transcript {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.msg {
  padding: 10px 14px;
  border-radius: 12px;
  max-width: 85%;
  font-size: 0.875rem;
  line-height: 1.5;
  animation: fadeIn 0.2s ease-out;
}
@keyframes fadeIn {
  from { opacity:0; transform: translateY(8px); }
  to { opacity:1; transform: translateY(0); }
}
.msg .time {
  display: block;
  font-size: 0.6rem;
  opacity: 0.5;
  font-family: 'SF Mono', monospace;
  margin-top: 3px;
}
.msg.user {
  align-self: flex-end;
  background: linear-gradient(135deg, rgba(0,212,255,0.2), rgba(0,212,255,0.1));
  border: 1px solid rgba(0,212,255,0.15);
  border-bottom-right-radius: 4px;
}
.msg.gemini {
  align-self: flex-start;
  background: linear-gradient(135deg, rgba(124,58,237,0.2), rgba(124,58,237,0.1));
  border: 1px solid rgba(124,58,237,0.15);
  border-bottom-left-radius: 4px;
}
.msg.system {
  align-self: center;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.75rem;
  max-width: 100%;
  text-align: center;
}
.tool-card {
  align-self: center;
  background: rgba(16,185,129,0.08);
  border: 1px solid rgba(16,185,129,0.2);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 0.75rem;
  color: var(--green);
  max-width: 100%;
  animation: fadeIn 0.2s ease-out;
}
.tool-card .tool-name { font-weight: 700; }
.tool-card pre {
  margin-top: 6px;
  color: var(--secondary);
  font-size: 0.7rem;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
</head>
<body>
<div class="top-bar">
  <span class="brand">Live Call Transcript</span>
  <div class="status">
    <span class="dot" id="statusDot"></span>
    <span id="statusText">Waiting for call...</span>
  </div>
</div>
<div class="container">
  <div class="waiting" id="waiting">
    <h2>No active call</h2>
    <p>Start a call using the "Call Me" button or dial +1 (978) 571-5824.<br>The transcript will appear here in real-time.</p>
  </div>
  <div id="transcript"></div>
</div>
<script>
const transcript = document.getElementById('transcript');
const waiting = document.getElementById('waiting');
const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
let currentUser = null;
let currentGemini = null;

const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(protocol + '//' + location.host + '/live/ws');

ws.onopen = () => { statusText.textContent = 'Connected — waiting for call...'; };
ws.onclose = () => { statusText.textContent = 'Disconnected'; statusDot.className = 'dot'; };

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);

  if (msg.type === 'call_start') {
    waiting.style.display = 'none';
    statusDot.className = 'dot live';
    statusText.textContent = 'Call in progress';
    addSystem('Call started');
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'call_end') {
    statusDot.className = 'dot';
    statusText.textContent = 'Call ended';
    addSystem('Call ended');
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'user') {
    if (currentUser) {
      currentUser.querySelector('.text').textContent += msg.text;
    } else {
      currentUser = addMsg('user', msg.text);
      currentGemini = null;
    }
  }
  else if (msg.type === 'gemini') {
    if (currentGemini) {
      currentGemini.querySelector('.text').textContent += msg.text;
    } else {
      currentGemini = addMsg('gemini', msg.text);
      currentUser = null;
    }
  }
  else if (msg.type === 'turn_complete') {
    currentUser = null;
    currentGemini = null;
  }
  else if (msg.type === 'tool_call') {
    addTool(msg.name, msg.result);
  }

  window.scrollTo(0, document.body.scrollHeight);
};

function addMsg(type, text) {
  const time = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const div = document.createElement('div');
  div.className = 'msg ' + type;
  div.innerHTML = '<span class="text"></span><span class="time">' + time + '</span>';
  div.querySelector('.text').textContent = text;
  transcript.appendChild(div);
  return div;
}

function addSystem(text) {
  const div = document.createElement('div');
  div.className = 'msg system';
  div.textContent = text;
  transcript.appendChild(div);
}

function addTool(name, result) {
  const div = document.createElement('div');
  div.className = 'tool-card';
  div.innerHTML = '<span class="tool-name">' + name + '</span><pre>' +
    JSON.stringify(result, null, 2).slice(0, 500) + '</pre>';
  transcript.appendChild(div);
}
</script>
</body>
</html>"""


ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin · Call Analytics</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0e17; --card:rgba(17,24,39,0.75); --border:rgba(255,255,255,0.08);
  --cyan:#00d4ff; --purple:#7c3aed; --green:#10b981; --red:#ef4444; --amber:#f59e0b;
  --text:#f1f5f9; --muted:#64748b; --secondary:#94a3b8;
  --mono:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;}
body::before{content:'';position:fixed;inset:0;background-image:
  linear-gradient(rgba(0,212,255,0.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(0,212,255,0.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0;}
.hidden{display:none !important;}
a{color:var(--cyan);}

/* ---- login ---- */
.login-wrap{position:relative;z-index:1;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
.login-card{background:var(--card);backdrop-filter:blur(14px);border:1px solid var(--border);border-radius:18px;padding:34px 30px;width:100%;max-width:380px;text-align:center;}
.login-card h1{font-size:1.15rem;margin-bottom:6px;}
.login-card p{color:var(--muted);font-size:0.8rem;margin-bottom:20px;}
input,select{font-family:inherit;background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:10px;color:var(--text);padding:10px 12px;font-size:0.85rem;width:100%;outline:none;}
input:focus,select:focus{border-color:var(--cyan);}
.btn{font-family:inherit;cursor:pointer;border:none;border-radius:10px;padding:10px 16px;font-size:0.82rem;font-weight:600;color:#04121a;background:var(--cyan);transition:opacity .15s;}
.btn:hover{opacity:.88;}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--secondary);}
.login-card .btn{width:100%;margin-top:14px;}
.err{color:var(--red);font-size:0.75rem;margin-top:10px;min-height:1em;}

/* ---- shell ---- */
.top-bar{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;
  padding:12px 22px;background:rgba(10,14,23,0.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);}
.brand{font-weight:800;font-size:0.92rem;color:var(--cyan);letter-spacing:.2px;}
.top-actions{display:flex;align-items:center;gap:10px;}
.status{display:flex;align-items:center;gap:6px;font-size:0.72rem;color:var(--secondary);}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2.4s infinite;}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(16,185,129,.4);}50%{opacity:.6;box-shadow:0 0 0 4px rgba(16,185,129,0);}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
@keyframes shimmer{0%{background-position:-400px 0;}100%{background-position:400px 0;}}
.container{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:22px;}
.section-title{font-size:0.7rem;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin:26px 4px 12px;}

/* ---- stat cards ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;}
.stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;animation:fadeIn .25s;}
.stat .label{font-size:0.66rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);}
.stat .value{font-size:1.5rem;font-weight:800;margin-top:8px;font-family:var(--mono);}
.stat .sub{font-size:0.68rem;color:var(--secondary);margin-top:4px;}
.stat.hi .value{color:var(--purple);}
.stat.cy .value{color:var(--cyan);}
.stat.gr .value{color:var(--green);}

/* ---- chart + breakdown ---- */
.row2{display:grid;grid-template-columns:2fr 1fr;gap:14px;}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;}
.panel h3{font-size:0.8rem;font-weight:600;margin-bottom:14px;color:var(--secondary);}
#trend svg{width:100%;height:220px;display:block;}
.brk-row{display:flex;align-items:center;justify-content:space-between;font-size:0.78rem;padding:7px 0;border-bottom:1px solid var(--border);}
.brk-row:last-child{border-bottom:none;}
.brk-bar{height:6px;border-radius:3px;background:var(--cyan);margin-top:5px;}

/* ---- filters + table ---- */
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px;}
.filters input,.filters select{width:auto;}
.filters .grow{flex:1;min-width:160px;}
.table-scroll{overflow-x:auto;background:var(--card);border:1px solid var(--border);border-radius:14px;}
table{width:100%;border-collapse:collapse;font-size:0.78rem;min-width:880px;}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--border);white-space:nowrap;}
th{font-size:0.66rem;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);cursor:pointer;user-select:none;position:sticky;top:0;background:#0d1320;}
th.num,td.num{text-align:right;font-family:var(--mono);}
tbody tr{cursor:pointer;transition:background .12s;}
tbody tr:hover{background:rgba(0,212,255,0.05);}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:0.66rem;font-weight:600;}
.pill.completed{background:rgba(16,185,129,.15);color:var(--green);}
.pill.in_progress{background:rgba(0,212,255,.15);color:var(--cyan);}
.pill.abandoned,.pill.failed{background:rgba(239,68,68,.15);color:var(--red);}
.pill.src{background:rgba(124,58,237,.15);color:#b794f6;}
.est{color:var(--amber);font-size:0.6rem;margin-left:4px;}
.tick{color:var(--green);font-weight:700;}
.dash{color:var(--muted);}
.empty{text-align:center;color:var(--muted);padding:40px 20px;font-size:0.82rem;}

/* ---- drawer ---- */
#backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;}
#drawer{position:fixed;top:0;right:0;height:100vh;width:480px;max-width:100%;z-index:50;
  background:#0c111c;border-left:1px solid var(--border);overflow-y:auto;animation:slideIn .2s ease-out;}
@keyframes slideIn{from{transform:translateX(100%);}to{transform:translateX(0);}}
.dh{position:sticky;top:0;background:rgba(12,17,28,.96);backdrop-filter:blur(8px);border-bottom:1px solid var(--border);padding:14px 18px;display:flex;justify-content:space-between;align-items:flex-start;gap:10px;}
.dh .who{font-weight:700;font-size:0.9rem;}
.dh .meta{font-size:0.7rem;color:var(--muted);margin-top:3px;}
.dbody{padding:16px 18px;}
.x{cursor:pointer;color:var(--muted);font-size:1.3rem;line-height:1;background:none;border:none;}
.cost-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:8px;}
.cost-card{border:1px solid var(--border);border-radius:12px;padding:12px;}
.cost-card.gem{border-color:rgba(124,58,237,.3);}
.cost-card.tw{border-color:rgba(0,212,255,.3);}
.cost-card .ct{font-size:0.66rem;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px;}
.cost-card .big{font-size:1.25rem;font-weight:800;font-family:var(--mono);}
.cost-card.gem .big{color:var(--purple);}
.cost-card.tw .big{color:var(--cyan);}
.kv{display:flex;justify-content:space-between;font-size:0.7rem;color:var(--secondary);padding:3px 0;}
.kv span:last-child{font-family:var(--mono);color:var(--text);}
.sub-h{font-size:0.7rem;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin:18px 0 10px;}
.msg{padding:9px 13px;border-radius:12px;max-width:88%;font-size:0.82rem;line-height:1.5;margin-bottom:7px;animation:fadeIn .2s;}
.msg .t{display:block;font-size:0.58rem;opacity:.5;font-family:var(--mono);margin-top:3px;}
.msg.user{margin-left:auto;background:linear-gradient(135deg,rgba(0,212,255,.2),rgba(0,212,255,.08));border:1px solid rgba(0,212,255,.15);}
.msg.gemini{background:linear-gradient(135deg,rgba(124,58,237,.2),rgba(124,58,237,.08));border:1px solid rgba(124,58,237,.15);}
.tool{background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);border-radius:8px;padding:9px 12px;font-size:0.72rem;color:var(--green);margin-bottom:7px;}
.tool b{font-weight:700;}
.tool pre{margin-top:5px;color:var(--secondary);font-size:0.66rem;white-space:pre-wrap;word-break:break-word;}

/* ---- toasts + skeleton ---- */
#toasts{position:fixed;top:14px;right:14px;z-index:80;display:flex;flex-direction:column;gap:8px;}
.toast{background:var(--card);backdrop-filter:blur(10px);border:1px solid var(--border);border-left-width:3px;border-radius:10px;padding:10px 14px;font-size:0.78rem;animation:fadeIn .2s;min-width:200px;}
.toast.error{border-left-color:var(--red);}
.toast.success{border-left-color:var(--green);}
.toast.info{border-left-color:var(--cyan);}
.skel{background:linear-gradient(90deg,rgba(255,255,255,.03),rgba(255,255,255,.08),rgba(255,255,255,.03));background-size:800px 100%;animation:shimmer 1.4s infinite;border-radius:8px;height:14px;}

@media(max-width:820px){.row2{grid-template-columns:1fr;}#drawer{width:100%;}}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login" class="login-wrap">
  <div class="login-card">
    <h1>Admin · Call Analytics</h1>
    <p>Enter the admin key to view call logs, transcripts and costing.</p>
    <input id="keyInput" type="password" placeholder="Admin key" autocomplete="current-password"/>
    <button class="btn" id="loginBtn">Sign in</button>
    <div class="err" id="loginErr"></div>
  </div>
</div>

<!-- DASHBOARD -->
<div id="dash" class="hidden">
  <div class="top-bar">
    <span class="brand">Admin · Call Analytics</span>
    <div class="top-actions">
      <span class="status"><span class="dot"></span><span id="updated">—</span></span>
      <button class="btn ghost" id="refreshBtn">Refresh costs</button>
      <button class="btn ghost" id="logoutBtn">Log out</button>
    </div>
  </div>

  <div class="container">
    <div class="section-title">Project costing</div>
    <div class="stats" id="stats"></div>

    <div class="section-title">Spend trend</div>
    <div class="row2">
      <div class="panel" id="trend"><h3>Cost by day (Gemini + Twilio) &amp; call volume</h3><div id="trendBody"></div></div>
      <div class="panel"><h3>Breakdown</h3><div id="breakdown"></div></div>
    </div>

    <div class="section-title">Call logs</div>
    <div class="filters">
      <input type="date" id="fromDate" title="From date"/>
      <input type="date" id="toDate" title="To date"/>
      <select id="sourceFilter"><option value="">All sources</option><option value="twilio">Phone (Twilio)</option><option value="browser">Browser</option></select>
      <input class="grow" id="search" placeholder="Search caller / call SID…"/>
      <button class="btn ghost" id="csvBtn">Export CSV</button>
    </div>
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th data-k="started_at">Time</th>
          <th data-k="caller">Caller</th>
          <th data-k="source">Source</th>
          <th data-k="duration_seconds" class="num">Duration</th>
          <th data-k="language">Lang</th>
          <th data-k="status">Status</th>
          <th data-k="booking_created">Booking</th>
          <th data-k="gemini_cost_usd" class="num">Gemini $</th>
          <th data-k="twilio" class="num">Twilio $</th>
          <th data-k="total_cost_usd" class="num">Total $</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div id="count" class="section-title" style="margin-top:10px;"></div>
  </div>
</div>

<div id="toasts"></div>

<script>
const $=(id)=>document.getElementById(id);
const KEY=()=>localStorage.getItem('admin_key')||'';
const state={summary:null,calls:[],sort:{k:'started_at',dir:'desc'}};

/* ---------- fetch helper ---------- */
async function api(path,opts={}){
  const res=await fetch(path,{...opts,headers:{...(opts.headers||{}),'X-Admin-Key':KEY()}});
  if(res.status===401){logout();throw new Error('unauthorized');}
  if(!res.ok)throw new Error('HTTP '+res.status);
  return res;
}

/* ---------- auth ---------- */
function showDash(on){$('login').classList.toggle('hidden',on);$('dash').classList.toggle('hidden',!on);}
async function login(){
  const k=$('keyInput').value.trim();
  if(!k){$('loginErr').textContent='Enter a key';return;}
  localStorage.setItem('admin_key',k);
  try{await loadAll();showDash(true);$('loginErr').textContent='';}
  catch(e){localStorage.removeItem('admin_key');$('loginErr').textContent='Invalid key';}
}
function logout(){localStorage.removeItem('admin_key');showDash(false);}

/* ---------- formatting ---------- */
const fmtUSD=(n)=>(n==null||isNaN(n))?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:4}).format(n);
const fmtUSD2=(n)=>(n==null||isNaN(n))?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
const fmtNum=(n)=>(n==null||isNaN(n))?'—':new Intl.NumberFormat('en-US').format(n);
const fmtPct=(x)=>(x==null||isNaN(x))?'—':(x*100).toFixed(1)+'%';
function fmtDur(s){s=s||0;const m=Math.floor(s/60),r=Math.round(s%60);return m+':'+String(r).padStart(2,'0');}
function fmtDT(iso){if(!iso)return '—';const d=new Date(iso);return d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

/* ---------- loaders ---------- */
async function loadAll(){
  const [sum,calls]=await Promise.all([
    api('/api/admin/summary').then(r=>r.json()),
    api('/api/admin/calls'+filterQS()).then(r=>r.json()),
  ]);
  state.summary=sum;state.calls=calls.items||[];
  renderStats();renderTrend();renderBreakdown();renderRows();
  $('updated').textContent='Updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function filterQS(){
  const p=new URLSearchParams();
  if($('fromDate').value)p.set('from',$('fromDate').value);
  if($('toDate').value)p.set('to',$('toDate').value);
  if($('sourceFilter').value)p.set('source',$('sourceFilter').value);
  if($('search').value.trim())p.set('q',$('search').value.trim());
  const s=p.toString();return s?('?'+s):'';
}
async function fetchCalls(){
  try{const r=await api('/api/admin/calls'+filterQS()).then(r=>r.json());state.calls=r.items||[];renderRows();}
  catch(e){if(e.message!=='unauthorized')toast('Failed to load calls','error');}
}

/* ---------- render: stats ---------- */
function renderStats(){
  const s=state.summary||{};
  const cards=[
    {label:'Total calls',value:fmtNum(s.total_calls),sub:(s.by_source?Object.entries(s.by_source).map(([k,v])=>k+': '+v).join(' · '):'')},
    {label:'Total minutes',value:(s.total_minutes!=null?s.total_minutes.toFixed(1):'—')},
    {label:'AI (Gemini) cost',value:fmtUSD(s.gemini_cost_usd),cls:'hi',sub:'real token usage'},
    {label:'Twilio cost',value:fmtUSD(s.twilio_cost_usd),cls:'cy'},
    {label:'Total real cost',value:fmtUSD(s.total_cost_usd),cls:'cy'},
    {label:'Avg cost / call',value:fmtUSD(s.avg_cost_per_call)},
    {label:'This month',value:fmtUSD2((s.this_month||{}).cost_usd),sub:'proj '+fmtUSD2(s.projected_month_cost)},
    {label:'Booking conversion',value:fmtPct(s.booking_conversion_rate),cls:'gr',sub:(s.bookings||0)+' bookings'},
  ];
  $('stats').innerHTML=cards.map(c=>
    '<div class="stat '+(c.cls||'')+'"><div class="label">'+c.label+'</div><div class="value">'+c.value+'</div>'+
    (c.sub?'<div class="sub">'+esc(c.sub)+'</div>':'')+'</div>').join('');
}

/* ---------- render: trend (inline SVG) ---------- */
function renderTrend(){
  const days=(state.summary&&state.summary.by_day)||[];
  const box=$('trendBody');
  if(!days.length){box.innerHTML='<div class="empty">No spend data yet</div>';return;}
  const W=640,H=220,pad={l:46,r:38,t:14,b:26};
  const iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
  const maxCost=Math.max(...days.map(d=>d.cost_usd),0.0001);
  const maxCalls=Math.max(...days.map(d=>d.calls),1);
  const n=days.length,bw=Math.max(4,Math.min(46,iw/n*0.6));
  const x=(i)=>pad.l+(iw/n)*(i+0.5);
  const yC=(v)=>pad.t+ih-(v/maxCost)*ih;
  const yN=(v)=>pad.t+ih-(v/maxCalls)*ih;
  let bars='',line='',dots='',xlabels='';
  const step=Math.ceil(n/8);
  days.forEach((d,i)=>{
    const h=(d.cost_usd/maxCost)*ih;
    bars+='<rect x="'+(x(i)-bw/2)+'" y="'+(pad.t+ih-h)+'" width="'+bw+'" height="'+h+'" rx="3" fill="url(#g)"><title>'+d.date+' · '+fmtUSD(d.cost_usd)+' · '+d.calls+' calls</title></rect>';
    line+=(i?' L':'M')+x(i)+' '+yN(d.calls);
    dots+='<circle cx="'+x(i)+'" cy="'+yN(d.calls)+'" r="3" fill="#7c3aed"/>';
    if(i%step===0)xlabels+='<text x="'+x(i)+'" y="'+(H-8)+'" fill="#64748b" font-size="9" text-anchor="middle">'+d.date.slice(5)+'</text>';
  });
  // y axis (cost) ticks
  let yticks='';
  for(let t=0;t<=2;t++){const v=maxCost*t/2,yy=yC(v);
    yticks+='<line x1="'+pad.l+'" y1="'+yy+'" x2="'+(W-pad.r)+'" y2="'+yy+'" stroke="rgba(255,255,255,0.05)"/>'+
            '<text x="'+(pad.l-6)+'" y="'+(yy+3)+'" fill="#64748b" font-size="9" text-anchor="end">'+fmtUSD2(v)+'</text>';}
  box.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#00d4ff" stop-opacity="0.9"/><stop offset="1" stop-color="#00d4ff" stop-opacity="0.25"/></linearGradient></defs>'+
    yticks+bars+'<path d="'+line+'" fill="none" stroke="#7c3aed" stroke-width="2"/>'+dots+xlabels+'</svg>'+
    '<div style="display:flex;gap:16px;font-size:0.66rem;color:#94a3b8;margin-top:6px;"><span style="color:#00d4ff;">■ cost / day</span><span style="color:#7c3aed;">● calls / day</span></div>';
}

/* ---------- render: breakdown ---------- */
function renderBreakdown(){
  const s=state.summary||{};
  const g=s.gemini_cost_usd||0,t=s.twilio_cost_usd||0,tot=(g+t)||1;
  const langs=s.by_language||{};
  let html='';
  html+=brkRow('Gemini (AI)',fmtUSD(g),g/tot,'#7c3aed');
  html+=brkRow('Twilio (telephony)',fmtUSD(t),t/tot,'#00d4ff');
  html+='<div class="sub-h" style="margin:14px 0 6px;">By language</div>';
  const le=Object.entries(langs).sort((a,b)=>b[1]-a[1]);
  if(!le.length)html+='<div class="kv"><span>—</span><span></span></div>';
  le.forEach(([k,v])=>html+='<div class="kv"><span>'+esc(k)+'</span><span>'+v+'</span></div>');
  if(s.pending_twilio_price)html+='<div class="kv" style="margin-top:10px;color:#f59e0b;"><span>Pending Twilio price</span><span>'+s.pending_twilio_price+'</span></div>';
  $('breakdown').innerHTML=html;
}
function brkRow(label,val,frac,color){
  return '<div class="brk-row"><span>'+label+'</span><span style="font-family:var(--mono)">'+val+'</span></div>'+
    '<div class="brk-bar" style="width:'+Math.max(2,frac*100).toFixed(1)+'%;background:'+color+'"></div>';
}

/* ---------- render: table ---------- */
function renderRows(){
  const tb=$('rows');
  let rows=[...state.calls];
  const {k,dir}=state.sort,mul=dir==='asc'?1:-1;
  rows.sort((a,b)=>{
    let av=a[k],bv=b[k];
    if(k==='twilio'){av=(a.twilio||{}).price_usd;bv=(b.twilio||{}).price_usd;}
    if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;
    if(typeof av==='string')return av.localeCompare(bv)*mul;
    return (av-bv)*mul;
  });
  $('count').textContent=rows.length+' call'+(rows.length===1?'':'s');
  if(!rows.length){tb.innerHTML='<tr><td colspan="10"><div class="empty">No calls match your filters</div></td></tr>';return;}
  tb.innerHTML=rows.map(c=>{
    const tw=(c.twilio||{}).price_usd;
    const est=c.cost_estimated?'<span class="est" title="estimated">~</span>':'';
    return '<tr onclick="openDrawer(\\''+c.id+'\\')">'+
      '<td>'+fmtDT(c.started_at)+'</td>'+
      '<td>'+esc(c.caller||(c.source==='browser'?'Web visitor':'—'))+'</td>'+
      '<td><span class="pill src">'+esc(c.source||'')+'</span></td>'+
      '<td class="num">'+fmtDur(c.duration_seconds)+'</td>'+
      '<td>'+esc(c.language||'—')+'</td>'+
      '<td><span class="pill '+esc(c.status||'')+'">'+esc(c.status||'')+'</span></td>'+
      '<td>'+(c.booking_created?'<span class="tick">✓</span>':'<span class="dash">—</span>')+'</td>'+
      '<td class="num">'+fmtUSD(c.gemini_cost_usd)+'</td>'+
      '<td class="num">'+(tw==null?'<span class="dash">—</span>':fmtUSD(tw))+'</td>'+
      '<td class="num">'+fmtUSD(c.total_cost_usd)+est+'</td>'+
    '</tr>';
  }).join('');
}
function sortBy(k){
  if(state.sort.k===k)state.sort.dir=state.sort.dir==='asc'?'desc':'asc';
  else state.sort={k,dir:'desc'};
  renderRows();
}

/* ---------- drawer ---------- */
async function openDrawer(id){
  document.body.insertAdjacentHTML('beforeend','<div id="backdrop" onclick="closeDrawer()"></div><div id="drawer"><div class="dbody"><div class="skel" style="height:120px;margin-bottom:12px;"></div><div class="skel" style="height:240px;"></div></div></div>');
  try{
    const c=await api('/api/admin/calls/'+id).then(r=>r.json());
    renderDrawer(c);
  }catch(e){closeDrawer();if(e.message!=='unauthorized')toast('Failed to load call','error');}
}
function closeDrawer(){const d=$('drawer'),b=$('backdrop');if(d)d.remove();if(b)b.remove();}
function renderDrawer(c){
  const cb=c.cost_breakdown||{},gem=cb.gemini||{},tk=gem.tokens||{},tw=cb.twilio||{};
  const tcalls=(c.tool_calls||[]).map(t=>
    '<div class="tool"><b>'+esc(t.name)+'</b><pre>'+esc(JSON.stringify(t.result,null,2)||'').slice(0,600)+'</pre></div>').join('')||'<div class="kv"><span>No tool calls</span><span></span></div>';
  const tr=(c.transcript||[]).map(m=>
    '<div class="msg '+(m.role==='user'?'user':'gemini')+'"><span>'+esc(m.text)+'</span><span class="t">'+esc((m.role||'').toUpperCase())+'</span></div>').join('')||'<div class="empty">No transcript captured</div>';
  $('drawer').innerHTML=
    '<div class="dh"><div><div class="who">'+esc(c.caller||(c.source==='browser'?'Web visitor':c.call_sid||'Call'))+'</div>'+
      '<div class="meta">'+esc(c.source)+' · '+fmtDT(c.started_at)+' · '+fmtDur(c.duration_seconds)+' · '+esc(c.status||'')+'</div></div>'+
      '<button class="x" onclick="closeDrawer()">×</button></div>'+
    '<div class="dbody">'+
      '<div class="cost-grid">'+
        '<div class="cost-card gem"><div class="ct">Gemini (AI) cost</div><div class="big">'+fmtUSD(gem.cost_usd)+'</div>'+
          '<div class="kv"><span>Audio in</span><span>'+fmtNum(tk.audio_in)+'</span></div>'+
          '<div class="kv"><span>Audio out</span><span>'+fmtNum(tk.audio_out)+'</span></div>'+
          '<div class="kv"><span>Text in</span><span>'+fmtNum(tk.text_in)+'</span></div>'+
          '<div class="kv"><span>Text out</span><span>'+fmtNum(tk.text_out)+'</span></div>'+
          '<div class="kv"><span>Thinking</span><span>'+fmtNum(tk.thoughts)+'</span></div>'+
          '<div class="kv"><span>Total tokens</span><span>'+fmtNum(tk.total)+'</span></div>'+
        '</div>'+
        '<div class="cost-card tw"><div class="ct">Twilio cost</div><div class="big">'+(tw.price_usd==null?'—':fmtUSD(tw.price_usd))+'</div>'+
          '<div class="kv"><span>Duration</span><span>'+fmtDur(tw.duration_seconds)+'</span></div>'+
          '<div class="kv"><span>Unit</span><span>'+esc(tw.price_unit||'—')+'</span></div>'+
          '<div class="kv"><span>Estimated</span><span>'+(cb.cost_estimated?'yes':'no')+'</span></div>'+
          (c.source==='twilio'?'<button class="btn ghost" style="margin-top:10px;width:100%;" onclick="refreshOne(\\''+c.id+'\\')">Refresh price</button>':'')+
        '</div>'+
      '</div>'+
      '<div class="kv" style="padding:8px 2px;"><span>Total real cost</span><span style="font-weight:700;">'+fmtUSD(cb.total_cost_usd)+'</span></div>'+
      '<div class="sub-h">Tool calls</div>'+tcalls+
      '<div class="sub-h">Transcript</div>'+tr+
      '<button class="btn ghost" style="margin-top:16px;width:100%;" onclick="exportCall(\\''+c.id+'\\')">Export call JSON</button>'+
    '</div>';
}
async function refreshOne(id){
  toast('Refreshing price…','info');
  try{const r=await api('/api/admin/calls/'+id+'/refresh',{method:'POST'}).then(r=>r.json());
    toast(r.updated?'Price updated':'Price not available yet',r.updated?'success':'info');
    await loadAll();if($('drawer'))openDrawer(id);
  }catch(e){if(e.message!=='unauthorized')toast('Refresh failed','error');}
}
async function exportCall(id){
  try{const blob=await api('/api/admin/calls/'+id+'/export').then(r=>r.blob());
    dl(blob,'call_'+id+'.json');
  }catch(e){if(e.message!=='unauthorized')toast('Export failed','error');}
}

/* ---------- exports ---------- */
function dl(blob,name){const u=URL.createObjectURL(blob);const a=document.createElement('a');a.href=u;a.download=name;a.click();URL.revokeObjectURL(u);}
function exportCSV(){
  const rows=state.calls;
  const cols=['started_at','call_sid','source','caller','duration_seconds','language','status','booking_created','gemini_cost_usd','twilio_price_usd','total_cost_usd'];
  const lines=[cols.join(',')];
  rows.forEach(c=>{
    const v=[c.started_at,c.call_sid,c.source,c.caller,c.duration_seconds,c.language,c.status,c.booking_created,c.gemini_cost_usd,(c.twilio||{}).price_usd,c.total_cost_usd];
    lines.push(v.map(x=>{x=x==null?'':String(x);return /[",\\n]/.test(x)?'"'+x.replace(/"/g,'""')+'"':x;}).join(','));
  });
  dl(new Blob([lines.join('\\n')],{type:'text/csv'}),'call_logs.csv');
}
async function refreshCosts(){
  toast('Refreshing Twilio prices…','info');
  try{const r=await api('/api/admin/refresh-costs',{method:'POST'}).then(r=>r.json());
    toast('Updated '+r.updated+' of '+r.checked+' pending','success');await loadAll();
  }catch(e){if(e.message!=='unauthorized')toast('Refresh failed','error');}
}

/* ---------- toast ---------- */
function toast(msg,type){const el=document.createElement('div');el.className='toast '+(type||'info');el.textContent=msg;$('toasts').appendChild(el);setTimeout(()=>el.remove(),4000);}

/* ---------- wire up ---------- */
$('loginBtn').onclick=login;
$('keyInput').addEventListener('keypress',e=>{if(e.key==='Enter')login();});
$('logoutBtn').onclick=logout;
$('refreshBtn').onclick=refreshCosts;
$('csvBtn').onclick=exportCSV;
$('sourceFilter').onchange=fetchCalls;
$('fromDate').onchange=fetchCalls;
$('toDate').onchange=fetchCalls;
let st;$('search').addEventListener('input',()=>{clearTimeout(st);st=setTimeout(fetchCalls,300);});
document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>sortBy(th.dataset.k));
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});

/* ---------- boot ---------- */
(async()=>{
  if(KEY()){try{await loadAll();showDash(true);}catch(e){showDash(false);}}
  else showDash(false);
})();
</script>
</body>
</html>"""


# ============ CLIENT ANALYTICS DASHBOARD (usage & bookings, NO cost) ============

CLIENT_ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Autoverse AI · Call Analytics</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0e17; --card:rgba(17,24,39,0.75); --border:rgba(255,255,255,0.08);
  --cyan:#00d4ff; --purple:#7c3aed; --green:#10b981; --red:#ef4444; --amber:#f59e0b;
  --text:#f1f5f9; --muted:#64748b; --secondary:#94a3b8;
  --mono:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;}
body::before{content:'';position:fixed;inset:0;background-image:
  linear-gradient(rgba(0,212,255,0.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(0,212,255,0.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0;}
.hidden{display:none !important;}
a{color:var(--cyan);}
.login-wrap{position:relative;z-index:1;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
.login-card{background:var(--card);backdrop-filter:blur(14px);border:1px solid var(--border);border-radius:18px;padding:34px 30px;width:100%;max-width:380px;text-align:center;}
.login-card h1{font-size:1.15rem;margin-bottom:6px;}
.login-card p{color:var(--muted);font-size:0.8rem;margin-bottom:20px;}
input,select{font-family:inherit;background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:10px;color:var(--text);padding:10px 12px;font-size:0.85rem;width:100%;outline:none;}
input:focus,select:focus{border-color:var(--cyan);}
.btn{font-family:inherit;cursor:pointer;border:none;border-radius:10px;padding:10px 16px;font-size:0.82rem;font-weight:600;color:#04121a;background:var(--cyan);transition:opacity .15s;}
.btn:hover{opacity:.88;}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--secondary);}
.login-card .btn{width:100%;margin-top:14px;}
.err{color:var(--red);font-size:0.75rem;margin-top:10px;min-height:1em;}
.top-bar{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;
  padding:12px 22px;background:rgba(10,14,23,0.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);}
.brand{font-weight:800;font-size:0.92rem;color:var(--cyan);letter-spacing:.2px;}
.top-actions{display:flex;align-items:center;gap:10px;}
.status{display:flex;align-items:center;gap:6px;font-size:0.72rem;color:var(--secondary);}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2.4s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.5;}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.container{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:22px;}
.section-title{font-size:0.7rem;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin:26px 4px 12px;}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;}
.stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;animation:fadeIn .25s;}
.stat .label{font-size:0.66rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);}
.stat .value{font-size:1.5rem;font-weight:800;margin-top:8px;font-family:var(--mono);}
.stat .sub{font-size:0.68rem;color:var(--secondary);margin-top:4px;}
.stat.gr .value{color:var(--green);}
.stat.cy .value{color:var(--cyan);}
.stat.hi .value{color:var(--purple);}
.row2{display:grid;grid-template-columns:1.4fr 1fr;gap:12px;}
@media(max-width:820px){.row2{grid-template-columns:1fr;}}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;}
.panel h3{font-size:0.8rem;font-weight:600;margin-bottom:14px;color:var(--secondary);}
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:0.72rem;}
.bar-row .k{width:78px;color:var(--secondary);text-transform:capitalize;}
.bar-row .track{flex:1;height:8px;background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;}
.bar-row .fill{height:100%;background:linear-gradient(90deg,var(--cyan),var(--purple));border-radius:6px;}
.bar-row .v{width:38px;text-align:right;font-family:var(--mono);color:var(--text);}
.chart{display:flex;align-items:flex-end;gap:4px;height:150px;padding-top:10px;}
.chart .col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:4px;min-width:0;}
.chart .cbar{width:70%;background:linear-gradient(180deg,var(--cyan),rgba(0,212,255,0.25));border-radius:4px 4px 0 0;min-height:2px;}
.chart .clabel{font-size:0.55rem;color:var(--muted);white-space:nowrap;transform:rotate(-45deg);transform-origin:top left;margin-top:6px;}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;}
.filters input,.filters select{width:auto;}
.filters .grow{flex:1;min-width:160px;}
.table-scroll{overflow-x:auto;border:1px solid var(--border);border-radius:14px;}
table{width:100%;border-collapse:collapse;font-size:0.78rem;}
th,td{padding:10px 12px;text-align:left;white-space:nowrap;}
thead th{background:rgba(255,255,255,0.03);color:var(--muted);font-weight:600;font-size:0.68rem;text-transform:uppercase;letter-spacing:.6px;}
tbody tr{border-top:1px solid var(--border);cursor:pointer;transition:background .12s;}
tbody tr:hover{background:rgba(0,212,255,0.05);}
.num{text-align:right;font-family:var(--mono);}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:0.66rem;font-weight:600;}
.pill.yes{background:rgba(16,185,129,0.15);color:var(--green);}
.pill.no{background:rgba(255,255,255,0.06);color:var(--secondary);}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(560px,94vw);background:#0d131f;border-left:1px solid var(--border);z-index:50;padding:22px;overflow-y:auto;transform:translateX(100%);transition:transform .2s;}
.drawer.open{transform:translateX(0);}
.drawer h2{font-size:1rem;margin-bottom:4px;}
.drawer .close{position:absolute;top:16px;right:18px;cursor:pointer;color:var(--muted);font-size:1.4rem;background:none;border:none;}
.msg{margin:8px 0;padding:9px 12px;border-radius:10px;font-size:0.8rem;line-height:1.4;}
.msg.user{background:rgba(0,212,255,0.08);}
.msg.gemini{background:rgba(124,58,237,0.10);}
.msg .who{font-size:0.6rem;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:3px;}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:40;opacity:0;pointer-events:none;transition:opacity .2s;}
.overlay.on{opacity:1;pointer-events:auto;}
#toasts{position:fixed;bottom:18px;right:18px;z-index:100;display:flex;flex-direction:column;gap:8px;}
.toast{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:0.78rem;animation:fadeIn .2s;}
.toast.error{border-color:var(--red);color:var(--red);}
</style>
</head>
<body>
<div id="login" class="login-wrap">
  <div class="login-card">
    <h1>Autoverse AI · Call Analytics</h1>
    <p>Enter your API key to view call activity and booking performance.</p>
    <input id="keyInput" type="password" placeholder="API key" autocomplete="current-password"/>
    <button class="btn" id="loginBtn">Sign in</button>
    <div class="err" id="loginErr"></div>
  </div>
</div>

<div id="dash" class="hidden">
  <div class="top-bar">
    <span class="brand">Autoverse AI · Call Analytics</span>
    <div class="top-actions">
      <span class="status"><span class="dot"></span><span id="updated">—</span></span>
      <button class="btn ghost" id="logoutBtn">Log out</button>
    </div>
  </div>
  <div class="container">
    <div class="section-title">Overview</div>
    <div class="stats" id="stats"></div>

    <div class="section-title">Activity</div>
    <div class="row2">
      <div class="panel"><h3>Calls per day</h3><div class="chart" id="chart"></div></div>
      <div class="panel"><h3>Breakdown</h3><div id="breakdown"></div></div>
    </div>

    <div class="section-title">Call log</div>
    <div class="filters">
      <input type="date" id="fromDate" title="From date"/>
      <input type="date" id="toDate" title="To date"/>
      <select id="sourceFilter"><option value="">All sources</option><option value="twilio">Phone</option><option value="browser">Browser</option></select>
      <input class="grow" id="search" placeholder="Search caller / language…"/>
      <button class="btn ghost" id="csvBtn">Export CSV</button>
    </div>
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>Time</th><th>Caller</th><th>Source</th>
          <th class="num">Duration</th><th>Lang</th><th>Status</th><th>Booking</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div id="count" class="section-title" style="margin-top:10px;"></div>
  </div>
</div>

<div class="overlay" id="overlay" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <button class="close" onclick="closeDrawer()">&times;</button>
  <div id="drawerBody"></div>
</div>
<div id="toasts"></div>

<script>
const $=(id)=>document.getElementById(id);
const KEY=()=>localStorage.getItem('analytics_key')||'';
const state={summary:null,calls:[]};

async function api(path){
  const res=await fetch(path,{headers:{'Authorization':'Bearer '+KEY()}});
  if(res.status===401){logout();throw new Error('unauthorized');}
  if(!res.ok)throw new Error('HTTP '+res.status);
  return res;
}
function showDash(on){$('login').classList.toggle('hidden',on);$('dash').classList.toggle('hidden',!on);}
async function login(){
  const k=$('keyInput').value.trim();
  if(!k){$('loginErr').textContent='Enter a key';return;}
  localStorage.setItem('analytics_key',k);
  try{await loadAll();showDash(true);$('loginErr').textContent='';}
  catch(e){localStorage.removeItem('analytics_key');$('loginErr').textContent='Invalid key';}
}
function logout(){localStorage.removeItem('analytics_key');showDash(false);}

const fmtNum=(n)=>(n==null||isNaN(n))?'—':new Intl.NumberFormat('en-US').format(n);
const fmtPct=(x)=>(x==null||isNaN(x))?'—':(x*100).toFixed(1)+'%';
function fmtDur(s){s=s||0;const m=Math.floor(s/60),r=Math.round(s%60);return m+':'+String(r).padStart(2,'0');}
function fmtDT(iso){if(!iso)return '—';const d=new Date(iso);return d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
const LANG={hi:'Hindi',gu:'Gujarati',en:'English',mr:'Marathi',te:'Telugu',kn:'Kannada',ta:'Tamil',ml:'Malayalam',bn:'Bengali',pa:'Punjabi',or:'Odia',unknown:'Unrecognised',no_speech:'No speech'};

function filterQS(){
  const p=new URLSearchParams();
  if($('fromDate').value)p.set('from',$('fromDate').value);
  if($('toDate').value)p.set('to',$('toDate').value);
  if($('sourceFilter').value)p.set('source',$('sourceFilter').value);
  if($('search').value.trim())p.set('q',$('search').value.trim());
  const s=p.toString();return s?('?'+s):'';
}
async function loadAll(){
  const [sum,calls]=await Promise.all([
    api('/api/v1/analytics/summary').then(r=>r.json()),
    api('/api/v1/analytics/calls'+filterQS()).then(r=>r.json()),
  ]);
  state.summary=sum;state.calls=calls.items||[];
  renderStats();renderChart();renderBreakdown();renderRows();
  $('updated').textContent='Updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}
async function fetchCalls(){
  try{const r=await api('/api/v1/analytics/calls'+filterQS()).then(r=>r.json());state.calls=r.items||[];renderRows();}
  catch(e){if(e.message!=='unauthorized')toast('Failed to load calls','error');}
}

function renderStats(){
  const s=state.summary||{};
  const avg=s.total_calls?Math.round((s.total_seconds||0)/s.total_calls):0;
  const cards=[
    {label:'Total calls',value:fmtNum(s.total_calls),cls:'cy',sub:(s.by_source?Object.entries(s.by_source).map(([k,v])=>k+': '+v).join(' · '):'')},
    {label:'Total minutes',value:(s.total_minutes!=null?s.total_minutes.toFixed(1):'—')},
    {label:'Avg call length',value:fmtDur(avg)},
    {label:'Bookings',value:fmtNum(s.bookings),cls:'gr'},
    {label:'Booking conversion',value:fmtPct(s.booking_conversion_rate),cls:'gr'},
    {label:'Calls this month',value:fmtNum((s.this_month||{}).calls),cls:'hi'},
  ];
  $('stats').innerHTML=cards.map(c=>
    '<div class="stat '+(c.cls||'')+'"><div class="label">'+c.label+'</div><div class="value">'+c.value+'</div>'+(c.sub?'<div class="sub">'+esc(c.sub)+'</div>':'')+'</div>'
  ).join('');
}
function renderChart(){
  const days=(state.summary||{}).by_day||[];
  if(!days.length){$('chart').innerHTML='<div style="color:var(--muted);font-size:0.75rem;">No data yet.</div>';return;}
  const max=Math.max(...days.map(d=>d.calls),1);
  $('chart').innerHTML=days.slice(-30).map(d=>{
    const h=Math.round((d.calls/max)*130);
    return '<div class="col" title="'+d.date+': '+d.calls+' calls"><div class="cbar" style="height:'+h+'px"></div><div class="clabel">'+d.date.slice(5)+'</div></div>';
  }).join('');
}
function renderBreakdown(){
  const s=state.summary||{};
  const lang=s.by_language||{};const src=s.by_source||{};
  const total=Object.values(lang).reduce((a,b)=>a+b,0)||1;
  let h='<div style="font-size:0.66rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:10px;">By language</div>';
  h+=Object.entries(lang).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
    const pct=Math.round(v/total*100);
    return '<div class="bar-row"><span class="k">'+(LANG[k]||k)+'</span><span class="track"><span class="fill" style="width:'+pct+'%"></span></span><span class="v">'+v+'</span></div>';
  }).join('')||'<div style="color:var(--muted);font-size:0.72rem;">—</div>';
  h+='<div style="font-size:0.66rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin:16px 0 10px;">By source</div>';
  const tsrc=Object.values(src).reduce((a,b)=>a+b,0)||1;
  h+=Object.entries(src).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
    const pct=Math.round(v/tsrc*100);
    return '<div class="bar-row"><span class="k">'+esc(k)+'</span><span class="track"><span class="fill" style="width:'+pct+'%"></span></span><span class="v">'+v+'</span></div>';
  }).join('')||'<div style="color:var(--muted);font-size:0.72rem;">—</div>';
  $('breakdown').innerHTML=h;
}
function renderRows(){
  const rows=state.calls;
  $('count').textContent=rows.length+' call'+(rows.length===1?'':'s');
  $('rows').innerHTML=rows.map(c=>
    '<tr onclick="openCall(\\''+c.id+'\\')">'+
    '<td>'+fmtDT(c.started_at)+'</td>'+
    '<td>'+esc(c.caller||'—')+'</td>'+
    '<td>'+esc(c.source||'—')+'</td>'+
    '<td class="num">'+fmtDur(c.duration_seconds)+'</td>'+
    '<td>'+esc(LANG[c.language]||c.language||'—')+'</td>'+
    '<td>'+esc(c.status||'—')+'</td>'+
    '<td>'+(c.booking_created?'<span class="pill yes">Booked</span>':'<span class="pill no">—</span>')+'</td>'+
    '</tr>'
  ).join('')||'<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:24px;">No calls found.</td></tr>';
}
async function openCall(id){
  try{
    const c=await api('/api/v1/analytics/calls/'+id).then(r=>r.json());
    let h='<h2>'+esc(c.caller||'Call')+'</h2>';
    h+='<div style="color:var(--muted);font-size:0.75rem;margin-bottom:14px;">'+fmtDT(c.started_at)+' · '+fmtDur(c.duration_seconds)+' · '+esc(LANG[c.language]||c.language||'—')+' · '+esc(c.source||'')+'</div>';
    h+='<div style="margin-bottom:14px;">'+(c.booking_created?'<span class="pill yes">Booking confirmed</span>':'<span class="pill no">No booking</span>')+'</div>';
    if((c.tool_calls||[]).length){
      h+='<div class="section-title" style="margin:6px 0 8px;">Actions</div>';
      h+=c.tool_calls.map(t=>'<div class="msg gemini"><div class="who">'+esc(t.name)+'</div>'+esc(JSON.stringify(t.args||{}))+'</div>').join('');
    }
    h+='<div class="section-title" style="margin:16px 0 8px;">Transcript</div>';
    h+=(c.transcript||[]).map(m=>'<div class="msg '+(m.role==='user'?'user':'gemini')+'"><div class="who">'+esc(m.role)+'</div>'+esc(m.text)+'</div>').join('')||'<div style="color:var(--muted);font-size:0.75rem;">No transcript.</div>';
    $('drawerBody').innerHTML=h;
    $('drawer').classList.add('open');$('overlay').classList.add('on');
  }catch(e){if(e.message!=='unauthorized')toast('Failed to load call','error');}
}
function closeDrawer(){$('drawer').classList.remove('open');$('overlay').classList.remove('on');}
function exportCSV(){
  const url='/api/v1/analytics/calls.csv'+filterQS();
  fetch(url,{headers:{'Authorization':'Bearer '+KEY()}}).then(r=>r.blob()).then(b=>{
    const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='call_analytics.csv';a.click();
  }).catch(()=>toast('Export failed','error'));
}
function toast(msg,type){const t=document.createElement('div');t.className='toast'+(type?' '+type:'');t.textContent=msg;$('toasts').appendChild(t);setTimeout(()=>t.remove(),3200);}

$('loginBtn').onclick=login;
$('keyInput').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
$('logoutBtn').onclick=logout;
$('csvBtn').onclick=exportCSV;
$('sourceFilter').onchange=fetchCalls;
$('fromDate').onchange=fetchCalls;
$('toDate').onchange=fetchCalls;
let st;$('search').addEventListener('input',()=>{clearTimeout(st);st=setTimeout(fetchCalls,300);});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});

(async()=>{
  if(KEY()){try{await loadAll();showDash(true);}catch(e){showDash(false);}}
  else showDash(false);
})();
</script>
</body>
</html>"""


# ============ ADMIN DASHBOARD (call logs, transcripts, costing) ============

def require_admin(request: Request):
    """Gate admin endpoints with ANALYTICS_SECRET (header X-Admin-Key or ?key=)."""
    key = request.headers.get("X-Admin-Key") or request.query_params.get("key") or ""
    if not (ANALYTICS_SECRET and secrets.compare_digest(str(key), str(ANALYTICS_SECRET))):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _filters_from_request(request: Request):
    qp = request.query_params
    return {
        "source": qp.get("source") or None,
        "from": qp.get("from") or None,
        "to": qp.get("to") or None,
        "q": qp.get("q") or None,
        "booking": qp.get("booking"),
        "limit": qp.get("limit"),
        "offset": qp.get("offset"),
    }


# ============ CLIENT-FACING ANALYTICS API (cost-free) ============
# The client resells the calls, so NONE of our cost/margin data may be exposed.
# These endpoints are gated by a SEPARATE key (ANALYTICS_API_KEY) and every
# response is built with a whitelist so no cost/token field can ever leak.

def require_client_api(request: Request):
    """Gate /api/v1/analytics/* with ANALYTICS_API_KEY (Bearer / X-API-Key / ?key=)."""
    if not ANALYTICS_API_KEY:
        raise HTTPException(status_code=503, detail="Analytics API not configured")
    auth = request.headers.get("Authorization") or ""
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    key = bearer or request.headers.get("X-API-Key") or request.query_params.get("key") or ""
    if not secrets.compare_digest(str(key), str(ANALYTICS_API_KEY)):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Whitelisted fields that are safe to expose to the client (NO cost/token data).
_PUBLIC_CALL_FIELDS = (
    "id", "started_at", "ended_at", "duration_seconds", "language",
    "status", "source", "caller", "booking_created",
)
_PUBLIC_CALL_DETAIL_FIELDS = ("transcript", "tool_calls")


def _sanitize_transcript(transcript):
    """Neutralize the AI role so the client can't tell which model we use.
    'gemini' -> 'agent'; keep only role/text/ts."""
    out = []
    for m in (transcript or []):
        role = "agent" if m.get("role") == "gemini" else m.get("role")
        out.append({"role": role, "text": m.get("text"), "ts": m.get("ts")})
    return out


def _sanitize_tool_calls(tool_calls):
    """Expose only the action taken, not internal implementation detail."""
    out = []
    for t in (tool_calls or []):
        out.append({"name": t.get("name"), "args": t.get("args"),
                    "result": t.get("result"), "ts": t.get("ts")})
    return out


def public_call(call, include_detail=False):
    """Return a cost-free view of a call (whitelist — never leaks cost/tokens/model)."""
    if not call:
        return None
    out = {k: call.get(k) for k in _PUBLIC_CALL_FIELDS}
    if include_detail:
        out["transcript"] = _sanitize_transcript(call.get("transcript"))
        out["tool_calls"] = _sanitize_tool_calls(call.get("tool_calls"))
    return out


def public_summary(s):
    """Return a cost-free aggregate summary (whitelist)."""
    if not s:
        return {}
    return {
        "total_calls": s.get("total_calls", 0),
        "by_source": s.get("by_source", {}),
        "by_language": s.get("by_language", {}),
        "total_minutes": s.get("total_minutes", 0),
        "total_seconds": s.get("total_seconds", 0),
        "bookings": s.get("bookings", 0),
        "booking_conversion_rate": s.get("booking_conversion_rate", 0),
        # by_day: keep only date + call count (drop cost columns)
        "by_day": [
            {"date": d.get("date"), "calls": d.get("calls", 0)}
            for d in (s.get("by_day") or [])
        ],
        "this_month": {"calls": (s.get("this_month") or {}).get("calls", 0)},
    }


async def _refresh_call_price(call_id):
    """Re-fetch and persist Twilio's real billed price for one call."""
    call = await store.load_call(call_id)
    if not call or call.get("source") != "twilio":
        return False
    sid = call.get("call_sid")
    if not sid or str(sid).startswith("web-"):
        return False
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, pricing.fetch_twilio_price, sid)
    if not info:
        return False
    call["twilio"].update({
        "price_unit": info.get("price_unit"),
        "status": info.get("status"),
        "duration_seconds": info.get("duration_seconds"),
    })
    updated = False
    if info.get("price_usd") is not None:
        call["twilio"]["price_usd"] = info["price_usd"]
        if info.get("duration_seconds"):
            call["duration_seconds"] = info["duration_seconds"]
        total, estimated = pricing.compute_total(call)
        call["total_cost_usd"] = total
        call["cost_estimated"] = estimated
        updated = True
    await store.save_call(call)
    return updated


@app.get("/admin")
async def admin_dashboard():
    """Admin dashboard — call logs, transcripts and real costing."""
    return HTMLResponse(ADMIN_DASHBOARD_HTML)


@app.get("/api/admin/summary")
async def admin_summary(request: Request):
    require_admin(request)
    filters = _filters_from_request(request)
    return JSONResponse(await store.summary(filters))


@app.get("/api/admin/calls")
async def admin_calls(request: Request):
    require_admin(request)
    filters = _filters_from_request(request)
    if filters.get("limit") is None:
        filters["limit"] = 500
    return JSONResponse(await store.list_calls(filters))


@app.get("/api/admin/calls.csv")
async def admin_calls_csv(request: Request):
    require_admin(request)
    filters = _filters_from_request(request)
    filters["limit"] = None
    data = await store.list_calls(filters)
    buf = io.StringIO()
    cols = ["started_at", "call_sid", "source", "caller", "duration_seconds",
            "language", "status", "booking_created", "gemini_cost_usd",
            "twilio_price_usd", "total_cost_usd", "cost_estimated"]
    writer = csv.writer(buf)
    writer.writerow(cols)
    for c in data["items"]:
        writer.writerow([
            c.get("started_at"), c.get("call_sid"), c.get("source"), c.get("caller"),
            c.get("duration_seconds"), c.get("language"), c.get("status"),
            c.get("booking_created"), c.get("gemini_cost_usd"),
            (c.get("twilio") or {}).get("price_usd"), c.get("total_cost_usd"),
            c.get("cost_estimated"),
        ])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=call_logs.csv"})


@app.get("/api/admin/calls/{call_id}")
async def admin_call_detail(call_id: str, request: Request):
    require_admin(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    tw = call.get("twilio") or {}
    call["cost_breakdown"] = {
        "gemini": pricing.gemini_cost_breakdown(call.get("tokens")),
        "twilio": {
            "duration_seconds": tw.get("duration_seconds") or call.get("duration_seconds"),
            "price_usd": tw.get("price_usd"),
            "price_unit": tw.get("price_unit"),
        },
        "total_cost_usd": call.get("total_cost_usd"),
        "cost_estimated": call.get("cost_estimated"),
    }
    return JSONResponse(call)


@app.get("/api/admin/calls/{call_id}/export")
async def admin_call_export(call_id: str, request: Request):
    require_admin(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return Response(
        content=json.dumps(call, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=call_{call_id}.json"},
    )


@app.post("/api/admin/refresh-costs")
async def admin_refresh_costs(request: Request):
    require_admin(request)
    data = await store.list_calls({"source": "twilio", "limit": None})
    pending = [c for c in data["items"] if (c.get("twilio") or {}).get("price_usd") is None]
    updated = 0
    for c in pending:
        try:
            if await _refresh_call_price(c["id"]):
                updated += 1
        except Exception as e:
            logger.warning(f"refresh-costs failed for {c.get('id')}: {e}")
    return {"checked": len(pending), "updated": updated}


@app.post("/api/admin/calls/{call_id}/refresh")
async def admin_call_refresh(call_id: str, request: Request):
    require_admin(request)
    updated = await _refresh_call_price(call_id)
    return {"updated": bool(updated)}


# ============ CLIENT ANALYTICS ROUTES (cost-free, Bearer-gated) ============

@app.get("/analytics")
async def analytics_dashboard():
    """Client-facing analytics dashboard — usage & bookings, no cost data."""
    return HTMLResponse(CLIENT_ANALYTICS_HTML)


@app.get("/api/v1/analytics/summary")
async def v1_analytics_summary(request: Request):
    require_client_api(request)
    filters = _filters_from_request(request)
    return JSONResponse(public_summary(await store.summary(filters)))


@app.get("/api/v1/analytics/calls")
async def v1_analytics_calls(request: Request):
    require_client_api(request)
    filters = _filters_from_request(request)
    if filters.get("limit") is None:
        filters["limit"] = 500
    data = await store.list_calls(filters)
    return JSONResponse({
        "items": [public_call(c) for c in data["items"]],
        "total": data["total"],
    })


@app.get("/api/v1/analytics/calls.csv")
async def v1_analytics_calls_csv(request: Request):
    require_client_api(request)
    filters = _filters_from_request(request)
    filters["limit"] = None
    data = await store.list_calls(filters)
    buf = io.StringIO()
    cols = ["started_at", "source", "caller", "duration_seconds",
            "language", "status", "booking_created"]
    writer = csv.writer(buf)
    writer.writerow(cols)
    for c in data["items"]:
        pc = public_call(c)
        writer.writerow([pc.get(k) for k in cols])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=call_analytics.csv"})


@app.get("/api/v1/analytics/calls/{call_id}")
async def v1_analytics_call_detail(call_id: str, request: Request):
    require_client_api(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return JSONResponse(public_call(call, include_detail=True))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
