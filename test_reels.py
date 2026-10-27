"""Offline check of reel offers (dashboard's Reels tab): a reel with its own messages and WhatsApp link.
python test_reels.py (no network, no real DMs, a throwaway database)."""
import os
import tempfile
from datetime import timedelta

os.environ.update(WORKSHOP_SINCE="2026-10-18T22:17:00+05:30", KEYWORD="workshop", IG_USER_ID="me", MEET_URL="",
                  START_AFTER="", PAGE_ID="page1", PAGE_ACCESS_TOKEN="test", WA_MAIN_CHAT="22 Oct Group")
import main
import dashboard
import bot_messages

main.DB = os.path.join(tempfile.mkdtemp(), "test.db")
main.WORKSHOP_LINK = "https://chat.whatsapp.com/OCT22"
main.RECHECK_DELAYS = ()
main.REMIND_FROM = main.now() - timedelta(days=30)
main.WS_TTL = 0
main.profile_url = lambda: "https://www.instagram.com/me/"
main.time.sleep = lambda s: None
with open("webhook_url.txt") as f:
    BASE = f.read().strip().removesuffix("/webhook")
out = []


def fake_send(recipient, text, buttons=None):
    out.append((recipient, text, [b["title"] for b in buttons or []]))
    main.note_send("ig", recipient.get("id") or getattr(main._ctx, "who", None), text, buttons, True)
    return True


main.send = fake_send
follows = {}
main.check_follow = lambda igsid: (follows.get(igsid, True), "user_" + igsid)
MEDIA = [{"id": "111", "permalink": "https://www.instagram.com/reel/OLD1/"},
         {"id": "222", "permalink": "https://www.instagram.com/reel/DIWALI99/"}]


def fake_api(method, path, **kw):
    if path == "me/media":
        return {"data": MEDIA}
    return next((m for m in MEDIA if m["id"] == path), {})


main.api = fake_api

from fastapi.testclient import TestClient

bot = TestClient(main.app)
dash = TestClient(dashboard.app, headers={"x-local": "1"})
fails = 0


def check(ok, what):
    global fails
    fails += not ok
    print("OK  " if ok else "FAIL", what)


n = 0


def run(entry):
    global n
    n += 1
    out.clear()
    main.process({"entry": [{"id": "me", **entry}]})
    return [t for _, t, _ in out]


def comment(sid, media):
    return run({"changes": [{"field": "comments", "value": {"id": f"c{n}", "text": "info", "from": {
        "id": sid, "username": sid}, "media": media}}]})


def dm(sid, text):
    return run({"messaging": [{"sender": {"id": sid}, "message": {"mid": f"m{n}", "text": text}}]})


def tap(sid):
    return run({"messaging": [{"sender": {"id": sid}, "postback": {
        "mid": f"p{n}", "payload": main.CHECK_FOLLOW, "title": "I'm following ✅"}}]})


REEL, AD, POST = {"id": "222"}, {"id": "ad", "ad_id": "1"}, {"id": "333"}
R99 = bot_messages.REEL_99_TEXTS
reel_link = lambda texts: any("/j/r" in t for t in texts)
ws_link = lambda texts: any("/j/" in t and "/j/r" not in t for t in texts)

# ---------- set up, not connected: nothing changes ----------
r = dash.post("/api/reels", json={"name": "₹99 Diwali"})
check(r.status_code == 200, "new reel offer")
rid = r.json()["id"]
rl = dash.get("/api/reels").json()["reels"][0]
check(rl["wa_link"] == bot_messages.REEL_99_LINK and rl["active"] and not rl["media_id"],
      "first reel offer starts with the ₹99 WhatsApp link, on, no reel connected")
texts = comment("a1", REEL)
check(ws_link(texts) and not any("₹99" in t for t in texts),
      "before the reel is connected, a comment on it gets the workshop's flow")

# ---------- connect ----------
r = dash.post(f"/api/reels/{rid}", json={"reel_url": "https://www.instagram.com/reel/NOPE/"})
check(r.status_code == 400 and "isn't on this Instagram account" in r.json()["detail"], "unknown reel is refused")
r = dash.post(f"/api/reels/{rid}", json={"reel_url": "instagram.com/reel/DIWALI99/?igsh=abc"})
check(r.status_code == 200, "connect by link")
rl = dash.get("/api/reels").json()["reels"][0]
check(rl["media_id"] == "222" and rl["reel_url"].endswith("/DIWALI99/"), "reel id found from the link")
r2 = dash.post("/api/reels", json={"name": "Other"}).json()["id"]
check(dash.post(f"/api/reels/{r2}", json={"reel_url": "https://www.instagram.com/reel/DIWALI99/"}).json()["detail"].startswith("That reel is already"),
      "one reel can't be connected to two offers")
dash.delete(f"/api/reels/{r2}")

# ---------- a follower comments on the reel ----------
texts = comment("u1", REEL)
check(len(texts) == 1 and texts[0].startswith(R99["LINK_MSG"].split("{link}")[0]) and reel_link(texts),
      "follower commenting on the reel gets the ₹99 booking link (its own /j/r… link)")
code = texts[0].rsplit("/j/", 1)[1]
r = bot.get(f"/j/{code}", headers={"user-agent": "Mozilla/5.0 Instagram 300.0"}, follow_redirects=False)
check(r.status_code == 302 and r.headers["location"] == bot_messages.REEL_99_LINK, "reel link opens the wa.me booking chat")
with main.db() as conn:
    check(conn.execute("SELECT COUNT(*) FROM reel_clicks").fetchone()[0] == 1
          and conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0] == 0,
          "reel taps are kept apart from the members dashboard's clicks")
check(dash.get("/api/reels").json()["reels"][0]["stats"]["tapped"] == 1, "tap counted on the Reels tab")
texts = comment("u1", REEL)
check(texts == [], "second comment: already has the reel link, no reply")

# ---------- a non-follower: comment, then DMs without the keyword ----------
follows["u2"] = False
texts = comment("u2", REEL)
check(texts == [R99["FOLLOW_MSG"]], "non-follower gets the reel's follow request")
texts = dm("u2", "kitne ka hai?")
check(texts[0].startswith(R99["FREE_MSG"][:30]), "a DM without the keyword still answers (price → ₹99 answer)")
check(R99["INTRO_MSG"] in texts, "first DM gets the reel's welcome")
check(not any("/j/" in t for t in texts), "no link while not following")
check(dm("u2", "hello")[-1] == R99["FOLLOW_MSG"], "any DM from someone in the reel's flow starts the bot")
follows["u2"] = True
texts = tap("u2")
check(reel_link(texts) and texts[0].startswith("Thanks for following! 🎉\n\n₹99"), "tap after following → ₹99 link")
texts = dm("u2", "material kya lagega")
check(texts[0].startswith("🛍️ Material kit: ₹1,000") and reel_link(texts[:1]),
      "material answer is the kit + price, with their booking link under it")
texts = dm("u2", "recording milegi?")
check(texts == [R99["RECORDING_MSG"]], "recording answer, then nothing (already has the link)")

# ---------- someone who already has the workshop link ----------
follows["w1"] = True
check(ws_link(comment("w1", AD)), "w1 gets the workshop link from an ad")
texts = comment("w1", REEL)
check(reel_link(texts), "a workshop link holder still gets the reel's link (separate offers)")
texts = comment("w1", AD)
check(texts == [], "back on the ad: already has the workshop link")
check(dm("w1", "hello") == [], "after an ad, a DM without the keyword is the workshop's again (no reply)")
texts = dm("w1", "workshop kab hai")
check(texts and texts[0] == main.WHEN_MSG, "and questions get the workshop's answers")

# ---------- the workshop flow is untouched ----------
follows["p1"] = True
texts = comment("p1", POST)
check(ws_link(texts) and texts[0].startswith(main.LINK_MSG.split("{}")[0]), "other posts: the workshop's link message")
follows["d1"] = False
texts = dm("d1", "workshop")
check(main.INTRO_MSG in texts and main.FOLLOW_MSG in texts, "plain DM: workshop welcome + follow request")

# ---------- re-check and reminders ----------
follows["u3"] = False
comment("u3", REEL)
dm("u3", "hi")
with main.db() as conn:
    conn.execute("UPDATE reel_people SET asked_at = ? WHERE igsid = 'u3'",
                 ((main.now() - timedelta(hours=4)).isoformat(),))
check("u3" not in {p[0] for p in main.pending_follows()}, "the workshop's re-check leaves reel people alone")
out.clear()
d = main.recheck_reels()
check(d["reminders"] == 1 and out[-1][1] == R99["REMIND_MSG"] and out[-1][2] == ["Visit Profile", "I'm following ✅"],
      "reel follow reminder after 3 h, with the buttons")
out.clear()
check(main.recheck_reels()["reminders"] == 0 and not out, "only once")
follows["u3"] = True
d = main.recheck_reels()
check(d["links"] == 1 and reel_link([t for _, t, _ in out]), "follow found by the re-check → reel link")
with main.db() as conn:
    conn.execute("UPDATE reel_people SET sent_at = ? WHERE igsid = 'u3'",
                 ((main.now() - timedelta(hours=4)).isoformat(),))
out.clear()
d = main.recheck_reels()
check(d["link_reminders"] == 0, "no link reminder for someone who already got the follow reminder")
with main.db() as conn:
    conn.execute("UPDATE reel_people SET sent_at = ? WHERE igsid = 'u2'",
                 ((main.now() - timedelta(hours=4)).isoformat(),))
out.clear()
d = main.recheck_reels()
check(d["link_reminders"] == 1 and out[0][1].startswith("Booking ho gayi?"), "untapped reel link → booking reminder")

# ---------- no follow step ----------
dash.post(f"/api/reels/{rid}", json={"follow_first": False})
follows["n1"] = False
texts = comment("n1", REEL)
check(reel_link(texts), "follow step off: the link goes out straight away")
dash.post(f"/api/reels/{rid}", json={"follow_first": True})

# ---------- Messages tab for the reel ----------
m = dash.get(f"/api/messages?reel={rid}").json()
keys = {x["key"] for x in m["messages"]}
check(m["reel"]["name"] == "₹99 Diwali" and "PAST_MSG" not in keys and "MEET_MSG" not in keys and "LINK_MSG" in keys,
      "reel messages list leaves out live / offer / warning texts")
check(next(x for x in m["messages"] if x["key"] == "WHEN_MSG")["text"] == R99["WHEN_MSG"], "reel texts shown")
check(next(f for f in m["faqs"] if f["id"] == "free")["label"] == "Price / booking?", "price question on the reel")
r = dash.post("/api/messages", json={"key": "WHEN_MSG", "text": "25 Oct, 3 baje!", "reel": rid})
check(r.status_code == 200, "save a reel message")
check(dm("u2", "kab hai") == ["25 Oct, 3 baje!"], "the bot uses the edited reel text")
check(dash.get("/api/messages").json()["messages"][[x["key"] for x in bot_messages.CATALOG].index("WHEN_MSG")]["text"]
      == main.WHEN_MSG, "the workshop's text is unchanged")
t = dash.post("/api/messages/test", json={"text": "price?", "reel": rid}).json()
check(t["triggers"] and t["answers"][0]["id"] == "free", "Try a message works for the reel")
faqs = m["faqs"]
faqs.append({"label": "Kit", "keywords": ["kit"], "on": True, "answer": "Kit ₹1,000 hai", "stop": True})
check(dash.post("/api/faqs", json={"faqs": faqs, "reel": rid}).status_code == 200, "custom reply on the reel")
check(dm("u2", "kit?") == ["Kit ₹1,000 hai"], "reel custom reply answers")
check(dm("w1", "workshop kit?") != ["Kit ₹1,000 hai"], "the workshop doesn't get the reel's custom reply")

# ---------- off ----------
dash.post(f"/api/reels/{rid}", json={"active": False})
follows["z1"] = True
check(ws_link(comment("z1", REEL)), "switched off: comments on the reel get the workshop's flow")
check(dm("u2", "hello") == [], "and its people are back in the workshop's flow")
check(dash.delete(f"/api/reels/{rid}").status_code == 400, "can't delete an offer people went through")
check(dash.post(f"/api/reels/{rid}", json={"active": True, "wa_link": ""}).status_code == 400,
      "can't save without a WhatsApp link")

print("\nall good" if not fails else f"\n{fails} FAILED")
