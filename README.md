# AI Receptionist — Glow & Style Salon

Friendly AI receptionist that answers salon questions, books appointments in Google Calendar, and sends email confirmations. Works over text chat and laptop voice.

> **Status:** text chat + voice mode are live. Phone mode (Twilio) is **parked** — the FastAPI webhook code exists in `main.py --serve` and is tested locally, but no Twilio account is connected, so it is not in use. See "Future work" below.

## Features

- **Text chat** — answers about services, prices, hours + books appointments
- **Google Calendar booking** — `check_slots` / `book_appointment` with guards for closed days, outside-hours, and double-booking
- **Auto email confirmation** — Gmail API, sent in Python right after booking (not via LLM tool call, for reliability)
- **Voice mode** — laptop mic (Groq Whisper) + Edge TTS reply, short 1–2 sentence spoken answers
- **Cancel / reschedule** — `cancel_appointment` / `reschedule_appointment` by phone + date
- **Returning customers** — `customers.json` remembers name/email/visits by phone; bot greets them back
- **Booking log** — every book/cancel/reschedule/email appended to `bookings.log` (JSON lines)
- **Phone mode (parked)** — FastAPI + Twilio `<Gather>` / `<Say>` code present, not connected

## Tech stack (all free to start)

| Part | Choice |
|---|---|
| Brain (LLM) | Groq `openai/gpt-oss-20b` with function calling |
| Calendar | Google Calendar API |
| Email | Gmail API (`gmail.send`) |
| STT | Groq Whisper `whisper-large-v3-turbo` |
| TTS | Edge TTS (`en-IN-NeerjaNeural`) |
| Phone | Parked (Twilio code in `main.py --serve`, no account connected) |
| Server | FastAPI + Uvicorn (phone mode only) |

## Project structure

```
ai_receptionist/
├── main.py           # app + chat loop + voice loop + phone (FastAPI) server
├── tools.py          # check_slots, book/cancel/reschedule, send_confirmation, customers, log + Google auth
├── salon.json        # services, prices, durations, opening hours
├── requirements.txt
├── test_calendar.py  # direct Calendar test (no LLM)
├── .env              # GROQ_API_KEY (never commit)
├── credentials.json  # Google OAuth client (never commit)
└── token.json        # Google OAuth token, auto-created on first run (never commit)
```

## Setup

1. **Python 3.10+** recommended.

```powershell
pip install -r requirements.txt
```

2. **Groq API key** — free at [console.groq.com](https://console.groq.com). Create `.env`:

```
GROQ_API_KEY=your_key_here
```

3. **Google Calendar + Gmail access:**
   - Go to Google Cloud Console → APIs & Services → enable **Google Calendar API** + **Gmail API**
   - Credentials → Create Credentials → OAuth client ID (Desktop app) → download as `credentials.json` into this folder
   - First run opens a browser for consent and saves `token.json` automatically

4. **Customise the salon** — edit `salon.json` (name, phone, address, `opening_hours`, `services`).

## Usage

```powershell
# 1. Test Calendar directly (no LLM)
python test_calendar.py

# 2. Text chat
python main.py

# 3. Voice mode (laptop mic + speakers)
python main.py --voice
# Press Enter to speak, or type instead (handy for phone/email)
```

> Phone mode is parked: `python main.py --serve` + `ngrok http 8000` + Twilio webhook
> still work technically, but no Twilio account is connected right now.

## Booking flow

1. Customer asks to book → bot asks service → date → rough time
2. `check_slots(date, service, around?)` shows nearest options
3. Customer picks time → bot asks name, phone, email
4. `book_appointment(name, phone, service, date, time)` creates Calendar event
5. Confirmation email auto-sent to the email found in chat history
6. If no email was given, bot asks for it and sends on the next turn

Cancel / move: customer gives phone (+ date) → `cancel_appointment` deletes the
event, or `reschedule_appointment` patches it to the new slot (same guards as
booking). Several matches → bot asks which one.

Guards: closed days (e.g. Sunday) blocked, outside-hours blocked, double-booking blocked, invalid phone/date rejected with a clear message so the LLM can re-ask.

## Spoken-input handling

Voice/STT text is normalised before the LLM sees it:

- `plus 91 ...` → `+91...`
- `puru.agar99 at gmail dot com` / `at the rate` → proper email
- Digit words (`double seven`, `niner`) → digits for phone numbers
- `Rs.300` / `09:00` → cleaned for speech (`300 rupees`, `9 AM`)

## Security

Never commit these (already in `.gitignore`):

```
__pycache__/
.env
credentials.json
token.json
```

## Future work (when needed)

- **Phone mode** — connect a Twilio number to `main.py --serve` (webhook `/voice`, see parked code)
- **WhatsApp/SMS** confirmations (needs paid API + DLT registration, skipped for now)
