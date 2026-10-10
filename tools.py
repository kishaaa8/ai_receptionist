"""
tools.py — Google Calendar tools for the AI Receptionist.

Functions:
    get_calendar_service()  → authenticated Calendar API client
    check_slots(date, service) → list of free time slots
    book_appointment(name, phone, service, date, time) → created event details
"""

import os
import json
import base64
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Paths ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
SALON_DATA_PATH = os.path.join(BASE_DIR, "salon.json")

# Calendar API needs read/write access, Gmail API for send-only confirmations
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

# ── Load salon data ──────────────────────────────────────────────────
with open(SALON_DATA_PATH, "r", encoding="utf-8") as f:
    SALON = json.load(f)

# Build a quick lookup: service name (lowered) → service dict
SERVICE_LOOKUP = {s["name"].lower(): s for s in SALON["services"]}

# Spoken digit words → digits (for voice/STT phone numbers)
_DIGIT_WORDS = {
    "zero": "0", "o": "0", "oh": "0",
    "one": "1", "two": "2", "three": "3",
    "four": "4", "for": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "ate": "8",
    "nine": "9", "niner": "9",
}


def normalize_phone(raw: str) -> str:
    """
    Flexible phone parsing: plain digit strings pass through tidied,
    spoken forms ("plus nine one ...", "double seven") become digits.
    """
    raw = raw or ""
    if re.search(r"\d{7,}", re.sub(r"\D", "", raw)):
        # Already digit-based — just tidy (keep leading +, drop spaces/dashes)
        s = raw.strip()
        return ("+" if s.startswith("+") else "") + re.sub(r"\D", "", s)
    tokens = re.findall(r"[a-z]+|\d+", raw.lower())
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "plus" and not out:
            out.append("+")
        elif tok in ("double", "triple") and i + 1 < len(tokens) and tokens[i + 1] in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tokens[i + 1]] * (2 if tok == "double" else 3))
            i += 1
        elif tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok])
        elif tok.isdigit():
            out.append(tok)
        i += 1
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────
#  Auth helper
# ─────────────────────────────────────────────────────────────────────
def _get_credentials():
    """
    Returns valid OAuth credentials for all SCOPES.
    Reuses token.json, refreshes when expired.
    If saved token lacks a scope (e.g. gmail.send added in Phase 4),
    forces a fresh login so the user can grant the new permission.
    """
    creds = None

    # Load existing token
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        # Old token from Phase 2 only has calendar scope → re-login needed
        if creds and creds.scopes and not set(SCOPES).issubset(set(creds.scopes)):
            creds = None

    # If no valid creds, do the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_PATH}!\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials.\n"
                    "See the setup guide for details."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the token for next time
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_calendar_service():
    """
    Returns an authenticated Google Calendar API service object.
    On first run, opens a browser for OAuth consent and saves token.json.
    On subsequent runs, reuses (and refreshes) the saved token.
    """
    return build("calendar", "v3", credentials=_get_credentials())


def get_gmail_service():
    """Returns an authenticated Gmail API service object (send-only)."""
    return build("gmail", "v1", credentials=_get_credentials())


# ─────────────────────────────────────────────────────────────────────
#  Helper: get opening hours for a date
# ─────────────────────────────────────────────────────────────────────
def _get_hours_for_date(date_str: str) -> dict | None:
    """
    Given a date string (YYYY-MM-DD), returns {"open": "HH:MM", "close": "HH:MM"}
    or None if the salon is closed that day.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_name = dt.strftime("%A").lower()  # e.g. "monday"
    hours = SALON["opening_hours"].get(day_name)

    if hours is None or hours == "closed":
        return None
    return hours


def _to_naive_ist(value: str, ist: timezone) -> datetime:
    """
    Parse a Calendar event bound to naive IST datetime for slot comparison.
    Handles: date-only all-day events ("2026-10-10"), RFC3339 with "Z",
    and naive dateTimes (assumed IST already).
    """
    # All-day event: date only → midnight that day (end date is exclusive,
    # so a single-day block naturally covers the whole day).
    if len(value) == 10:  # YYYY-MM-DD
        return datetime.strptime(value, "%Y-%m-%d")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt  # already naive/local → treat as IST
    return dt.astimezone(ist).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────
#  Tool 1: check_slots
# ─────────────────────────────────────────────────────────────────────
def check_slots(date: str, service: str, around: str | None = None) -> list[dict]:
    """
    Returns a list of available time slots for the given service on the
    given date.

    Args:
        date:    Date string in YYYY-MM-DD format (e.g. "2026-10-07")
        service: Service name (e.g. "Haircut (Men)")
        around:  Optional preferred time HH:MM (e.g. "16:00"). When given,
                 returns only the 3 free slots nearest to it — for voice
                 mode so the bot offers 1-2 options instead of the full list.

    Returns:
        List of dicts: [{"start": "09:00", "end": "09:30"}, ...]
        Empty list if the salon is closed or no slots are free.
    """
    # ── Validate service ────────────────────────────────────────────
    svc = SERVICE_LOOKUP.get(service.lower())
    if not svc:
        raise ValueError(
            f"Unknown service: '{service}'. "
            f"Available: {', '.join(s['name'] for s in SALON['services'])}"
        )

    duration = timedelta(minutes=svc["duration_minutes"])

    # ── Check if salon is open ──────────────────────────────────────
    hours = _get_hours_for_date(date)
    if hours is None:
        return []  # closed that day

    # Parse opening/closing times into datetimes
    day_dt = datetime.strptime(date, "%Y-%m-%d")
    open_time = day_dt.replace(
        hour=int(hours["open"].split(":")[0]),
        minute=int(hours["open"].split(":")[1]),
    )
    close_time = day_dt.replace(
        hour=int(hours["close"].split(":")[0]),
        minute=int(hours["close"].split(":")[1]),
    )

    # ── Fetch existing events from Google Calendar ──────────────────
    service_api = get_calendar_service()

    # Use IST offset (+05:30) since salon hours are in local time
    time_min = open_time.isoformat() + "+05:30"
    time_max = close_time.isoformat() + "+05:30"

    events_result = service_api.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = events_result.get("items", [])

    # Build list of busy intervals (convert everything to naive IST)
    IST = timezone(timedelta(hours=5, minutes=30))
    busy = []
    for event in events:
        start_raw = event.get("start", {}).get("dateTime", event.get("start", {}).get("date"))
        end_raw = event.get("end", {}).get("dateTime", event.get("end", {}).get("date"))
        if not start_raw or not end_raw:
            continue  # skip malformed events
        try:
            start_dt = _to_naive_ist(start_raw, IST)
            end_dt = _to_naive_ist(end_raw, IST)
        except ValueError:
            continue  # skip unparseable events rather than crashing
        busy.append((start_dt, end_dt))

    # ── Generate candidate slots and filter out busy ones ───────────
    free_slots = []
    slot_start = open_time

    while slot_start + duration <= close_time:
        slot_end = slot_start + duration
        # Check if this slot overlaps with any busy period
        is_free = True
        for busy_start, busy_end in busy:
            if slot_start < busy_end and slot_end > busy_start:
                is_free = False
                break

        if is_free:
            free_slots.append({
                "start": slot_start.strftime("%H:%M"),
                "end": slot_end.strftime("%H:%M"),
            })

        # Move to next slot (30-min increments for flexibility)
        slot_start += timedelta(minutes=30)

    # ── Narrow to nearest slots when a preference is given ──────────
    if around is not None and free_slots:
        try:
            want = datetime.strptime(around, "%H:%M")
            want_min = want.hour * 60 + want.minute

            def _dist(s: dict) -> int:
                h, m = int(s["start"][:2]), int(s["start"][3:])
                return abs((h * 60 + m) - want_min)

            free_slots = sorted(free_slots, key=_dist)[:3]
        except ValueError:
            pass  # bad around format → ignore, return full list

    return free_slots


# ─────────────────────────────────────────────────────────────────────
#  Tool 2: book_appointment
# ─────────────────────────────────────────────────────────────────────
def book_appointment(
    name: str,
    phone: str,
    service: str,
    date: str,
    time: str,
) -> dict:
    """
    Books an appointment by creating a Google Calendar event.

    Args:
        name:    Customer name (e.g. "Rahul Sharma")
        phone:   Customer phone (e.g. "+91-9876543210")
        service: Service name (e.g. "Haircut (Men)")
        date:    Date in YYYY-MM-DD format
        time:    Start time in HH:MM format (e.g. "10:00")

    Returns:
        Dict with booking confirmation details.
    """
    # ── Light input validation (clear errors so the LLM can re-ask) ──
    if not name or not name.strip():
        raise ValueError("Customer name is required.")
    phone = normalize_phone(phone or "")  # handles "plus nine one..." from voice
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError(f"Phone number '{phone}' looks invalid. Ask for a valid phone number.")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Date '{date}' must be YYYY-MM-DD format.")
    try:
        datetime.strptime(time, "%H:%M")
    except ValueError:
        raise ValueError(f"Time '{time}' must be HH:MM format (e.g. '14:00').")
    # ── Validate service ────────────────────────────────────────────
    svc = SERVICE_LOOKUP.get(service.lower())
    if not svc:
        raise ValueError(
            f"Unknown service: '{service}'. "
            f"Available: {', '.join(s['name'] for s in SALON['services'])}"
        )

    duration = timedelta(minutes=svc["duration_minutes"])

    # ── Guard: closed day / outside hours ─────────────────────────
    hours = _get_hours_for_date(date)
    if hours is None:
        day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        raise ValueError(f"Salon is closed on {day_name} ({date}). Suggest the next open day.")

    # ── Build start/end datetimes ───────────────────────────────────
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + duration

    open_dt = datetime.strptime(f"{date} {hours['open']}", "%Y-%m-%d %H:%M")
    close_dt = datetime.strptime(f"{date} {hours['close']}", "%Y-%m-%d %H:%M")
    if start_dt < open_dt or end_dt > close_dt:
        raise ValueError(
            f"Requested time {time} is outside working hours "
            f"({hours['open']}-{hours['close']}) on {date}."
        )

    # ── Guard: double booking (slot must still be free) ───────────
    free = check_slots(date=date, service=svc["name"])
    if not any(s["start"] == time for s in free):
        raise ValueError(
            f"Slot {time} on {date} is no longer available for '{svc['name']}'. "
            f"Please pick another time."
        )

    # ── Create Google Calendar event ────────────────────────────────
    event_body = {
        "summary": f"💇 {svc['name']} — {name}",
        "description": (
            f"Customer: {name}\n"
            f"Phone: {phone}\n"
            f"Service: {svc['name']}\n"
            f"Price: ₹{svc['price']}\n"
            f"Duration: {svc['duration_minutes']} minutes\n\n"
            f"Booked by AI Receptionist"
        ),
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Asia/Kolkata",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
            ],
        },
    }

    service_api = get_calendar_service()
    created_event = service_api.events().insert(
        calendarId="primary",
        body=event_body,
    ).execute()

    return {
        "status": "confirmed",
        "event_id": created_event["id"],
        "event_link": created_event.get("htmlLink"),
        "summary": created_event["summary"],
        "start": start_dt.strftime("%Y-%m-%d %H:%M"),
        "end": end_dt.strftime("%Y-%m-%d %H:%M"),
        "service": svc["name"],
        "price": f"₹{svc['price']}",
        "customer": name,
        "phone": phone,
    }


# ─────────────────────────────────────────────────────────────────────
#  Tool 3: send_confirmation (Phase 4 — Gmail API)
# ─────────────────────────────────────────────────────────────────────
def send_confirmation(
    to_email: str,
    name: str,
    service: str,
    date: str,
    time: str,
) -> dict:
    """
    Sends a booking confirmation email via the Gmail API.

    Args:
        to_email: Customer's email address (e.g. "rahul@example.com")
        name:     Customer name
        service:  Service name (e.g. "Haircut (Men)")
        date:     Date in YYYY-MM-DD format
        time:     Start time in HH:MM format

    Returns:
        Dict with message id and recipient.
    """
    # Booking works without email — this only validates when email IS given,
    # so a missing/invalid email never breaks the booking itself.
    if not re.fullmatch(r"[\w\.-]+@[\w\.-]+\.\w+", (to_email or "").strip()):
        raise ValueError(
            f"Email '{to_email}' looks invalid. Ask the customer for a valid email address."
        )
    to_email = to_email.strip()
    svc = SERVICE_LOOKUP.get(service.lower())
    price_str = f"₹{svc['price']}" if svc else ""
    service_name = svc["name"] if svc else service

    subject = f"Appointment Confirmed — {SALON['salon_name']} on {date} at {time}"
    body = (
        f"Hi {name},\n\n"
        f"Your appointment at {SALON['salon_name']} is confirmed!\n\n"
        f"Service: {service_name} ({price_str})\n"
        f"Date: {date}\n"
        f"Time: {time}\n\n"
        f"Address: {SALON['address']}\n"
        f"Phone: {SALON['phone']}\n\n"
        f"Reply to this email or call {SALON['phone']} to reschedule.\n\n"
        f"Thank you for choosing {SALON['salon_name']}!"
    )

    # Build MIME message and base64url-encode it
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    gmail = get_gmail_service()
    sent = gmail.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    return {
        "status": "sent",
        "message_id": sent.get("id"),
        "to": to_email,
        "subject": subject,
    }
