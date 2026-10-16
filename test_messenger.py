"""Offline check of the Messenger side of the bot. python test_messenger.py (no network, no real messages)."""
import hashlib
import hmac
import json
import os
import tempfile
from datetime import timedelta

os.environ.update(KEYWORD="*", MEET_URL="", PAGE_ID="page1", PAGE_ACCESS_TOKEN="test", FB_APP_SECRET="fbsecret",
                  APP_SECRET="igsecret")
import main
import messenger

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
out = []  # (psid, text, has buttons) of each message
messenger.send = lambda psid, text, buttons=None: out.append((psid, text, bool(buttons))) or True
messenger.lookup_name = lambda psid: "Name " + psid
ago = lambda h: (main.now() - timedelta(hours=h)).isoformat()
n = 0


def event(psid="p1", **e):
    global n
    n += 1
    if "message" in e:
        e["message"].setdefault("mid", f"m{n}")
    return {"object": "page", "entry": [{"id": "page1", "messaging": [{"sender": {"id": psid},
                                                                       "recipient": {"id": "page1"}, **e}]}]}


def run(payload):
    out.clear()
    main.process(payload)
    return [t for _, t, _ in out]


def msg(text, psid="p1", **extra):
    return run(event(psid, message={"text": text, **extra}))


def tap(psid="p1", payload=main.CHECK_FOLLOW):
    return run(event(psid, postback={"mid": f"pb{n}", "payload": payload, "title": main.FOLLOWING_TITLE}))


# --- wording: the Instagram texts with the Page instead of the profile ---
for t in (messenger.RULES_MSG, messenger.INTRO_MSG, messenger.FOLLOW_MSG, messenger.REMIND_MSG):
    assert "rofile" not in t, t
for t in (messenger.RULES_MSG, messenger.INTRO_MSG):
    for b in messenger.follow_buttons():
        assert len(b["title"]) <= 20 and b["title"] in t, (b["title"], t)
assert messenger.follow_buttons()[0]["url"] == "https://www.facebook.com/page1"
print("OK  Messenger texts say Page, name both buttons, titles fit")

# --- the flow ---
link = lambda psid: messenger.personal_link(psid)
assert msg("hi") == [messenger.INTRO_MSG, messenger.FOLLOW_MSG] and out[1][2]
print("OK  first message -> intro, then the follow request with buttons")
assert msg("hello?") == [messenger.FOLLOW_MSG]
print("OK  rules not repeated within 24h")
messenger.update("p1", rules_sent_at=ago(25))
assert msg("hello again") == [messenger.RULES_MSG, messenger.FOLLOW_MSG]
print("OK  rules sent again after 24h")
assert tap() == [main.LINK_MSG.format(link("p1")), main.LINK_BUTTON_MSG] and [b for *_, b in out] == [False, True]
assert link("p1").endswith("/j/" + main.db().execute("SELECT code FROM links WHERE igsid = 'fb:p1'").fetchone()[0])
assert main.db().execute("SELECT name FROM ig_names WHERE igsid = 'fb:p1'").fetchone() == ("Name p1 (Facebook)",)
print("OK  'I'm following' tap -> personal join link (trusted, Facebook can't check follows)")
assert msg("thanks") == []
assert tap() == [main.LINK_MSG.format(link("p1")), main.LINK_BUTTON_MSG]
print("OK  link holders: nothing on plain messages, the link again if they tap 'I'm following'")
assert msg("kab hai?") == [main.WHEN_MSG]
assert msg("material list?") == [main.MATERIAL_MSG + "\n\n" + main.MATERIAL_LINK_MSG.format(link("p1"))]
print("OK  questions answered for link holders, material question gets the link again")
assert msg("done", psid="p2") == [main.LINK_MSG.format(link("p2")), main.LINK_BUTTON_MSG]
print("OK  DONE from someone new -> link")
assert run(event("p3", referral={"ad_id": "123", "source": "ADS"}, timestamp=1)) == [messenger.INTRO_MSG,
                                                                                    messenger.FOLLOW_MSG]
print("OK  opening the chat from an ad (referral only) -> intro and buttons")
assert msg("is it free?", psid="p4") == [main.FREE_MSG, messenger.INTRO_MSG, messenger.FOLLOW_MSG]
print("OK  first message with a question -> answer, then intro and buttons")

# --- what's ignored ---
assert run(event("p5", message={"text": "hi", "mid": "same"})) and run(event("p5", message={"text": "hi", "mid": "same"})) == []
print("OK  a message Meta delivers twice is answered once")
assert msg("hi", psid="page1") == [] and msg("hi", psid="p6", is_echo=True) == []
print("OK  the Page's own messages (echoes) are ignored")
assert tap("p7", payload="SOMETHING_ELSE") == [] and run(event("p7", message={"mid": "sticker"})) == []
print("OK  other buttons and stickers are ignored")
messenger.PAGE_ACCESS_TOKEN = ""
assert msg("hi", psid="p8") == []
messenger.PAGE_ACCESS_TOKEN = "test"
print("OK  nothing happens until PAGE_ACCESS_TOKEN is set")
out.clear()
ig = []
real_handle, main.handle = main.handle, lambda igsid, *a, **k: ig.append(igsid)  # no calls to Instagram
main.process({"object": "instagram", "entry": [{"id": "x", "messaging": [{"sender": {"id": "ig1"},
                                                                           "message": {"mid": "igm", "text": "x"}}]}]})
main.handle = real_handle
assert ig == ["ig1"] and out == []
print("OK  Instagram webhooks don't go to Messenger")

# --- comments on the Page's posts and ads ---
calls = []
def fake_api(method, path, **kw):
    calls.append((method, path, kw))
    return {"recipient_id": "cp"} if path.endswith("/messages") else {"id": "x"}
messenger.api = fake_api
feed = lambda **v: {"object": "page", "entry": [{"id": "page1", "changes": [{"field": "feed", "value": {
    "item": "comment", "verb": "add", "comment_id": "c1", "post_id": "page1_9", "parent_id": "page1_9",
    "from": {"id": "u9", "name": "Asha"}, "message": "interested", **v}}]}]}
main.process(feed())
(m1, p1_, k1), (m2, p2_, k2) = calls
assert p1_ == "page1/messages" and k1["json"]["recipient"] == {"comment_id": "c1"}
tpl = k1["json"]["message"]["attachment"]["payload"]
assert tpl["text"] == messenger.INTRO_MSG and tpl["buttons"] == messenger.follow_buttons()
assert p2_ == "c1/comments" and k2["data"] == {"message": main.COMMENT_REPLY}
u = messenger.user("cp")
assert u["asked_at"] and u["rules_sent_at"] and not u["last_msg_at"]
print("OK  comment -> one private Messenger reply (intro + buttons) and the public reply")
calls.clear()
main.process(feed())
main.process(feed(comment_id="c2", **{"from": {"id": "page1"}}))
main.process(feed(comment_id="c3", parent_id="c1"))
main.process(feed(comment_id="c4", verb="edited"))
main.process(feed(comment_id="c5", item="reaction"))
assert calls == [], calls
print("OK  repeats, the Page's own comments, replies in threads, edits and reactions are ignored")
assert msg("hi", psid="cp") == [messenger.FOLLOW_MSG]
print("OK  commenter who then messages gets the buttons without the intro again")

# --- name lookup happens once, even when Facebook doesn't share the name ---
looked = []
messenger.lookup_name = lambda psid: looked.append(psid)
msg("hi", psid="nn"); msg("hello", psid="nn")
assert looked == ["nn"] and messenger.user("nn")["name"] == ""
print("OK  name looked up only once")

# --- reminders: one per person, inside the 24h window ---
for psid in ("r1", "r2", "r3", "r4", "r5"):
    messenger.update(psid, last_msg_at=ago(4))
messenger.update("r1", asked_at=ago(4))                          # never tapped -> follow reminder
messenger.update("r2", asked_at=ago(1))                          # asked too recently
messenger.update("r3", asked_at=ago(4), link_sent_at=ago(4))     # link, no tap -> link reminder
messenger.update("r4", asked_at=ago(4), link_sent_at=ago(4))     # link, tapped in Messenger
messenger.update("r5", asked_at=ago(4), last_msg_at=ago(30))     # window closed
PHONE = "Mozilla/5.0 (Linux; Android 14; wv) AppleWebKit/537.36 Mobile Safari/537.36 [FB_IAB/Orca-Android;FBAV/400.0;]"
with main.db() as conn:
    conn.execute("INSERT INTO clicks VALUES (?, ?, ?, ?, ?)", ("x", "fb:r4", ago(3), PHONE, "1.2.3.4"))
out.clear()  # p1-p5 from above were asked or got their link just now, so none of them is due
messenger.remind()
assert sorted(out) == sorted([("r1", messenger.REMIND_MSG, True),
                              ("r3", main.LINK_REMIND_MSG.format(link("r3")), False)]), out
out.clear()
messenger.remind()
assert out == [], out
print("OK  follow reminder after 3h, join-link reminder if no real tap, each only once, only inside 24h")

# --- webhook signature: Instagram's and the Meta app's secret both work ---
from fastapi.testclient import TestClient

main.process = lambda payload: None
client = TestClient(main.app)
body = json.dumps({"object": "page", "entry": []}).encode()
sign = lambda secret: "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
for secret, want in (("fbsecret", 200), ("igsecret", 200), ("wrong", 403)):
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(secret),
                                                        "Content-Type": "application/json"})
    assert r.status_code == want, (secret, r.status_code)
print("OK  webhook accepts Facebook and Instagram signatures, rejects others")
