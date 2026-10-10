# brain.md — AI Receptionist for a Salon

## Goal
A voice assistant that answers calls for a salon, books appointments in Google Calendar, and sends confirmations.

## Rules for Claude Code
- Build **one phase at a time**. Stop at the end of each phase and let me test it.
- Keep it simple: few files, no extra abstractions, short comments.
- Use **free tiers** only. Put all keys in `.env`.
- Language: Python.

## Tech Stack (all free to start)
| Part | Choice |
|---|---|
| Brain (LLM) | Gemini or Groq free API, with function calling |
| Calendar | Google Calendar API |
| Speech-to-text | Whisper (local) or Groq Whisper |
| Text-to-speech | Edge TTS |
| Confirmation | Email (SMTP) first, WhatsApp later |
| Phone | Twilio trial (last phase only) |
| Server | FastAPI |

## Project Structure
```
salon-receptionist/
├── main.py          # app + conversation loop
├── tools.py         # check_slots, book_appointment, send_confirmation
├── salon.json       # services, prices, hours
├── .env             # API keys
└── requirements.txt
```
Keep to this. Add a file only if really needed.

## What the bot can do (tools)
1. `check_slots(date, service)` → returns free time slots
2. `book_appointment(name, phone, service, date, time)` → creates calendar event
3. `send_confirmation(contact, details)` → sends confirmation message

---

## Phase 1 — Salon info + text chatbot
**Do:**
- Create `salon.json` with services, prices, durations, opening hours.
- In `main.py`, build a command-line chatbot using the LLM that answers questions using `salon.json`.

**Done when:** I can ask "how much is a haircut?" and "are you open on Sunday?" and get correct answers.

## Phase 2 — Google Calendar
**Do:**
- Set up Google Calendar API (OAuth, one-time login).
- In `tools.py`, write `check_slots` and `book_appointment`.
- Test them directly without the LLM first.

**Done when:** running a script books a real event in my calendar and shows free slots.

## Phase 3 — Connect tools to the LLM
**Do:**
- Give the LLM the tools using function calling.
- Bot should ask for missing details (name, service, date, time) before booking.
- Prevent double booking and booking outside working hours.

**Done when:** I can chat "book a haircut tomorrow at 4" and the event appears in my calendar.

## Phase 4 — Confirmations
**Do:**
- Write `send_confirmation` using the **Gmail API** (no app password, no SMTP).
- Reuse the same Google login from Phase 2, just add the `gmail.send` permission.
- Bot calls it automatically after a successful booking.

**Done when:** I get a confirmation email right after booking.

## Phase 5 — Voice (laptop mic)
**Do:**
- Add speech-to-text for input and Edge TTS for the reply.
- Keep replies short (1–2 sentences) so voice feels fast.
- Simple loop: listen → think → speak.

**Done when:** I can talk to the bot and it books an appointment by voice.

## Phase 6 — Real phone calls
**Do:**
- Add a FastAPI endpoint that Twilio calls on an incoming call.
- Stream call audio to speech-to-text, reply with text-to-speech.
- Use `ngrok` to expose the local server for testing.

**Done when:** I call the Twilio number and can book an appointment.

## Phase 7 — Polish (optional)
Pick only what is useful:
- Cancel / reschedule appointments
- Send WhatsApp or SMS confirmation
- Remember returning customers
- Use a vector DB so the bot can answer many salon FAQs
- Log every call and booking in a simple file

---

## Notes
- Free LLM tiers have rate limits, so test speed early.
- Indian SMS needs DLT registration, so use email or WhatsApp for confirmations at first.
- Never commit `.env` or Google credentials to GitHub.