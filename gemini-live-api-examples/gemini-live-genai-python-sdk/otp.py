"""
Email-based OTP gate for the browser demo.

Free anti-spam: before a user can start a demo call, they must enter their email,
receive a 6-digit code (sent via Gmail SMTP), and verify it. On success we issue a
short-lived signed token that the /ws endpoint requires.

Env vars (see .env.example):
  SMTP_HOST         default smtp.gmail.com
  SMTP_PORT         default 587
  SMTP_USER         the Gmail address that sends the OTP
  SMTP_PASS         a Gmail App Password (NOT your normal password)
  SMTP_FROM         optional display From (defaults to SMTP_USER)
  OTP_SECRET        secret used to sign verification tokens
  OTP_TTL_SECONDS   how long a code is valid (default 300)
  OTP_TOKEN_TTL     how long a verified token is valid (default 1800)
"""

import hashlib
import hmac
import logging
import os
import random
import re
import smtplib
import time
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER

OTP_SECRET = os.getenv("OTP_SECRET", "change-me-otp-secret")
OTP_TTL_SECONDS = int(os.getenv("OTP_TTL_SECONDS", "300"))     # code valid 5 min
OTP_TOKEN_TTL = int(os.getenv("OTP_TOKEN_TTL", "1800"))        # token valid 30 min

# Rate limits (in-memory; fine for a single-process demo server)
MAX_SENDS_PER_EMAIL_PER_HOUR = 5
MAX_SENDS_PER_IP_PER_HOUR = 15
RESEND_COOLDOWN_SECONDS = 30

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# email -> {"code": str, "expires": float, "last_sent": float, "attempts": int}
_codes = {}
# key -> list[timestamps] for rate limiting
_send_log_email = {}
_send_log_ip = {}


def _now():
    return time.time()


def _prune(log, key, window=3600):
    now = _now()
    log[key] = [t for t in log.get(key, []) if now - t < window]
    return log[key]


def normalize_email(email):
    return (email or "").strip().lower()


def is_valid_email(email):
    return bool(_EMAIL_RE.match(email or ""))


def _rate_ok(email, ip):
    e = _prune(_send_log_email, email)
    i = _prune(_send_log_ip, ip)
    if len(e) >= MAX_SENDS_PER_EMAIL_PER_HOUR:
        return False, "Too many codes requested for this email. Try again later."
    if len(i) >= MAX_SENDS_PER_IP_PER_HOUR:
        return False, "Too many requests from your network. Try again later."
    rec = _codes.get(email)
    if rec and _now() - rec.get("last_sent", 0) < RESEND_COOLDOWN_SECONDS:
        wait = int(RESEND_COOLDOWN_SECONDS - (_now() - rec["last_sent"]))
        return False, f"Please wait {wait}s before requesting another code."
    return True, ""


def _send_email(to_email, code):
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP not configured (set SMTP_USER and SMTP_PASS).")

    body = (
        f"Your Autoverse AI demo verification code is:\n\n"
        f"    {code}\n\n"
        f"This code expires in {OTP_TTL_SECONDS // 60} minutes.\n"
        f"If you did not request this, you can ignore this email.\n\n"
        f"— Autoverse AI"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"{code} is your Autoverse AI verification code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())


def request_code(email, ip):
    """Generate + email a code. Returns (ok: bool, message: str)."""
    email = normalize_email(email)
    if not is_valid_email(email):
        return False, "Please enter a valid email address."

    ok, reason = _rate_ok(email, ip)
    if not ok:
        return False, reason

    code = f"{random.randint(0, 999999):06d}"
    now = _now()
    _codes[email] = {
        "code": code,
        "expires": now + OTP_TTL_SECONDS,
        "last_sent": now,
        "attempts": 0,
    }
    try:
        _send_email(email, code)
    except Exception as e:
        logger.error(f"OTP email send failed for {email}: {e}")
        return False, "Could not send the code right now. Please try again."

    _send_log_email.setdefault(email, []).append(now)
    _send_log_ip.setdefault(ip, []).append(now)
    logger.info(f"OTP sent to {email} from ip={ip}")
    return True, "Code sent. Check your email."


def verify_code(email, code):
    """Verify a code. Returns (ok: bool, token_or_message: str)."""
    email = normalize_email(email)
    rec = _codes.get(email)
    if not rec:
        return False, "No code found. Please request a new one."
    if _now() > rec["expires"]:
        _codes.pop(email, None)
        return False, "Code expired. Please request a new one."
    rec["attempts"] += 1
    if rec["attempts"] > 6:
        _codes.pop(email, None)
        return False, "Too many attempts. Please request a new code."
    if (code or "").strip() != rec["code"]:
        return False, "Incorrect code. Please try again."

    _codes.pop(email, None)
    return True, issue_token(email)


def issue_token(email):
    """Create a signed token: base = email|expiry, signature = HMAC."""
    email = normalize_email(email)
    expiry = int(_now()) + OTP_TOKEN_TTL
    base = f"{email}|{expiry}"
    sig = hmac.new(OTP_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return f"{base}|{sig}"


def verify_token(token):
    """Validate a token issued by issue_token(). Returns True/False."""
    try:
        email, expiry, sig = (token or "").split("|")
    except ValueError:
        return False
    base = f"{email}|{expiry}"
    expected = hmac.new(OTP_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        if int(expiry) < _now():
            return False
    except ValueError:
        return False
    return True
