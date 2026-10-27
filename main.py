import csv
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from functools import cache, lru_cache

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import bot_messages

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
# media ids (comma-separated) whose comments the bot ignores entirely, even ads; the bot still answers DMs
IGNORE_MEDIA = {m.strip() for m in os.getenv("IGNORE_MEDIA", "").split(",") if m.strip()}

API = "https://graph.instagram.com/v23.0"
DB = "bot.db"
# The texts below are the defaults. Each workshop can have its own versions, edited on the dashboard's Messages
# tab (workshops.texts); the bot always goes through text(KEY). bot_messages.CATALOG says when each one is sent.
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
             "📅 22 Oct (Thursday), subah 11 se dopahar 2 baje tak (3 ghante)\n"
             "🎥 Recording bhi available hogi\n\n"
             "Link ke liye: \"Visit Profile\" → Follow karo → wapas aake \"I'm following ✅\" dabao")
# INTRO_MSG and WHEN_MSG while a class link is set (meet_url())
INTRO_LIVE_MSG = ("🔴 FREE Resin Pooja Thali workshop abhi LIVE hai!\n\n"
                  "Link ke liye: \"Visit Profile\" → Follow karo → wapas aake \"I'm following ✅\" dabao")
CONTACT_NUMBER = os.getenv("CONTACT_NUMBER", "")
WHEN_MSG = "📅 Workshop 22 October (Thursday) ko subah 11 baje shuru hoga, dopahar 2 baje tak (3 ghante) chalega 🪔"
WHEN_LIVE_MSG = "🔴 Workshop abhi LIVE hai! Jaldi join karo 🪔"
# "ab kya karna hai" / "what now" / "class kaise hogi"; the live version while a class link is set
CLASS_MSG = "💻 Class online hogi, Google Meet pe! Meet ka link 22 October ko share kiya jayega 😊"
CLASS_LIVE_MSG = "🔴 Class abhi Google Meet pe LIVE hai! Jaldi join karo 🪔"
FREE_MSG = (f"Haan, workshop bilkul FREE hai 🎉\n"
            f"Material ke liye {CONTACT_NUMBER} pe contact karo, ya khud bhi arrange kar sakte ho 😊")
# the list itself is only shared inside the WhatsApp group (pinned there), to bring people into the group;
# link holders also get their join link again, everyone else gets the follow-gate right after
MATERIAL_MSG = (f"Material list WhatsApp group mein pinned hai 📌 Group join karke dekh lo!\n"
                f"Material ke liye {CONTACT_NUMBER} pe bhi contact kar sakte ho 😊")
MATERIAL_LINK_MSG = "Group ka link 👇\n{}"
# people whose join link is from an earlier FREE workshop (sent before WORKSHOP_SINCE, the start of the current
# one) go through the follow-gate again like anyone new; with their new link they also get PAST_MSG (the paid
# workshop offer), once. Their old link doesn't open the new group until then.
PAST_BEFORE = os.getenv("WORKSHOP_SINCE", "").strip()
PAST_BEFORE = datetime.fromisoformat(PAST_BEFORE) if PAST_BEFORE else None
PAST_MSG = ("Welcome back! 🙏 Aap pehle bhi hamara FREE workshop join kar chuke ho.\n\n"
            "🎉 Aapke liye special offer: hamara PAID workshop 25 October ko sirf ₹99 mein!\n"
            "⏳ Offer sirf 21 October tak valid hai\n"
            "🛍️ Material bhi discount pe milega\n"
            f"Book karne ke liye {CONTACT_NUMBER} pe message karo 😊\n\n"
            "22 October ke FREE workshop ka naya link bhi neeche hai 👇")
RECORDING_MSG = ("Haan, workshop ki recording bhi available hogi 🎥\n"
                 "Live join na kar pao toh bhi tension nahi, saari details WhatsApp group mein milengi 😊")
# DM questions answered on top of the follow-gate (also for people who already have the link): the keyword lists
# are bot_messages.FAQ_DEFAULTS, or the workshop's own (workshops.faqs), see faq_matches
REMIND_MSG = "Reminder 🪔 Free Resin Pooja Thali workshop 22 Oct, subah 11 baje! Link ke liye profile follow karo, phir neeche button dabao 👇"
# to link holders who haven't tapped their link LINK_REMIND_AFTER later (taps judged like the members dashboard)
LINK_REMIND_MSG = "Group join kiya? 🪔 Workshop 22 Oct, subah 11 baje hai. Saari updates aur material list group mein hai 👇\n{}"
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
# sent from the dashboard's To remove card (Send warning), with the follow buttons; REFOLLOW_MSG answers a warned
# person who then taps "I'm following ✅" and really follows again
UNFOLLOW_WARN_MSG = ("Hi! 🙏 Humne dekha ki aapne hamara Instagram account unfollow kar diya hai.\n\n"
                     "Workshop ka WhatsApp group sirf followers ke liye hai. Agar aap wapas follow nahi karte, "
                     "toh aapko group se remove kar diya jayega 😕\n\n"
                     "Wapas follow karke neeche \"I'm following ✅\" dabao 👇")
REFOLLOW_MSG = "Thank you! 🙏 Follow wapas mil gaya ✅ Aap WhatsApp group mein bane rahoge 🪔"
# between workshops (ended on the dashboard, next one not started): any DM or comment, at most once per RULES_EVERY
BETWEEN_MSG = ("Namaste 🙏 Abhi koi workshop scheduled nahi hai.\n"
               "Agla workshop jaldi announce hoga! Profile follow karke rakho, saari details yahin milengi 😊")
# pages shown when a personal link is opened: first line is the heading
USED_LINK_PAGE_TEXT = ("Ye link use ho chuka hai\nAap WhatsApp group mein already join ho chuke ho 🪔\n"
                       "Group nahi mil raha? Hume Instagram pe DM karo.")
PAST_LINK_PAGE_TEXT = ("Ye link pichle workshop ka tha\n22 October ke FREE workshop ka naya link paane ke liye\n"
                       "hume Instagram pe DM karo")
RULES_EVERY = timedelta(hours=24)
WINDOW = timedelta(hours=23)  # stay inside Instagram's 24h messaging window
RECHECK_EVERY = 600  # seconds between background follow re-checks
AUDIT_EVERY = 2 * 3600  # seconds between follow checks of everyone who got the link (catches unfollows)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


@asynccontextmanager
async def lifespan(_app):
    import join_sync
    threading.Thread(target=join_sync.loop, args=(sys.modules[__name__],), daemon=True).start()
    threading.Thread(target=recheck_loop, daemon=True).start()
    threading.Thread(target=audit_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
# added later to users: rules_sent_at = last RULES_MSG, asked_at = last follow-request buttons sent by DM,
# last_msg_at = last DM or button tap from the user (opens the 24h window)
# follow_checked_at = last follow lookup, unfollowed_at = when an audit first found a link holder not following
# reminded_at / link_reminded_at = when REMIND_MSG / LINK_REMIND_MSG went out; each person gets only one of them, once
# meet_sent_at = last Meet link sent, live_sent_at = when live.py's "workshop is live" broadcast reached them
# warned_at / warn_tried_at / warn_error = UNFOLLOW_WARN_MSG sent / last tried / why it failed (dashboard),
# refollowed_at = REFOLLOW_MSG sent, between_sent_at = last BETWEEN_MSG
# The one-per-person columns (reminders, Meet link, warning, first DM) count per workshop: a time before the
# current workshop started counts as not sent (see this_workshop).
USER_EXTRA_COLS = ("rules_sent_at", "asked_at", "last_msg_at", "follow_checked_at", "unfollowed_at", "reminded_at",
                   "link_reminded_at", "meet_sent_at", "live_sent_at", "past_msg_at", "warned_at", "warn_tried_at",
                   "warn_error", "refollowed_at", "between_sent_at")


def db():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS bypass_media (media_id TEXT PRIMARY KEY, url TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_source (igsid TEXT PRIMARY KEY, media_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sent (igsid TEXT PRIMARY KEY, username TEXT, sent_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    # everyone who triggered the bot, whether or not they followed; read by report.py
    conn.execute("CREATE TABLE IF NOT EXISTS users (igsid TEXT PRIMARY KEY, username TEXT, "
                 "first_seen TEXT, last_seen TEXT, follows INTEGER)")
    # personal join links: one code per user; every tap of /j/<code> is logged in clicks
    conn.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, igsid TEXT UNIQUE, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS clicks (code TEXT, igsid TEXT, clicked_at TEXT, user_agent TEXT)")
    # people the members dashboard is sure (99%+ or settled by hand) joined the current group through their
    # link; their link stops working. Rewritten by dashboard.expire_loop every minute.
    conn.execute("CREATE TABLE IF NOT EXISTS used_links (igsid TEXT PRIMARY KEY, who TEXT, joined_at TEXT)")
    if "ip" not in {row[1] for row in conn.execute("PRAGMA table_info(clicks)")}:
        conn.execute("ALTER TABLE clicks ADD COLUMN ip TEXT")  # tells Meta's link scanners apart from people
    have = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col in USER_EXTRA_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    # workshops set up on the dashboard (Workshop tab), see workshop(); closed_at NULL = the running one
    conn.execute("CREATE TABLE IF NOT EXISTS workshops (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, date TEXT, "
                 "started_at TEXT, closed_at TEXT, wa_group TEXT, wa_link TEXT, meet_url TEXT, past_offer INTEGER, "
                 "texts TEXT, faqs TEXT, stats TEXT)")
    # reels with their own offer (dashboard's Reels tab, see reel()); media_id '' = no reel connected yet, so
    # nothing is routed to it. texts / faqs work like a workshop's.
    conn.execute("CREATE TABLE IF NOT EXISTS reels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, media_id TEXT, "
                 "reel_url TEXT, wa_link TEXT, follow_first INTEGER, active INTEGER, created_at TEXT, texts TEXT, "
                 "faqs TEXT)")
    # one row per person and reel: that reel's one-offs (welcome, steps, follow request, reminders, link) and
    # their personal link code; taps of those links go to reel_clicks, not clicks (the members dashboard's)
    conn.execute("CREATE TABLE IF NOT EXISTS reel_people (reel_id INTEGER, igsid TEXT, joined_at TEXT, dm_at TEXT, "
                 "rules_at TEXT, asked_at TEXT, reminded_at TEXT, sent_at TEXT, link_reminded_at TEXT, "
                 "code TEXT UNIQUE, PRIMARY KEY (reel_id, igsid))")
    conn.execute("CREATE TABLE IF NOT EXISTS reel_clicks (code TEXT, reel_id INTEGER, igsid TEXT, clicked_at TEXT, "
                 "user_agent TEXT, ip TEXT)")
    # the reel whose flow someone is in: set by a comment on that reel, removed by any other trigger (an ad,
    # another post, a story reply)
    conn.execute("CREATE TABLE IF NOT EXISTS funnel (igsid TEXT PRIMARY KEY, reel_id INTEGER, at TEXT)")
    # what the bot and the dashboard did, for the dashboard's Activity tab (see activity())
    conn.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, "
                 "who TEXT, channel TEXT, summary TEXT, text TEXT, detail TEXT)")
    # state of each background loop (see job())
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, "
                 "next_at TEXT, running INTEGER, done INTEGER, total INTEGER, result TEXT, error TEXT)")
    return conn


def now():
    return datetime.now(timezone.utc)


# ---------- workshops: settings and texts that change from one workshop to the next ----------
# Set on the dashboard (Workshop tab) and read here, so a change reaches the bot within WS_TTL seconds, no restart.
# Until any workshop exists (fresh install, the offline tests) the .env values above are used.
WS_TTL = 2
_ws = {"at": -WS_TTL, "db": None, "rows": []}


def workshops(fresh=False):
    """Every workshop, oldest first, as dicts (texts and faqs decoded, started = datetime)."""
    if fresh or _ws["db"] != DB or time.monotonic() - _ws["at"] > WS_TTL:
        conn = db()
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM workshops ORDER BY started_at, id")]
        conn.close()
        for w in rows:
            w["texts"] = json.loads(w["texts"] or "{}")
            w["faqs"] = json.loads(w["faqs"]) if w["faqs"] else None
            w["stats"] = json.loads(w["stats"]) if w["stats"] else None
            w["started"] = datetime.fromisoformat(w["started_at"])
        _ws.update(at=time.monotonic(), db=DB, rows=rows)
    return _ws["rows"]


def env_workshop():
    """The workshop as .env describes it (WORKSHOP_SINCE, WORKSHOP_LINK, WA_MAIN_CHAT, MEET_URL)."""
    return {"id": None, "name": "", "date": None, "started": PAST_BEFORE,
            "started_at": PAST_BEFORE.isoformat() if PAST_BEFORE else None, "closed_at": None,
            "wa_group": os.getenv("WA_MAIN_CHAT", ""), "wa_link": WORKSHOP_LINK, "meet_url": MEET_URL,
            "past_offer": 1, "texts": {}, "faqs": None, "stats": None}


def latest_workshop():
    """The running workshop; between workshops, the one that ended last (its links and texts still apply).
    While a reel's person is handled (in_reel), that reel: its texts, questions and link."""
    if reel():
        return reel()
    rows = workshops()
    return rows[-1] if rows else env_workshop()


def workshop():
    """The running workshop, or None between workshops (after End workshop, before the next one starts)."""
    w = latest_workshop()
    return None if w["closed_at"] else w


# ---------- reels with their own offer (dashboard's Reels tab) ----------
# A comment on a connected reel puts the person in that reel's flow; their later DMs and taps stay in it until
# something else triggers the bot (an ad, another post, a story reply). Inside in_reel(r) every text, question,
# link and one-off below is the reel's, so handle() runs unchanged.
_reels = {"at": -WS_TTL, "db": None, "rows": []}


def reels(fresh=False):
    """Every reel offer, oldest first, shaped like a workshop (texts/faqs decoded, started, no live class)."""
    if fresh or _reels["db"] != DB or time.monotonic() - _reels["at"] > WS_TTL:
        conn = db()
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM reels ORDER BY id")]
        conn.close()
        for r in rows:
            r.update(texts=json.loads(r["texts"] or "{}"), faqs=json.loads(r["faqs"]) if r["faqs"] else None,
                     started=datetime.fromisoformat(r["created_at"]), started_at=r["created_at"], closed_at=None,
                     meet_url="", past_offer=0, wa_group="", date=None, stats=None, reel=True)
        _reels.update(at=time.monotonic(), db=DB, rows=rows)
    return _reels["rows"]


def reel():
    """The reel whose person this thread is handling (see in_reel), else None."""
    return getattr(_ctx, "reel", None)


@contextmanager
def in_reel(r):
    """Handle someone in reel r's flow (None = the workshop's own)."""
    before = reel()
    _ctx.reel = r
    try:
        yield
    finally:
        _ctx.reel = before


def live_reel(rid):
    """Reel rid if it's on and connected to a reel."""
    return next((r for r in reels() if r["id"] == rid and r["active"] and r["media_id"]), None)


def reel_for_media(media_id):
    """The reel offer connected to this post/reel, if it's on."""
    return next((r for r in reels() if media_id and r["media_id"] == media_id and r["active"]), None)


def reel_of(igsid):
    """The reel flow this person is in (their last trigger was a comment on it), if that reel is still on."""
    with db() as conn:
        row = conn.execute("SELECT reel_id FROM funnel WHERE igsid = ?", (igsid,)).fetchone()
    return live_reel(row[0]) if row else None


def enter_funnel(igsid, r):
    """Someone triggered the bot from reel r (None = from anywhere else): from now on they're in its flow."""
    with db() as conn:
        if not r:
            conn.execute("DELETE FROM funnel WHERE igsid = ?", (igsid,))
            return
        conn.execute("INSERT INTO funnel VALUES (?, ?, ?) ON CONFLICT (igsid) DO UPDATE SET reel_id = excluded.reel_id, "
                     "at = excluded.at", (igsid, r["id"], now().isoformat()))
        conn.execute("INSERT INTO reel_people (reel_id, igsid, joined_at) VALUES (?, ?, ?) ON CONFLICT (reel_id, igsid) DO UPDATE "
                     "SET joined_at = excluded.joined_at", (r["id"], igsid, now().isoformat()))


def reel_person(igsid, col):
    """One of this person's columns in reel_people for the reel being handled."""
    with db() as conn:
        row = conn.execute(f"SELECT {col} FROM reel_people WHERE reel_id = ? AND igsid = ?",
                           (reel()["id"], igsid)).fetchone()
    return row[0] if row else None


def update_reel_person(igsid, **cols):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO reel_people (reel_id, igsid, joined_at) VALUES (?, ?, ?)",
                     (reel()["id"], igsid, now().isoformat()))
        conn.execute(f"UPDATE reel_people SET {', '.join(f'{c} = ?' for c in cols)} WHERE reel_id = ? AND igsid = ?",
                     (*cols.values(), reel()["id"], igsid))


def past_before():
    """Start of the current workshop: join links sent before it are from an earlier one (see PAST_MSG)."""
    return latest_workshop()["started"]


def this_workshop(at):
    """Whether a stored time (ISO) falls in the current workshop."""
    start = past_before()
    return bool(at) and (not start or datetime.fromisoformat(at) >= start)


def meet_url():
    """The live class link while the workshop is live (Go live on the dashboard), else ''."""
    w = workshop()
    return (w["meet_url"] or "").strip() if w else ""


def workshop_link():
    """The WhatsApp group personal links forward to."""
    return latest_workshop()["wa_link"] or WORKSHOP_LINK


def text(key):
    """A bot message as the current workshop has it (edited on the dashboard), else the default in this file.
    The welcome, "when" and "how" answers switch to their live versions while a class link is set."""
    if key in ("INTRO_MSG", "WHEN_MSG", "CLASS_MSG") and meet_url():
        key = key.replace("_MSG", "_LIVE_MSG")
    return latest_workshop()["texts"].get(key) or (globals().get(key, "") if key in bot_messages.BY_KEY else "")


def fill(template, link):
    """Put a link into a message: {link}, or {} in the older texts."""
    return template.replace("{link}", link).replace("{}", link)


def faqs():
    """The questions the bot answers: [{id, label, keywords, on}]. Built-in questions added after a workshop
    saved its own list are put in too."""
    saved = latest_workshop()["faqs"]
    if not saved:
        return [dict(f) for f in bot_messages.FAQ_DEFAULTS]
    have = {f["id"] for f in saved}
    return saved + [dict(f) for f in bot_messages.FAQ_DEFAULTS if f["id"] not in have]


@lru_cache(maxsize=256)
def _pattern(keywords):
    return bot_messages.keyword_pattern(keywords)


def faq_hits(message, questions=None):
    """[(question, answer text)] for a DM, in list order; a material question makes the free answer redundant."""
    found = []
    for f in questions or faqs():
        if f.get("on", True) and _pattern(tuple(f.get("keywords") or ())).search(message):
            answer = text(bot_messages.FAQ_TEXT.get(f["id"], f["id"]))
            if answer:
                found.append((f, answer))
    if any(f["id"] == "material" for f, _ in found):
        found = [(f, a) for f, a in found if f["id"] != "free"]  # "material cost?" -> MATERIAL_MSG has the number
    return found


def faq_matches(message, questions=None):
    """[(question id, answer text)] for a DM (see faq_hits)."""
    return [(f["id"], answer) for f, answer in faq_hits(message, questions)]


def custom(f):
    """A custom reply added on the dashboard (Messages tab → Custom replies), not a built-in question. These
    answer between workshops too, can carry a link button, and can stop the follow step (stop)."""
    return f["id"] not in bot_messages.FAQ_TEXT


def reply_buttons(f):
    """A custom reply's link button, if it has one."""
    if f.get("button_title") and f.get("button_url"):
        return [{"type": "web_url", "url": f["button_url"], "title": f["button_title"]}]
    return None


def answers_for(said):
    """The questions a DM gets answers to: built-in ones only while a workshop runs (they're about it)."""
    running = workshop() is not None
    return [(f, a) for f, a in faq_hits(said) if running or custom(f)]


# ---------- activity feed and background job state, for the dashboard's Activity tab ----------
_ctx = threading.local()  # .who = the person being handled, for sends that don't carry their id (comment replies)
_err = threading.local()  # .last = the last failed API call in this thread


def activity(kind, who=None, summary="", body=None, detail=None, channel="ig"):
    """Record one thing that happened. kind: in, out, fail, skip, link, wa, follow, job, admin.
    Never lets a recording problem get in the way of the bot."""
    try:
        with db() as conn:
            conn.execute("INSERT INTO activity (at, kind, who, channel, summary, text, detail) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (now().isoformat(), kind, who, channel, summary, body,
                          json.dumps(detail, ensure_ascii=False) if detail else None))
    except Exception:
        log.exception("activity record failed")


def job(name, **cols):
    """Update a background job's row (started_at, ended_at, next_at, running, done, total, result, error)."""
    try:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO jobs (name) VALUES (?)", (name,))
            conn.execute(f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in cols)} WHERE name = ?",
                         (*cols.values(), name))
    except Exception:
        log.exception("job state update failed")


WINDOW_CLOSED = {2534022, 2018278}  # Instagram / Messenger: outside the 24h messaging window


def last_error():
    """Why the last API call of this thread failed, in plain words (None if it worked)."""
    e = getattr(_err, "last", None)
    if not e:
        return None
    if e.get("subcode") in WINDOW_CLOSED:
        return "24 h window closed (no DM from them in the last day)"
    if e.get("subcode") == 2534014 or e.get("code") == 551:
        return "account not found (deleted, or they blocked us)"
    return f"error {e.get('code') or e.get('status')}: {e.get('message')}"


def set_error(r):
    """Keep why an API call failed (r = the httpx response; None = it worked)."""
    if r is None:
        _err.last = None
        return
    try:
        e = r.json().get("error") or {}
    except ValueError:
        e = {}
    _err.last = {"status": r.status_code, "code": e.get("code"), "subcode": e.get("error_subcode"),
                 "message": e.get("message") or r.text[:200]}


def which_text(sent, variant=None):
    """The catalog key (or question id) a sent message came from, for the activity feed.
    variant: how the channel rewords texts (Messenger says Page instead of Profile)."""
    keys = [m["key"] for m in bot_messages.CATALOG if m["kind"] not in ("title", "page")]
    keys += [f["id"] for f in faqs() if f["id"] not in bot_messages.FAQ_TEXT]
    for key in keys:
        full = text(key)
        full = variant(full) if variant else full
        if not full:
            continue
        if "{link}" in full or "{}" in full:
            head, _, tail = full.replace("{link}", "{}").partition("{}")
            if sent.startswith(head) and sent.endswith(tail):
                return key
        elif sent == full or (len(full) > 30 and sent.startswith(full)):
            return key  # e.g. the material answer with the link under it
    return None


def note_send(channel, who, message, buttons, ok, variant=None):
    """Activity row for a message the bot sent (or failed to send)."""
    try:
        detail = {"key": which_text(message, variant)}
        if reel():
            detail["reel"] = reel()["name"]
        if buttons:
            detail["buttons"] = [b.get("title") for b in buttons]
        if not ok:
            detail["error"] = last_error()
        activity("out" if ok else "fail", who, "" if ok else f"couldn't send: {detail['error']}", message, detail,
                 channel)
    except Exception:
        log.exception("activity record failed")


def first_time(event_id):
    """True the first time an event id (comment id / message mid) is seen; blocks Meta retries."""
    with db() as conn:
        return conn.execute("INSERT OR IGNORE INTO seen VALUES (?)", (event_id,)).rowcount == 1


def already_sent(igsid):
    """Has a join link for the current workshop (a link from an earlier one doesn't count, see PAST_MSG)."""
    if reel():
        return bool(reel_person(igsid, "sent_at"))
    with db() as conn:
        row = conn.execute("SELECT sent_at FROM sent WHERE igsid = ?", (igsid,)).fetchone()
    return bool(row) and this_workshop(row[0])


def past_attendee(igsid):
    """Got their join link for an earlier workshop and none yet for this one (see PAST_MSG)."""
    if reel():
        return False
    with db() as conn:
        row = conn.execute("SELECT sent_at FROM sent WHERE igsid = ?", (igsid,)).fetchone()
    return bool(row) and not this_workshop(row[0])


def record_sent(igsid, username):
    """A past attendee's row is moved to now, so from here on they count for the current workshop."""
    if reel():
        update_reel_person(igsid, sent_at=now().isoformat())
        return
    with db() as conn:
        conn.execute("INSERT INTO sent VALUES (?, ?, ?) ON CONFLICT (igsid) DO UPDATE SET username = "
                     "COALESCE(excluded.username, username), sent_at = excluded.sent_at",
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
    """True if this user hasn't DMed us or tapped a button in this workshop yet (a comment doesn't count)."""
    if reel():
        return not reel_person(igsid, "dm_at")
    with db() as conn:
        row = conn.execute("SELECT last_msg_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    return not (row and this_workshop(row[0]))


def faq_answers(message):
    """The answers a DM gets, in order (see faq_matches)."""
    return [answer for _, answer in faq_matches(message)]


def send_rules(igsid, first=False):
    """DM the plain-text steps (INTRO_MSG on a first DM) unless this user already got them within RULES_EVERY."""
    if reel():
        row = (reel_person(igsid, "rules_at"),)
    else:
        with db() as conn:
            row = conn.execute("SELECT rules_sent_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    if row and row[0] and datetime.fromisoformat(row[0]) > now() - RULES_EVERY:
        return False
    if send({"id": igsid}, text("INTRO_MSG") if first else text("RULES_MSG")):
        if reel():
            update_reel_person(igsid, rules_at=now().isoformat())
        else:
            update_user(igsid, rules_sent_at=now().isoformat())
        return True
    return False


def follow_buttons():
    return [{"type": "web_url", "url": profile_url(), "title": text("VISIT_TITLE")},
            {"type": "postback", "title": text("FOLLOWING_TITLE"), "payload": CHECK_FOLLOW}]


def ask_to_follow(igsid, recipient, message, fallback, first=False):
    """Send the follow-request buttons (plain-text fallback if they fail); in DMs, the rules go first.
    A comment gets only one private reply, so commenters see the rules once they answer in the DM."""
    dm = "id" in recipient
    if dm:
        send_rules(igsid, first)
    if send(recipient, message, follow_buttons()) or send(recipient, fallback):
        if dm and reel():
            update_reel_person(igsid, asked_at=now().isoformat())  # its own re-check (recheck_reels)
        elif dm:
            update_user(igsid, asked_at=now().isoformat())
        return True
    return False


def personal_link(igsid):
    """This user's tracked join link (same code every time), served through the current tunnel.
    In a reel's flow, their link for that reel (it opens the reel's WhatsApp link)."""
    if reel():
        code = reel_person(igsid, "code")
        if not code:
            code = "r" + secrets.token_urlsafe(6)
            update_reel_person(igsid, code=code)
    else:
        with db() as conn:
            row = conn.execute("SELECT code FROM links WHERE igsid = ?", (igsid,)).fetchone()
            code = row[0] if row else secrets.token_urlsafe(6)
            if not row:
                conn.execute("INSERT INTO links VALUES (?, ?, ?)", (code, igsid, datetime.now(timezone.utc).isoformat()))
    base = os.getenv("PUBLIC_LINK_BASE", "").strip().rstrip("/")
    if base:
        import join_sync
        if join_sync.configured():
            try:
                state = "used" if not reel() and link_used(igsid) else "active"
                join_sync.register(code, workshop_link(), state)
            except Exception as exc:
                log.warning("Could not register hosted join link (%s); using direct WhatsApp invite", type(exc).__name__)
                return workshop_link()
    if not base:
        base = Path(__file__).with_name("webhook_url.txt").read_text().strip().removesuffix("/webhook")
    return f"{base}/j/{code}"


def send_meet(igsid, recipient):
    """The live workshop's Meet link (plain link first, then as a button in DMs)."""
    url = meet_url()
    sent = send(recipient, fill(text("MEET_MSG"), url))
    if "id" in recipient:
        sent = send(recipient, text("MEET_BUTTON_MSG"),
                    [{"type": "web_url", "url": url, "title": text("MEET_TITLE")}]) or sent
    if sent:
        update_user(igsid, meet_sent_at=now().isoformat())
    return sent


def meet_sent(igsid):
    """Got the Meet link in this workshop."""
    with db() as conn:
        row = conn.execute("SELECT meet_sent_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    return bool(row) and this_workshop(row[0])


def send_link(igsid, recipient):
    """A comment allows only one private reply, so commenters get just the link message
    (the Meet link while the workshop is live; DMs get it first, then the WhatsApp link)."""
    if meet_url():
        sent = send_meet(igsid, recipient)
        if "comment_id" in recipient:
            return sent
    url = personal_link(igsid)
    sent = send(recipient, fill(text("LINK_MSG"), url))
    if "id" in recipient:
        sent = send(recipient, text("LINK_BUTTON_MSG"),
                    [{"type": "web_url", "url": url, "title": text("JOIN_TITLE")}]) or sent
    return sent


def api(method, path, **kwargs):
    r = httpx.request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"},
                      timeout=15, **kwargs)
    if r.is_error:
        log.error("API %s /%s failed: %s %s", method, path, r.status_code, r.text)
        set_error(r)
        return None
    set_error(None)
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


def send(recipient, message_text, buttons=None):
    if bypassed_person(recipient.get("id") or getattr(_ctx, "who", None)):
        return False
    message = {"text": message_text}
    if buttons:
        message = {"attachment": {"type": "template", "payload": {
            "template_type": "button", "text": message_text, "buttons": buttons}}}
    ok = api("POST", f"{IG_USER_ID}/messages", json={"recipient": recipient, "message": message}) is not None
    note_send("ig", recipient.get("id") or getattr(_ctx, "who", None), message_text, buttons, ok)
    return ok


def warned(igsid):
    """Warned about unfollowing in this workshop (dashboard) and not yet thanked for following again."""
    if reel():
        return False
    with db() as conn:
        row = conn.execute("SELECT warned_at, refollowed_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    return bool(row) and this_workshop(row[0]) and not (row[1] and row[1] >= row[0])


def answer_refollow(igsid, username, recipient):
    """A warned unfollower says they follow again: REFOLLOW_MSG if Instagram agrees, otherwise the buttons again."""
    follows, api_username = check_follow(igsid)
    for delay in RECHECK_DELAYS:
        if follows is not False:
            break
        time.sleep(delay)
        follows, api_username = check_follow(igsid)
    username = api_username or username
    record_user(igsid, username, follows)
    if follows:
        update_user(igsid, unfollowed_at=None, follow_checked_at=now().isoformat())
        activity("follow", igsid, "followed again after the warning")
        if send(recipient, text("REFOLLOW_MSG")):
            update_user(igsid, refollowed_at=now().isoformat())
            log.info("thanked %s for following again", username or igsid)
            return True
        return False
    if follows is False and ask_to_follow(igsid, recipient, text("NOT_FOLLOWING_MSG"), text("NOT_FOLLOWING_TEXT")):
        log.info("sent follow not detected to warned %s", username or igsid)
        return True
    return False


def send_between(igsid, username, recipient):
    """No workshop running: BETWEEN_MSG with the profile button instead of the follow-gate, at most once a day."""
    record_user(igsid, username)
    if "id" in recipient:
        update_user(igsid, last_msg_at=now().isoformat())
    with db() as conn:
        row = conn.execute("SELECT between_sent_at FROM users WHERE igsid = ?", (igsid,)).fetchone()
    if row and row[0] and datetime.fromisoformat(row[0]) > now() - RULES_EVERY:
        log.info("skip %s: no workshop running, already told today", username or igsid)
        activity("skip", igsid, "no workshop running, already told today: no reply")
        return False
    message = text("BETWEEN_MSG")
    button = [{"type": "web_url", "url": profile_url(), "title": text("VISIT_TITLE")}]
    if send(recipient, message, button) or send(recipient, message):
        update_user(igsid, between_sent_at=now().isoformat())
        log.info("sent between-workshops message to %s", username or igsid)
        return True
    return False


def handle(igsid, username, recipient, claimed=False):
    """Follow-gate one user; returns True if a DM went out.
    recipient is {"comment_id": ...} for comments, {"id": ...} for DMs.
    claimed: the user says they followed ("I'm following" tap or DONE), so re-check before saying no."""
    _ctx.who = igsid
    if bypassed_person(igsid):
        return False
    if workshop() is None:
        return send_between(igsid, username, recipient)
    first = "id" in recipient and first_dm(igsid)
    record_user(igsid, username)
    if "id" in recipient:
        update_user(igsid, last_msg_at=now().isoformat())
        if reel():
            update_reel_person(igsid, dm_at=now().isoformat())
    has_link = already_sent(igsid)
    if has_link and claimed and warned(igsid):
        return answer_refollow(igsid, username, recipient)
    live = meet_url()
    # while live, link holders get the Meet link once (again whenever they tap "I'm following" or say DONE)
    if has_link and not (live and (claimed or not meet_sent(igsid))):
        log.info("skip %s: already has the link", username or igsid)
        activity("skip", igsid, "already has the link: no reply")
        return False
    if reel() and not reel()["follow_first"]:
        follows, api_username = True, None  # this reel sends the link without asking to follow
    else:
        follows, api_username = check_follow(igsid)
    if claimed and follows is not True:
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
        if past_attendee(igsid) and latest_workshop()["past_offer"]:
            # a comment allows only one private reply, so there the offer and the link go together
            together = "comment_id" in recipient and not live
            if send(recipient, text("PAST_MSG") + ("\n" + personal_link(igsid) if together else "")):
                update_user(igsid, past_msg_at=now().isoformat())
                log.info("sent past-attendee offer to %s", username or igsid)
                if together:
                    record_sent(igsid, username)
                    log.info("link sent to %s", username or igsid)
                    return True
        if send_link(igsid, recipient):
            record_sent(igsid, username)
            log.info("link sent to %s", username or igsid)
            return True
        return False
    if follows is None:
        # Instagram hides follow status (error 230) until the user interacts; a button tap counts.
        # Same buttons as the follow request so they match the steps in RULES_MSG.
        kind, message, fallback = "opener", text("OPENER_MSG"), text("FOLLOW_TEXT")
    elif claimed:
        kind, message, fallback = "follow not detected", text("NOT_FOLLOWING_MSG"), text("NOT_FOLLOWING_TEXT")
    else:
        kind, message, fallback = "follow request", text("FOLLOW_MSG"), text("FOLLOW_TEXT")
    if ask_to_follow(igsid, recipient, message, fallback, first):
        log.info("sent %s to %s", kind, username or igsid)
        return True
    return False


def utc_iso(at):
    """A datetime as stored in the database (UTC ISO), '' for None (sorts before any time)."""
    return at.astimezone(timezone.utc).isoformat() if at else ""


def pending_follows():
    """Users asked to follow by DM within WINDOW who still have no link and messaged within WINDOW."""
    cutoff = (now() - WINDOW).isoformat()
    with db() as conn:
        return conn.execute("SELECT igsid, username, asked_at, reminded_at FROM users WHERE asked_at >= ? "
                            "AND last_msg_at >= ? AND igsid NOT IN (SELECT igsid FROM sent WHERE sent_at >= ?) "
                            "AND igsid NOT IN (SELECT f.igsid FROM funnel f JOIN reels r ON r.id = f.reel_id "
                            "WHERE r.active = 1 AND r.media_id != '')",
                            (cutoff, cutoff, utc_iso(past_before()))).fetchall()


def due_reminder(asked_at, reminded_at):
    asked = datetime.fromisoformat(asked_at)
    return not this_workshop(reminded_at) and REMIND_FROM <= asked <= now() - REMIND_AFTER


def recheck_follows():
    """Send the link to anyone who followed after we asked, without waiting for a tap or DONE;
    everyone else gets one reminder per workshop, while the 24h window is open. Returns what it did."""
    done = {"waiting": 0, "links": 0, "reminders": 0}
    for igsid, username, asked_at, reminded_at in pending_follows():
        done["waiting"] += 1
        follows, api_username = check_follow(igsid)
        username = api_username or username
        if follows is not None:
            update_user(igsid, follows=int(follows), username=username)
        if follows:
            if not already_sent(igsid):
                activity("follow", igsid, "started following (found by the 10-minute re-check)")
                if send_link(igsid, {"id": igsid}):
                    record_sent(igsid, username)
                    done["links"] += 1
                    log.info("link sent to %s (follow found by background re-check)", username or igsid)
        elif not meet_url() and due_reminder(asked_at, reminded_at) and send({"id": igsid}, text("REMIND_MSG"),
                                                                              follow_buttons()):
            update_user(igsid, reminded_at=now().isoformat())
            done["reminders"] += 1
            log.info("sent reminder to %s", username or igsid)
        time.sleep(1)  # keep the API calls modest
    return done


def remind_untapped():
    """Link holders (links sent after REMIND_FROM) with no real tap LINK_REMIND_AFTER later get their link
    once more, while the 24h window is open, unless they already got the follow reminder (one reminder per person
    and workshop). Returns how many were reminded."""
    if meet_url():
        return 0  # LINK_REMIND_MSG talks about the class time; the live broadcast replaces it
    from dashboard import real_taps  # same rules as the dashboard: Meta's scanners and the owner's phone don't count
    start = utc_iso(past_before())
    with db() as conn:
        due = conn.execute("SELECT s.igsid, s.username FROM sent s JOIN users u USING (igsid) "
                           "WHERE s.sent_at >= ? AND s.sent_at <= ? AND u.last_msg_at >= ? "
                           "AND (u.link_reminded_at IS NULL OR u.link_reminded_at < ?) "
                           "AND (u.reminded_at IS NULL OR u.reminded_at < ?)",  # earlier workshops' don't count
                           (utc_iso(max(REMIND_FROM, past_before() or REMIND_FROM)),
                            (now() - LINK_REMIND_AFTER).isoformat(), (now() - WINDOW).isoformat(), start, start)
                           ).fetchall()
        clicks = conn.execute("SELECT igsid, clicked_at, COALESCE(user_agent, ''), ip FROM clicks "
                              "WHERE igsid IS NOT NULL").fetchall()
    tapped = real_taps(clicks)
    n = 0
    for igsid, username in due:
        if igsid not in tapped and send({"id": igsid}, fill(text("LINK_REMIND_MSG"), personal_link(igsid))):
            update_user(igsid, link_reminded_at=now().isoformat())
            n += 1
            log.info("sent join-link reminder to %s (no tap yet)", username or igsid)
    return n


def recheck_reels():
    """recheck_follows and remind_untapped for the people in each reel's flow (with that reel's texts and link)."""
    from dashboard import real_taps
    done = {"waiting": 0, "links": 0, "reminders": 0, "link_reminders": 0}
    window = (now() - WINDOW).isoformat()
    for r in reels():
        if not (r["active"] and r["media_id"]) or r["media_id"] in bypassed_media():
            continue
        with db() as conn:
            waiting = conn.execute(
                "SELECT p.igsid, u.username, p.asked_at, p.reminded_at FROM reel_people p JOIN users u USING (igsid) "
                "JOIN funnel f ON f.igsid = p.igsid AND f.reel_id = p.reel_id WHERE p.reel_id = ? "
                "AND p.sent_at IS NULL AND p.asked_at >= ? AND u.last_msg_at >= ?", (r["id"], window, window)).fetchall()
            untapped = conn.execute(
                "SELECT p.igsid, u.username FROM reel_people p JOIN users u USING (igsid) WHERE p.reel_id = ? "
                "AND p.sent_at <= ? AND u.last_msg_at >= ? AND p.link_reminded_at IS NULL AND p.reminded_at IS NULL",
                (r["id"], (now() - LINK_REMIND_AFTER).isoformat(), window)).fetchall()
            clicks = conn.execute("SELECT igsid, clicked_at, COALESCE(user_agent, ''), ip FROM reel_clicks "
                                  "WHERE reel_id = ? AND igsid IS NOT NULL", (r["id"],)).fetchall()
        tapped = real_taps(clicks)
        with in_reel(r):
            for igsid, username, asked_at, reminded_at in waiting:
                done["waiting"] += 1
                follows, api_username = check_follow(igsid)
                username = api_username or username
                if follows is not None:
                    update_user(igsid, follows=int(follows), username=username)
                if follows:
                    activity("follow", igsid, f"started following (found by the 10-minute re-check, {r['name']})")
                    if send_link(igsid, {"id": igsid}):
                        record_sent(igsid, username)
                        done["links"] += 1
                        log.info("reel %s: link sent to %s (follow found by background re-check)", r["id"],
                                 username or igsid)
                elif (not reminded_at and datetime.fromisoformat(asked_at) <= now() - REMIND_AFTER
                      and send({"id": igsid}, text("REMIND_MSG"), follow_buttons())):
                    update_reel_person(igsid, reminded_at=now().isoformat())
                    done["reminders"] += 1
                time.sleep(1)
            for igsid, username in untapped:
                if igsid not in tapped and send({"id": igsid}, fill(text("LINK_REMIND_MSG"), personal_link(igsid))):
                    update_reel_person(igsid, link_reminded_at=now().isoformat())
                    done["link_reminders"] += 1
    return done


def recheck_loop():
    while True:
        job("recheck", next_at=(now() + timedelta(seconds=RECHECK_EVERY)).isoformat())
        time.sleep(RECHECK_EVERY)
        job("recheck", started_at=now().isoformat(), running=1, error=None)
        parts, errors, busy = [], [], False
        try:
            d = recheck_reels()
            if any(d.values()):
                parts.append(f"Reels: {d['waiting']} waiting for a follow · {d['links']} links sent · "
                             f"{d['reminders']} follow + {d['link_reminders']} link reminders")
                busy = d["links"] or d["reminders"] or d["link_reminders"]
        except Exception as e:
            log.exception("reel re-check failed")
            errors.append(f"reels: {e}")
        if workshop() is None:
            result = " · ".join(["No workshop running"] + parts)
            job("recheck", ended_at=now().isoformat(), running=0, result=result, error="; ".join(errors) or None)
            if busy or errors:
                activity("job", None, "Follow re-check: " + result + (" · failed: " + "; ".join(errors) if errors else ""))
            continue
        try:
            d = recheck_follows()
            parts.append(f"{d['waiting']} waiting for a follow · {d['links']} links sent · "
                         f"{d['reminders']} follow reminders")
            busy = d["links"] or d["reminders"]
        except Exception as e:
            log.exception("background follow re-check failed")
            errors.append(f"follow re-check: {e}")
        try:
            n = remind_untapped()
            parts.append(f"{n} join-link reminders")
            busy = busy or n
        except Exception as e:
            log.exception("join-link reminder failed")
            errors.append(f"join-link reminders: {e}")
        try:
            import messenger
            m = messenger.remind()
            if m:
                parts.append(f"Messenger: {m['follow']} follow + {m['link']} join-link reminders")
                busy = busy or m["follow"] or m["link"]
        except Exception as e:
            log.exception("Messenger reminder failed")
            errors.append(f"Messenger reminders: {e}")
        result = " · ".join(parts)
        job("recheck", ended_at=now().isoformat(), running=0, result=result, error="; ".join(errors) or None)
        if busy or errors:
            activity("job", None, "Follow re-check: " + result + (" · failed: " + "; ".join(errors) if errors else ""))


def audit_follows():
    """Re-check everyone who got the link; records unfollows (and re-follows) for the members dashboard."""
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (igsid, username, first_seen, last_seen) "
                     "SELECT igsid, username, sent_at, sent_at FROM sent")
        people = conn.execute("SELECT u.igsid, u.username, u.follows FROM users u JOIN sent s USING (igsid)").fetchall()
    job("audit", started_at=now().isoformat(), running=1, done=0, total=len(people), error=None)
    gone = back = failed = 0
    for i, (igsid, username, before) in enumerate(people, 1):
        follows, api_username = check_follow(igsid)
        if follows is not None:
            cols = dict(follows=int(follows), follow_checked_at=now().isoformat(), username=api_username or username)
            if not follows and before != 0:
                cols["unfollowed_at"] = now().isoformat()
                log.info("unfollow detected: %s", api_username or username or igsid)
                activity("follow", igsid, "unfollowed (found by the unfollow check)")
                gone += 1
            elif follows:
                cols["unfollowed_at"] = None
                if before == 0:
                    activity("follow", igsid, "followed again (found by the unfollow check)")
                    back += 1
            update_user(igsid, **cols)
        else:
            failed += 1
        if i % 20 == 0:
            job("audit", done=i)
        time.sleep(2)  # ~350 people -> ~12 minutes, well under Instagram's rate limits
    result = (f"{len(people)} link holders checked · {gone} unfollowed · {back} followed again"
              + (f" · {failed} lookups failed" if failed else ""))
    job("audit", ended_at=now().isoformat(), running=0, done=len(people), result=result)
    activity("job", None, "Unfollow check: " + result)


def audit_loop():
    while True:
        try:
            audit_follows()
        except Exception as e:
            log.exception("follow audit failed")
            job("audit", ended_at=now().isoformat(), running=0, error=str(e))
        job("audit", next_at=(now() + timedelta(seconds=AUDIT_EVERY)).isoformat())
        time.sleep(AUDIT_EVERY)


def matches(said):
    return KEYWORD == "*" or KEYWORD in said.lower()


_posted_at = {}  # media id -> post time; only successful lookups are cached


def is_new(media_id):
    """Use the current campaign's start as well as the configured minimum post date."""
    starts = [t for t in (START_AFTER, past_before() if workshops() else None) if t]
    cutoff = max(starts) if starts else None
    if not cutoff:
        return True
    if media_id not in _posted_at:
        data = api("GET", media_id, params={"fields": "timestamp"})
        if not data or "timestamp" not in data:
            return False
        _posted_at[media_id] = datetime.strptime(data["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    return _posted_at[media_id] >= cutoff


def current_event(stamp):
    """Ignore delayed webhook events from before this workshop; accept legacy events without a time."""
    start = past_before() if workshops() else None
    if not stamp or not start:
        return True
    try:
        if isinstance(stamp, (int, float)) or str(stamp).isdigit():
            stamp = float(stamp)
            at = datetime.fromtimestamp(stamp / 1000 if stamp > 100_000_000_000 else stamp, timezone.utc)
        else:
            at = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
        return at >= start
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def answer_dm(sid, said):
    """Answer the questions in a DM that starts the bot, then the follow step (unless a custom reply stops it)."""
    hits = answers_for(said)
    for f, answer in hits:
        if f["id"] == "material" and already_sent(sid) and not link_used(sid):
            answer += "\n\n" + fill(text("MATERIAL_LINK_MSG"), personal_link(sid))
        buttons = reply_buttons(f)
        if (send({"id": sid}, answer, buttons) if buttons else send({"id": sid}, answer)) or (
                buttons and send({"id": sid}, f"{answer}\n{f['button_url']}")):
            log.info("answered question from %s", sid)
    if said.upper() != "DONE" and any(f.get("stop") for f, _ in hits):
        record_user(sid, None)  # still opens their 24h window, but no follow step after this reply
        update_user(sid, last_msg_at=now().isoformat())
        log.info("skip %s: a custom reply stops here", sid)
        activity("skip", sid, "custom reply set to \"stop here\": no follow step")
    else:
        handle(sid, None, {"id": sid}, claimed=said.upper() == "DONE")


def bypassed_media():
    with db() as conn:
        return dict(conn.execute("SELECT media_id, url FROM bypass_media"))


def set_media_source(sid, media_id):
    if sid:
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO media_source VALUES (?, ?)", (sid, str(media_id or "")))


def bypassed_person(sid):
    with db() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM media_source s JOIN bypass_media b USING (media_id) WHERE s.igsid = ? "
            "UNION SELECT 1 FROM funnel f JOIN reels r ON r.id = f.reel_id "
            "JOIN bypass_media b ON b.media_id = r.media_id WHERE f.igsid = ? "
            "AND NOT EXISTS (SELECT 1 FROM media_source WHERE igsid = ?)", (sid, sid, sid)).fetchone())


def message_media_source(event):
    """Only use structured origin metadata, never a link casually mentioned in DM text."""
    msg = event.get("message") or {}
    refs = [event.get("referral") or {}, msg.get("referral") or {},
            (msg.get("reply_to") or {}).get("story") or {}]
    refs += [a.get("payload") or {} for a in msg.get("attachments", [])
             if a.get("type") in ("share", "reel", "ig_reel", "story_mention")]
    blocked = bypassed_media()
    for ref in refs:
        mid = ref.get("media_id") or ref.get("post_id") or ref.get("reel_id") or ref.get("id")
        if mid:
            return str(mid)
        url = ref.get("url") or ref.get("permalink") or ""
        code = re.search(r"/(?:reels?|p|tv)/([\w-]+)", url)
        if code:
            for mid, saved in blocked.items():
                if f"/{code[1]}/" in saved + "/":
                    return mid
    return None


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
            if not current_event(v.get("created_time") or entry.get("time")):
                continue
            sender = v.get("from", {})
            media = v.get("media", {})
            if sender.get("id") in me or not first_time(v["id"]):
                continue
            who, said, named = sender.get("id"), v.get("text"), {"username": sender.get("username")}
            set_media_source(who, media.get("id"))
            if str(media.get("id")) in bypassed_media():
                continue
            if media.get("id") in IGNORE_MEDIA:
                log.info("skip comment on media=%s: in IGNORE_MEDIA", media.get("id"))
                activity("skip", who, "commented on a muted post (IGNORE_MEDIA): ignored", said, named)
                continue
            is_ad = media.get("ad_id") or media.get("media_product_type") == "AD"
            r = reel_for_media(media.get("id"))  # a reel with its own offer (Reels tab)
            if not r and not is_ad and not is_new(media.get("id")):
                log.info("skip comment on media=%s: posted before START_AFTER", media.get("id"))
                activity("skip", who, "commented on an old post (before START_AFTER): ignored", said, named)
                continue
            log.info("comment trigger from %s: %r", sender.get("username"), said)
            activity("in", who, f"commented on the reel “{r['name']}”" if r else "commented on an ad" if is_ad
                     else "commented on a post", said, named)
            enter_funnel(who, r)
            with in_reel(r):
                if handle(sender["id"], sender.get("username"), {"comment_id": v["id"]}):
                    # public reply; our own reply comes back as a webhook but is skipped via `me`
                    reply = text("COMMENT_REPLY")
                    if api("POST", f"{v['id']}/replies", params={"message": reply}):
                        log.info("replied publicly to comment %s", v["id"])
                        activity("out", who, "public reply under the comment", reply, {"key": "COMMENT_REPLY"})

        for m in entry.get("messaging", []):
            if not current_event(m.get("timestamp") or entry.get("time")):
                continue
            msg, pb = m.get("message", {}), m.get("postback", {})
            sid = m.get("sender", {}).get("id")
            if msg.get("is_echo") or sid in me:
                continue
            source = message_media_source(m)
            referral = msg.get("referral") or m.get("referral") or {}
            if source:
                set_media_source(sid, source)
            elif referral.get("ad_id") or msg.get("reply_to", {}).get("story"):
                set_media_source(sid, None)
            if bypassed_person(sid):
                continue
            if pb.get("payload") in (GET_LINK, CHECK_FOLLOW) and first_time(pb.get("mid", "")):
                log.info("button tap from %s: %r", sid, pb.get("title"))
                activity("in", sid, f"tapped “{pb.get('title') or 'a button'}”")
                # "I'm following" buttons sent before CHECK_FOLLOW existed still carry GET_LINK
                claimed = pb["payload"] == CHECK_FOLLOW or pb.get("title", "").startswith("I'm following")
                with in_reel(reel_of(sid)):
                    handle(sid, None, {"id": sid}, claimed)
                continue
            said = msg.get("text", "").strip()
            # click-to-message ads attach a referral (inside the message, or alone as messaging_referral)
            ad = (msg.get("referral") or m.get("referral") or {}).get("ad_id")
            story = msg.get("reply_to", {}).get("story", {}).get("id")
            if not (said or ad) or not first_time(msg.get("mid") or f"ref:{sid}:{m.get('timestamp')}"):
                continue
            if ad or story:
                enter_funnel(sid, None)  # from an ad or a story: the workshop's own flow again
            r = None if ad or story else reel_of(sid)
            if ad or (story and is_new(story)) or said.upper() == "DONE" or matches(said) or r:
                log.info("DM trigger from %s (ad=%s story=%s reel=%s): %r", sid, ad, story, r and r["id"], said)
                activity("in", sid, "opened the chat from an ad" if ad and not said else "DM from an ad" if ad
                         else "replied to a story" if story else "DM", said or None)
                with in_reel(r):
                    answer_dm(sid, said)
            else:
                activity("in", sid, "DM without the keyword: no reply", said)


PREVIEW_BOTS = ("facebookexternalhit", "facebot", "whatsapp", "bot", "crawler", "preview")


LINK_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<body style="margin:0;font-family:system-ui,sans-serif;background:#fbf5ee;color:#5a1426;display:grid;place-items:center;
min-height:100vh;text-align:center;padding:24px;box-sizing:border-box"><div style="max-width:360px">
<div style="font-size:48px">{icon}</div><h2 style="margin:12px 0">{heading}</h2>
<p style="font-size:17px;line-height:1.5">{body}</p></div></body>"""


def link_page(key, title, icon):
    """USED_LINK_PAGE_TEXT / PAST_LINK_PAGE_TEXT as a page: the first line is the heading."""
    heading, _, body = text(key).strip().partition("\n")
    return LINK_PAGE.format(title=title, icon=icon, heading=html.escape(heading),
                            body="<br>\n".join(html.escape(line) for line in body.splitlines()))


def link_used(igsid):
    if reel():
        return False  # reel links aren't matched to joins
    with db() as conn:
        return bool(igsid and conn.execute("SELECT 1 FROM used_links WHERE igsid = ?", (igsid,)).fetchone())


def person_tap(ua, ip):
    from dashboard import is_person  # same rule as the members dashboard: Meta's link scanners aren't people
    return is_person(ua, ip)


LINK_HEADERS = {"Cache-Control": "no-store, max-age=0", "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"}


def whatsapp_page(url, used=False):
    """A deliberate tap can open WhatsApp from an in-app browser; retain HTTPS and copy fallbacks."""
    parsed = urlparse(url)
    invite = parsed.path.strip('/')
    if parsed.scheme != 'https' or parsed.netloc != 'chat.whatsapp.com' or not invite or not invite.isalnum():
        return RedirectResponse(url, status_code=302, headers=LINK_HEADERS)
    safe = html.escape(url, quote=True)
    heading = 'WhatsApp group join karein'
    note = 'Neeche button dabakar WhatsApp kholein, phir Join dabayein.'
    if used:
        heading = 'Aapka link use ho chuka hai'
        note = ('Dashboard ke hisaab se aap join kar chuke hain. Agar group nahi dikh raha ya join nahi hua, '
                'neeche se dobara khol sakte hain.')
    page = '''<!doctype html><html lang="hi-Latn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Join workshop on WhatsApp</title>
<style>body{margin:0;background:#fbf5ee;color:#491426;font:17px/1.6 system-ui,sans-serif;padding:24px;
min-height:100dvh;box-sizing:border-box;display:grid;place-items:center}main{width:100%;max-width:420px}
h1{font-size:27px;line-height:1.25}a,button{box-sizing:border-box;display:block;width:100%;padding:15px 18px;
border-radius:12px;text-align:center;margin:12px 0;font:inherit;font-weight:650;cursor:pointer}
.primary{background:#147d50;color:white;text-decoration:none}button{background:white;color:#491426;border:1px solid #d6b9bf}
input{box-sizing:border-box;width:100%;padding:12px;font-size:14px;border:1px solid #d6b9bf;border-radius:8px}
small{display:block;color:#69595e}summary{cursor:pointer}footer{margin-top:28px;font-size:13px;color:#69595e}</style></head>
<body><main><small>KALAKRITI RESIN ART STUDIO</small><h1>HEADING</h1><p>NOTE</p>
<a class="primary" href="whatsapp://chat?code=INVITE">Open WhatsApp</a>
<a href="SAFE" rel="noreferrer">Open invite in browser</a>
<details><summary>Link nahi khul raha?</summary>
<p>Instagram/Facebook ke menu (⋮ ya …) mein <b>Open in browser</b> chunein.
Ya neeche ka link copy karke Chrome / Safari mein paste karein.</p>
<label for="invite">WhatsApp invite link</label><input id="invite" readonly value="SAFE">
<button id="copy" type="button">Copy invite link</button><small id="status" role="status"></small>
<a href="https://web.whatsapp.com/accept?code=INVITE" rel="noreferrer">Use WhatsApp Web</a></details>
<footer>Kalakriti · Free online workshop</footer></main>
<script>document.getElementById('copy').onclick=async()=>{const field=document.getElementById('invite');
try{await navigator.clipboard.writeText(field.value);document.getElementById('status').textContent='Link copied';}
catch(e){field.focus();field.select();document.getElementById('status').textContent='Link select ho gaya. Copy karein.';}};</script>
</body></html>'''
    return HTMLResponse(page.replace('HEADING', heading).replace('NOTE', note)
                        .replace('INVITE', invite).replace('SAFE', safe), headers=LINK_HEADERS)


@app.get("/j/{code}")
def join(code: str, request: Request):
    """Log the tap of a personal link, then forward to the WhatsApp group (or say it's used, once they joined)."""
    with db() as conn:
        row = conn.execute("SELECT igsid FROM links WHERE code = ?", (code,)).fetchone()
        rp = None if row else conn.execute("SELECT reel_id, igsid FROM reel_people WHERE code = ?", (code,)).fetchone()
    if rp:
        return reel_join(code, *rp, request)
    if not row:
        return HTMLResponse('This invite link is incomplete. Please DM us for a fresh workshop link.',
                            status_code=404, headers=LINK_HEADERS)
    if row and past_attendee(row[0]):
        # a link from an earlier free workshop must not open the current group
        log.info("past-workshop join link opened: code=%s igsid=%s", code, row[0])
        activity("link", row[0], "opened an earlier workshop's link: shown the \"DM us for the new link\" page")
        return HTMLResponse(link_page("PAST_LINK_PAGE_TEXT", "Link expired", "🪔"), headers=LINK_HEADERS)
    if row and link_used(row[0]):
        # not logged as a click: a tap after the join would only confuse the matching of other people's joins
        log.info("used join link opened: code=%s igsid=%s", code, row[0])
        activity("link", row[0], "opened their link after joining: shown the \"already used\" page")
        return whatsapp_page(workshop_link(), used=True)
    ua = request.headers.get("user-agent", "")
    if not any(b in ua.lower() for b in PREVIEW_BOTS):  # link previews aren't real clicks
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
        with db() as conn:
            conn.execute("INSERT INTO clicks (code, igsid, clicked_at, user_agent, ip) VALUES (?, ?, ?, ?, ?)",
                         (code, row[0] if row else None, datetime.now(timezone.utc).isoformat(), ua, ip))
        log.info("join link tapped: code=%s igsid=%s", code, row[0] if row else None)
        if row and person_tap(ua, ip):
            activity("link", row[0], "opened their WhatsApp link")
    return whatsapp_page(workshop_link())


def reel_join(code, rid, igsid, request):
    """A reel's personal link: log the tap (reel_clicks), then forward to that reel's WhatsApp link."""
    r = next((r for r in reels() if r["id"] == rid), None)
    ua = request.headers.get("user-agent", "")
    if not any(b in ua.lower() for b in PREVIEW_BOTS):
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
        with db() as conn:
            conn.execute("INSERT INTO reel_clicks VALUES (?, ?, ?, ?, ?, ?)",
                         (code, rid, igsid, now().isoformat(), ua, ip))
        log.info("reel join link tapped: code=%s reel=%s igsid=%s", code, rid, igsid)
        if person_tap(ua, ip):
            activity("link", igsid, f"opened their link for {r['name'] if r else 'a reel'}")
    return RedirectResponse((r and r["wa_link"]) or workshop_link(), status_code=302)


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
