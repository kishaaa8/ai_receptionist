Already done before I started:
- Phase 1: salon.json with 9 services, prices, hours + main.py CLI chatbot answering from it
- Phase 2: tools.py with check_slots + book_appointment, OAuth via credentials.json/token.json, test_calendar.py for direct Calendar test
- Phase 3 (mostly): main.py wired to Groq LLM with function calling (TOOLS + TOOL_FUNCTIONS + process_tool_calls recursion) — chat-to-booking worked
Progress I made (Phase 3 fix + Phase 4):
- Added missing Phase 3 guards in book_appointment: blocks closed days (e.g. Sunday), outside-hours, double-booking
- Added gmail.send to SCOPES in tools.py, refactored auth to _get_credentials() + get_calendar_service() + get_gmail_service() with auto re-login on old token
- Added send_confirmation(to_email, name, service, date, time) via Gmail API in tools.py
- Wired it into main.py: new tool definition, added to TOOL_FUNCTIONS, prompt now asks for email and calls email right after booking
- Verified: py_compile OK, imports OK, closed-day + outside-hours blocks pas

phase 5:
What I built:
- main.py --voice → voice_loop(): listen → think → speak, reusing the same brain (SYSTEM_PROMPT + TOOLS + auto-email). No new files.
- STT: laptop mic via sounddevice (16kHz, stops on 1.5s silence) → Groq Whisper whisper-large-v3-turbo, free tier, no local model.
- TTS: Edge TTS en-IN-NeerjaNeural, played via pygame. Replies capped at 150 tokens + voice addendum forces 1–2 short sentences.
- Refactored text loop to share get_bot_reply() — text mode unchanged (python main.py works as before).
Packages added (pip install already run here): edge-tts, sounddevice, pygame — also added to requirements.txt.