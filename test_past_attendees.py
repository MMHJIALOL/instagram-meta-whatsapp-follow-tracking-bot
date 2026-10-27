"""Offline check: people whose join link is from an earlier free workshop go through the follow-gate again and
get the new link, with the ₹99 offer once; follow/unfollow toggling doesn't break it; their old link doesn't open
the new group until then. python test_past_attendees.py (no network, no real DMs)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.update(WORKSHOP_SINCE="2026-10-18T22:17:00+05:30", KEYWORD="*", IG_USER_ID="me", MEET_URL="",
                  PAGE_ID="page1", PAGE_ACCESS_TOKEN="test")
import main
import messenger

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
main.WORKSHOP_LINK = "https://chat.whatsapp.com/NEW"
main.RECHECK_DELAYS = ()
out = []  # (recipient, text, has buttons)
main.send = lambda recipient, text, buttons=None: out.append((recipient, text, bool(buttons))) or True
follows = {"value": True}
main.check_follow = lambda igsid: (follows["value"], "user_" + igsid)
main.api = lambda method, path, **kw: {}
messenger.send = lambda psid, text, buttons=None: out.append(({"psid": psid}, text, bool(buttons))) or True
messenger.lookup_name = lambda psid: ""
before = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc).isoformat()  # 18 Oct afternoon IST: past workshop
after = (main.now() - timedelta(hours=1)).isoformat()
with main.db() as conn:
    for sid in ("old", "old2", "old3"):
        conn.execute("INSERT INTO sent VALUES (?, ?, ?)", (sid, sid + "_user", before))
    conn.execute("INSERT INTO sent VALUES ('new', 'new_user', ?)", (after,))
n = 0


def dm(sid, text):
    global n
    n += 1
    return {"entry": [{"id": "me", "messaging": [{"sender": {"id": sid}, "message": {"mid": f"m{n}", "text": text}}]}]}


def tap(sid):
    global n
    n += 1
    return {"entry": [{"id": "me", "messaging": [{"sender": {"id": sid}, "postback": {
        "mid": f"p{n}", "payload": main.CHECK_FOLLOW, "title": "I'm following ✅"}}]}]}


def comment(sid):
    global n
    n += 1
    return {"entry": [{"id": "me", "changes": [{"field": "comments", "value": {
        "id": f"c{n}", "text": "info", "from": {"id": sid, "username": "x"}, "media": {"id": "ad", "ad_id": "1"}}}]}]}


fails = 0
def check(ok, what):
    global fails
    fails += not ok
    print("OK  " if ok else "FAIL", what)


def run(payload):
    out.clear()
    main.process(payload)
    return [t for _, t, _ in out]


link = lambda texts: any("/j/" in t for t in texts)
from fastapi.testclient import TestClient
client = TestClient(main.app)
old_code = main.personal_link("old").rsplit("/", 1)[1]
r = client.get(f"/j/{old_code}", follow_redirects=False)
check(r.status_code == 200 and "NEW" not in r.text and "DM" in r.text, "old link before re-joining: 'DM us' page, not the new group")

texts = run(dm("old", "Workshop kab hai?"))
check(texts[0] == main.WHEN_MSG and main.PAST_MSG in texts and link(texts),
      "past attendee who follows: the 22 Oct answer, the ₹99 offer, then the new link")
check(texts.index(main.PAST_MSG) < [i for i, t in enumerate(texts) if "/j/" in t][0], "...offer comes before the link")
check(main.already_sent("old") and not main.past_attendee("old"), "...and now counts as a 22 Oct link holder")
r = client.get(f"/j/{old_code}", follow_redirects=False)
check(r.status_code == 200 and main.WORKSHOP_LINK in r.text, "...so their (same) link opens the new group")
check(run(dm("old", "hello?")) == [], "next message: skipped like any link holder (no second offer)")
follows["value"] = False
check(run(tap("old")) == [], "unfollows, taps 'I'm following': nothing breaks, no follow request, no new link")
follows["value"] = True
check(run(dm("old", "DONE")) == [], "follows again and says DONE: still just a link holder")

follows["value"] = False
texts = run(dm("old2", "hi"))
check(not link(texts) and main.PAST_MSG not in texts and any(b for _, _, b in out), "past attendee not following: follow buttons, no link yet")
follows["value"] = True
texts = run(tap("old2"))
check(main.PAST_MSG in texts and link(texts), "...then follows and taps: offer + new link")

texts = run(comment("old3"))
check(len(texts) == 1 and texts[0].startswith(main.PAST_MSG) and "/j/" in texts[0] and main.already_sent("old3"),
      "comment from a past attendee: one private reply with the offer and the link")
check(run(dm("new", "Workshop kab hai?")) == [main.WHEN_MSG], "22 Oct link holder: normal answer, no offer")
texts = run(dm("fresh", "hi"))
check(main.PAST_MSG not in texts and link(texts), "brand-new person: normal flow, no offer")

# Messenger
messenger.update("fbold", link_sent_at=before)
messenger.update("fbnew", link_sent_at=after)
fb = lambda psid, text: {"object": "page", "entry": [{"id": "page1", "messaging": [{"sender": {"id": psid},
    "recipient": {"id": "page1"}, "message": {"mid": f"fb{psid}{text}", "text": text}}]}]}
out.clear(); messenger.process(fb("fbold", "Kya ye free hai?"))
texts = [t for _, t, _ in out]
check(texts[0] == main.FREE_MSG and main.PAST_MSG not in texts and not link(texts), "Messenger past attendee: answer + follow steps")
out.clear(); messenger.process(fb("fbold", "DONE"))
texts = [t for _, t, _ in out]
check(texts[0] == main.PAST_MSG and link(texts), "...then DONE: offer + new link")
out.clear(); messenger.process(fb("fbold", "DONE again"))
check(not any(t == main.PAST_MSG for _, t, _ in out), "...and no second offer")
out.clear(); messenger.process(fb("fbnew", "Kya ye free hai?"))
check([t for _, t, _ in out] == [main.FREE_MSG], "Messenger 22 Oct link holder: normal answer")
raise SystemExit(1 if fails else 0)
