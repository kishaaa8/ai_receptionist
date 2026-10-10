import os
import sys
import io
import json
import re
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv
from tools import (
    check_slots, book_appointment, send_confirmation,
    cancel_appointment, reschedule_appointment,
    lookup_customer, remember_customer,
)

# ── Auto-email helpers (send_confirmation is NOT exposed to the LLM) ──
EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")

_LAST_BOOKING: dict | None = None       # last confirmed booking args + result
_EMAIL_SENT_EVENT_IDS: set = set()      # event_ids already emailed


def normalize_spoken_input(text: str) -> str:
    """
    Voice/STT often produces 'puru.agar99 at gmail.com', 'john dot doe at gmail dot com',
    'plus 91 ...'. Convert to a form the regexes and the LLM can use.
    Safe for normal typed text too.
    """
    if not text:
        return text
    t = text.strip()
    # "plus 91" at start (phone spoken) -> "+91"
    t = re.sub(r"(?i)^\s*plus\s+(?=\d)", "+", t)
    t = re.sub(r"(?i)\bplus\s+(?=\d)", "+", t)
    # "at the rate" (how Indians say @) must go BEFORE generic "at" handling,
    # else "john at the rate gmail dot com" becomes "john@therategmail.com".
    t = re.sub(r"(?i)\bat\s+the\s+rate\b", "@", t)
    # email spoken forms: " at " -> "@", " dot " -> "."
    # Only apply when the text looks email-ish to avoid corrupting normal sentences.
    if re.search(r"(?i)\b(at|dot)\b", t) and re.search(r"(?i)gmail|yahoo|hotmail|outlook|email|mail\.com|@|\bdot\b", t):
        t = re.sub(r"(?i)\s*\bat\b\s*", "@", t)
        t = re.sub(r"(?i)\s*\bdot\b\s*", ".", t)
        # remove spaces around @ and dots inside email-like token
        # e.g. "puru.agar99 @ gmail.com" -> "puru.agar99@gmail.com"
        t = re.sub(r"\s*@\s*", "@", t)
        # collapse spaces inside something that now contains @
        def _fix_email_spacing(m):
            return m.group(0).replace(" ", "")
        t = re.sub(r"[\w\.\-]+@[\w\.\- ]+\.\w+", _fix_email_spacing, t)
    return t


def extract_email(text: str) -> str | None:
    m = EMAIL_RE.search(normalize_spoken_input(text or ""))
    return m.group(0) if m else None


def find_email_in_conversation(conversation: list[dict]) -> str | None:
    """Return the most recent email typed by the user, or None."""
    for msg in reversed(conversation):
        if msg.get("role") == "user":
            email = extract_email(msg.get("content") or "")
            if email:
                return email
    return None


def auto_send_email_for_booking(booking_args: dict, booking_result: dict, conversation: list[dict]):
    """
    Called in Python right after a successful book_appointment.
    Looks for the customer's email in chat history and sends confirmation.
    Appends a system note so the LLM can tell the user what happened.
    """
    global _LAST_BOOKING
    _LAST_BOOKING = {
        **booking_args,
        "event_id": booking_result.get("event_id"),
        "event_link": booking_result.get("event_link"),
    }

    event_id = booking_result.get("event_id")
    if event_id in _EMAIL_SENT_EVENT_IDS:
        return  # already sent, don't resend

    email = find_email_in_conversation(conversation)
    if not email:
        conversation.append({
            "role": "system",
            "content": "[System note: booking succeeded but no customer email found in chat history. Ask the customer for their email address so confirmation can be sent.]",
        })
        return

    print(f"  [Auto-email] sending confirmation to {email}...")
    try:
        email_result = send_confirmation(
            to_email=email,
            name=booking_args["name"],
            service=booking_args["service"],
            date=booking_args["date"],
            time=booking_args["time"],
        )
        _EMAIL_SENT_EVENT_IDS.add(event_id)
        try:
            remember_customer(booking_args.get("phone", ""), email=email)
        except Exception:
            pass
        conversation.append({
            "role": "system",
            "content": f"[System note: confirmation email auto-sent to {email}: {json.dumps(email_result)}. Tell the customer the booking is confirmed and the email was sent.]",
        })
    except Exception as e:
        conversation.append({
            "role": "system",
            "content": f"[System note: auto-email to {email} failed: {e}. Tell the customer the booking is confirmed but the email failed, and ask them to verify their email.]",
        })


def try_send_pending_email(user_input: str, conversation: list[dict]):
    """
    Covers the case where booking succeeded earlier but email came later
    (e.g. user says 'my email is x@y.com' on the next turn).
    Sends it directly without involving the LLM's tool calling.
    """
    global _LAST_BOOKING
    if not _LAST_BOOKING:
        return
    if _LAST_BOOKING.get("event_id") in _EMAIL_SENT_EVENT_IDS:
        return
    email = extract_email(user_input)
    if not email:
        return
    print(f"  [Auto-email] sending pending confirmation to {email}...")
    try:
        email_result = send_confirmation(
            to_email=email,
            name=_LAST_BOOKING["name"],
            service=_LAST_BOOKING["service"],
            date=_LAST_BOOKING["date"],
            time=_LAST_BOOKING["time"],
        )
        _EMAIL_SENT_EVENT_IDS.add(_LAST_BOOKING["event_id"])
        try:
            remember_customer(_LAST_BOOKING.get("phone", ""), email=email)
        except Exception:
            pass
        conversation.append({
            "role": "system",
            "content": f"[System note: pending confirmation email auto-sent to {email}: {json.dumps(email_result)}. Confirm to the customer.]",
        })
    except Exception as e:
        conversation.append({
            "role": "system",
            "content": f"[System note: pending auto-email to {email} failed: {e}.]",
        })

# ── Fix Windows console encoding ────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Load environment variables ──────────────────────────────────────
load_dotenv()

# ── Load salon data ─────────────────────────────────────────────────
SALON_DATA_PATH = os.path.join(os.path.dirname(__file__), "salon.json")

with open(SALON_DATA_PATH, "r", encoding="utf-8") as f:
    salon_data = json.load(f)

# ── Service names for the prompt ────────────────────────────────────
service_list = "\n".join(
    f"  - {s['name']} (Rs.{s['price']}, ~{s['duration_minutes']} min)"
    for s in salon_data["services"]
)
# Single source of truth for tool descriptions (no hard-coded drift)
SERVICE_NAMES = ", ".join(s["name"] for s in salon_data["services"])

# ── Build system prompt ─────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are the friendly AI receptionist for {salon_data['salon_name']}.
Today's date is {datetime.now().strftime("%Y-%m-%d")} ({datetime.now().strftime("%A")}).

Your job:
1. Answer questions about the salon (services, prices, hours) using the info below.
2. Help customers book appointments using the tools provided.

### Salon info
- Phone: {salon_data['phone']}
- Address: {salon_data['address']}
- Opening hours:
{json.dumps(salon_data['opening_hours'], indent=2)}

### Services
{service_list}

### Booking flow
When a customer wants to book an appointment, follow these steps:
1. Ask what SERVICE they want (if not clear from the conversation).
2. Ask what DATE they prefer (if not specified). Convert relative dates like "tomorrow" or "next Monday" to YYYY-MM-DD format using today's date.
3. Call check_slots to find available times and show the customer a few options. If they hint at a time (e.g. evening, 4 PM), pass it as around to get just the nearest slots.
4. Let the customer pick a time slot.
5. Ask for their NAME, PHONE NUMBER, and EMAIL (if not already provided). You need all three before booking.
6. Call book_appointment with name, phone, service, date, time ONLY
   (it takes no email parameter — never add one, or the call fails).
   Do NOT call any email tool — the system sends the confirmation email
   automatically in Python right after a successful booking.
7. After booking, confirm with a friendly summary. If the system note says
   the email was sent, mention it. If it says no email was found, ask for
   the customer's email address.

### Cancelling / rescheduling
When the customer wants to cancel or move a booking:
1. Ask for their PHONE number (plus DATE, and TIME if they may have several bookings).
2. Call cancel_appointment or reschedule_appointment.
3. If it returns need_choice with matches, read the options out and ask which one.
4. Confirm the result with a friendly summary.

### Returning customers
If the customer gives a phone number any time, you may call lookup_customer.
Greet returning customers by name and reuse their saved name/email instead of asking again.

### Guidelines
- Use Rs. or INR for prices.
- Use friendly time language ("about 30 minutes").
- Format hours nicely ("Monday-Thursday: 9 AM - 8 PM").
- Keep answers concise (2-3 sentences) unless listing services.
- If the salon is closed on the requested day, suggest the next open day.
- If you don't know something, suggest calling {salon_data['phone']}.
- IMPORTANT: When calling tools, service names must exactly match one of the services listed above.
"""

# ── Tool definitions (OpenAI function calling format) ────────────────
# NOTE: send_confirmation is intentionally NOT exposed to the LLM.
# gpt-oss-20b appends "<|channel|>commentary" to the second chained tool
# name, which Groq rejects with 400 tool_use_failed. Email is auto-sent
# in Python instead (see auto_send_email_for_booking).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_slots",
            "description": "Check available appointment slots for a given service on a specific date. Returns a list of free time slots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "The date to check, in YYYY-MM-DD format (e.g. '2026-10-09')"
                    },
                    "service": {
                        "type": "string",
                        "description": f"The service name, must exactly match one of: {SERVICE_NAMES}"
                    },
                    "around": {
                        "type": "string",
                        "description": "Optional preferred time HH:MM (e.g. '16:00'). Pass it when the customer hints at a time — returns only the 3 nearest free slots."
                    }
                },
                "required": ["date", "service"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book an appointment by creating a Google Calendar event. Only call this after confirming all details with the customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's full name"
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number"
                    },
                    "service": {
                        "type": "string",
                        "description": "The service name, must exactly match one of the salon services"
                    },
                    "date": {
                        "type": "string",
                        "description": "Appointment date in YYYY-MM-DD format"
                    },
                    "time": {
                        "type": "string",
                        "description": "Appointment start time in HH:MM format (e.g. '14:00')"
                    }
                },
                "required": ["name", "phone", "service", "date", "time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel a booking by deleting its calendar event. Ask for phone first (+ date/time if ambiguous).",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number (any format)"
                    },
                    "date": {
                        "type": "string",
                        "description": "Booking date YYYY-MM-DD (to narrow down)"
                    },
                    "time": {
                        "type": "string",
                        "description": "Booking start time HH:MM (to narrow down further)"
                    }
                },
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Move a booking to a new date/time. Ask for phone, current date, and the new date/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number (any format)"
                    },
                    "date": {
                        "type": "string",
                        "description": "Current booking date YYYY-MM-DD"
                    },
                    "new_date": {
                        "type": "string",
                        "description": "New date YYYY-MM-DD"
                    },
                    "new_time": {
                        "type": "string",
                        "description": "New start time HH:MM"
                    },
                    "time": {
                        "type": "string",
                        "description": "Current start time HH:MM (to disambiguate multiple bookings)"
                    }
                },
                "required": ["phone", "date", "new_date", "new_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_customer",
            "description": "Look up a returning customer by phone. Returns saved name/email if known.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number (any format)"
                    }
                },
                "required": ["phone"]
            }
        }
    },
]

# ── Map tool names to actual functions ───────────────────────────────
TOOL_FUNCTIONS = {
    "check_slots": check_slots,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
    "reschedule_appointment": reschedule_appointment,
    "lookup_customer": lookup_customer,
}

# ── Initialise Groq client ──────────────────────────────────────────
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
MODEL = "openai/gpt-oss-20b"


# ── Tool-capable calls need room for full JSON args ─────────────────
# Voice mode previously used max_tokens=150, which truncates the model's
# tool-call JSON → Groq replies 400 "Failed to parse tool call arguments".
# Always give tool calls >=1024 tokens; shorten only the *spoken* text after.
MIN_TOOL_TOKENS = 1024


def chat_create(conversation, max_tokens: int = 512):
    """Single place for chat calls: enforces enough tokens for tool-call JSON."""
    return client.chat.completions.create(
        model=MODEL,
        messages=conversation,
        tools=TOOLS,
        tool_choice="auto",
        temperature=0.2,
        max_tokens=max(MIN_TOOL_TOKENS, max_tokens),
    )


def chat_create_text_only(conversation, max_tokens: int = 512):
    """Fallback without tools — used after a 400 tool_use_failed retry."""
    return client.chat.completions.create(
        model=MODEL,
        messages=conversation,
        temperature=0.2,
        max_tokens=max_tokens,
    )


def sanitize_tool_calls(message):
    """Return OpenAI-format tool_calls with channel tokens stripped + valid JSON."""
    clean = []
    for tc in message.tool_calls or []:
        raw_name = (tc.function.name or "").split("<|")[0].strip()
        raw_args = tc.function.arguments or "{}"
        try:
            parsed = json.loads(raw_args)
            raw_args = json.dumps(parsed, ensure_ascii=False)
        except Exception:
            # Model produced broken JSON (often truncated). Mark so caller
            # returns an error result instead of crashing, letting the LLM re-ask.
            raw_args = None
        clean.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": raw_name, "arguments": raw_args},
        })
    return clean


# ── Execute tool calls and get follow-up response ───────────────────
def process_tool_calls(response, conversation, max_tokens: int = 512):
    """
    When the LLM responds with tool_calls, execute each tool,
    send results back, and get the LLM's follow-up response.
    Email is auto-sent in Python after book_appointment (not via LLM).
    """
    message = response.choices[0].message
    clean_calls = sanitize_tool_calls(message)

    # Add the assistant's message (with tool_calls) to conversation.
    # Store only sanitized calls; drop ones with unparseable args so the
    # history we send back to Groq is always valid JSON.
    valid_calls = [c for c in clean_calls if c["function"]["arguments"] is not None]
    broken = len(valid_calls) != len(clean_calls)
    conversation.append({
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": valid_calls if valid_calls else None,
    })
    # Remove the key entirely if there are no valid calls (text-only turn).
    if not valid_calls:
        del conversation[-1]["tool_calls"]

    if broken:
        conversation.append({
            "role": "system",
            "content": "[System note: your last tool call had malformed JSON arguments and was dropped. Re-ask the customer for the missing details and retry with valid JSON.]",
        })

    # Execute each valid tool call
    for call in valid_calls:
        tc_id = call["id"]
        func_name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"])
        except Exception as e:
            conversation.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps({"error": f"Invalid tool arguments JSON: {e}. Please retry with valid JSON."}, ensure_ascii=False),
            })
            continue

        print(f"  [Tool] {func_name}({json.dumps(args, ensure_ascii=False)})")

        if func_name not in TOOL_FUNCTIONS:
            tool_result = json.dumps({"error": f"Unknown tool '{func_name}'. Available: check_slots, book_appointment, cancel_appointment, reschedule_appointment, lookup_customer."}, ensure_ascii=False)
            conversation.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result,
            })
            continue

        try:
            # Guard: keep only known params so stray keys (e.g. email) can't crash the call
            import inspect as _inspect
            sig = _inspect.signature(TOOL_FUNCTIONS[func_name])
            args = {k: v for k, v in args.items() if k in sig.parameters}
            result = TOOL_FUNCTIONS[func_name](**args)
            tool_result = json.dumps(result, ensure_ascii=False)
        except TypeError as e:
            result = {"error": f"Bad arguments for {func_name}: {e}"}
            tool_result = json.dumps(result, ensure_ascii=False)
        except Exception as e:
            result = {"error": str(e)}
            tool_result = json.dumps(result, ensure_ascii=False)

        # Add tool result to conversation
        conversation.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": tool_result,
        })

        # ── Auto-email: right after a successful booking ──
        if func_name == "book_appointment" and isinstance(result, dict) and result.get("status") == "confirmed":
            auto_send_email_for_booking(args, result, conversation)

    # Get the LLM's follow-up response
    follow_up = chat_create(conversation, max_tokens=max_tokens)

    # If the LLM wants to call more tools, recurse
    if follow_up.choices[0].message.tool_calls:
        return process_tool_calls(follow_up, conversation, max_tokens=max_tokens)

    # Otherwise, return the final text response
    return follow_up.choices[0].message.content


# ── Shared chat turn (text + voice both use this) ───────────────────
def get_bot_reply(user_input: str, conversation: list[dict], max_tokens: int = 512):
    """Append user msg, call LLM (+tools), append assistant reply, return it."""
    # Normalize spoken forms ("puru.agar99 at gmail.com" -> "...@gmail.com",
    # "plus 91..." -> "+91...") before the LLM / email-regex sees them.
    user_input = normalize_spoken_input(user_input)
    snapshot = len(conversation)
    conversation.append({"role": "user", "content": user_input})

    # If user just gave their email after a booking, send it now (no LLM tool needed)
    try_send_pending_email(user_input, conversation)

    try:
        response = chat_create(conversation, max_tokens=max_tokens)
    except Exception as e:
        # 400 tool_use_failed ROG often means the stored history has a bad
        # tool_call (e.g. truncated args from an earlier turn). Roll back to
        # the pre-turn snapshot and retry text-only so the user gets an answer
        # instead of "[Error] 400...".
        if "tool_use_failed" in str(e) or "Failed to parse tool call" in str(e):
            del conversation[snapshot:]  # drop user msg + any notes from this turn
            # Re-add user msg and retry WITHOUT tools for one clean answer.
            conversation.append({"role": "user", "content": user_input})
            print(f"  [Retry] tool-call 400, retrying text-only: {e}")
            response = chat_create_text_only(conversation, max_tokens=max_tokens)
            bot_reply = response.choices[0].message.content
            conversation.append({"role": "assistant", "content": bot_reply})
            return bot_reply
        # Any other error: roll back fully so next turn starts clean.
        del conversation[snapshot:]
        raise

    try:
        if response.choices[0].message.tool_calls:
            bot_reply = process_tool_calls(response, conversation, max_tokens=max_tokens)
        else:
            bot_reply = response.choices[0].message.content
    except Exception:
        # process_tool_calls already appended partial tool history; roll the
        # whole turn back so the next request doesn't resend orphaned tool_calls.
        del conversation[snapshot:]
        # One clean text-only retry so the conversation keeps moving.
        conversation.append({"role": "user", "content": user_input})
        try:
            response2 = chat_create_text_only(conversation, max_tokens=max_tokens)
            bot_reply = response2.choices[0].message.content
        except Exception:
            del conversation[snapshot:]
            raise
        conversation.append({"role": "assistant", "content": bot_reply})
        return bot_reply

    conversation.append({"role": "assistant", "content": bot_reply})
    return bot_reply


# ── Voice mode (Phase 5: laptop mic + Edge TTS) ───────────────────────
VOICE_ADDENDUM = (
    "\n\nVOICE MODE: the user hears your reply spoken aloud. Rules:\n"
    "1. Keep EVERY reply to 1-2 short sentences. Never use markdown, "
    "asterisks, lists, or symbols — plain words only.\n"
    "2. NEVER call check_slots before asking the customer for a rough time "
    "(morning/afternoon/evening, or an hour like 4 PM). "
    "Pass their wish as the around parameter.\n"
    "3. Offer AT MOST 2 time options. If the wished time is free, confirm it "
    "directly. If taken, offer only the 1-2 nearest alternatives.\n"
    "4. Write times in spoken form: 9 AM, 2:30 PM — never 09:00.\n"
    "5. For the phone number ask them to say digits one by one; for email "
    "ask them to spell it slowly. If speech keeps failing, tell them they "
    "can type it instead."
)


def clean_for_speech(text: str) -> str:
    """
    Safety net so the TTS never reads formatting aloud:
    strips markdown, converts 24h times (09:00) to spoken form (9 AM),
    and Rs. amounts to rupees.
    """
    import re as _re

    t = text or ""
    # Markdown: links [text](url) -> text, then strip *, _, `, #, >, -
    t = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = t.replace("**", "").replace("__", "")
    t = _re.sub(r"[*_`#>|~]", "", t)
    t = _re.sub(r"^\s*[-•]\s+", "", t, flags=_re.MULTILINE)
    # Rs.300 / Rs 300 -> 300 rupees
    t = _re.sub(r"Rs\.?\s*(\d+)", r"\1 rupees", t)
    # HH:MM (24h) -> spoken: 09:00 -> 9 AM, 14:30 -> 2:30 PM
    def _spoken(m):
        h, mm = int(m.group(1)), m.group(2)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {suffix}" if mm == "00" else f"{h12}:{mm} {suffix}"
    t = _re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", _spoken, t)
    return _re.sub(r"\s+", " ", t).strip()

def listen(timeout_s: float = 20.0) -> str:
    """
    Record from laptop mic (Enter-to-talk) and transcribe with Groq Whisper.
    Stops on ~1.5s silence or timeout. Returns transcribed text ("" if nothing).
    """
    import io as _io
    import wave
    import numpy as np
    import sounddevice as sd

    sr, chunk = 16000, 8000  # 0.5s chunks
    silence_need = int(1.5 / 0.5)
    silent, frames, heard = 0, [], False

    print("  Listening... (speak now)")
    with sd.InputStream(samplerate=sr, channels=1, dtype="int16") as stream:
        total = 0
        while total < timeout_s:
            block, _ = stream.read(chunk)
            frames.append(block.copy())
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
            if rms > 300:  # speech energy threshold
                heard, silent = True, 0
            elif heard:
                silent += 1
                if silent >= silence_need:
                    break
            total += 0.5

    if not heard:
        return ""

    audio = np.concatenate(frames)
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio.tobytes())
    buf.seek(0)

    result = client.audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=("mic.wav", buf.read(), "audio/wav"),
        language="en",
        response_format="text",
    )
    return result.strip() if isinstance(result, str) else str(result).strip()


def speak(text: str):
    """Speak text with Edge TTS (Indian English voice). Prints + plays audio."""
    import asyncio
    import tempfile
    import os as _os

    print(f"\nBot: {text}\n")
    say = clean_for_speech(text)
    if not say:
        return

    async def _save(mp3_path: str):
        await __import__("edge_tts").Communicate(
            say, voice="en-IN-NeerjaNeural"
        ).save(mp3_path)

    fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    _os.close(fd)
    try:
        asyncio.run(_save(mp3_path))
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(mp3_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.wait(100)
        pygame.mixer.music.unload()
    finally:
        if _os.path.exists(mp3_path):
            _os.remove(mp3_path)


def voice_loop():
    """Simple loop: listen -> think -> speak. Reuses the same brain + tools."""
    try:
        import sounddevice  # noqa: F401
        import edge_tts  # noqa: F401
        import pygame  # noqa: F401
    except ImportError as e:
        print(f"\n[Error] missing voice package: {e}")
        print("Run: pip install edge-tts sounddevice pygame\n")
        return

    print(f"\n{'='*50}")
    print(f"  {salon_data['salon_name']} — Voice mode")
    print(f"  Press Enter + speak, or just type (handy for numbers/email).")
    print(f"  Say or type 'quit' to exit.")
    print(f"{'='*50}\n")

    conversation: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + VOICE_ADDENDUM},
    ]
    speak(f"Welcome to {salon_data['salon_name']}! How can I help you?")

    while True:
        # Typed fallback: speak normally, but TYPE phone/email if mic keeps
        # mishearing them — typed text is used directly, no STT involved.
        typed = input("You (Enter = speak, or type instead): ").strip()
        if typed:
            user_input = typed
        else:
            try:
                user_input = listen()
            except Exception as e:
                print(f"\n[Mic error] {e}\n")
                continue

        if not user_input:
            print("  (didn't catch that — try again)\n")
            continue
        print(f"You: {user_input}")

        if user_input.lower() in ("quit", "exit", "bye"):
            speak("Thank you for visiting! Have a great day!")
            break

        try:
            # NOTE: max_tokens here limits only the final spoken text length.
            # Tool calls internally always get >=1024 tokens (see chat_create)
            # so their JSON is never truncated into a 400 error.
            bot_reply = get_bot_reply(user_input, conversation, max_tokens=300)
        except Exception as e:
            # get_bot_reply already rolled the conversation back to a clean
            # state, so just report — do NOT pop() here (that would corrupt
            # history and cause follow-up 400s).
            print(f"\n[Error] {e}\n")
            continue

        try:
            speak(bot_reply)
        except Exception as e:
            print(f"[TTS error] {e} (reply printed above)")


# ── Phone mode (Phase 6: real calls via Twilio) ───────────────────────
# Twilio flow: incoming call -> POST /voice (greeting + Gather) ->
# caller speaks -> POST /gather (brain reply + Gather) -> ... -> Hangup.
# Same SYSTEM_PROMPT/tools/auto-email as text + voice. No audio streaming:
# Twilio does STT (<Gather>) and TTS (<Say>, Polly.Raveena en-IN).

PHONE_ADDENDUM = (
    "\n\nPHONE CALL: the customer hears your reply via Twilio text-to-speech. Rules:\n"
    "1. Keep EVERY reply to 1-2 short sentences. Plain words only — no markdown, lists, or symbols.\n"
    "2. NEVER call check_slots before asking for a rough time (morning/afternoon/evening, or an hour). "
    "Pass it as around.\n"
    "3. Offer AT MOST 2 time options. Wished time free → confirm directly.\n"
    "4. Write times like 9 AM, 2:30 PM — never 09:00.\n"
    "5. The caller's phone number is in the system note — NEVER ask for it; pass it to book_appointment.\n"
    "6. Ask for email by spelling; if they can't, confirm the booking anyway and read back the details."
)

PHONE_VOICE = "Polly.Raveena"  # Twilio Indian-English voice
PHONE_LANG = "en-IN"
PHONE_QUIT = {"bye", "goodbye", "good bye", "hang up", "that's all", "thats all"}

CALLS: dict[str, list[dict]] = {}  # CallSid → conversation


def _phone_base(request) -> str:
    return str(request.base_url).rstrip("/")


def _twiml_say_gather(text: str, action_url: str) -> str:
    """Speak reply, then listen again. Redirect loops back on silence."""
    import xml.sax.saxutils as _x
    safe = _x.escape(clean_for_speech(text))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="{PHONE_VOICE}" language="{PHONE_LANG}">{safe}</Say>'
        f'<Gather input="speech" language="en-IN" speechTimeout="auto" '
        f'action="{action_url}" method="POST"/>'
        f'<Redirect method="POST">{action_url}</Redirect>'
        "</Response>"
    )


def _twiml_say_hangup(text: str) -> str:
    import xml.sax.saxutils as _x
    safe = _x.escape(clean_for_speech(text))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="{PHONE_VOICE}" language="{PHONE_LANG}">{safe}</Say>'
        "<Hangup/>"
        "</Response>"
    )


def _new_call_conversation(caller: str) -> list[dict]:
    conv: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + PHONE_ADDENDUM},
    ]
    if caller:
        conv.append({
            "role": "system",
            "content": f"[System note: inbound call from {caller}. "
                       f"Use this as the phone for book_appointment; never ask the caller for it.]",
        })
    return conv


def create_phone_app():
    """FastAPI app Twilio calls. Imports are lazy so text/voice modes don't need fastapi."""
    from fastapi import FastAPI, Request
    from fastapi.responses import Response

    app = FastAPI(title="Salon AI Receptionist — phone")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/voice")
    async def voice(request: Request):
        form = await request.form()
        sid = str(form.get("CallSid", "unknown"))
        caller = str(form.get("From", ""))
        CALLS[sid] = _new_call_conversation(caller)
        twiml = _twiml_say_gather(
            f"Welcome to {salon_data['salon_name']}! Are you calling to book an appointment?",
            f"{_phone_base(request)}/gather",
        )
        return Response(content=twiml, media_type="application/xml")

    @app.post("/gather")
    async def gather(request: Request):
        form = await request.form()
        sid = str(form.get("CallSid", "unknown"))
        speech = str(form.get("SpeechResult", "") or "").strip()
        action = f"{_phone_base(request)}/gather"
        conv = CALLS.setdefault(sid, _new_call_conversation(str(form.get("From", ""))))

        if not speech:
            return Response(
                content=_twiml_say_gather("Sorry, I didn't catch that. How can I help?", action),
                media_type="application/xml",
            )
        if speech.lower().strip(" .!,").strip() in PHONE_QUIT:
            CALLS.pop(sid, None)
            return Response(
                content=_twiml_say_hangup("Thank you for calling! Have a great day!"),
                media_type="application/xml",
            )
        try:
            # get_bot_reply rolls back internally on failure, so conv stays clean.
            reply = get_bot_reply(speech, conv, max_tokens=200)
        except Exception as e:
            print(f"  [Phone error] {e}")
            return Response(
                content=_twiml_say_gather("Sorry, something went wrong. Could you say that again?", action),
                media_type="application/xml",
            )
        return Response(
            content=_twiml_say_gather(reply, action),
            media_type="application/xml",
        )

    @app.post("/status")
    async def status(request: Request):
        form = await request.form()
        if str(form.get("CallStatus", "")) in ("completed", "busy", "no-answer", "failed", "canceled"):
            CALLS.pop(str(form.get("CallSid", "")), None)
        return {"ok": True}

    return app


def serve_phone(port: int = 8000):
    import uvicorn
    print(f"  Phone server on http://0.0.0.0:{port} — expose with: ngrok http {port}")
    print(f"  Set Twilio number voice webhook to https://<ngrok-url>/voice (POST).")
    print(f"  Optional status callback: https://<ngrok-url>/status")
    uvicorn.run(create_phone_app(), host="0.0.0.0", port=port)


# ── Chat loop ───────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"  Welcome to {salon_data['salon_name']}!")
    print(f"  AI Receptionist ready. Type 'quit' to exit.")
    print(f"{'='*50}\n")

    conversation: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    while True:
        user_input = input("You: ").strip()

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "bye"):
            print("\nBot: Thank you for visiting! Have a great day!\n")
            break

        try:
            bot_reply = get_bot_reply(user_input, conversation)
            print(f"\nBot: {bot_reply}\n")

        except Exception as e:
            # get_bot_reply already rolled back on failure — no pop() here.
            print(f"\n[Error] {e}\n")


if __name__ == "__main__":
    import sys as _sys
    if "--serve" in _sys.argv:
        _port = 8000
        if "--port" in _sys.argv:
            try:
                _port = int(_sys.argv[_sys.argv.index("--port") + 1])
            except (ValueError, IndexError):
                pass
        serve_phone(_port)
    elif "--voice" in _sys.argv:
        voice_loop()
    else:
        main()
