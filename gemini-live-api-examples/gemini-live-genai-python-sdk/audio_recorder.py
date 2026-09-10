"""
AudioRecorder — captures both sides of a call and writes one mono 16 kHz WAV.

Both call paths already have the two raw streams in Python:
  * customer audio  : PCM16 mono 16 kHz  (browser mic, or Twilio mulaw upsampled)
  * assistant audio : PCM16 mono 24 kHz  (Gemini output)

Chunks are stamped onto a shared 16 kHz timeline using the same rule the
browser uses to schedule playback (start = max(now, previous_end)), so real-time
mic audio and burst-delivered assistant audio both land where they were actually
heard, and silences are preserved. When the customer interrupts, assistant audio
that had not started playing yet is dropped.

Everything is buffered in memory during the call (a 7-minute call is ~13 MB
mixed) and written once, in a worker thread, when the call ends. Any failure is
logged and swallowed so recording can never break a live call. Pure Python:
no numpy, and no audioop (removed in Python 3.13).
"""

import array
import asyncio
import logging
import os
import sys
import time
import wave

logger = logging.getLogger(__name__)

RATE = 16000          # timeline / output sample rate
OUT_RATE = 24000      # Gemini output sample rate
MAX_SECONDS = 3600    # hard cap on recording length (safety)


def _to_array(pcm_bytes):
    a = array.array("h")
    a.frombytes(pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)])
    if sys.byteorder != "little":
        a.byteswap()
    return a


def _resample_24k_to_16k(seg):
    """3:2 decimation with a light average on the dropped sample."""
    n = len(seg) - (len(seg) % 3)
    if n <= 0:
        return array.array("h")
    a = seg[0:n:3]
    b = seg[1:n:3]
    c = seg[2:n:3]
    avg = array.array("h", [(x + y) >> 1 for x, y in zip(b, c)])
    out = array.array("h", bytes(2 * (len(a) + len(avg))))
    out[0::2] = a
    out[1::2] = avg
    return out


class AudioRecorder:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._t0 = time.monotonic()
        self._in_segs = []      # (start_sample, pcm16k bytes)
        self._out_segs = []     # (start_sample, pcm24k bytes)
        self._in_end = 0
        self._out_end = 0
        self._finalized = False

    # ---- capture (called on the event loop; must be cheap) -----------------

    def _now(self):
        return int((time.monotonic() - self._t0) * RATE)

    def add_input(self, data):
        if not self.enabled or self._finalized or not data:
            return
        n = len(data) // 2
        start = max(self._now(), self._in_end)
        if start >= MAX_SECONDS * RATE:
            return
        self._in_segs.append((start, bytes(data)))
        self._in_end = start + n

    def add_output(self, data):
        if not self.enabled or self._finalized or not data:
            return
        n16 = (len(data) // 2) * RATE // OUT_RATE
        start = max(self._now(), self._out_end)
        if start >= MAX_SECONDS * RATE:
            return
        self._out_segs.append((start, bytes(data)))
        self._out_end = start + n16

    def on_interrupt(self):
        """Customer started talking over the assistant: drop unplayed audio."""
        if not self.enabled or self._finalized:
            return
        now = self._now()
        kept = []
        for start, b in self._out_segs:
            if start >= now:
                continue
            n16 = (len(b) // 2) * RATE // OUT_RATE
            if start + n16 > now:
                keep24 = (now - start) * OUT_RATE // RATE
                b = b[: keep24 * 2]
                if not b:
                    continue
            kept.append((start, b))
        self._out_segs = kept
        self._out_end = min(self._out_end, now)

    # ---- finalize -----------------------------------------------------------

    async def finalize(self, path):
        """Mix + write the WAV in a worker thread. Returns metadata dict or None."""
        if self._finalized:
            return None
        self._finalized = True
        if not self.enabled or (not self._in_segs and not self._out_segs):
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._write_sync, path)
        except Exception as e:
            logger.warning(f"AudioRecorder.finalize failed: {e}")
            return None

    def _write_sync(self, path):
        total = min(max(self._in_end, self._out_end), MAX_SECONDS * RATE)
        if total <= 0:
            return None

        buf = array.array("h", bytes(2 * total))

        # Customer track: segments never overlap each other -> straight copy.
        for start, b in self._in_segs:
            seg = _to_array(b)
            end = min(total, start + len(seg))
            if end > start:
                buf[start:end] = seg[: end - start]

        # Assistant track: resample, then mix into any region that already has audio.
        for start, b in self._out_segs:
            seg = _resample_24k_to_16k(_to_array(b))
            end = min(total, start + len(seg))
            if end <= start:
                continue
            seg = seg[: end - start]
            region = buf[start:end]
            if region.count(0) == len(region):
                buf[start:end] = seg
            else:
                buf[start:end] = array.array(
                    "h", [max(-32768, min(32767, x + y)) for x, y in zip(region, seg)]
                )

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(buf.tobytes())
        os.replace(tmp, path)

        size = os.path.getsize(path)
        meta = {
            "path": os.path.basename(path),
            "bytes": size,
            "duration_seconds": round(total / RATE, 2),
            "sample_rate": RATE,
            "channels": 1,
            "format": "wav",
        }
        logger.info(f"Recording written: {path} ({size} bytes, {meta['duration_seconds']}s)")
        return meta
