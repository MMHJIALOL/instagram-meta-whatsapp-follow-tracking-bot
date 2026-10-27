"""One-time: the workshop list (dashboard Workshop tab) from what we know about the workshops so far.

python seed_workshops.py [database]    (default bot.db; does nothing if workshops are already set up)

- 11 Oct workshop: everything before 12 Oct 17:50 IST. Its group isn't counted for join matching (as before).
- 18 Oct workshop: 12 Oct 17:50 to 18 Oct 22:17 IST, group "Karwa Chauth special worshop 18th Oct".
- 22 Oct workshop, running: from WORKSHOP_SINCE with WA_MAIN_CHAT, WORKSHOP_LINK and MEET_URL from .env. Its texts
  are main.py's, so nothing the bot sends changes.
"""
import os
import sys

import main

HISTORY = (  # name, class date, started, ended, WhatsApp group
    ("11 Oct workshop", "2026-10-11", "2026-09-30T11:30:00+05:30", "2026-10-12T17:50:00+05:30", ""),
    ("18 Oct workshop", "2026-10-18", "2026-10-12T17:50:00+05:30", "2026-10-18T22:17:00+05:30",
     "Karwa Chauth special worshop 18th Oct"),
)

if __name__ == "__main__":
    main.DB = sys.argv[1] if sys.argv[1:] else main.DB
    with main.db() as conn:
        if conn.execute("SELECT 1 FROM workshops").fetchone():
            sys.exit(f"{main.DB} already has workshops; nothing done")
        for name, date, start, end, group in HISTORY:
            conn.execute("INSERT INTO workshops (name, date, started_at, closed_at, wa_group, wa_link, meet_url, "
                         "past_offer, texts) VALUES (?, ?, ?, ?, ?, '', '', 0, '{}')", (name, date, start, end, group))
        conn.execute("INSERT INTO workshops (name, date, started_at, wa_group, wa_link, meet_url, past_offer, texts) "
                     "VALUES ('22 Oct workshop', '2026-10-22', ?, ?, ?, ?, 1, '{}')",
                     (main.PAST_BEFORE.isoformat(), os.getenv("WA_MAIN_CHAT", ""), main.WORKSHOP_LINK, main.MEET_URL))
    for w in main.workshops(fresh=True):
        print(f"{w['name']:18} {w['started_at']} -> {w['closed_at'] or 'running'}  group={w['wa_group']!r}")
