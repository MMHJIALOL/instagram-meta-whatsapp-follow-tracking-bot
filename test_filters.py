"""Offline check of which events trigger the bot. python test_filters.py (no network, no real DMs)."""
import os
from datetime import datetime

os.environ.update(START_AFTER="2026-10-02T21:00:00+05:30", KEYWORD="workshop", IG_USER_ID="me", MEET_URL="")
import tempfile

import main

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")  # never the real bot.db (the bot records its activity there)
main.first_time = lambda _id: True
times = {"old": "2026-09-01T10:00:00+0000", "new": "2026-10-02T16:00:00+0000"}  # 16:00 UTC = 21:30 IST
main.api = lambda method, path, **kw: {"timestamp": times[path]} if path in times else None
fired = []  # the `claimed` flag of each handle() call
real_handle = main.handle
main.handle =lambda igsid, username, recipient, claimed=False: fired.append(claimed) or True


def comment(media):
    return {"entry": [{"id": "me", "changes": [{"field": "comments", "value": {
        "id": "c1", "text": "nice", "from": {"id": "u1", "username": "x"}, "media": media}}]}]}


def dm(**message):
    return {"entry": [{"id": "me", "messaging": [{"sender": {"id": "u1"}, "message": {"mid": "m1", **message}}]}]}


def tap(payload, title):
    return {"entry": [{"id": "me", "messaging": [{"sender": {"id": "u1"}, "postback": {
        "mid": "p1", "payload": payload, "title": title}}]}]}


cases = [
    (comment({"id": "new"}), True, "comment on new post"),
    (comment({"id": "old"}), False, "comment on old post"),
    (comment({"id": "old", "ad_id": "123"}), True, "comment on ad (boosted old post)"),
    (comment({"id": "x", "media_product_type": "AD"}), True, "comment on Ads Manager ad"),
    (dm(text="hi", reply_to={"story": {"id": "new"}}), True, "reply to new story"),
    (dm(text="hi", reply_to={"story": {"id": "old"}}), False, "reply to old story"),
    (dm(text="hi", referral={"ad_id": "999"}), True, "DM from any ad"),
    (dm(text="hi"), False, "plain DM without keyword"),
    (dm(text="WORKSHOP price?"), True, "plain DM with keyword"),
    (dm(text="done"), True, "DONE"),
]
for payload, want, name in cases:
    fired.clear()
    main.process(payload)
    assert bool(fired) == want, f"{name}: expected trigger={want}"
    print("OK ", name)

# claimed=True means a "not followed" result gets "Follow not detected" instead of the first follow request
claims = [
    (tap("CHECK_FOLLOW", "I'm following ✅"), True, "I'm following tap"),
    (tap("GET_LINK", "I'm following ✅"), True, "I'm following tap on an older message"),
    (tap("GET_LINK", "Send me the link"), False, "Send me the link tap"),
    (dm(text="done"), True, "DONE"),
    (dm(text="WORKSHOP price?"), False, "keyword DM"),
]
for payload, want, name in claims:
    fired.clear()
    main.process(payload)
    assert fired == [want], f"{name}: expected claimed={want}, got {fired}"
    print("OK ", name, "-> claimed" if want else "-> not claimed")
assert main.START_AFTER == datetime.fromisoformat("2026-10-02T21:00:00+05:30")

# --- rules message: plain text before DM button messages, never on comment replies, at most once per 24h ---
import tempfile
from datetime import timedelta

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
main.RECHECK_DELAYS = ()
main.profile_url = lambda: "https://www.instagram.com/me/"
main.time.sleep = lambda s: None
status = {}  # igsid -> follows (True/False/None)
main.check_follow = lambda igsid: (status.get(igsid), "user_" + igsid)
out = []  # (recipient, text, has buttons) of each DM
main.send = lambda recipient, text, buttons=None: out.append((recipient, text, bool(buttons))) or True


def texts(**kw):
    out.clear()
    real_handle(**kw)
    return [t for _, t, _ in out]


for b in main.follow_buttons():
    assert len(b["title"]) <= 20, b["title"]
    assert b["title"] in main.RULES_MSG, f"RULES_MSG should name the {b['title']!r} button"
    assert b["title"] in main.INTRO_MSG, f"INTRO_MSG should name the {b['title']!r} button"
assert len(main.JOIN_TITLE) <= 20
print("OK  button titles fit and match RULES_MSG")

status["a"] = False
assert texts(igsid="a", username=None, recipient={"id": "a"}) == [main.INTRO_MSG, main.FOLLOW_MSG]
print("OK  first DM, not following -> intro then follow request")
assert texts(igsid="a", username=None, recipient={"id": "a"}, claimed=True) == [main.NOT_FOLLOWING_MSG]
print("OK  rules not repeated within 24h")
main.update_user("a", rules_sent_at=(main.now() - timedelta(hours=25)).isoformat())
assert texts(igsid="a", username=None, recipient={"id": "a"}, claimed=True) == [main.RULES_MSG, main.NOT_FOLLOWING_MSG]
print("OK  rules sent again after 24h")

status["c"] = False
assert texts(igsid="c", username="c", recipient={"comment_id": "c1"}) == [main.FOLLOW_MSG]
print("OK  comment -> private reply only, no rules")
assert texts(igsid="c", username=None, recipient={"id": "c"}, claimed=True) == [main.INTRO_MSG, main.NOT_FOLLOWING_MSG]
print("OK  commenter taps button in DM -> intro then follow not detected")

assert texts(igsid="u", username=None, recipient={"id": "u"}) == [main.INTRO_MSG, main.OPENER_MSG]
assert out[1][2], "opener should carry buttons"
print("OK  unknown follow status -> intro then opener")
status["f"] = True
link = [main.LINK_MSG.format(main.personal_link("f")), main.LINK_BUTTON_MSG]
assert texts(igsid="f", username=None, recipient={"id": "f"}) == link and [b for *_, b in out] == [False, True]
status["g"] = True
assert texts(igsid="g", username="g", recipient={"comment_id": "g1"}) == [main.LINK_MSG.format(main.personal_link("g"))]
print("OK  commenter follower -> link message only (one private reply per comment)")
print("OK  follower -> link only, no rules")

# --- background re-check: link goes out once someone asked within 23h follows ---
status.update(a=True, c=True)
main.update_user("c", asked_at=(main.now() - timedelta(hours=30)).isoformat())  # asked too long ago
out.clear()
main.recheck_follows()
url = main.personal_link("a")
assert out == [({"id": "a"}, main.LINK_MSG.format(url), False), ({"id": "a"}, main.LINK_BUTTON_MSG, True)], out
assert main.already_sent("a") and not main.already_sent("c")
out.clear()
main.recheck_follows()
assert out == [], "link must not be sent twice"
print("OK  background re-check sends the link once, only inside the 23h window")

# --- reminders: one per 12h for follow requests sent after REMIND_FROM, only inside the 23h window ---
ago = lambda h: (main.now() - timedelta(hours=h)).isoformat()
main.REMIND_FROM = main.now() - timedelta(days=1)
status["r"] = False
main.record_user("r", "r")
main.update_user("r", asked_at=ago(4), last_msg_at=ago(4))
out.clear()
main.recheck_follows()
assert out == [({"id": "r"}, main.REMIND_MSG, True)], out
out.clear()
main.recheck_follows()
assert out == [], "only one reminder per 12h"
main.update_user("r", reminded_at=ago(13))
main.recheck_follows()
assert out == [], "never a second reminder"
print("OK  reminder after 3h, only once ever")
status["s"] = False
main.record_user("s", "s")
main.update_user("s", asked_at=ago(1), last_msg_at=ago(1))  # too soon
status["t"] = False
main.record_user("t", "t")
main.update_user("t", asked_at=(main.REMIND_FROM - timedelta(minutes=1)).isoformat(), last_msg_at=ago(4))
main.update_user("r", reminded_at=ago(1))
out.clear()
main.recheck_follows()
assert out == [], out
print("OK  no reminder within 3h of the ask, or for asks before REMIND_FROM")

# --- FAQ answers ---
for q in ("What's the workshop schedule?", "When?", "Workshop is on which date", "kab hai", "कब है?", "What time?"):
    assert main.faq_answers(q) == [main.WHEN_MSG], q
for q in ("Is it really free?", "Fees kya hai", "Free hai kya?", "What's the workshop fee?"):
    assert main.faq_answers(q) == [main.FREE_MSG], q
for q in ("What's the material list?", "What's the material cost?", "saman kya kya lagega", "Samaan list bhejo",
          "सामान क्या चाहिए", "materials needed?", "material kaha milega"):
    assert main.faq_answers(q) == [main.MATERIAL_MSG], q
for q in ("Ab kya karna hai?", "ab kya karna h", "What now?", "what now", "Class kaise hogi?", "class kese hogi",
          "Workshop online hai ya offline?", "Google meet pe hoga?", "अब क्या करना है", "Done, now what?"):
    assert main.faq_answers(q) == [main.CLASS_MSG], q
for q in ("Hello, can I get more info on this?", "I'm interested! Send me the workshop link", "Hi", "sometimes"):
    assert main.faq_answers(q) == [], q
out.clear()
main.handle = lambda *a, **k: None
main.process(dm(text="What's the workshop schedule?"))
assert [t for _, t, _ in out] == [main.WHEN_MSG], out
print("OK  date/time and free/material questions get their answer")

# material list: link holders get their join link with it, others only the pointer (follow-gate comes from handle)
out.clear()
main.process(dm(text="workshop material list?"))
assert [t for _, t, _ in out] == [main.MATERIAL_MSG], out
main.record_sent("u1", "u1")
out.clear()
main.process(dm(text="workshop ka saman?"))
assert [t for _, t, _ in out] == [main.MATERIAL_MSG + "\n\n" + main.MATERIAL_LINK_MSG.format(main.personal_link("u1"))], out
print("OK  material list -> pinned-in-group pointer, plus the join link for link holders")

# --- join-link reminder: once, 3h after the link, only if the person never really tapped it ---
SCANNER = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
PHONE = "Mozilla/5.0 (Linux; Android 15; moto g34 5G; wv) AppleWebKit/537.36 Mobile Safari/537.36 Instagram 449.0.0.52.84 Android"
for igsid, ua in (("n1", SCANNER), ("n2", PHONE)):
    main.record_user(igsid, igsid)
    main.update_user(igsid, last_msg_at=ago(4))
    main.record_sent(igsid, igsid)
    with main.db() as conn:
        conn.execute("UPDATE sent SET sent_at = ? WHERE igsid = ?", (ago(4), igsid))
        conn.execute("INSERT INTO clicks VALUES (?, ?, ?, ?, ?)", ("x", igsid, ago(4), ua, "1.2.3.4" if ua == PHONE else "5.6.7.8"))
with main.db() as conn:
    conn.execute("UPDATE sent SET sent_at = ? WHERE igsid = 'u1'", (ago(30),))  # outside the window
out.clear()
main.remind_untapped()
assert out == [({"id": "n1"}, main.LINK_REMIND_MSG.format(main.personal_link("n1")), False)], out
out.clear()
main.remind_untapped()
assert out == [], "join-link reminder goes only once"
print("OK  join-link reminder: scanner hits don't count as taps, real taps skip it, sent only once")

# one reminder per person in total: someone who got the follow reminder gets no join-link reminder
main.record_user("n3", "n3")
main.update_user("n3", last_msg_at=ago(4), reminded_at=ago(5))
main.record_sent("n3", "n3")
with main.db() as conn:
    conn.execute("UPDATE sent SET sent_at = ? WHERE igsid = 'n3'", (ago(4),))
out.clear()
main.remind_untapped()
assert out == [], out
print("OK  follow reminder already sent -> no join-link reminder")
