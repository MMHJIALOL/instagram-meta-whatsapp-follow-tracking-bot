"""Offline check of workshops, editable messages, unfollow warnings and the activity feed.
python test_workshops.py (no network, no real DMs, a throwaway database)."""
import os
import tempfile
import time
from datetime import timedelta

os.environ.update(WORKSHOP_SINCE="2026-10-18T22:17:00+05:30", KEYWORD="*", IG_USER_ID="me", MEET_URL="",
                  PAGE_ID="page1", PAGE_ACCESS_TOKEN="test", WA_MAIN_CHAT="22 Oct Group")
import main
import messenger
import dashboard

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
main.WORKSHOP_LINK = "https://chat.whatsapp.com/OCT22"
main.RECHECK_DELAYS = ()
main.REMIND_FROM = main.now() - timedelta(days=30)
main.WS_TTL = 0  # see every change at once (the bot itself caches for 2 s)
main.profile_url = lambda: "https://www.instagram.com/me/"
dashboard.WARN_GAP = 0
out = []  # (recipient, text, buttons)
sent_ok = {"value": True}


def fake_send(recipient, text, buttons=None):
    out.append((recipient, text, [b["title"] for b in buttons or []]))
    if not sent_ok["value"]:
        main._err.last = {"status": 400, "code": 10, "subcode": 2534022, "message": "outside window"}
    main.note_send("ig", recipient.get("id") or getattr(main._ctx, "who", None), text, buttons, sent_ok["value"])
    return sent_ok["value"]


main.send = fake_send
follows = {}
main.check_follow = lambda igsid: (follows.get(igsid, True), "user_" + igsid)
main.api = lambda method, path, **kw: {}
messenger.send = lambda psid, text, buttons=None: out.append(({"psid": psid}, text, [b["title"] for b in buttons or []])) or True
messenger.lookup_name = lambda psid: ""
ago = lambda h: (main.now() - timedelta(hours=h)).isoformat()

from fastapi.testclient import TestClient

bot = TestClient(main.app)
dash = TestClient(dashboard.app, headers={"x-local": "1"})

fails = 0
def check(ok, what):
    global fails
    fails += not ok
    print("OK  " if ok else "FAIL", what)


n = 0
def dm(sid, text):
    global n
    n += 1
    out.clear()
    main.process({"entry": [{"id": "me", "messaging": [{"sender": {"id": sid}, "message": {"mid": f"m{n}", "text": text}}]}]})
    return [t for _, t, _ in out]


def tap(sid):
    global n
    n += 1
    out.clear()
    main.process({"entry": [{"id": "me", "messaging": [{"sender": {"id": sid}, "postback": {
        "mid": f"p{n}", "payload": main.CHECK_FOLLOW, "title": "I'm following ✅"}}]}]})
    return [t for _, t, _ in out]


def comment(sid):
    global n
    n += 1
    out.clear()
    main.process({"entry": [{"id": "me", "changes": [{"field": "comments", "value": {
        "id": f"c{n}", "text": "info", "from": {"id": sid, "username": "x"}, "media": {"id": "ad", "ad_id": "1"}}}]}]})
    return [t for _, t, _ in out]


link = lambda texts: any("/j/" in t for t in texts)

# --- no workshops yet: .env, exactly as before ---
check(main.workshops() == [] and main.workshop()["wa_link"] == main.WORKSHOP_LINK, "no workshops: .env values")
check(main.text("INTRO_MSG") == main.INTRO_MSG, "...and main.py's texts")
dashboard.ensure_workshops()
ws = main.workshops()
check(len(ws) == 1 and ws[0]["wa_group"] == "22 Oct Group" and ws[0]["started"] == main.PAST_BEFORE
      and ws[0]["wa_link"] == main.WORKSHOP_LINK, "ensure_workshops: the running workshop from .env")
dashboard.ensure_workshops()
check(len(main.workshops()) == 1, "...only once")
wid = ws[0]["id"]

# --- editing messages ---
r = dash.post("/api/messages", json={"key": "LINK_MSG", "text": "Naya text without link"})
check(r.status_code == 400 and "{link}" in r.json()["detail"], "link message without {link} is refused")
r = dash.post("/api/messages", json={"key": "VISIT_TITLE", "text": "A very long button title here"})
check(r.status_code == 400, "button title over 20 characters is refused")
check(dash.post("/api/messages", json={"key": "FOLLOW_MSG", "text": "  "}).status_code == 400, "empty text is refused")
check(dash.post("/api/messages", json={"key": "FOLLOW_MSG", "text": "Follow karo {link}"}).status_code == 400,
      "{link} in a message that has no link is refused (it would go out as written)")
check(dash.post("/api/messages", json={"key": "LINK_MSG", "text": "Yeh lo link 👉 {link} 🎉"}).status_code == 200,
      "link message with {link} saves")
check(dash.post("/api/messages", json={"key": "FOLLOW_MSG", "text": "Follow karo please 🙏"}).status_code == 200,
      "follow request saves")
texts = dm("new1", "hello")  # follows (default) -> link
check(texts[0].startswith("Yeh lo link 👉 https://") and texts[0].endswith(" 🎉") and "/j/" in texts[0],
      "edited link message reaches the DM with the link filled in")
follows["new2"] = False
texts = dm("new2", "hello")
check(texts == [main.INTRO_MSG, "Follow karo please 🙏"], "edited follow request reaches the DM (intro unchanged)")
r = dash.post("/api/messages", json={"key": "FOLLOW_MSG", "text": main.FOLLOW_MSG})
check(r.status_code == 200 and "FOLLOW_MSG" not in main.latest_workshop()["texts"],
      "saving the default text again drops the edit")
msgs = dash.get("/api/messages").json()
check(len(msgs["messages"]) == len(main.bot_messages.CATALOG) and msgs["workshop"]["id"] == wid,
      "messages API lists every message")
check(dash.post("/api/messages", json={"key": "VISIT_TITLE", "text": "Open Profile"}).json()["warning"],
      "renaming a button the steps quote gives a warning")
dash.post("/api/messages", json={"key": "VISIT_TITLE", "text": main.VISIT_TITLE})

# --- questions ---
faqs = msgs["faqs"]
check([f["id"] for f in faqs] == ["when", "recording", "material", "free", "class"], "the five built-in questions")
faqs[0]["keywords"] += ["din"]
faqs.append({"label": "Location", "keywords": ["kahan", "location", "jagah"], "on": True,
             "answer": "Workshop online hai, Google Meet pe 💻"})
faqs[3]["on"] = False
r = dash.post("/api/faqs", json={"faqs": faqs})
check(r.status_code == 200, "question list saves")
check(main.faq_answers("kis din hai?") == [main.WHEN_MSG], "added keyword works")
check(main.faq_answers("Location kya hai") == ["Workshop online hai, Google Meet pe 💻"], "added question works")
check(main.faq_answers("is it free?") == [], "switched-off question stays quiet")
check(main.faq_answers("recording milegi?") == [main.RECORDING_MSG], "wildcard keyword record* still works")
t = dash.post("/api/messages/test", json={"text": "jagah aur date?"}).json()
check([a["id"] for a in t["answers"]][0] == "when" and t["answers"][1]["label"] == "Location", "message tester")
bad = [dict(f) for f in faqs if f.get("id") != "free"]
check(dash.post("/api/faqs", json={"faqs": bad}).status_code == 400, "built-in questions can't be deleted")
texts = dm("new1", "location?")
check(texts == ["Workshop online hai, Google Meet pe 💻"], "custom answer in a real DM (link holder: just the answer)")
faqs = dash.get("/api/messages").json()["faqs"]
faqs[3]["on"] = True
dash.post("/api/faqs", json={"faqs": faqs})

# --- custom replies: a link button, and "stop here" (no follow step after it) ---
base = dash.get("/api/messages").json()["faqs"]
addr = {"label": "Address", "keywords": ["address", "pata"], "on": True, "answer": "Hum online hain, koi address nahi 🙏",
        "button_title": "Our Instagram", "button_url": "https://www.instagram.com/me/", "stop": True}
for wrong, why in (({**addr, "button_url": ""}, "a button title without a link"),
                   ({**addr, "button_title": "A much too long button title"}, "a button title over 20 characters"),
                   ({**addr, "button_url": "www.example.com"}, "a button link without https://"),
                   ({**addr, "answer": "Link: {link}"}, "{link} in a custom reply"),
                   ({**addr, "keywords": []}, "no words to watch for")):
    check(dash.post("/api/faqs", json={"faqs": base + [wrong]}).status_code == 400, f"custom reply refused: {why}")
check(dash.post("/api/faqs", json={"faqs": base + [addr]}).status_code == 200,
      "custom reply with a button and 'stop here' saves")
follows["c1"] = False
texts = dm("c1", "aapka address kya hai?")
check(texts == ["Hum online hain, koi address nahi 🙏"] and out[0][2] == ["Our Instagram"],
      "stop here: only the reply, with its button, no follow step")
check(main.db().execute("SELECT last_msg_at FROM users WHERE igsid = 'c1'").fetchone()[0],
      "...their DM still counts for the 24 h window")
texts = dm("c1", "location?")
check(texts[0] == "Workshop online hai, Google Meet pe 💻" and main.FOLLOW_MSG in texts,
      "carry on as usual: the reply, then the follow step")
t = dash.post("/api/messages/test", json={"text": "address please"}).json()
check(t["answers"][0]["stop"] and t["answers"][0]["custom"] and t["answers"][0]["button"] == "Our Instagram",
      "tester shows 'stop here' and the button")
saved = next(f for f in dash.get("/api/messages").json()["faqs"] if f["label"] == "Address")
check(saved["stop"] is True and saved["button_url"] == "https://www.instagram.com/me/", "...and the page gets them back")

# --- go live / end live without a restart ---
r = dash.post(f"/api/workshops/{wid}", json={"meet_url": "https://meet.google.com/abc"})
check(r.status_code == 200 and main.meet_url() == "https://meet.google.com/abc", "Go live sets the class link")
check(dm("new1", "kab hai")[:2] == [main.WHEN_LIVE_MSG, main.MEET_MSG.format("https://meet.google.com/abc")],
      "live: the 'when' answer switches and link holders get the class link")
dash.post(f"/api/workshops/{wid}", json={"meet_url": ""})
check(main.meet_url() == "" and dm("new1", "kab hai") == [main.WHEN_MSG], "End live: back to normal")
check(dash.post(f"/api/workshops/{wid}", json={"wa_link": "chat.whatsapp.com/x"}).status_code == 400,
      "invite link must be https://")

# --- unfollow warning ---
main.record_user("u1", "u1")
main.record_sent("u1", "u1")
main.update_user("u1", follows=0, unfollowed_at=ago(1), last_msg_at=ago(2))
follows["u1"] = False
out.clear()
r = dash.post("/api/warn", json={"igsids": ["u1"]})
for _ in range(50):
    if not dashboard._warn["running"]:
        break
    time.sleep(0.05)
check(r.json()["started"] and out and out[0][1] == main.UNFOLLOW_WARN_MSG and out[0][2] == [main.VISIT_TITLE,
      main.FOLLOWING_TITLE], "Send warning: the warning with the follow buttons")
row = main.db().execute("SELECT warned_at, warn_error FROM users WHERE igsid = 'u1'").fetchone()
check(row[0] and row[1] is None and main.warned("u1"), "...recorded as warned")
check(tap("u1") == [main.RULES_MSG, main.NOT_FOLLOWING_MSG],
      "warned, taps I'm following but still not following: the steps and the buttons again")
follows["u1"] = True
check(tap("u1") == [main.REFOLLOW_MSG], "follows again and taps: thank-you message")
row = main.db().execute("SELECT unfollowed_at, follows FROM users WHERE igsid = 'u1'").fetchone()
check(row == (None, 1) and not main.warned("u1"), "...unfollow cleared, out of the To remove list")
check(tap("u1") == [], "a second tap: plain link holder again (no reply)")

main.record_user("u2", "u2")
main.record_sent("u2", "u2")
main.update_user("u2", follows=0, unfollowed_at=ago(1), last_msg_at=ago(50))
sent_ok["value"] = False
dash.post("/api/warn", json={"igsids": ["u2"]})
for _ in range(50):
    if not dashboard._warn["running"]:
        break
    time.sleep(0.05)
sent_ok["value"] = True
row = main.db().execute("SELECT warned_at, warn_error FROM users WHERE igsid = 'u2'").fetchone()
check(row[0] is None and "24 h window" in row[1], "Instagram refuses (24 h rule): reason kept, not marked warned")
r = dict(next(x for x in dashboard.build()["rows"] if x["igsid"] == "u2"))
check(r["warn_error"] and not r["warned_at"] and r["last_msg_at"], "...and shown on the dashboard row")

# --- start the next workshop ---
main.record_user("old", "old")
main.record_sent("old", "old")
with main.db() as conn:  # got a link and a reminder in the 22 Oct workshop
    conn.execute("UPDATE users SET reminded_at = ?, last_msg_at = ? WHERE igsid = 'old'", (ago(5), ago(5)))
before = main.workshop()
r = dash.post("/api/workshops", json={"name": "29 Oct workshop", "date": "2026-10-29", "wa_group": "29 Oct Group",
                                      "wa_link": "https://chat.whatsapp.com/OCT29", "past_offer": True,
                                      "texts": {"INTRO_MSG": "🪔 Naya workshop 29 Oct!\nVisit Profile → I'm following ✅",
                                                "LINK_MSG": main.LINK_MSG},
                                      "faqs": dash.get("/api/messages").json()["faqs"]})
check(r.status_code == 200, "Start next workshop")
ws = main.workshops()
check(len(ws) == 2 and ws[0]["closed_at"] and ws[0]["stats"]["people"] > 0 and main.workshop()["name"] == "29 Oct workshop",
      "...old one closed with its numbers frozen, new one running")
check(main.faq_answers("Location kya hai") == ["Workshop online hai, Google Meet pe 💻"],
      "...questions carried over to the new workshop")
check(dash.post("/api/workshops", json={"name": "x", "wa_group": "g"}).status_code == 400, "invite link is required")
check(main.past_attendee("old") and not main.already_sent("old"), "22 Oct link holder is now a past attendee")
r = bot.get(main.personal_link("old").replace(open("webhook_url.txt").read().strip().removesuffix("/webhook"), ""),
            follow_redirects=False)
check(r.status_code == 200 and "pichle workshop" in r.text, "their old link shows the earlier-workshop page")
texts = dm("old", "hi")
check(main.PAST_MSG in texts and link(texts), "they follow: offer + new link")
r = bot.get("/j/" + main.personal_link("old").rsplit("/", 1)[1], follow_redirects=False)
check(r.status_code == 200 and "https://chat.whatsapp.com/OCT29" in r.text, "the link now opens the 29 Oct group")
follows["back"] = False
main.record_user("back", "back")
main.update_user("back", last_msg_at=ago(30), reminded_at=ago(30))
texts = dm("back", "hello")
check(texts[0] == "🪔 Naya workshop 29 Oct!\nVisit Profile → I'm following ✅",
      "someone from before DMs again: the new workshop's welcome, not just the steps")
check(main.due_reminder(ago(4), ago(30)), "a reminder from the last workshop doesn't block this one's")
check(dash.get("/api/wa/groups").json()["groups"] == ["29 Oct Group", "22 Oct Group"], "listener watches both groups")
check(dashboard.main_group() == "29 Oct Group" and dashboard.since() == main.workshop()["started"],
      "matching uses the new group and start")
d = dashboard.build()
cur = [x for x in d["rows"] if x["current"]]
check({x["igsid"] for x in cur} >= {"old", "back"} and "u2" not in {x["igsid"] for x in cur},
      "members list: people active since the start are current, the rest under the old workshop")

# --- between workshops ---
check(dash.post("/api/workshops-end").status_code == 200 and main.workshop() is None, "End workshop")
check(dash.post("/api/workshops-end").status_code == 400, "...can't end twice")
texts = dm("someone", "workshop kab hai?")
check(texts == [main.BETWEEN_MSG] and out[0][2] == [main.VISIT_TITLE], "between workshops: the 'no workshop' message, no answers, no link")
check(dm("someone", "hello?") == [], "...once a day")
check(comment("c9") == [main.BETWEEN_MSG], "...comments too (private reply)")
out.clear()
messenger.process({"object": "page", "entry": [{"id": "page1", "messaging": [{"sender": {"id": "fb1"},
                   "recipient": {"id": "page1"}, "message": {"mid": "fbm1", "text": "hi"}}]}]})
check([t for _, t, _ in out] == [main.BETWEEN_MSG], "...and on Messenger")
check(dm("someone3", "address?") == ["Hum online hain, koi address nahi 🙏"],
      "...custom replies still answer between workshops (carried over to the new workshop)")
r = bot.get("/j/" + main.personal_link("old").rsplit("/", 1)[1], follow_redirects=False)
check(r.status_code == 200 and "https://chat.whatsapp.com/OCT29" in r.text, "...links still open the last group")
check(not any(x["current"] for x in dashboard.build()["rows"]), "...nobody is 'current' (To remove stays empty)")
check(dash.post("/api/workshops-reopen").status_code == 200 and main.workshop()["name"] == "29 Oct workshop",
      "Reopen undoes End workshop")

# --- Messenger uses the edited texts in its own wording ---
dash.post("/api/messages", json={"key": "FOLLOW_MSG", "text": "Profile pe jaake follow karo 🙏"})
out.clear()
messenger.process({"object": "page", "entry": [{"id": "page1", "messaging": [{"sender": {"id": "fb2"},
                   "recipient": {"id": "page1"}, "message": {"mid": "fbm2", "text": "hi"}}]}]})
check([t for _, t, _ in out][-1] == "Page pe jaake follow karo 🙏", "Messenger: edited text with Page wording")
out.clear()
messenger.process({"object": "page", "entry": [{"id": "page1", "messaging": [{"sender": {"id": "fb3"},
                   "recipient": {"id": "page1"}, "message": {"mid": "fbm3", "text": "address?"}}]}]})
check(out == [({"psid": "fb3"}, "Hum online hain, koi address nahi 🙏", ["Our Instagram"])],
      "Messenger: custom reply with its button, stop here")

# --- activity feed ---
feed = dash.get("/api/activity").json()
kinds = {r["kind"] for r in feed["rows"]}
check({"in", "out", "fail", "admin", "skip"} <= kinds, f"activity has incoming, outgoing, failed, skipped and your actions ({kinds})")
warn_row = next(r for r in dash.get("/api/activity?kind=problems").json()["rows"])
check(warn_row["who"] == "u2" and "24 h" in warn_row["summary"] and warn_row["name"] == "@u2",
      "a refused warning shows as a problem, with the name")
outs = dash.get("/api/activity?kind=out").json()["rows"]
check(any(r["label"] == "Thanks for following again" for r in outs) and any(r["label"] == "Location" for r in outs),
      "sent messages are labelled with their name (also your own questions)")
latest = feed["latest"]
dm("new1", "kab hai")
check(dash.get(f"/api/activity?after={latest}").json()["rows"][-1]["kind"] == "in", "live updates: only rows after an id")
check(dash.get("/api/activity?who=u1").json()["rows"] and all(r["who"] == "u1" for r in dash.get("/api/activity?who=u1").json()["rows"]),
      "one person's history")
main.job("recheck", started_at=ago(0), ended_at=ago(0), result="3 waiting")
jobs = {j["name"]: j for j in dash.get("/api/jobs").json()["jobs"]}
check(jobs["recheck"]["result"] == "3 waiting" and "audit" in jobs and "listener" in jobs, "background jobs list")
raise SystemExit(1 if fails else 0)
