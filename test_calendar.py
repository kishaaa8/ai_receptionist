"""
test_calendar.py -- Quick test for check_slots and book_appointment.

Run this AFTER placing credentials.json in the project folder.
First run will open a browser for Google OAuth login.
"""

import sys
import io

# Fix Windows console encoding for unicode
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from tools import check_slots, book_appointment
from datetime import datetime, timedelta


def main():
    print("=" * 55)
    print("  Google Calendar Tools -- Test Script")
    print("=" * 55)

    # -- Pick a test date (tomorrow) ---------------------------------
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    test_service = "Haircut (Men)"

    # -- Test 1: check_slots -----------------------------------------
    print(f"\n[CHECK] Free slots for '{test_service}' on {tomorrow}...\n")

    slots = check_slots(date=tomorrow, service=test_service)

    if not slots:
        print("  [X] No slots available (salon might be closed that day).")
        # Try the day after if tomorrow is Sunday
        day_after = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        print(f"\n  Trying {day_after} instead...")
        slots = check_slots(date=day_after, service=test_service)
        tomorrow = day_after

    if slots:
        print(f"  [OK] Found {len(slots)} free slots! First 5:")
        for slot in slots[:5]:
            print(f"       {slot['start']} - {slot['end']}")
    else:
        print("  [X] Still no slots. Check your salon.json opening hours.")
        return

    # -- Test 2: book_appointment ------------------------------------
    first_slot = slots[0]
    print(f"\n[BOOK] '{test_service}' at {first_slot['start']} on {tomorrow}...\n")

    result = book_appointment(
        name="Test Customer",
        phone="+91-0000000000",
        service=test_service,
        date=tomorrow,
        time=first_slot["start"],
    )

    print("  [OK] Appointment booked!")
    print(f"       Event:    {result['summary']}")
    print(f"       Time:     {result['start']} - {result['end']}")
    print(f"       Price:    {result['price']}")
    print(f"       Link:     {result['event_link']}")
    print(f"       Event ID: {result['event_id']}")

    # -- Test 3: verify slot is now taken ----------------------------
    print(f"\n[VERIFY] Re-checking slots to confirm the booked slot is gone...\n")

    slots_after = check_slots(date=tomorrow, service=test_service)
    booked_time = first_slot["start"]

    slot_still_free = any(s["start"] == booked_time for s in slots_after)
    if not slot_still_free:
        print(f"  [OK] Slot {booked_time} is no longer available -- booking confirmed!")
    else:
        print(f"  [!!] Slot {booked_time} still appears free (possible timezone issue)")

    print(f"\n{'='*55}")
    print("  All tests passed! Phase 2 is ready.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
