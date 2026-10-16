import csv
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import cache

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

load_dotenv()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
APP_SECRET = os.getenv("APP_SECRET", "")
# Instagram webhooks are signed with the Instagram app secret above, Facebook Page (Messenger) ones with the
# Meta app's own secret (App settings > Basic); see messenger.py
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")
WORKSHOP_LINK = os.getenv("WORKSHOP_LINK", "")
# set while the workshop is live: followers get this Meet link, and date-based reminders stop
MEET_URL = os.getenv("MEET_URL", "").strip()
KEYWORD = os.getenv("KEYWORD", "workshop").strip().lower()  # plain DMs only; "*" = any DM triggers
# Comments / story replies only trigger on media posted at or after this time; ads always trigger. Empty = all media.
START_AFTER = os.getenv("START_AFTER", "").strip()
START_AFTER = datetime.fromisoformat(START_AFTER).astimezone() if START_AFTER else None

API = "https://graph.instagram.com/v23.0"
DB = "bot.db"
# plain-text how-to sent before each button message in DMs (not on comment replies), at most once per RULES_EVERY
RULES_MSG = ("Link paane ke 3 simple steps 👇\n"
             "1️⃣ Neeche \"Visit Profile\" button dabao\n"
             "2️⃣ Profile pe \"Follow\" button dabao\n"
             "3️⃣ Wapas yahan aao aur \"I'm following ✅\" dabao (ya bas DONE likh do)\n"
             "Bas! Link turant aa jayega ✨")
OPENER_MSG = "Hey! Aapka swagat hai 😊\n\nProfile follow karo aur neeche button dabao, link turant aa jayega ✨"
FOLLOW_MSG = "Bas ek step baaki hai! Profile pe jaake Follow karo, phir neeche wala button dabao 😄"
FOLLOW_TEXT = "Pehle is account ko follow karo, phir yahan DONE likh do, link turant aa jayega."  # if buttons fail
NOT_FOLLOWING_MSG = "Follow detect nahi hua 😅 Please dobara try karo: \"Visit Profile\" → Follow → phir \"I'm following ✅\" dabao"
NOT_FOLLOWING_TEXT = "Follow detect nahi hua 😅 Please dobara try karo: account follow karo, phir yahan DONE likh do."
# sent instead of RULES_MSG on someone's very first DM (or button tap): what the workshop is, plus the steps
INTRO_MSG = ("🪔 FREE online Resin Pooja Thali workshop\n"
             "📅 18 Oct, 3 baje se (~3 ghante)\n\n"
             "Link ke liye: \"Visit Profile\" → Follow karo → wapas aake \"I'm following ✅\" dabao")
if MEET_URL:
    INTRO_MSG = ("🔴 FREE Resin Pooja Thali workshop abhi LIVE hai!\n\n"
                 "Link ke liye: \"Visit Profile\" → Follow karo → wapas aake \"I'm following ✅\" dabao")
CONTACT_NUMBER = os.getenv("CONTACT_NUMBER", "")
WHEN_MSG = "📅 Workshop 18 October (Sunday) ko dopahar 3 baje shuru hoga, lagbhag 3 ghante chalega 🪔"
if MEET_URL:
    WHEN_MSG = "🔴 Workshop abhi LIVE hai! Jaldi join karo 🪔"
FREE_MSG = (f"Haan, workshop bilkul FREE hai 🎉\n"
            f"Material ke liye {CONTACT_NUMBER} pe contact karo, ya khud bhi arrange kar sakte ho 😊")
# the list itself is only shared inside the WhatsApp group (pinned there), to bring people into the group;
# link holders also get their join link again, everyone else gets the follow-gate right after
MATERIAL_MSG = (f"Material list WhatsApp group mein pinned hai 📌 Group join karke dekh lo!\n"
                f"Material ke liye {CONTACT_NUMBER} pe bhi contact kar sakte ho 😊")
MATERIAL_LINK_MSG = "Group ka link 👇\n{}"
# DM questions answered on top of the follow-gate (also for people who already have the link)
FAQS = ((re.compile(r"\b(when|schedule|date|time|timing|kab|kitne baje)\b|कब", re.I), WHEN_MSG),
        (re.compile(r"\b(materials?|saa?ma+n|list|items|requirements?|kya kya)\b|सामान|मटेरियल", re.I), MATERIAL_MSG),
        (re.compile(r"\b(free|fees?|charges?|cost|price|paid|paise)\b|फ्री", re.I), FREE_MSG))
REMIND_MSG = "Reminder 🪔 Free Resin Pooja Thali workshop 18 Oct, 3 baje! Link ke liye profile follow karo, phir neeche button dabao 👇"
# to link holders who haven't tapped their link LINK_REMIND_AFTER later (taps judged like the members dashboard)
LINK_REMIND_MSG = "Group join kiya? 🪔 Workshop 18 Oct, 3 baje hai. Saari updates aur material list group mein hai 👇\n{}"
LINK_REMIND_AFTER = timedelta(hours=3)
REMIND_AFTER = timedelta(hours=3)  # first reminder this long after the follow request
REMIND_FROM = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)  # only follow requests sent after reminders went live
# button titles: Instagram cuts them at 20 characters; RULES_MSG quotes these two
VISIT_TITLE, FOLLOWING_TITLE = "Visit Profile", "I'm following ✅"
GET_LINK = "GET_LINK"  # postback payload of the old opener's "Send me the link" button
CHECK_FOLLOW = "CHECK_FOLLOW"  # postback payload of the "I'm following" button
RECHECK_DELAYS = (2, 3)  # seconds; a fresh follow can take a moment to show up in the API
COMMENT_REPLY = "Aapko DM bheja hai, inbox check karo! 📩"
# the user's personal /j/<code> link, so taps can be matched to WhatsApp joins: plain link first
# (Instagram web doesn't show buttons), then the same link as a button
LINK_MSG = "Thanks for following! 🎉\n\nWhatsApp group join karne ke liye is link pe tap karo 👇\n{}"
LINK_BUTTON_MSG = "Ya phir neeche button dabao 👇"
JOIN_TITLE = "Join WhatsApp group"
MEET_MSG = "🔴 Workshop LIVE hai! Abhi join karo 👇\n{}"
MEET_BUTTON_MSG = "Ya neeche button dabao 👇"
MEET_TITLE = "Join workshop 🔴"
RULES_EVERY = timedelta(hours=24)
WINDOW = timedelta(hours=23)  # stay inside Instagram's 24h messaging window
RECHECK_EVERY = 600  # seconds between background follow re-checks
AUDIT_EVERY = 2 * 3600  # seconds between follow checks of everyone who got the link (catches unfollows)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=recheck_loop, daemon=True).start()
    threading.Thread(target=audit_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
# added later to users: rules_sent_at = last RULES_MSG, asked_at = last follow-request buttons sent by DM,
# last_msg_at = last DM or button tap from the user (opens the 24h window)
# follow_checked_at = last follow lookup, unfollowed_at = when an audit first found a link holder not following
# reminded_at / link_reminded_at = when REMIND_MSG / LINK_REMIND_MSG went out; each person gets only one of them, once
# meet_sent_at = last Meet link sent, live_sent_at = when live.py's "workshop is live" broadcast reached them
USER_EXTRA_COLS = ("rules_sent_at", "asked_at", "last_msg_at", "follow_checked_at", "unfollowed_at", "reminded_at",
                   "link_reminded_at", "meet_sent_at", "live_sent_at")


def db():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS sent (igsid TEXT PRIMARY KEY, username TEXT, sent_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    # everyone who triggered the bot, whether or not they followed; read by report.py
    conn.execute("CREATE TABLE IF NOT EXISTS users (igsid TEXT PRIMARY KEY, username TEXT, "
                 "first_seen TEXT, last_seen TEXT, follows INTEGER)")
    # personal join links: one code per user; every tap of /j/<code> is logged in clicks
    conn.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, igsid TEXT UNIQUE, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS clicks (code TEXT, igsid TEXT, clicked_at TEXT, user_agent TEXT)")
    if "ip" not in {row[1] for row in conn.execute("PRAGMA table_info(clicks)")}:
        conn.execute("ALTER TABLE clicks ADD COLUMN ip TEXT")  # tells Meta's link scanners apart from people
    have = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col in USER_EXTRA_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    return conn


def now():
    return datetime.now(timezone.utc)


def first_time(event_id):
    """True the first time an event id (comment id / message mid) is seen; blocks Meta retries."""
    with db() as conn:
        return conn.execute("INSERT OR IGNORE INTO seen VALUES (?)", (event_id,)).rowcount == 1


def already_sent(igsid):
    with db() as conn:
        return conn.execute("SELECT 1 FROM sent WHERE igsid = ?", (igsid,)).fetchone() is not None


def record_sent(igsid, username):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?)",
                     (igsid, username, datetime.now(timezone.utc).isoformat()))


def record_user(igsid, username, follows=None):
    """Upsert a user who triggered the bot; follows is the last known follow status (None = unknown)."""
    at = now().isoformat()
    with db() as conn:
        conn.execute("INSERT INTO users (igsid, username, first_seen, last_seen, follows) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(igsid) DO UPDATE SET "
                     "username = COALESCE(excluded.username, username), last_seen = excluded.last_seen, "
                     "follows = COALESCE(excluded.follows, follows)",
                     (igsid, username, at, at, None if follows is None else int(follows)))


def update_user(igsid, **cols):
    with db() as conn:
        conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in cols)} WHERE igsid = ?",
                     (*cols.values(), igsid))


def first_dm(igsid):
    """True if this user never DMed us or tapped a button before (a comment doesn't count)."""
    with db() as conn:
        row = conn.execute("SELECT last_msg_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    return not (row and row[0])


def faq_answers(text):
    answers = [answer for pattern, answer in FAQS if pattern.search(text)]
    if MATERIAL_MSG in answers and FREE_MSG in answers:
        answers.remove(FREE_MSG)  # "material cost?" -> MATERIAL_MSG already has the contact number
    return answers


def send_rules(igsid, first=False):
    """DM the plain-text steps (INTRO_MSG on a first DM) unless this user already got them within RULES_EVERY."""
    with db() as conn:
        row = conn.execute("SELECT rules_sent_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    if row and row[0] and datetime.fromisoformat(row[0]) > now() - RULES_EVERY:
        return False
    if send({"id": igsid}, INTRO_MSG if first else RULES_MSG):
        update_user(igsid, rules_sent_at=now().isoformat())
        return True
    return False


def follow_buttons():
    return [{"type": "web_url", "url": profile_url(), "title": VISIT_TITLE},
            {"type": "postback", "title": FOLLOWING_TITLE, "payload": CHECK_FOLLOW}]


def ask_to_follow(igsid, recipient, text, fallback, first=False):
    """Send the follow-request buttons (plain-text fallback if they fail); in DMs, the rules go first.
    A comment gets only one private reply, so commenters see the rules once they answer in the DM."""
    dm = "id" in recipient
    if dm:
        send_rules(igsid, first)
    if send(recipient, text, follow_buttons()) or send(recipient, fallback):
        if dm:
            update_user(igsid, asked_at=now().isoformat())
        return True
    return False


def personal_link(igsid):
    """This user's tracked join link (same code every time), served through the current tunnel."""
    with db() as conn:
        row = conn.execute("SELECT code FROM links WHERE igsid = ?", (igsid,)).fetchone()
        code = row[0] if row else secrets.token_urlsafe(6)
        if not row:
            conn.execute("INSERT INTO links VALUES (?, ?, ?)", (code, igsid, datetime.now(timezone.utc).isoformat()))
    base = open("webhook_url.txt").read().strip().removesuffix("/webhook")
    return f"{base}/j/{code}"


def send_meet(igsid, recipient):
    """The live workshop's Meet link (plain link first, then as a button in DMs)."""
    sent = send(recipient, MEET_MSG.format(MEET_URL))
    if "id" in recipient:
        sent = send(recipient, MEET_BUTTON_MSG, [{"type": "web_url", "url": MEET_URL, "title": MEET_TITLE}]) or sent
    if sent:
        update_user(igsid, meet_sent_at=now().isoformat())
    return sent


def meet_sent(igsid):
    with db() as conn:
        row = conn.execute("SELECT meet_sent_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    return bool(row and row[0])


def send_link(igsid, recipient):
    """A comment allows only one private reply, so commenters get just the link message
    (the Meet link while the workshop is live; DMs get it first, then the WhatsApp link)."""
    if MEET_URL:
        sent = send_meet(igsid, recipient)
        if "comment_id" in recipient:
            return sent
    url = personal_link(igsid)
    sent = send(recipient, LINK_MSG.format(url))
    if "id" in recipient:
        sent = send(recipient, LINK_BUTTON_MSG, [{"type": "web_url", "url": url, "title": JOIN_TITLE}]) or sent
    return sent


def api(method, path, **kwargs):
    r = httpx.request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"},
                      timeout=15, **kwargs)
    if r.is_error:
        log.error("API %s /%s failed: %s %s", method, path, r.status_code, r.text)
        return None
    return r.json()


def check_follow(igsid):
    """Returns (follows, username); follows is None when the lookup fails."""
    data = api("GET", igsid, params={"fields": "is_user_follow_business,username"})
    if not data:
        return None, None
    return bool(data.get("is_user_follow_business")), data.get("username")


@cache
def profile_url():
    data = api("GET", "me", params={"fields": "username"})
    return f"https://www.instagram.com/{data['username']}/" if data else "https://www.instagram.com/"


def send(recipient, text, buttons=None):
    message = {"text": text}
    if buttons:
        message = {"attachment": {"type": "template", "payload": {
            "template_type": "button", "text": text, "buttons": buttons}}}
    return api("POST", f"{IG_USER_ID}/messages", json={"recipient": recipient, "message": message}) is not None


def handle(igsid, username, recipient, claimed=False):
    """Follow-gate one user; returns True if a DM went out.
    recipient is {"comment_id": ...} for comments, {"id": ...} for DMs.
    claimed: the user says they followed ("I'm following" tap or DONE), so re-check before saying no."""
    first = "id" in recipient and first_dm(igsid)
    record_user(igsid, username)
    if "id" in recipient:
        update_user(igsid, last_msg_at=now().isoformat())
    has_link = already_sent(igsid)
    # while live, link holders get the Meet link once (again whenever they tap "I'm following" or say DONE)
    if has_link and not (MEET_URL and (claimed or not meet_sent(igsid))):
        log.info("skip %s: already has the link", username or igsid)
        return False
    follows, api_username = check_follow(igsid)
    if claimed:
        for delay in RECHECK_DELAYS:
            if follows is not False:
                break
            time.sleep(delay)
            follows, api_username = check_follow(igsid)
    username = api_username or username
    record_user(igsid, username, follows)
    if follows:
        if has_link:
            if send_meet(igsid, recipient):
                log.info("meet link sent to %s", username or igsid)
                return True
            return False
        if send_link(igsid, recipient):
            record_sent(igsid, username)
            log.info("link sent to %s", username or igsid)
            return True
        return False
    if follows is None:
        # Instagram hides follow status (error 230) until the user interacts; a button tap counts.
        # Same buttons as the follow request so they match the steps in RULES_MSG.
        kind, text, fallback = "opener", OPENER_MSG, FOLLOW_TEXT
    elif claimed:
        kind, text, fallback = "follow not detected", NOT_FOLLOWING_MSG, NOT_FOLLOWING_TEXT
    else:
        kind, text, fallback = "follow request", FOLLOW_MSG, FOLLOW_TEXT
    if ask_to_follow(igsid, recipient, text, fallback, first):
        log.info("sent %s to %s", kind, username or igsid)
        return True
    return False


def pending_follows():
    """Users asked to follow by DM within WINDOW who still have no link and messaged within WINDOW."""
    cutoff = (now() - WINDOW).isoformat()
    with db() as conn:
        return conn.execute("SELECT igsid, username, asked_at, reminded_at FROM users WHERE asked_at >= ? "
                            "AND last_msg_at >= ? AND igsid NOT IN (SELECT igsid FROM sent)", (cutoff, cutoff)).fetchall()


def due_reminder(asked_at, reminded_at):
    asked = datetime.fromisoformat(asked_at)
    return not reminded_at and REMIND_FROM <= asked <= now() - REMIND_AFTER


def recheck_follows():
    """Send the link to anyone who followed after we asked, without waiting for a tap or DONE;
    everyone else gets one reminder ever, while the 24h window is open."""
    for igsid, username, asked_at, reminded_at in pending_follows():
        follows, api_username = check_follow(igsid)
        username = api_username or username
        if follows is not None:
            update_user(igsid, follows=int(follows), username=username)
        if follows:
            if not already_sent(igsid) and send_link(igsid, {"id": igsid}):
                record_sent(igsid, username)
                log.info("link sent to %s (follow found by background re-check)", username or igsid)
        elif not MEET_URL and due_reminder(asked_at, reminded_at) and send({"id": igsid}, REMIND_MSG, follow_buttons()):
            update_user(igsid, reminded_at=now().isoformat())
            log.info("sent reminder to %s", username or igsid)
        time.sleep(1)  # keep the API calls modest


def remind_untapped():
    """Link holders (links sent after REMIND_FROM) with no real tap LINK_REMIND_AFTER later get their link
    once more, while the 24h window is open, unless they already got the follow reminder (one reminder per person)."""
    if MEET_URL:
        return  # LINK_REMIND_MSG talks about 3 baje; the live broadcast replaces it
    from dashboard import real_taps  # same rules as the dashboard: Meta's scanners and the owner's phone don't count
    with db() as conn:
        due = conn.execute("SELECT s.igsid, s.username FROM sent s JOIN users u USING (igsid) "
                           "WHERE s.sent_at >= ? AND s.sent_at <= ? AND u.last_msg_at >= ? AND u.link_reminded_at IS NULL "
                           "AND u.reminded_at IS NULL",
                           (REMIND_FROM.isoformat(), (now() - LINK_REMIND_AFTER).isoformat(),
                            (now() - WINDOW).isoformat())).fetchall()
        clicks = conn.execute("SELECT igsid, clicked_at, COALESCE(user_agent, ''), ip FROM clicks "
                              "WHERE igsid IS NOT NULL").fetchall()
    tapped = real_taps(clicks)
    for igsid, username in due:
        if igsid not in tapped and send({"id": igsid}, LINK_REMIND_MSG.format(personal_link(igsid))):
            update_user(igsid, link_reminded_at=now().isoformat())
            log.info("sent join-link reminder to %s (no tap yet)", username or igsid)


def recheck_loop():
    while True:
        time.sleep(RECHECK_EVERY)
        try:
            recheck_follows()
        except Exception:
            log.exception("background follow re-check failed")
        try:
            remind_untapped()
        except Exception:
            log.exception("join-link reminder failed")
        try:
            import messenger
            messenger.remind()
        except Exception:
            log.exception("Messenger reminder failed")


def audit_follows():
    """Re-check everyone who got the link; records unfollows (and re-follows) for the members dashboard."""
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (igsid, username, first_seen, last_seen) "
                     "SELECT igsid, username, sent_at, sent_at FROM sent")
        people = conn.execute("SELECT u.igsid, u.username, u.follows FROM users u JOIN sent s USING (igsid)").fetchall()
    for igsid, username, before in people:
        follows, api_username = check_follow(igsid)
        if follows is not None:
            cols = dict(follows=int(follows), follow_checked_at=now().isoformat(), username=api_username or username)
            if not follows and before != 0:
                cols["unfollowed_at"] = now().isoformat()
                log.info("unfollow detected: %s", api_username or username or igsid)
            elif follows:
                cols["unfollowed_at"] = None
            update_user(igsid, **cols)
        time.sleep(2)  # ~350 people -> ~12 minutes, well under Instagram's rate limits


def audit_loop():
    while True:
        try:
            audit_follows()
        except Exception:
            log.exception("follow audit failed")
        time.sleep(AUDIT_EVERY)


def matches(text):
    return KEYWORD == "*" or KEYWORD in text.lower()


_posted_at = {}  # media id -> post time; only successful lookups are cached


def is_new(media_id):
    """True if our post/reel/story was published at or after START_AFTER."""
    if not START_AFTER:
        return True
    if media_id not in _posted_at:
        data = api("GET", media_id, params={"fields": "timestamp"})
        if not data or "timestamp" not in data:
            return False
        _posted_at[media_id] = datetime.strptime(data["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    return _posted_at[media_id] >= START_AFTER


def process(payload):
    if payload.get("object") == "page":  # Facebook Page messages (Messenger)
        import messenger
        messenger.process(payload)
        return
    for entry in payload.get("entry", []):
        log.info("webhook received: object=%s fields=%s messaging=%d", payload.get("object"),
                 [c.get("field") for c in entry.get("changes", [])], len(entry.get("messaging", [])))
        me = {IG_USER_ID, str(entry.get("id", ""))}
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            v = change.get("value", {})
            sender = v.get("from", {})
            media = v.get("media", {})
            if sender.get("id") in me or not first_time(v["id"]):
                continue
            is_ad = media.get("ad_id") or media.get("media_product_type") == "AD"
            if not is_ad and not is_new(media.get("id")):
                log.info("skip comment on media=%s: posted before START_AFTER", media.get("id"))
                continue
            log.info("comment trigger from %s: %r", sender.get("username"), v.get("text"))
            if handle(sender["id"], sender.get("username"), {"comment_id": v["id"]}):
                # public reply; our own reply comes back as a webhook but is skipped via `me`
                if api("POST", f"{v['id']}/replies", params={"message": COMMENT_REPLY}):
                    log.info("replied publicly to comment %s", v["id"])

        for m in entry.get("messaging", []):
            msg, pb = m.get("message", {}), m.get("postback", {})
            sid = m.get("sender", {}).get("id")
            if msg.get("is_echo") or sid in me:
                continue
            if pb.get("payload") in (GET_LINK, CHECK_FOLLOW) and first_time(pb.get("mid", "")):
                log.info("button tap from %s: %r", sid, pb.get("title"))
                # "I'm following" buttons sent before CHECK_FOLLOW existed still carry GET_LINK
                claimed = pb["payload"] == CHECK_FOLLOW or pb.get("title", "").startswith("I'm following")
                handle(sid, None, {"id": sid}, claimed)
                continue
            text = msg.get("text", "").strip()
            # click-to-message ads attach a referral (inside the message, or alone as messaging_referral)
            ad = (msg.get("referral") or m.get("referral") or {}).get("ad_id")
            story = msg.get("reply_to", {}).get("story", {}).get("id")
            if not (text or ad) or not first_time(msg.get("mid") or f"ref:{sid}:{m.get('timestamp')}"):
                continue
            if ad or (story and is_new(story)) or text.upper() == "DONE" or matches(text):
                log.info("DM trigger from %s (ad=%s story=%s): %r", sid, ad, story, text)
                for answer in faq_answers(text):
                    if answer == MATERIAL_MSG and already_sent(sid):
                        answer += "\n\n" + MATERIAL_LINK_MSG.format(personal_link(sid))
                    if send({"id": sid}, answer):
                        log.info("answered question from %s", sid)
                handle(sid, None, {"id": sid}, claimed=text.upper() == "DONE")


PREVIEW_BOTS = ("facebookexternalhit", "facebot", "whatsapp", "bot", "crawler", "preview")


@app.get("/j/{code}")
def join(code: str, request: Request):
    """Log the tap of a personal link, then forward to the WhatsApp group."""
    ua = request.headers.get("user-agent", "")
    if not any(b in ua.lower() for b in PREVIEW_BOTS):  # link previews aren't real clicks
        with db() as conn:
            row = conn.execute("SELECT igsid FROM links WHERE code = ?", (code,)).fetchone()
            ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
            conn.execute("INSERT INTO clicks (code, igsid, clicked_at, user_agent, ip) VALUES (?, ?, ?, ?, ?)",
                         (code, row[0] if row else None, datetime.now(timezone.utc).isoformat(), ua, ip))
        log.info("join link tapped: code=%s igsid=%s", code, row[0] if row else None)
    return RedirectResponse(WORKSHOP_LINK, status_code=302)


@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(q.get("hub.verify_token", ""), VERIFY_TOKEN):
        log.info("webhook verification OK")
        return PlainTextResponse(q.get("hub.challenge", ""))
    log.warning("rejected GET /webhook: mode or verify token mismatch")
    raise HTTPException(403)


@app.post("/webhook")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not any(secret and hmac.compare_digest(signature, "sha256=" + hmac.new(secret.encode(), body,
                                                                              hashlib.sha256).hexdigest())
               for secret in (APP_SECRET, FB_APP_SECRET)):
        log.warning("rejected POST /webhook: bad signature")
        raise HTTPException(403)
    tasks.add_task(process, await request.json())  # reply 200 fast; Meta retries slow webhooks
    return "ok"


if __name__ == "__main__" and sys.argv[1:] == ["export"]:
    with db() as conn, open("sent_users.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["igsid", "username", "sent_at"])
        w.writerows(conn.execute("SELECT igsid, username, sent_at FROM sent ORDER BY sent_at"))
    print("wrote sent_users.csv")
