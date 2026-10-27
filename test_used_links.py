"""Offline check that personal links stop working once the dashboard is sure the person joined.
python test_used_links.py (no network)."""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.update(WA_MAIN_CHAT="22 Oct Group", WORKSHOP_SINCE="2026-10-18T22:17:00+05:30")
import main
import dashboard

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
from fastapi.testclient import TestClient

client = TestClient(main.app)
main.WORKSHOP_LINK = "https://chat.whatsapp.com/NEW"
after = (dashboard.WORKSHOP_SINCE + timedelta(hours=1))
before = (dashboard.WORKSHOP_SINCE - timedelta(days=2))
code = {ig: main.personal_link(ig).rsplit("/", 1)[1] for ig in ("sure", "maybe", "old", "hand", "notthem")}
with dashboard.db() as conn:
    for who, at, chat in (("+911", after, "22 Oct  group"), ("+912", after, "22 Oct Group"),
                          ("+913", before, "Karwa Chauth special worshop 18th Oct"), ("+914", after, "22 Oct Group"),
                          ("+915", after, "")):
        conn.execute("INSERT INTO wa_events VALUES (?, 'join', ?, ?, 'live')", (who, at.isoformat(), chat))
row = lambda ig, conf, who, at: {"igsid": ig, "conf": conf, "who": who, "joined_at": at.isoformat()}
rows = [row("sure", "sure", "+911", after), row("maybe", "maybe", None, after), row("old", "sure", "+913", before),
        row("hand", "confirmed", "+914", after), row("notthem", "sure", "+915", after)]
dashboard.refresh_used_links(rows)


def opens(ig):
    r = client.get(f"/j/{code[ig]}", follow_redirects=False, headers={"user-agent": "Instagram 300"})
    return r.status_code == 200 and main.WORKSHOP_LINK in r.text and 'use ho chuka' not in r.text


fails = 0
def check(ok, what):
    global fails
    fails += not ok
    print("OK  " if ok else "FAIL", what)


check(not opens("sure"), "99% match to a join in the current group -> link used up")
check(not opens("hand"), "match settled by hand ('This one') -> link used up")
check(opens("maybe"), "not sure ('Check') -> link still works")
check(opens("old"), "joined the previous workshop's group -> link still works for the new group")
check(opens("notthem"), "join with no group name (chat export) -> not counted, link still works")
with dashboard.db() as conn:
    taps = dict(conn.execute("SELECT igsid, COUNT(*) FROM clicks GROUP BY igsid"))
check("sure" not in taps and taps.get("maybe") == 1, "taps on a used link aren't logged as clicks")
dashboard.refresh_used_links([r for r in rows if r["igsid"] != "sure"])  # e.g. marked "Not them" on the page
check(opens("sure"), "match undone -> link works again")
r = client.get(f"/j/{code['hand']}")
check(r.status_code == 200 and "use ho chuka" in r.text and 'Open WhatsApp' in r.text,
      "used link offers recovery without logging another matching click")
raise SystemExit(1 if fails else 0)
