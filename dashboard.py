"""Local members dashboard: who followed, who joined WhatsApp (and with which number), who to remove.

python -m uvicorn dashboard:app --host 127.0.0.1 --port 8001   (started by members_keepalive.py)

Only listens on 127.0.0.1, so it never goes through the Cloudflare tunnel (that one points at port 8000).
WhatsApp joins/leaves come from wa/listener.js (live) and from uploaded chat exports (zip or txt);
they are matched to personal-link taps (`clicks`, recorded by main.py) by time, see "matching" below.
"""
import bisect
import ctypes
import io
import ipaddress
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import backfill
import bot_messages
import main

main.logging.getLogger("httpx").setLevel(main.logging.WARNING)
log = main.log
HERE = Path(__file__).resolve().parent
SAME_EVENT = timedelta(seconds=90)  # live event and export line this close are the same join/leave
# The community's main chat. The personal link joins people here; joins of its other groups (e.g. "free resin
# pooja thali workshop 11th Oct") are members who are already in, so they aren't matched to taps.
# Exports (chat '') are of the main chat.
MAIN_CHAT = os.getenv("WA_MAIN_CHAT", "Karwa Chauth special worshop 18th Oct")
# Earlier workshops' main chats: their personal links joined people there, so those joins still match (and keep
# showing numbers) after WA_MAIN_CHAT moves on to the next workshop's group.
PAST_MAIN_CHATS = ["Karwa Chauth special worshop 18th Oct"]
# People whose link, tap or join is at or after this time belong to the current workshop; the rest sit below a line.
WORKSHOP_SINCE = datetime.fromisoformat(os.getenv("WORKSHOP_SINCE", "2026-10-12T17:50:00+05:30"))
# The three above are only used until workshops are set up (Workshop tab, see main.workshops); from then on each
# workshop has its own group and start, and every workshop's group counts as main.
LOG_LINE = re.compile(r"^(\S+ \S+),\d+ INFO (?:DM trigger|button tap) from (\d+)(?: \(.*?\))?: ")

app = FastAPI()


def db():
    conn = main.db()
    conn.execute("CREATE TABLE IF NOT EXISTS wa_events (who TEXT, kind TEXT, at TEXT, chat TEXT, source TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS removed (igsid TEXT PRIMARY KEY, at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # matches settled by hand on the page: verdict 'yes' (this number is them) or 'no' (it isn't; who '' = none)
    conn.execute("CREATE TABLE IF NOT EXISTS match_fix (igsid TEXT, who TEXT, verdict TEXT, at TEXT, "
                 "PRIMARY KEY (igsid, who))")
    # names for the name check: WhatsApp profile names (from the listener) and Instagram display names
    conn.execute("CREATE TABLE IF NOT EXISTS wa_names (who TEXT PRIMARY KEY, name TEXT, username TEXT, at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ig_names (igsid TEXT PRIMARY KEY, name TEXT, at TEXT)")
    return conn


def get_meta(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


# ---------- WhatsApp events ----------

def clean(s):
    return re.sub("[‎‏‪‬﻿\r]", "", s).replace(" ", " ").replace("\xa0", " ").strip()


def norm_who(who):
    """'+91 98765 43210' -> '+919876543210'; saved-contact names stay as they are."""
    who = clean(who).strip("~ ")
    digits = re.sub(r"[^\d]", "", who)
    return "+" + digits if re.fullmatch(r"\+?[\d\s()-]{8,}", who) else who


def add_event(who, kind, at, chat="", source="export"):
    """Store a join/leave unless the same one is already there (live and export see the same event)."""
    who, at = norm_who(who), at.astimezone(timezone.utc)
    with db() as conn:
        near = conn.execute("SELECT 1 FROM wa_events WHERE who = ? AND kind = ? AND at BETWEEN ? AND ?",
                            (who, kind, (at - SAME_EVENT).isoformat(), (at + SAME_EVENT).isoformat())).fetchone()
        if near:
            return False
        conn.execute("INSERT INTO wa_events VALUES (?, ?, ?, ?, ?)", (who, kind, at.isoformat(), chat, source))
    return True


# iPhone: "[05/10/26, 11:03:05 AM] Sender: body"   Android: "05/10/26, 11:03 am - body"
IOS = re.compile(r"^\[(\d{1,2}/\d{1,2}/\d{2,4}), (\d{1,2}:\d\d(?::\d\d)?)(?: ?([AaPp][Mm]))?\] [^:]*: (.*)$")
ANDROID = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4}), (\d{1,2}:\d\d(?::\d\d)?)(?: ?([AaPp]\.? ?[Mm]\.?))? - (.*)$")


def parse_time(date, clock, ampm):
    ampm = (ampm or "").replace(".", "").replace(" ", "").upper()
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            day = datetime.strptime(date, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    parts = [int(x) for x in clock.split(":")] + [0]
    h, m, sec = parts[0], parts[1], parts[2]
    if ampm == "PM" and h != 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return day.replace(hour=h, minute=m, second=sec).astimezone()  # export times are the phone's local time


def split_people(text):
    return [p for p in re.split(r", | and ", text) if p]


def events_in_line(body):
    """(who, kind) pairs from a system message, e.g. '+91 … joined using a community link'."""
    if m := re.fullmatch(r"(.+?) joined(?: using .*)?", body):
        return [(m[1], "join")]
    if m := re.fullmatch(r"(.+?) left", body):
        return [(m[1], "leave")]
    if m := re.fullmatch(r"(?:You|.+?) removed (.+)", body):
        return [(p, "leave") for p in split_people(m[1])]
    if m := re.fullmatch(r"(?:You|.+?) added (.+)", body):
        return [(p, "join") for p in split_people(m[1])]
    return []


def import_export(data, filename=""):
    """Read a WhatsApp chat export (.zip or .txt bytes); returns (joins/leaves found, new ones added)."""
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            txt = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not txt:
                raise ValueError("no .txt chat file inside the zip")
            data = z.read(txt)
    found = added = 0
    latest = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = clean(line)
        m = IOS.match(line) or ANDROID.match(line)
        if not m:
            continue
        at = parse_time(m[1], m[2], m[3])
        if not at:
            continue
        latest = max(latest or at, at)
        for who, kind in events_in_line(m[4]):
            found += 1  # Android exports only give the minute, which makes timing matches much looser
            added += add_event(who, kind, at, source="export" if m[2].count(":") == 2 else "export-min")
    if found:
        set_meta("export", {"uploaded_at": main.now().isoformat(), "latest": latest.isoformat(),
                            "found": found, "added": added})
    return found, added


# ---------- follow checks ----------

def backfill_users_from_log():
    """People who DMed before the users table existed are only in uvicorn.log."""
    first = {}
    with open(HERE / "uvicorn.log", encoding="utf-8", errors="replace") as f:
        for line in f:
            if m := LOG_LINE.match(line.lstrip("﻿")):
                at = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc).isoformat()
                lo, hi = first.get(m[2], (at, at))
                first[m[2]] = (min(lo, at), max(hi, at))
    with db() as conn:
        conn.executemany("INSERT OR IGNORE INTO users (igsid, first_seen, last_seen) VALUES (?, ?, ?)",
                         [(igsid, lo, hi) for igsid, (lo, hi) in first.items()])
        conn.execute("INSERT OR IGNORE INTO users (igsid, username, first_seen, last_seen) "
                     "SELECT igsid, username, sent_at, sent_at FROM sent")


def recheck_everyone():
    """Manual 'Re-check follows': everyone, not only link holders (main.audit_follows does those every 2h)."""
    backfill_users_from_log()
    with db() as conn:
        people = conn.execute("SELECT igsid, username, follows FROM users").fetchall()
        sent = {r[0] for r in conn.execute("SELECT igsid FROM sent")}
    set_meta("recheck", {"running": True, "done": 0, "total": len(people)})
    for i, (igsid, username, before) in enumerate(people, 1):
        follows, api_username = main.check_follow(igsid)
        if follows is not None:
            cols = dict(follows=int(follows), follow_checked_at=main.now().isoformat(), username=api_username or username)
            if igsid in sent and not follows and before != 0:
                cols["unfollowed_at"] = main.now().isoformat()
            elif follows:
                cols["unfollowed_at"] = None
            main.update_user(igsid, **cols)
        if i % 10 == 0:
            set_meta("recheck", {"running": True, "done": i, "total": len(people)})
        time.sleep(0.5)
    set_meta("recheck", {"running": False, "done": len(people), "total": len(people), "at": main.now().isoformat()})


# ---------- matching ----------
# Which WhatsApp number belongs to which Instagram user is worked out from tap and join times. Every match gets
# a probability and is only called "sure" at 99% or more. What the first live day (5 Oct) showed:
# - Most logged taps aren't people. Meta fetches every link the bot sends from its own servers, right away
#   and again later, with made-up desktop or "Android 10; K" user agents. People open the link in Instagram's
#   in-app browser, which puts "Instagram" in the user agent.
# - People join 6.5-32 s after their tap (median 12 s), never before it.
# - Each join is one person and each person joins with one number. When two people tap seconds apart and
#   two numbers join, either way round fits, so both stay "check" until something tells them apart: another
#   join, a WhatsApp profile name that matches, or a click on the page.
SURE = 0.99
SHOW_FROM = 0.02  # chances below this are left out (a 1% "maybe" is noise)
P_JOIN = 0.8  # share of people who tap and then join
MIN_GAP, MAX_GAP = 1, 240  # seconds from tap to join
STRANGER_PRIOR = (1, 4 * 3600)  # joins that follow nobody's tap: 1 per 4 h until the live data says otherwise
NAME_LR, COMMON_NAME_LR = 30, 3  # how much a matching profile name counts (a shared common surname, less)
COMMON_NAMES = {"kumar", "kumari", "devi", "singh", "sharma", "verma", "gupta", "patel", "shah", "jain", "yadav",
                "khan", "agarwal", "agrawal", "mehta", "joshi", "reddy", "rani", "rajput", "thakur", "chauhan",
                "mishra", "pandey", "tiwari", "dubey", "shukla", "sahu", "chaudhary", "choudhary", "soni"}
NOT_NAMES = {"official", "resin", "arts", "craft", "crafts", "creation", "creations", "studio", "india", "queen",
             "baby", "love", "life", "world", "king", "girl", "happy", "beauty", "insta", "with", "home", "shop",
             "store", "mommy", "mummy", "account", "available", "busy", "the"}
META_NETS = [ipaddress.ip_network(n) for n in (  # Meta's own addresses (AS32934): link scanners, not people
    "31.13.24.0/21", "31.13.64.0/18", "45.64.40.0/22", "57.141.0.0/16", "57.144.0.0/14", "66.220.144.0/20",
    "69.63.176.0/20", "69.171.224.0/19", "74.119.76.0/22", "102.132.96.0/20", "103.4.96.0/22", "129.134.0.0/16",
    "147.75.208.0/20", "157.240.0.0/16", "163.70.128.0/17", "173.252.64.0/18", "179.60.192.0/22",
    "185.60.216.0/22", "185.89.216.0/22", "204.15.20.0/22", "2620:0:1c00::/40", "2a03:2880::/32")]


def ts(s):
    return datetime.fromisoformat(s)


def from_meta(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in META_NETS)


IN_APP = (" Instagram ", "FB_IAB/", "FBAN/")  # in-app browsers of Instagram, and of Facebook / Messenger


def is_person(ua, ip):
    """Whether a tap came from someone's phone: Instagram's in-app browser (Android and iPhone), or Facebook's /
    Messenger's for links sent on Messenger, not from Meta's servers. Other user agents aren't counted: the
    scanners also come from cloud servers (AWS) with phone-like user agents, so a link opened in some other
    browser can't be told apart from them."""
    return any(app in ua for app in IN_APP) and not (ip and from_meta(ip))


def real_taps(clicks):
    """{igsid: [times]} of taps people made, from (igsid, time, user agent, ip) rows. One phone (same user
    agent and IP) that opened several people's links is the account owner reading DMs, so it counts for none."""
    phones = defaultdict(set)
    for igsid, _, ua, ip in clicks:
        phones[ua, ip].add(igsid)
    taps = defaultdict(list)
    for igsid, at, ua, ip in clicks:
        if is_person(ua, ip) and len(phones[ua, ip]) == 1:
            taps[igsid].append(at)
    return taps


def lognorm(x, median, sigma):
    return math.exp(-math.log(x / median) ** 2 / (2 * sigma * sigma)) / (x * sigma * math.sqrt(2 * math.pi))


def delay_like(gap):
    """Likelihood of joining `gap` seconds after the tap. Fit to the 26 sure matches of 5 Oct (6.5-32 s, none
    faster, median 12 s), plus a tail for slow people that adds nothing to the fast end."""
    if not MIN_GAP <= gap <= MAX_GAP:
        return 0.0
    return 0.85 * lognorm(gap, 12, 0.43) + 0.15 * lognorm(gap, 60, 0.7)


def join_like(join_at, minute_only, taps):
    """How well one join fits one person's taps (best tap). Export lines without seconds: any time in the minute."""
    best = 0.0
    for tap in taps:
        gap = (join_at - tap).total_seconds() + 0.5  # WhatsApp times are cut to whole seconds
        like = sum(delay_like(gap + s) for s in range(0, 60, 2)) / 30 if minute_only else delay_like(gap)
        best = max(best, like)
    return best


def words(s):
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in NOT_NAMES}


def name_like(wa, ig):
    """How much more likely WhatsApp profile (name, username) is Instagram user (username, name) than someone
    else is: same username, or a shared name word, e.g. "Rani Jaiswal" and jaiswalrani_2007."""
    (wa_name, wa_user), (ig_user, ig_name) = wa, ig
    if wa_user and ig_user and wa_user.lower().lstrip("@") == ig_user.lower():
        return NAME_LR * 10
    chunks = re.findall("[a-z]+", f"{ig_user or ''} {ig_name or ''}".lower())
    wa_parts = set(re.findall("[a-z]+", (wa_name or "").lower()))

    def fits(w, c):
        """Usernames run words together ("jaiswalrani"): the name must be a whole word, or what's left of the
        username must be another word (4+ letters, or part of the WhatsApp name), so "priya" isn't "priyanka"."""
        rest = c[len(w):] if c.startswith(w) else c[:-len(w)] if c.endswith(w) else None
        return c == w or (rest is not None and (len(rest) >= 4 or rest in wa_parts))

    shared = {w for w in words(wa_name) if any(fits(w, c) for c in chunks)}
    if not shared:
        return 1.0
    return COMMON_NAME_LR if shared <= COMMON_NAMES else NAME_LR


def solve(edges, stranger):
    """Probabilities for joins that share candidates, in time order. edges[i] = {igsid: weight} for join i; a
    join can also be someone who never tapped (weight `stranger`), and each person is at most one join.
    Sums over every way of pairing them up (forward-backward over the joins). Returns [{igsid|None: p}]."""
    k = len(edges)
    later = [frozenset()] * (k + 1)  # people who can still be used from join i on; the rest drop out of the state
    for i in range(k - 1, -1, -1):
        later[i] = later[i + 1] | frozenset(edges[i])

    def steps(i, used):
        for who, w in [(None, stranger)] + [(u, w) for u, w in edges[i].items() if u not in used]:
            yield who, w, (used | {who} if who else used) & later[i + 1]

    fwd = [{frozenset(): 1.0}]
    for i in range(k):
        nxt = defaultdict(float)
        for used, a in fwd[i].items():
            for _, w, after in steps(i, used):
                nxt[after] += a * w
        if len(nxt) > 20000:  # a very long busy stretch: drop the least likely states
            nxt = dict(sorted(nxt.items(), key=lambda kv: -kv[1])[:20000])
        total = sum(nxt.values())
        fwd.append({s: v / total for s, v in nxt.items()})
    bwd = [{}] * k + [{frozenset(): 1.0}]
    for i in range(k - 1, -1, -1):
        cur = {used: sum(w * bwd[i + 1].get(after, 0.0) for _, w, after in steps(i, used)) for used in fwd[i]}
        total = sum(cur.values()) or 1.0
        bwd[i] = {s: v / total for s, v in cur.items()}
    out = []
    for i in range(k):
        p = defaultdict(float)
        for used, a in fwd[i].items():
            for who, w, after in steps(i, used):
                p[who] += a * w * bwd[i + 1].get(after, 0.0)
        total = sum(p.values()) or 1.0
        out.append({who: v / total for who, v in p.items()})
    return out


def match(events, clicks, fixes, ig_info, wa_names, now):
    """Pairs WhatsApp joins with Instagram users.
    events: (time, who, kind, source) sorted by time; clicks: (igsid, time, ua, ip); fixes: (igsid, who, verdict);
    ig_info: {igsid: (username, display name)}; wa_names: {who: (profile name, username)}.
    Returns ({igsid: match}, {who: [(igsid, p)]}) where a match is {conf, who, p, joined, options, p_none}."""
    taps = real_taps(clicks)
    timeline = sorted((at, ig) for ig, times in taps.items() for at in times)
    confirmed = {ig: who for ig, who, verdict in fixes if verdict == "yes"}
    ruled_out = {(ig, who) for ig, who, verdict in fixes if verdict == "no"}
    gone = {ig for ig, who in ruled_out if who == ""}  # "didn't join with any of these numbers"
    first_join = {}
    for at, who, kind, _ in events:
        if kind == "join":
            first_join.setdefault(who, at)

    # one join per number: its first join that follows somebody's tap (a later rejoin is the same person)
    items, done, unexplained = [], set(confirmed.values()), 0
    start = max(timeline[0][0], next((at for at, *_, src in events if src == "live"), timeline[0][0])) \
        if timeline else now
    for at, who, kind, source in events:
        if kind != "join" or who in done:
            continue
        lo = bisect.bisect_left(timeline, (at - timedelta(seconds=MAX_GAP + 60),))
        near = {ig for t, ig in timeline[lo:bisect.bisect_right(timeline, (at, chr(0x10ffff)))]}
        edges = {}
        for ig in near - set(confirmed) - gone:
            like = join_like(at, source == "export-min", taps[ig]) if (ig, who) not in ruled_out else 0
            if like:
                ig_name = ig_info.get(ig, (None, None))
                edges[ig] = P_JOIN / (1 - P_JOIN) * like * name_like(wa_names.get(who, (None, None)), ig_name)
        if edges:
            done.add(who)
            items.append((at, who, edges))
        elif source == "live" and at > start:
            unexplained += 1
    stranger = (STRANGER_PRIOR[0] + unexplained) / (STRANGER_PRIOR[1] + max(0, (now - start).total_seconds()))

    # joins that share a candidate are solved together
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_item = {}
    for i, (_, _, edges) in enumerate(items):
        for ig in edges:
            if ig in first_item:
                parent[root(i)] = root(first_item[ig])
            else:
                first_item[ig] = i
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[root(i)].append(i)
    probs = [None] * len(items)
    for idx in groups.values():
        for i, p in zip(idx, solve([items[i][2] for i in idx], stranger)):
            probs[i] = p

    per_who = {items[i][1]: sorted(((ig, v) for ig, v in p.items() if ig and v >= SHOW_FROM), key=lambda x: -x[1])
               for i, p in enumerate(probs)}
    results = {ig: {"conf": "confirmed", "who": who, "p": 1.0, "joined": first_join.get(who), "options": [],
                    "p_none": 0.0} for ig, who in confirmed.items()}
    options = defaultdict(list)
    for i, p in enumerate(probs):
        for ig, v in p.items():
            if ig and v >= SHOW_FROM:
                options[ig].append((v, i))
    for ig, opts in options.items():
        opts.sort(reverse=True)
        best, i = opts[0]
        p_none = max(0.0, 1 - sum(v for v, _ in opts))
        results[ig] = {
            "conf": "sure" if best >= SURE else "maybe", "who": items[i][1] if best >= SURE else None,
            "p": best, "joined": items[i][0] if best >= SURE else None, "p_none": p_none,
            "options": [{"who": items[i][1], "p": v, "joined": items[i][0]} for v, i in opts]}
    return results, per_who


def main_group():
    """The WhatsApp group of the current (or, between workshops, the last) workshop: personal links lead there."""
    rows = main.workshops()
    return rows[-1]["wa_group"] if rows else MAIN_CHAT


def all_groups():
    """Every workshop's group: joins in any of them are arrivals through a personal link."""
    rows = main.workshops()
    return [w["wa_group"] for w in rows if w["wa_group"]] if rows else [MAIN_CHAT, *PAST_MAIN_CHATS]


def since():
    """Start of the current (or last) workshop."""
    rows = main.workshops()
    return rows[-1]["started"] if rows else WORKSHOP_SINCE


def main_chat(events):
    """(time, who, kind, source, chat) events -> (the main chat's events without chat, {who: (kind, time)}).
    Membership comes from the main chat, or from the other groups for people we've never seen there."""
    norm = lambda chat: " ".join(chat.lower().split())
    mains = {norm(c) for c in all_groups()}
    is_main = lambda chat: not chat or norm(chat) in mains
    state = {}
    for at, who, kind, _, chat in events:
        if is_main(chat) or who not in state or not state[who][2]:
            state[who] = (kind, at, is_main(chat))
    return ([e[:4] for e in events if is_main(e[4])],
            {who: (kind, at) for who, (kind, at, _) in state.items()})


def build():
    """Everything the page shows, recomputed from the database on each request."""
    with db() as conn:
        users = conn.execute("SELECT igsid, username, follows, follow_checked_at, unfollowed_at, first_seen, last_msg_at, "
                             "warned_at, warn_tried_at, warn_error FROM users").fetchall()
        sent = dict(conn.execute("SELECT igsid, sent_at FROM sent"))
        clicks = [(igsid, ts(at), ua or "", ip) for igsid, at, ua, ip in conn.execute(
            "SELECT igsid, clicked_at, user_agent, ip FROM clicks WHERE igsid IS NOT NULL ORDER BY clicked_at")]
        events = [(ts(at), who, kind, source, chat) for who, kind, at, source, chat in
                  conn.execute("SELECT who, kind, at, source, chat FROM wa_events ORDER BY at")]
        removed = {igsid: ts(at) for igsid, at in conn.execute("SELECT igsid, at FROM removed")}
        fixes = conn.execute("SELECT igsid, who, verdict FROM match_fix").fetchall()
        wa_names = {who: (name, username) for who, name, username in
                    conn.execute("SELECT who, name, username FROM wa_names")}
        ig_names = dict(conn.execute("SELECT igsid, name FROM ig_names"))

    events, state = main_chat(events)
    names = {u[0]: u[1] for u in users}
    ig_info = {ig: (names.get(ig), ig_names.get(ig)) for ig in {c[0] for c in clicks}}
    matches, per_who = match(events, clicks, fixes, ig_info, wa_names, main.now())

    last_click = {ig: max(times) for ig, times in real_taps(clicks).items()}  # Meta's scanner hits don't count
    users += [(ig,) + (None,) * 9 for ig in last_click if ig not in names]  # e.g. test links
    label = lambda ig: names.get(ig) or ig
    in_group = lambda who: state.get(who, ("join",))[0] == "join"

    wss = main.workshops()
    starts = [w["started"] for w in wss]
    running = main.workshop() if wss else None
    rows = []
    for igsid, username, follows, checked, unfollowed_at, first_seen, last_msg, warned, tried, warn_error in users:
        got_link = igsid in sent
        if follows is None:
            f = "unknown"
        elif got_link:
            f = "following" if follows else "unfollowed"
        else:
            f = "no_link" if follows else "dm_only"
        m = matches.get(igsid)
        group, left_at = "none", None
        if m and m["who"]:
            kind, at = state.get(m["who"], ("join", m["joined"]))
            group = "in" if kind == "join" else "left"
            left_at = at if kind == "leave" else None
        options = [{"who": o["who"], "p": round(o["p"], 3), "joined": o["joined"].isoformat(),
                    "in_group": in_group(o["who"]), "wa_name": wa_names.get(o["who"], ("",))[0],
                    "others": [{"igsid": ig, "name": label(ig), "p": round(v, 3)}
                               for ig, v in per_who.get(o["who"], []) if ig != igsid],
                    } for o in (m["options"] if m else [])]
        # DM-only people have no link or tap, so their last DM counts too
        stamps = [sent.get(igsid), last_msg, first_seen, last_click[igsid].isoformat() if igsid in last_click else None,
                  m["joined"].isoformat() if m and m["joined"] else None]
        if wss:  # the workshop their latest link, tap, DM or join falls in
            last = max((datetime.fromisoformat(s) for s in stamps if s), default=None)
            ws = wss[max(0, bisect.bisect_right(starts, last) - 1)]["id"] if last else wss[0]["id"]
            current = bool(running) and ws == running["id"]
        else:
            ws, current = None, any(datetime.fromisoformat(s) >= WORKSHOP_SINCE for s in stamps if s)
        # unfollowers from a workshop that has ended are left alone
        cleared = lambda at: igsid in removed and at and removed[igsid] >= at
        to_remove = current and f == "unfollowed" and group == "in" and not cleared(m["joined"])
        # unfollowed, and maybe one of these numbers: worth a look before removing anyone
        to_check = (current and f == "unfollowed" and bool(m) and m["conf"] == "maybe"
                    and sum(o["p"] for o in options if o["in_group"]) >= 0.2 and not cleared(m["options"][0]["joined"]))
        rows.append({
            "igsid": igsid, "username": username, "follow": f, "got_link": got_link, "current": current,
            "link_sent_at": sent.get(igsid), "unfollowed_at": unfollowed_at, "checked_at": checked,
            "clicked_at": last_click[igsid].isoformat() if igsid in last_click else None,
            "joined_at": m["joined"].isoformat() if m and m["joined"] else None,
            "who": m["who"] if m else None, "conf": m["conf"] if m else None, "p": round(m["p"], 3) if m else None,
            "wa_name": wa_names.get(m["who"], ("",))[0] if m and m["who"] else "",
            "options": options, "p_none": round(m["p_none"], 3) if m else None,
            "group": group, "left_at": left_at.isoformat() if left_at else None,
            "to_remove": to_remove, "to_check": to_check,
            "removed_at": removed[igsid].isoformat() if igsid in removed else None, "first_seen": first_seen,
            "ws": ws, "last_msg_at": last_msg,
            # the unfollow warning (Send warning), only if it was in this workshop
            "warned_at": warned if main.this_workshop(warned) else None,
            "warn_tried_at": tried if main.this_workshop(tried) else None,
            "warn_error": warn_error if main.this_workshop(tried) else None,
        })
    # WhatsApp members nobody's tap explains (mostly people who joined before personal links existed)
    linked = {m["who"] for m in matches.values() if m["who"]} | {w for w, opts in per_who.items() if opts}
    wa_only = [{"who": who, "group": "in" if kind == "join" else "left", "at": at.isoformat(),
                "wa_name": wa_names.get(who, ("",))[0]} for who, (kind, at) in state.items() if who not in linked]
    return {"rows": rows, "wa_only": wa_only, "export": get_meta("export"), "recheck": get_meta("recheck"),
            "wa": get_meta("wa", {"state": "not started"}), "audit_every_h": main.AUDIT_EVERY / 3600,
            "now": main.now().isoformat(), "workshops": [ws_public(w) for w in wss],
            "running_id": running["id"] if running else None, "ws_stats": ws_stats(rows, wss), "warn": _warn}


def ws_public(w):
    """A workshop for the page (without its texts)."""
    return {k: w.get(k) for k in ("id", "name", "date", "started_at", "closed_at", "wa_group", "wa_link", "meet_url",
                                  "past_offer", "stats")}


def ws_stats(rows, wss):
    """Numbers per workshop id: people (latest activity in it), links sent, taps and joins inside it, and the
    current state of its people (in group, unfollowed, removed, warned)."""
    if not wss:
        return {}
    bounds = {w["id"]: (w["started"], wss[i + 1]["started"] if i + 1 < len(wss) else None) for i, w in enumerate(wss)}

    def inside(wid, at):
        if not at:
            return False
        lo, hi = bounds[wid]
        t = datetime.fromisoformat(at)
        return t >= lo and (hi is None or t < hi)

    out = {w["id"]: dict(people=0, links=0, tapped=0, joined=0, in_group=0, unfollowed=0, removed=0, warned=0)
           for w in wss}
    for r in rows:
        st = out[r["ws"]]
        st["people"] += 1
        st["links"] += inside(r["ws"], r["link_sent_at"])
        st["tapped"] += inside(r["ws"], r["clicked_at"])
        st["joined"] += inside(r["ws"], r["joined_at"])
        st["in_group"] += r["group"] == "in"
        st["unfollowed"] += r["follow"] == "unfollowed"
        st["removed"] += bool(r["removed_at"])
        st["warned"] += bool(r["warned_at"])
    return out


# state of this dashboard's own background loops, for the Activity tab (the bot's are in the jobs table)
_loops = defaultdict(dict)


def loop_state(name, **kw):
    _loops[name].update(kw)


def ig_names_loop():
    """Instagram display names of everyone who tapped a link, for the name check (one lookup per person)."""
    while True:
        loop_state("ig_names", started_at=main.now().isoformat(), running=1, error=None)
        try:
            with db() as conn:
                todo = [r[0] for r in conn.execute("SELECT DISTINCT igsid FROM clicks WHERE igsid IS NOT NULL "
                                                   "AND igsid NOT IN (SELECT igsid FROM ig_names)")]
            for igsid in todo:
                data = main.api("GET", igsid, params={"fields": "name"}) or {}
                with db() as conn:
                    conn.execute("INSERT OR REPLACE INTO ig_names VALUES (?, ?, ?)",
                                 (igsid, data.get("name") or "", main.now().isoformat()))
                time.sleep(0.5)
            loop_state("ig_names", result=f"{len(todo)} new names looked up")
        except Exception as e:
            log.exception("Instagram name lookup failed")
            loop_state("ig_names", error=str(e))
        loop_state("ig_names", ended_at=main.now().isoformat(), running=0,
                   next_at=(main.now() + timedelta(seconds=300)).isoformat())
        time.sleep(300)


# ---------- popup when someone new needs removing ----------

def refresh_used_links(rows):
    """Personal links stop working (main.join) once we're sure the person joined the current group with it:
    a 99%+ match or one settled by hand, to a join in WA_MAIN_CHAT since WORKSHOP_SINCE. "Check" stays open.
    Recomputed each time, so a "Not them" on the page reopens the link."""
    norm = lambda chat: " ".join((chat or "").lower().split())
    start, group = since(), norm(main_group())
    with db() as conn:
        joined_main = {who for who, chat in conn.execute(
            "SELECT who, chat FROM wa_events WHERE kind = 'join' AND at >= ?",
            (start.astimezone(timezone.utc).isoformat(),)) if group and norm(chat) == group}
        used = [(r["igsid"], r["who"], r["joined_at"]) for r in rows
                if r["conf"] in ("sure", "confirmed") and r["who"] in joined_main and r["joined_at"]
                and datetime.fromisoformat(r["joined_at"]) >= start]
        before = {ig for ig, in conn.execute("SELECT igsid FROM used_links")}
        conn.execute("DELETE FROM used_links")
        conn.executemany("INSERT INTO used_links VALUES (?, ?, ?)", used)
    now_used = {ig: who for ig, who, _ in used}
    added, gone = set(now_used) - before, before - set(now_used)
    ws_id = (main.workshops() or [{}])[-1].get("id")
    switched, _used_ws["id"] = _used_ws["id"] not in (None, ws_id), ws_id
    if added or gone:
        log.info("used links: %d (+%d -%d)", len(now_used), len(added), len(gone))
        if switched or len(added) + len(gone) > 10:  # a new workshop starts with no used links
            main.activity("wa", None, f"Used-up links: {len(now_used)} now ({len(added)} closed, {len(gone)} opened again)"
                          + (" because the workshop changed" if switched else ""))
        else:
            for ig in added:
                main.activity("wa", ig, f"joined WhatsApp as {now_used[ig]}: their link now shows \"already used\"")
            for ig in gone:
                main.activity("wa", ig, "match undone: their link opens the group again")
    return len(now_used)


_used_ws = {"id": None}  # the workshop refresh_used_links last saw


def notify_loop():
    while True:
        loop_state("notify", started_at=main.now().isoformat(), running=1, error=None)
        try:
            rows = build()["rows"]
            used = refresh_used_links(rows)
            todo = {r["igsid"]: r for r in rows if r["to_remove"] or r["to_check"]}
            loop_state("notify", result=f"{len(todo)} to remove or check · {used} links used up")
            told = set(get_meta("notified", []))
            new = [r for ig, r in todo.items() if ig not in told]
            if new:
                names = "\n".join(f"@{r['username'] or r['igsid']}  " + (r["who"] if r["to_remove"] else "check: " + " / ".join(
                    f"{o['who']} {o['p']:.0%}" for o in r["options"])) for r in new[:10])
                threading.Thread(target=ctypes.windll.user32.MessageBoxW, daemon=True, args=(
                    0, f"Unfollowed on Instagram but still in WhatsApp:\n\n{names}\n\nOpen http://127.0.0.1:8001",
                    "Resin members", 0x40000)).start()
            set_meta("notified", sorted(told | set(todo)))
        except Exception as e:
            log.exception("notify check failed")
            loop_state("notify", error=str(e))
        loop_state("notify", ended_at=main.now().isoformat(), running=0,
                   next_at=(main.now() + timedelta(seconds=60)).isoformat())
        time.sleep(60)


# ---------- the Instagram bot: is Meta reaching it, and a button that brings it back ----------
# keepalive.py restarts the bot and the tunnel when they die, but a new tunnel has a new address, and Meta keeps
# sending to the old one until the Callback URL is pasted in again (that was the 13 h outage on 6 Oct).
# So "live" means: a webhook arrived after the current tunnel address came up.

URL_FILE = HERE / "webhook_url.txt"
BOT_LOG = HERE / "uvicorn.log"
TUNNEL_LOG = HERE / "tunnel.log"
PYW = str(HERE / ".venv" / "Scripts" / "pythonw.exe")
NO_WINDOW = subprocess.CREATE_NO_WINDOW
BOT_EVENTS = (("webhook", " INFO webhook received"), ("verified", " INFO webhook verification OK"),
              ("rejected", " WARNING rejected GET /webhook"))
_bot = {"log_pos": 0, "webhook": None, "verified": None, "rejected": None, "url": None, "url_since": None,
        "local_ok": None, "tunnel_ok": None, "checked_at": None, "fix": None, "missed": None, "catchup": None}
_bot_wake = threading.Event()


def scan_bot_log():
    """Latest webhook / verification / rejected-verification times, reading only what was added since last time."""
    size = BOT_LOG.stat().st_size
    if size < _bot["log_pos"]:
        _bot["log_pos"] = 0  # log was cleared
    with open(BOT_LOG, "rb") as f:
        f.seek(_bot["log_pos"])
        chunk = f.read(size - _bot["log_pos"])
    end = chunk.rfind(b"\n") + 1  # a half-written last line waits for the next scan
    _bot["log_pos"] += end
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        for key, marker in BOT_EVENTS:
            # uvicorn's own lines ("ERROR:    [Errno 10048] ...") have no timestamp
            if marker in line and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line):
                _bot[key] = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").astimezone()


def tunnel_since(url):
    """When cloudflared first printed this tunnel address (tunnel.log, UTC); falls back to now."""
    host = re.escape(url.split("//")[-1].split("/")[0])
    try:
        with open(TUNNEL_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(rf"(\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ).*\|\s+https://{host}\s", line)
                if m:
                    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except OSError:
        pass
    return main.now()


def answers(base):
    """True if uvicorn main:app answers at base. /openapi.json has no side effects and isn't logged, unlike
    /webhook verification (that line is how we spot Meta's own verify attempts)."""
    try:
        return httpx.get(f"{base}/openapi.json", timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


def check_bot():
    url = URL_FILE.read_text().strip() if URL_FILE.exists() else ""
    if url != _bot["url"]:
        _bot["url"], _bot["url_since"] = url, tunnel_since(url) if url else None
    _bot["local_ok"] = answers("http://127.0.0.1:8000")
    base = url.removesuffix("/webhook")
    # a failed check is repeated once so an internet blip doesn't look like a dead tunnel
    _bot["tunnel_ok"] = bool(base) and (answers(base) or answers(base))
    _bot["checked_at"] = main.now()
    loop_state("bot_health", ended_at=main.now().isoformat(), running=0, next_at=(main.now() + timedelta(seconds=30)).isoformat(),
               result={"live": "Live: Instagram is reaching the bot", "verified": "Meta accepted the link, waiting for a DM",
                       "meta": "Meta still sends to an old link (Members tab → Instagram bot)",
                       "tunnel": "The bot's link is dead (Members tab → Make bot active)", "down": "The bot isn't running",
                       "off": "The bot is turned off"}.get(bot_state(), bot_state()))


def bot_health_loop():
    last_check = 0
    while True:
        try:
            scan_bot_log()
            if _bot_wake.is_set() or time.time() - last_check > 30:
                _bot_wake.clear()
                check_bot()
                last_check = time.time()
        except Exception:
            log.exception("bot health check failed")
        _bot_wake.wait(5)


def bot_state():
    if _bot["checked_at"] is None:
        return "checking"
    if not _bot["local_ok"]:
        return "down" if autostart("bot") else "off"  # turned off on purpose (Background apps card)
    if not _bot["tunnel_ok"]:
        return "tunnel"
    since = _bot["url_since"]
    if _bot["webhook"] and since and _bot["webhook"] >= since:
        return "live"
    if _bot["verified"] and since and _bot["verified"] >= since:
        return "verified"  # Meta accepted the new link; the first message will make it "live"
    return "meta"


KEEPALIVE_CMD = re.compile(r'\.exe"?\s+"?(?:[^"]*[\\/])?keepalive\.py"?\s*$')  # not members_keepalive.py
BOT_CMD = re.compile(r'\.exe"?\s+-m uvicorn main:app\b')
OTHER_CMDS = {"ads_keepalive": "admanager_keepalive.py", "ads": "uvicorn admanager:app",
              "members_keepalive": "members_keepalive.py", "members": "uvicorn dashboard:app"}


def processes():
    """Running processes of the bot (keepalive.py, uvicorn main:app, cloudflared), the ad manager and this
    dashboard (with its WhatsApp listener): {name: [pid, ...]}."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or "
                          "Name='cloudflared.exe' or Name='node.exe'\" | Select-Object ProcessId,Name,CommandLine "
                          "| ConvertTo-Json -Compress"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW).stdout.strip()
    rows = json.loads(out) if out else []
    found = {"keepalive": [], "bot": [], "tunnel": [], "listener": [], **{k: [] for k in OTHER_CMDS}}
    for r in [rows] if isinstance(rows, dict) else rows:
        cmd = r["CommandLine"] or ""
        if r["Name"] == "cloudflared.exe":
            found["tunnel"].append(r["ProcessId"])
        elif r["Name"] == "node.exe":
            if cmd.rstrip().endswith("listener.js"):
                found["listener"].append(r["ProcessId"])
        elif KEEPALIVE_CMD.search(cmd):
            found["keepalive"].append(r["ProcessId"])
        elif BOT_CMD.search(cmd):
            found["bot"].append(r["ProcessId"])
        else:
            for key, part in OTHER_CMDS.items():
                if part in cmd:
                    found[key].append(r["ProcessId"])
    return found


def kill(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)


def start_detached(cmd):
    """Through WMI, so the process doesn't count as a child of the dashboard and outlives its restarts."""
    cmd = cmd.replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Invoke-CimMethod Win32_Process -MethodName Create -Arguments "
                    f"@{{CommandLine='{cmd}'; CurrentDirectory='{HERE}'}}"],
                   capture_output=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def start_keepalive():
    start_detached(f'"{PYW}" "{HERE / "keepalive.py"}"')


def fix_bot(new_link=False):
    """Restart whatever is broken; keepalive.py brings killed processes back within 10 s.
    new_link: restart the tunnel even though it works (gives a new address that must go into Meta)."""
    def step(text):
        _bot["fix"] = {"running": True, "step": text, "at": main.now().isoformat()}
        log.info("bot fix: %s", text)

    try:
        step("Looking at what's running")
        check_bot()
        procs = processes()
        bot, tunnel = procs["bot"], procs["tunnel"]
        set_autostart("bot", True)  # a bot made active should also come back after a restart
        if not procs["keepalive"]:
            step("Starting the bot and the tunnel")
            kill(bot + tunnel)  # leftovers of a dead keepalive would hold port 8000 or an old tunnel
            start_keepalive()
        elif not _bot["local_ok"]:
            step("Restarting the bot")
            kill(bot)
        elif not _bot["tunnel_ok"] or new_link:
            step("Making a new link")
            kill(tunnel)
        else:
            _bot["fix"] = {"running": False, "step": "Bot and link are both working, nothing to restart",
                           "at": main.now().isoformat()}
            return
        for _ in range(30):  # up to 90 s
            time.sleep(3)
            check_bot()
            if _bot["local_ok"] and _bot["tunnel_ok"]:
                break
        ok = _bot["local_ok"] and _bot["tunnel_ok"]
        _bot["fix"] = {"running": False, "ok": ok, "at": main.now().isoformat(),
                       "step": "Done" if ok else "Still not working. Is the internet on?"}
    except Exception as e:
        log.exception("bot fix failed")
        _bot["fix"] = {"running": False, "ok": False, "step": f"Failed: {e}", "at": main.now().isoformat()}
    finally:
        _bot_wake.set()


def find_missed():
    _bot["missed"] = {"running": True}
    try:
        _bot["missed"] = {"running": False, "people": backfill.find_missed(), "at": main.now().isoformat()}
    except Exception as e:
        log.exception("missed DM lookup failed")
        _bot["missed"] = {"running": False, "error": str(e)}


def catch_up():
    shown = {p["igsid"] for p in (_bot["missed"] or {}).get("people") or []}
    _bot["catchup"] = {"running": True, "done": 0, "total": len(shown), "sent": 0}
    try:  # looked up again: anyone answered since the list was shown (by the bot or by hand) is left out
        people = [p for p in backfill.find_missed() if p["igsid"] in shown]
    except Exception:
        log.exception("missed DM lookup failed")
        people = []
    _bot["catchup"]["total"] = len(people)
    for p in people:
        try:
            _bot["catchup"]["sent"] += backfill.reply(p)
        except Exception:
            log.exception("catch-up reply to %s failed", p["igsid"])
        _bot["catchup"]["done"] += 1
    _bot["catchup"]["running"] = False
    find_missed()


def bot_status():
    iso = lambda d: d.isoformat() if d else None
    since = _bot["url_since"]
    rejected = _bot["rejected"] if _bot["rejected"] and since and _bot["rejected"] >= since else None
    return {"state": bot_state(), "url": _bot["url"], "url_since": iso(since), "verify_token": main.VERIFY_TOKEN,
            "last_webhook": iso(_bot["webhook"]), "verified": iso(_bot["verified"]), "rejected": iso(rejected),
            "checked_at": iso(_bot["checked_at"]), "fix": _bot["fix"], "missed": _bot["missed"],
            "catchup": _bot["catchup"]}


@app.on_event("startup")
def startup():
    db().close()
    ensure_workshops()
    threading.Thread(target=backfill_users_from_log, daemon=True).start()
    threading.Thread(target=notify_loop, daemon=True).start()
    threading.Thread(target=ig_names_loop, daemon=True).start()
    threading.Thread(target=bot_health_loop, daemon=True).start()


# ---------- HTTP ----------

def local_only(request: Request):
    """Pages on other sites can't send custom headers to us without a CORS preflight, which we never allow."""
    if request.headers.get("x-local") != "1":
        raise HTTPException(403)


@app.get("/")
def page():
    return FileResponse(HERE / "dashboard.html")


@app.get("/api/data")
def data(request: Request):
    local_only(request)
    return build()


@app.post("/api/upload")
async def upload(request: Request):
    local_only(request)
    try:
        found, added = import_export(await request.body(), request.headers.get("x-filename", ""))
    except (ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, str(e))
    if not found:
        raise HTTPException(400, "No joins or leaves found. Is this a WhatsApp chat export?")
    log.info("chat export imported: %d events, %d new", found, added)
    main.activity("admin", None, f"Uploaded a WhatsApp chat export: {found} joins/leaves, {added} new")
    return {"found": found, "added": added}


@app.post("/api/recheck")
def recheck(request: Request):
    local_only(request)
    if (get_meta("recheck") or {}).get("running"):
        return {"started": False}
    threading.Thread(target=recheck_everyone, daemon=True).start()
    main.activity("admin", None, "Started Re-check follows (everyone)")
    return {"started": True}


@app.get("/api/bot")
def bot(request: Request):
    local_only(request)
    return bot_status()


@app.post("/api/bot/fix")
def bot_fix(request: Request, new_link: bool = False):
    local_only(request)
    if (_bot["fix"] or {}).get("running"):
        return {"started": False}
    threading.Thread(target=fix_bot, args=(new_link,), daemon=True).start()
    main.activity("admin", None, "Made a new bot link" if new_link else "Pressed Make bot active")
    return {"started": True}


@app.post("/api/bot/missed")
def bot_missed(request: Request):
    """Look up DMs the bot never got (nothing is sent)."""
    local_only(request)
    if not (_bot["missed"] or {}).get("running"):
        threading.Thread(target=find_missed, daemon=True).start()
    return {"started": True}


@app.post("/api/bot/catchup")
def bot_catchup(request: Request):
    """Reply to the people found by /api/bot/missed, through the bot's normal flow."""
    local_only(request)
    if (_bot["catchup"] or {}).get("running") or not (_bot["missed"] or {}).get("people"):
        return {"started": False}
    threading.Thread(target=catch_up, daemon=True).start()
    main.activity("admin", None, f"Replying to {len(_bot['missed']['people'])} missed DMs")
    return {"started": True}


# ---------- keep the laptop awake: keep_awake.ps1 presses Ctrl 4x every 30 min, this turns it on/off ----------
AWAKE_SCRIPT = HERE / "keep_awake.ps1"
AWAKE_STATE = HERE / "keep_awake.json"  # written by the script: pid, last/next press, heartbeat (UTC)


class _LastInput(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint), ("t", ctypes.c_uint)]


def idle_seconds():
    """How long Windows thinks nobody has touched the keyboard or mouse."""
    kernel = ctypes.windll.kernel32
    kernel.GetTickCount.restype = ctypes.c_uint
    last = _LastInput(ctypes.sizeof(_LastInput), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last))
    return ((kernel.GetTickCount() - last.t) & 0xFFFFFFFF) / 1000


def pid_alive(pid):
    """Still running? (a killed process can still be openable while someone holds a handle, so check its exit code)"""
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def awake_status():
    try:
        s = json.loads(AWAKE_STATE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"running": False}
    pid, beat = s.get("pid"), s.get("beat")
    alive = bool(pid) and pid_alive(int(pid))
    fresh = bool(beat) and (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 90
    return {"running": alive and fresh, "pid": pid, "started": s.get("started"),
            "last_press": s.get("last_press"), "next_press": s.get("next_press")}


@app.get("/api/awake")
def awake(request: Request):
    local_only(request)
    return awake_status()


@app.post("/api/awake/start")
def awake_start(request: Request):
    local_only(request)
    if awake_status()["running"]:
        return {"started": False}
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(AWAKE_SCRIPT)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    log.info("keep-awake turned on")
    return {"started": True}


@app.post("/api/awake/stop")
def awake_stop(request: Request):
    local_only(request)
    s = awake_status()
    if s["running"]:
        subprocess.run(["taskkill", "/F", "/PID", str(s["pid"])], capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
        log.info("keep-awake turned off")
    return {"stopped": s["running"]}


@app.post("/api/awake/test")
def awake_test(request: Request):
    """Press Ctrl 4 times right now and report whether Windows' idle timer reacted."""
    local_only(request)
    before = idle_seconds()
    user32 = ctypes.windll.user32
    for _ in range(4):
        user32.keybd_event(0x11, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.3)
    return {"idle_before": round(before, 1), "idle_after": round(idle_seconds(), 1)}


# ---------- background apps: turn the bot, the ad manager and this dashboard off/on ----------
# Each one starts at logon from a shortcut in the Startup folder. Turning one off kills its keepalive first (or it
# would bring the rest back) and parks the shortcut in disabled_startup/, so it stays off after a restart too.
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
PARKED = HERE / "disabled_startup"
APPS = {  # keepalive first: killed before the processes it would restart
    "bot": {"lnk": "Resin workshop bot.lnk", "procs": ("keepalive", "bot", "tunnel")},
    "ads": {"lnk": "Resin ad manager.lnk", "procs": ("ads_keepalive", "ads"),
            "start": f'"{PYW}" "{HERE / "ads" / "admanager_keepalive.py"}"'},
    "members": {"lnk": "Resin members dashboard.lnk", "procs": ("members_keepalive", "listener", "members")},
}
_apps_busy = set()


def autostart(name):
    return (STARTUP / APPS[name]["lnk"]).exists()


def set_autostart(name, on):
    lnk = APPS[name]["lnk"]
    src, dst = (PARKED / lnk, STARTUP / lnk) if on else (STARTUP / lnk, PARKED / lnk)
    if src.exists() and not dst.exists():
        dst.parent.mkdir(exist_ok=True)
        src.replace(dst)


def apps_status():
    procs = processes()
    return {name: {"running": any(procs[p] for p in a["procs"]), "autostart": autostart(name),
                   "busy": name in _apps_busy} for name, a in APPS.items()}


def app_off(name):
    try:
        set_autostart(name, False)
        procs = processes()
        pids = [pid for p in APPS[name]["procs"] for pid in procs[p]]
        log.info("turning off %s: %s", name, pids)
        if name == "members":
            # this dashboard is one of them: a separate process kills it after the reply has gone out.
            # /T on the listener takes its Edge window with it.
            start_detached("powershell -NoProfile -WindowStyle Hidden -Command \"Start-Sleep 2; "
                           + "; ".join(f"taskkill /F /T /PID {pid}" for pid in pids) + '"')
            return
        kill(pids)
    finally:
        _apps_busy.discard(name)
        _bot_wake.set()


def app_on(name):
    try:
        set_autostart(name, True)
        if name == "bot":
            fix_bot()
        elif not any(processes()[p] for p in APPS[name]["procs"]):
            log.info("turning on %s", name)
            start_detached(APPS[name]["start"])
            time.sleep(5)
    finally:
        _apps_busy.discard(name)


@app.get("/api/apps")
def apps(request: Request):
    local_only(request)
    return apps_status()


@app.post("/api/apps/{name}/{action}")
def apps_action(name: str, action: str, request: Request):
    local_only(request)
    if name not in APPS or action not in ("on", "off") or (name, action) == ("members", "on"):
        raise HTTPException(404)
    if name in _apps_busy:
        return {"started": False}
    _apps_busy.add(name)
    threading.Thread(target=app_off if action == "off" else app_on, args=(name,), daemon=True).start()
    main.activity("admin", None, f"Turned {action} the {dict(bot='Instagram bot', ads='ad manager', members='dashboard')[name]}")
    return {"started": True}


@app.post("/api/removed/{igsid}")
def mark_removed(igsid: str, request: Request):
    local_only(request)
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO removed VALUES (?, ?)", (igsid, main.now().isoformat()))
    main.activity("admin", igsid, "You marked them removed from WhatsApp")
    return {"ok": True}


@app.post("/api/match")
async def set_match(request: Request):
    """Settle a match by hand: {igsid, who, verdict: yes (that number is them) | no (it isn't; who '' = didn't
    join) | clear (forget what was set for this person)}."""
    local_only(request)
    b = await request.json()
    igsid, who, verdict = str(b.get("igsid") or ""), str(b.get("who") or ""), b.get("verdict")
    if not igsid or verdict not in ("yes", "no", "clear") or (verdict == "yes" and not who):
        raise HTTPException(400)
    with db() as conn:
        if verdict == "clear":
            conn.execute("DELETE FROM match_fix WHERE igsid = ?", (igsid,))
        else:
            if verdict == "yes":  # one number per person and one person per number
                conn.execute("DELETE FROM match_fix WHERE verdict = 'yes' AND (igsid = ? OR who = ?)", (igsid, who))
            conn.execute("INSERT OR REPLACE INTO match_fix VALUES (?, ?, ?, ?)",
                         (igsid, who, verdict, main.now().isoformat()))
    log.info("match set by hand: %s %s %s", igsid, verdict, who)
    main.activity("admin", igsid, {"yes": f"You confirmed their number: {who}", "clear": "You undid their match",
                                   "no": f"You ruled out {who}" if who else "You marked them as not joined"}[verdict])
    return {"ok": True}


@app.delete("/api/removed/{igsid}")
def unmark_removed(igsid: str, request: Request):
    local_only(request)
    with db() as conn:
        conn.execute("DELETE FROM removed WHERE igsid = ?", (igsid,))
    main.activity("admin", igsid, "You unticked Removed")
    return {"ok": True}


@app.post("/api/wa/event")
async def wa_event(request: Request):
    """From wa/listener.js: {who, kind: join|leave, at: unix seconds, chat}."""
    local_only(request)
    e = await request.json()
    if e.get("kind") not in ("join", "leave") or not e.get("who"):
        raise HTTPException(400)
    at = datetime.fromtimestamp(int(e["at"]), timezone.utc)
    added = add_event(e["who"], e["kind"], at, e.get("chat", ""), "live")
    if added:
        log.info("WhatsApp %s: %s (%s)", e["kind"], norm_who(e["who"]), e.get("chat", ""))
        late = main.now() - at > timedelta(minutes=5)  # caught up after the listener was off
        main.activity("wa", None, f"{norm_who(e['who'])} {'joined' if e['kind'] == 'join' else 'left'} "
                      f"\"{e.get('chat') or 'WhatsApp'}\"" + (f" (at {at.astimezone():%d %b %H:%M})" if late else ""),
                      detail={"number": norm_who(e["who"])}, channel="wa")
    return {"added": added}


@app.post("/api/wa/names")
async def wa_names_in(request: Request):
    """From wa/listener.js: {people: [{who, name, username}]}, WhatsApp profile names of group members."""
    local_only(request)
    people = [p for p in (await request.json()).get("people", []) if p.get("who")]
    with db() as conn:
        conn.executemany("INSERT OR REPLACE INTO wa_names VALUES (?, ?, ?, ?)", [
            (norm_who(p["who"]), p.get("name") or "", p.get("username") or "", main.now().isoformat()) for p in people])
    return {"ok": True, "n": len(people)}


@app.post("/api/wa/status")
async def wa_status(request: Request):
    """From wa/listener.js: {state: qr|ready|disconnected|..., qr?, me?}."""
    local_only(request)
    s = await request.json()
    s["at"] = main.now().isoformat()
    set_meta("wa", s)
    return {"ok": True}


@app.post("/api/wa/history")
async def wa_history(request: Request):
    """From wa/listener.js after a join in the workshop group: {who, chat, result: sent|skip|failed, count?, why?}.
    The listener shares the group's message history with new members, like the phone's "Share message history"."""
    local_only(request)
    e = await request.json()
    who, chat = norm_who(e.get("who") or ""), e.get("chat") or "WhatsApp"
    if e.get("result") == "sent":
        main.activity("wa", None, f"Shared message history with {who} in \"{chat}\" ({e.get('count')} messages)",
                      detail={"number": who}, channel="wa")
    else:
        main.activity("wa" if e.get("result") == "skip" else "fail", None,
                      f"Didn't share message history with {who} in \"{chat}\": {e.get('why') or 'unknown reason'}",
                      detail={"number": who}, channel="wa")
    return {"ok": True}


# ---------- unfollow warnings (To remove card) ----------
# UNFOLLOW_WARN_MSG with the follow buttons. Instagram only lets it through within 24 h of the person's last DM;
# a refusal is kept (users.warn_error) and shown on the row. The bot answers their "I'm following ✅" tap
# (main.answer_refollow).
WARN_GAP = 2  # seconds between people when warning several
_warn = {"running": False, "done": 0, "total": 0, "sent": 0, "failed": 0, "at": None}


def warn_one(igsid):
    """Send the warning to one person; returns (sent, why not)."""
    main._ctx.who = igsid
    at = main.now().isoformat()
    ok = main.send({"id": igsid}, main.text("UNFOLLOW_WARN_MSG"), main.follow_buttons())
    why = None if ok else main.last_error() or "Instagram didn't accept it"
    main.update_user(igsid, **({"warned_at": at} if ok else {}), warn_tried_at=at, warn_error=why)
    log.info("unfollow warning %s: %s%s", "sent" if ok else "NOT sent", igsid, "" if ok else f" ({why})")
    return ok, why


def warn_people(igsids):
    _warn.update(running=True, done=0, total=len(igsids), sent=0, failed=0, at=main.now().isoformat())
    loop_state("warn", started_at=main.now().isoformat(), running=1, error=None)
    for i, igsid in enumerate(igsids):
        try:
            ok, _ = warn_one(igsid)
        except Exception:
            log.exception("unfollow warning to %s failed", igsid)
            ok = False
        _warn["sent" if ok else "failed"] += 1
        _warn["done"] = i + 1
        if i + 1 < len(igsids):
            time.sleep(WARN_GAP)
    result = f"{_warn['sent']} warned" + (f" · {_warn['failed']} couldn't be sent" if _warn["failed"] else "")
    main.activity("admin", None, f"You sent the unfollow warning: {result}")
    loop_state("warn", ended_at=main.now().isoformat(), running=0, result=result)
    _warn.update(running=False, at=main.now().isoformat())


@app.post("/api/warn")
async def warn(request: Request):
    """{igsids: [...]}: DM them UNFOLLOW_WARN_MSG, a couple of seconds apart (progress in /api/data -> warn)."""
    local_only(request)
    igsids = [str(i) for i in (await request.json()).get("igsids") or [] if i and not str(i).startswith("fb:")]
    if not igsids:
        raise HTTPException(400, "Nobody to warn")
    if _warn["running"]:
        return {"started": False}
    _warn["running"] = True
    threading.Thread(target=warn_people, args=(igsids,), daemon=True).start()
    return {"started": True, "total": len(igsids)}


@app.post("/api/follow/{igsid}")
def follow_now(igsid: str, request: Request):
    """Look up one person's follow status right now (instead of waiting for the 2-hourly check)."""
    local_only(request)
    follows, username = main.check_follow(igsid)
    if follows is None:
        return {"ok": False, "error": main.last_error() or "Instagram didn't answer"}
    with db() as conn:
        row = conn.execute("SELECT follows, username FROM users WHERE igsid = ?", (igsid,)).fetchone()
    before = row[0] if row else None
    cols = dict(follows=int(follows), follow_checked_at=main.now().isoformat(),
                username=username or (row[1] if row else None))
    if follows:
        cols["unfollowed_at"] = None
        if before == 0:
            main.activity("follow", igsid, "followed again (checked from the dashboard)")
    elif before != 0:
        cols["unfollowed_at"] = main.now().isoformat()
        main.activity("follow", igsid, "unfollowed (checked from the dashboard)")
    main.update_user(igsid, **cols)
    return {"ok": True, "follows": follows}


# ---------- workshops (Workshop tab) ----------

def ensure_workshops():
    """The first time, the running workshop as .env describes it, so there's always one to edit."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM workshops").fetchone():
            return
        start = since()
        conn.execute("INSERT INTO workshops (name, date, started_at, wa_group, wa_link, meet_url, past_offer, texts) "
                     "VALUES (?, NULL, ?, ?, ?, ?, 1, '{}')",
                     (f"Workshop from {start:%d %b}", start.isoformat(), MAIN_CHAT, main.WORKSHOP_LINK, main.MEET_URL))
    log.info("workshops set up from .env")
    main.workshops(fresh=True)


def clean_fields(b, need=()):
    """Workshop fields from the page, checked; raises 400 with what's wrong."""
    out = {}
    for k in ("name", "date", "wa_group", "wa_link", "meet_url"):
        if k in b:
            out[k] = " ".join(str(b[k] or "").split()) if k in ("name", "wa_group") else str(b[k] or "").strip()
    if "past_offer" in b:
        out["past_offer"] = int(bool(b["past_offer"]))
    for k in need:
        if not out.get(k):
            raise HTTPException(400, {"name": "Give the workshop a name", "wa_group": "Fill in the WhatsApp group name",
                                      "wa_link": "Paste the WhatsApp invite link"}[k])
    if len(out.get("name") or "") > 60:
        raise HTTPException(400, "Name: 60 characters at most")
    if out.get("date") and not re.fullmatch(r"\d{4}-\d\d-\d\d", out["date"]):
        raise HTTPException(400, "Class date must look like 2026-10-29")
    for k, what in (("wa_link", "WhatsApp invite link"), ("meet_url", "Class link")):
        if out.get(k) and not re.fullmatch(r"https://\S+", out[k]):
            raise HTTPException(400, f"{what} must start with https://")
    return out


def ws_update(wid, **cols):
    cols = {k: json.dumps(v, ensure_ascii=False) if k in ("texts", "faqs", "stats") and v is not None else v
            for k, v in cols.items()}
    with db() as conn:
        conn.execute(f"UPDATE workshops SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?", (*cols.values(), wid))
    main.workshops(fresh=True)


def clean_texts(texts):
    """{key: text} from the page -> only the ones that differ from main.py's defaults, each checked."""
    out = {}
    for k, v in (texts or {}).items():
        if k in bot_messages.BY_KEY:
            if err := bot_messages.check(k, v):
                raise HTTPException(400, f"{bot_messages.BY_KEY[k]['label']}: {err}")
            if v != getattr(main, k):
                out[k] = v
    return out


def clean_faqs(items):
    """The question list from the page, checked; returns (questions, {text key or question id: answer})."""
    out, answers, seen = [], {}, set()
    for f in items or []:
        fid = str(f.get("id") or "") or "faq_" + os.urandom(4).hex()
        if fid in seen or not (fid in bot_messages.FAQ_TEXT or re.fullmatch(r"faq_[0-9a-f]{8}", fid)):
            raise HTTPException(400, "Unknown question")
        seen.add(fid)
        q = {"id": fid, "label": " ".join(str(f.get("label") or "").split()), "on": bool(f.get("on", True)),
             "keywords": [" ".join(str(k).split()) for k in f.get("keywords") or [] if str(k).strip()]}
        if err := bot_messages.check_faq(q):
            raise HTTPException(400, f"{q['label'] or 'A question'}: {err}")
        answer = str(f.get("answer") or "")
        key = bot_messages.FAQ_TEXT.get(fid)
        if key:
            err = bot_messages.check(key, answer)
        else:  # a custom reply: optional link button, optional "stop here" (no follow step after it)
            q.update(button_title=" ".join(str(f.get("button_title") or "").split()),
                     button_url=str(f.get("button_url") or "").strip(), stop=bool(f.get("stop")))
            err = bot_messages.check_custom(q, answer)
        if err:
            raise HTTPException(400, f"{q['label']}: {err}")
        answers[key or fid] = answer
        out.append(q)
    if set(bot_messages.FAQ_TEXT) - seen:
        raise HTTPException(400, "The built-in questions can be switched off, not deleted")
    return out, answers


def faq_texts(base, answers):
    """A workshop's texts with the question answers put in (built-in ones only when they differ from main.py)."""
    texts = {k: v for k, v in base.items() if not k.startswith("faq_")}
    for k, v in answers.items():
        if k.startswith("faq_") or v != getattr(main, k):
            texts[k] = v
        else:
            texts.pop(k, None)
    return texts


@app.get("/api/workshops")
def workshops_list(request: Request):
    local_only(request)
    running = main.workshop()
    return {"workshops": [ws_public(w) for w in main.workshops()], "running_id": running["id"] if running else None,
            "settings": {"keyword": main.KEYWORD, "contact": main.CONTACT_NUMBER,
                         "start_after": main.START_AFTER.isoformat() if main.START_AFTER else None,
                         "ignore_media": len(main.IGNORE_MEDIA)}}


@app.post("/api/workshops/{wid}")
async def workshop_edit(wid: int, request: Request):
    """Edit a workshop's fields (name, date, wa_group, wa_link, meet_url, past_offer); meet_url = Go live / End live."""
    local_only(request)
    w = next((w for w in main.workshops() if w["id"] == wid), None)
    if not w:
        raise HTTPException(404)
    cols = clean_fields(await request.json())
    if "name" in cols and not cols["name"]:
        raise HTTPException(400, "Give the workshop a name")
    if cols.get("meet_url") and w["closed_at"]:
        raise HTTPException(400, "This workshop has ended")
    if not cols:
        return {"ok": True}
    ws_update(wid, **cols)
    what = ("went live: the bot now sends the class link" if cols.get("meet_url") and not w["meet_url"]
            else "ended live mode" if "meet_url" in cols and not cols["meet_url"] and w["meet_url"]
            else "changed " + ", ".join(c.replace("wa_", "WhatsApp ").replace("_", " ") for c in cols))
    main.activity("admin", None, f"{w['name']}: {what}")
    return {"ok": True}


def close_running(stats):
    """Close the running workshop (if any), freezing its numbers; returns it."""
    running = main.workshop() if main.workshops() else None
    if running:
        ws_update(running["id"], closed_at=main.now().isoformat(), meet_url="", stats=stats.get(running["id"]))
    return running


@app.post("/api/workshops")
async def workshop_start(request: Request):
    """Start the next workshop: {name, date, wa_group, wa_link, past_offer, texts, faqs}. Closes the running one."""
    local_only(request)
    b = await request.json()
    cols = clean_fields(b, need=("name", "wa_group", "wa_link"))
    started = main.now()
    if b.get("started_at"):
        try:
            started = datetime.fromisoformat(str(b['started_at']).replace('Z', '+00:00'))
            if started.tzinfo is None or started > main.now():
                raise ValueError()
            latest = main.latest_workshop()
            boundary = latest.get('closed_at') or latest.get('started_at')
            if boundary and started < datetime.fromisoformat(boundary):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, 'started_at must be an actual ad creation time with timezone, after the previous workshop')
    texts = clean_texts(b.get("texts"))
    questions = None
    if b.get("faqs") is not None:
        questions, answers = clean_faqs(b["faqs"])
        texts = faq_texts(texts, answers)
        if questions == [dict(f) for f in bot_messages.FAQ_DEFAULTS]:
            questions = None
    closed = close_running(build()["ws_stats"])
    with db() as conn:
        conn.execute("INSERT INTO workshops (name, date, started_at, wa_group, wa_link, meet_url, past_offer, texts, "
                     "faqs) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)",
                     (cols["name"], cols.get("date") or None, started.isoformat(), cols["wa_group"], cols["wa_link"],
                      cols.get("past_offer", 1), json.dumps(texts, ensure_ascii=False),
                      json.dumps(questions, ensure_ascii=False) if questions else None))
    main.workshops(fresh=True)
    log.info("workshop started: %s (closed %s)", cols["name"], closed and closed["name"])
    main.activity("admin", None, f"Started {cols['name']}" + (f" ({closed['name']} closed)" if closed else ""))
    return {"ok": True}


@app.post("/api/workshops-end")
def workshop_end(request: Request):
    """End the running workshop without starting the next one (the bot answers with BETWEEN_MSG)."""
    local_only(request)
    closed = close_running(build()["ws_stats"])
    if not closed:
        raise HTTPException(400, "No workshop is running")
    log.info("workshop ended: %s", closed["name"])
    main.activity("admin", None, f"Ended {closed['name']}: the bot answers with \"No workshop right now\"")
    return {"ok": True}


@app.post("/api/workshops-reopen")
def workshop_reopen(request: Request):
    """Undo End workshop: the last one runs again (only while no other one is running)."""
    local_only(request)
    rows = main.workshops()
    if not rows or not rows[-1]["closed_at"]:
        raise HTTPException(400, "A workshop is already running")
    ws_update(rows[-1]["id"], closed_at=None, stats=None)
    main.activity("admin", None, f"Reopened {rows[-1]['name']}")
    return {"ok": True}


# ---------- reels with their own offer (Reels tab) ----------
REEL_LINK = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([\w-]+)")


def reel_update(rid, **cols):
    cols = {k: json.dumps(v, ensure_ascii=False) if k in ("texts", "faqs") and v is not None else v
            for k, v in cols.items()}
    with db() as conn:
        conn.execute(f"UPDATE reels SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?", (*cols.values(), rid))
    main.reels(fresh=True)


def find_media(link):
    """The post/reel of our account that a link (or a media id) points to: {id, permalink, timestamp}."""
    link = link.strip()
    if re.fullmatch(r"\d{6,}", link):
        found = main.api("GET", link, params={"fields": "id,permalink,timestamp"})
        if not found:
            raise HTTPException(400, "Instagram doesn't know that id: " + (main.last_error() or "lookup failed"))
        return found
    m = REEL_LINK.search(link)
    if not m:
        raise HTTPException(400, "Paste the reel's link, like https://www.instagram.com/reel/ABC123/")
    code, params = m.group(1), {"fields": "id,permalink,timestamp", "limit": 50}
    for _ in range(8):  # the newest 400 posts
        page = main.api("GET", "me/media", params=params)
        if not page:
            raise HTTPException(400, "Couldn't ask Instagram: " + (main.last_error() or "lookup failed"))
        for x in page.get("data", []):
            if f"/{code}/" in (x.get("permalink") or "") + "/":
                return x
        after = (page.get("paging") or {}).get("cursors", {}).get("after")
        if not after or not (page.get("paging") or {}).get("next"):
            break
        params = {**params, "after": after}
    raise HTTPException(400, "That reel isn't on this Instagram account (or Instagram doesn't list it yet). "
                             "Check the link, or try again in a minute.")


def reel_stats(rid):
    with db() as conn:
        people, dmed, asked, links = conn.execute(
            "SELECT COUNT(*), COUNT(dm_at), COUNT(asked_at), COUNT(sent_at) FROM reel_people WHERE reel_id = ?",
            (rid,)).fetchone()
        clicks = conn.execute("SELECT igsid, clicked_at, COALESCE(user_agent, ''), ip FROM reel_clicks "
                              "WHERE reel_id = ? AND igsid IS NOT NULL", (rid,)).fetchall()
    return {"people": people, "dmed": dmed, "asked": asked, "links": links, "tapped": len(real_taps(clicks))}


def reel_public(r):
    return {k: r[k] for k in ("id", "name", "media_id", "reel_url", "wa_link", "created_at")} | {
        "follow_first": bool(r["follow_first"]), "active": bool(r["active"]), "stats": reel_stats(r["id"])}


@app.get("/api/bypassed-media")
def bypass_list(request: Request):
    local_only(request)
    return {"items": [{"media_id": mid, "url": url} for mid, url in main.bypassed_media().items()]}


@app.post("/api/bypassed-media")
async def bypass_add(request: Request):
    local_only(request)
    body = await request.json()
    url = str(body.get("url") or "").strip()
    parsed = main.urlparse(url if "://" in url else "https://" + url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("instagram.com", "www.instagram.com", "m.instagram.com") or not REEL_LINK.search(url):
        raise HTTPException(400, "Paste a valid Instagram reel or post link.")
    media = find_media(url)
    canonical = "https://www.instagram.com/" + ("reel/" if "/reel" in parsed.path else "p/") + REEL_LINK.search(url)[1] + "/"
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO bypass_media VALUES (?, ?, ?)",
                     (str(media["id"]), canonical, main.now().isoformat()))
    return {"ok": True}


@app.delete("/api/bypassed-media/{media_id}")
def bypass_remove(media_id: str, request: Request):
    local_only(request)
    with db() as conn:
        conn.execute("DELETE FROM bypass_media WHERE media_id = ?", (media_id,))
    return {"ok": True}


@app.get("/api/reels")
def reels_list(request: Request):
    local_only(request)
    return {"reels": [reel_public(r) for r in main.reels()]}


@app.post("/api/reels")
async def reel_new(request: Request):
    """{name, wa_link}: a new reel offer, off and with no reel connected; its texts and questions start as a copy
    of the latest reel's (the ₹99 offer's for the first one)."""
    local_only(request)
    b = await request.json()
    name, wa_link = " ".join(str(b.get("name") or "").split()), str(b.get("wa_link") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "Give it a name (60 characters at most)")
    if wa_link and not re.fullmatch(r"https://\S+", wa_link):
        raise HTTPException(400, "The WhatsApp link must start with https://")
    last = (main.reels() or [None])[-1]
    texts = last["texts"] if last else bot_messages.REEL_99_TEXTS
    faqs = last["faqs"] if last else bot_messages.REEL_99_FAQS
    with db() as conn:
        rid = conn.execute("INSERT INTO reels (name, media_id, reel_url, wa_link, follow_first, active, created_at, "
                           "texts, faqs) VALUES (?, '', '', ?, 1, 1, ?, ?, ?)",
                           (name, wa_link or (bot_messages.REEL_99_LINK if not last else ""), main.now().isoformat(),
                            json.dumps(texts, ensure_ascii=False),
                            json.dumps(faqs, ensure_ascii=False) if faqs else None)).lastrowid
    main.reels(fresh=True)
    main.activity("admin", None, f"Set up the reel offer {name} (no reel connected yet)")
    return {"ok": True, "id": rid}


@app.post("/api/reels/{rid}")
async def reel_edit(rid: int, request: Request):
    """Change name, wa_link, follow_first, active; reel_url connects a reel ('' disconnects it)."""
    local_only(request)
    r = reel_by_id(rid)
    b, cols = await request.json(), {}
    if "name" in b:
        cols["name"] = " ".join(str(b["name"] or "").split())
        if not cols["name"] or len(cols["name"]) > 60:
            raise HTTPException(400, "Give it a name (60 characters at most)")
    if "wa_link" in b:
        cols["wa_link"] = str(b["wa_link"] or "").strip()
        if not re.fullmatch(r"https://\S+", cols["wa_link"]):
            raise HTTPException(400, "Paste the WhatsApp link (starts with https://)")
    for k in ("follow_first", "active"):
        if k in b:
            cols[k] = int(bool(b[k]))
    if "reel_url" in b:
        if str(b["reel_url"] or "").strip():
            media = find_media(str(b["reel_url"]))
            other = next((x for x in main.reels() if x["media_id"] == media["id"] and x["id"] != rid), None)
            if other:
                raise HTTPException(400, f"That reel is already connected to {other['name']}")
            if media["id"] in main.IGNORE_MEDIA:
                raise HTTPException(400, "That reel is muted (IGNORE_MEDIA in .env): the bot ignores its comments")
            cols.update(media_id=media["id"], reel_url=media.get("permalink") or str(b["reel_url"]).strip())
        else:
            cols.update(media_id="", reel_url="")
    if not cols:
        return {"ok": True}
    if (cols.get("active", r["active"]) and (cols.get("media_id", r["media_id"]))
            and not cols.get("wa_link", r["wa_link"])):
        raise HTTPException(400, "Add the WhatsApp link first: it's where everyone's link takes them")
    reel_update(rid, **cols)
    what = (f"connected to {cols['reel_url']}: comments on it now get its messages" if cols.get("media_id")
            else "disconnected from its reel" if "media_id" in cols
            else "switched on" if cols.get("active") == 1 and not r["active"]
            else "switched off: its people get the workshop's messages again" if cols.get("active") == 0
            else "changed " + ", ".join(c.replace("wa_", "WhatsApp ").replace("_", " ") for c in cols))
    main.activity("admin", None, f"{r['name']}: {what}")
    return {"ok": True}


@app.delete("/api/reels/{rid}")
def reel_delete(rid: int, request: Request):
    """Delete a reel offer nobody has gone through yet (otherwise switch it off)."""
    local_only(request)
    r = reel_by_id(rid)
    if reel_stats(rid)["people"]:
        raise HTTPException(400, "People already went through it: switch it off instead (keeps their history)")
    with db() as conn:
        conn.execute("DELETE FROM reels WHERE id = ?", (rid,))
        conn.execute("DELETE FROM funnel WHERE reel_id = ?", (rid,))
    main.reels(fresh=True)
    main.activity("admin", None, f"Deleted the reel offer {r['name']}")
    return {"ok": True}


# ---------- bot messages (Messages tab) ----------

def reel_by_id(rid):
    """The reel offer the Messages tab is editing (?reel=ID / "reel": ID), or None for the workshop's texts."""
    if rid in (None, "", 0, "0"):
        return None
    r = next((r for r in main.reels() if str(r["id"]) == str(rid)), None)
    if not r:
        raise HTTPException(404, "That reel offer doesn't exist any more")
    return r


def messages_payload(r=None):
    with main.in_reel(r):
        return _messages_payload(r)


def _messages_payload(r):
    w = main.latest_workshop()
    edits = w["texts"]
    return {
        "workshop": {"id": w["id"], "name": w["name"], "closed": bool(w["closed_at"]), "date": w["date"]},
        "reel": {"id": r["id"], "name": r["name"], "connected": bool(r["media_id"]), "active": bool(r["active"])}
        if r else None,
        "reels": [{"id": x["id"], "name": x["name"]} for x in main.reels()],
        "groups": bot_messages.GROUPS,
        "messages": [{**m, "text": edits.get(m["key"]) or getattr(main, m["key"]), "default": getattr(main, m["key"]),
                      "edited": m["key"] in edits} for m in (bot_messages.REEL_CATALOG if r else bot_messages.CATALOG)],
        "faqs": [{**f, "builtin": f["id"] in bot_messages.FAQ_TEXT, "key": bot_messages.FAQ_TEXT.get(f["id"]),
                  "answer": main.text(bot_messages.FAQ_TEXT.get(f["id"], f["id"]))} for f in main.faqs()],
        "triggers": {"keyword": main.KEYWORD, "start_after": main.START_AFTER.isoformat() if main.START_AFTER else None,
                     "ignore_media": len(main.IGNORE_MEDIA)},
        "live": bool(main.meet_url()), "between": main.workshop() is None,
    }


@app.get("/api/messages")
def messages(request: Request, reel: str = ""):
    local_only(request)
    return messages_payload(reel_by_id(reel))


@app.post("/api/messages")
async def message_save(request: Request):
    """{key, text}: save one message for the current (or last) workshop; the bot uses it within seconds."""
    local_only(request)
    b = await request.json()
    k, t = str(b.get("key") or ""), str(b.get("text") or "")
    if err := bot_messages.check(k, t):
        raise HTTPException(400, err)
    r = reel_by_id(b.get("reel"))
    w = r or main.latest_workshop()
    if not w["id"]:
        raise HTTPException(400, "No workshop set up yet")
    texts = dict(w["texts"])
    if t == getattr(main, k):
        texts.pop(k, None)
    else:
        texts[k] = t
    (reel_update if r else ws_update)(w["id"], texts=texts)
    main.activity("admin", None, f"Edited the message \"{bot_messages.BY_KEY[k]['label']}\""
                  + (f" for {r['name']}" if r else ""), t, {"key": k})
    warning = None
    if k in ("VISIT_TITLE", "FOLLOWING_TITLE"):
        with main.in_reel(reel_by_id(b.get("reel"))):
            missing = [bot_messages.BY_KEY[m]["label"] for m in ("INTRO_MSG", "RULES_MSG") if t not in main.text(m)]
        if missing:
            warning = f"\"{t}\" isn't in: {', '.join(missing)}. People read the button name there, so update them too."
    return {"ok": True, "warning": warning}


@app.post("/api/faqs")
async def faqs_save(request: Request):
    """{faqs: [{id?, label, keywords, on, answer}]}: the whole question list (new ones get an id)."""
    local_only(request)
    b = await request.json()
    questions, answers = clean_faqs(b.get("faqs"))
    r = reel_by_id(b.get("reel"))
    w = r or main.latest_workshop()
    if not w["id"]:
        raise HTTPException(400, "No workshop set up yet")
    (reel_update if r else ws_update)(w["id"], texts=faq_texts(w["texts"], answers),
                                      faqs=None if questions == [dict(f) for f in bot_messages.FAQ_DEFAULTS] else questions)
    main.activity("admin", None, "Edited the questions the bot answers" + (f" for {r['name']}" if r else ""))
    return {"ok": True}


@app.post("/api/messages/test")
async def message_test(request: Request):
    """{text}: what the bot would answer to this DM (questions only; the follow step comes after)."""
    local_only(request)
    b = await request.json()
    said, r = str(b.get("text") or ""), reel_by_id(b.get("reel"))
    with main.in_reel(r):  # in a reel's flow every DM starts the bot
        return {"triggers": bool(r) or main.matches(said) or said.strip().upper() == "DONE",
                "between": main.workshop() is None,
                "answers": [{"id": f["id"], "label": f["label"], "answer": a, "custom": main.custom(f),
                             "stop": bool(f.get("stop")), "button": f.get("button_title") or None}
                            for f, a in main.answers_for(said)]}


# ---------- activity feed + background jobs (Activity tab) ----------
FEED_KINDS = {"in": ("in",), "out": ("out", "skip"), "links": ("link", "wa"), "follows": ("follow",),
              "jobs": ("job",), "you": ("admin",), "problems": ("fail",)}


@app.get("/api/activity")
def activity_feed(request: Request, after: int = 0, before: int = 0, kind: str = "", who: str = "", q: str = "",
                  limit: int = 150):
    """Newest first. after: only rows newer than this id (live updates); before: the page before this id."""
    local_only(request)
    where, args = [], []
    if after:
        where.append("id > ?")
        args.append(after)
    if before:
        where.append("id < ?")
        args.append(before)
    if kind in FEED_KINDS:
        where.append(f"kind IN ({','.join('?' * len(FEED_KINDS[kind]))})")
        args.extend(FEED_KINDS[kind])
    if who:
        where.append("who = ?")
        args.append(who)
    if q:
        where.append("(summary LIKE ? OR text LIKE ? OR who LIKE ? OR detail LIKE ? "
                     "OR who IN (SELECT igsid FROM users WHERE username LIKE ?))")
        args.extend([f"%{q}%"] * 5)
    with db() as conn:
        rows = conn.execute("SELECT id, at, kind, who, channel, summary, text, detail FROM activity "
                            + (f"WHERE {' AND '.join(where)} " if where else "") + "ORDER BY id DESC LIMIT ?",
                            (*args, min(max(limit, 1), 500))).fetchall()
        ids = tuple({r[3] for r in rows if r[3]})
        mark = ",".join("?" * len(ids))
        names = dict(conn.execute(f"SELECT igsid, username FROM users WHERE igsid IN ({mark})", ids)) if ids else {}
        fb = {f"fb:{p}": n for p, n in conn.execute(
            f"SELECT psid, name FROM fb_users WHERE 'fb:' || psid IN ({mark})", ids)} if ids else {}
        latest = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0] or 0
    labels = {m["key"]: m["label"] for m in bot_messages.CATALOG} | {f["id"]: f["label"] for f in main.faqs()}
    out = []
    for i, at, kind_, who_, channel, summary, body, detail in rows:
        detail = json.loads(detail) if detail else {}
        name = (names.get(who_) and "@" + names[who_]) or fb.get(who_) or (
            detail.get("username") and "@" + detail["username"]) or None
        out.append({"id": i, "at": at, "kind": kind_, "who": who_, "name": name, "channel": channel,
                    "summary": summary, "text": body, "label": labels.get(detail.get("key")),
                    "buttons": detail.get("buttons"), "error": detail.get("error")})
    return {"rows": out, "latest": latest}


JOB_INFO = (  # name, label, what it does, which process runs it, seconds between runs
    ("recheck", "Follow re-check & reminders", "Sends the link to anyone who followed after being asked; sends the "
     "one follow reminder and the one join-link reminder (Instagram and Messenger).", "bot", 600),
    ("audit", "Unfollow check", "Checks that everyone who got a link still follows (about 2 s per person). "
     "Unfollowers land in the To remove card.", "bot", 7200),
    ("notify", "Matching & used links", "Matches link taps to WhatsApp joins, closes used links, and pops up "
     "new people to remove.", "dashboard", 60),
    ("ig_names", "Instagram names", "Looks up the display name of new link tappers (helps matching).", "dashboard", 300),
    ("bot_health", "Bot & tunnel health", "Is the bot answering, is the tunnel up, is Meta sending to it.",
     "dashboard", 30),
    ("listener", "WhatsApp listener", "Live joins and leaves of the workshop groups.", "listener", 60),
    ("warn", "Unfollow warnings", "Runs when you press Send warning or Warn all.", "dashboard", None),
    ("awake", "Keep laptop awake", "Presses Ctrl every 30 min so Windows doesn't sleep.", "keep_awake", 1800),
)


@app.get("/api/jobs")
def jobs(request: Request):
    local_only(request)
    conn = db()
    conn.row_factory = sqlite3.Row
    stored = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
    conn.close()
    wa, aw = get_meta("wa", {}), awake_status()
    alive = {"bot": bool(_bot["local_ok"]), "dashboard": True,
             "listener": wa.get("state") == "ready" and bool(wa.get("at"))
             and main.now() - datetime.fromisoformat(wa["at"]) < timedelta(minutes=3),
             "keep_awake": aw.get("running", False)}
    out = []
    for name, label, about, proc, every in JOB_INFO:
        s = dict(stored.get(name) or _loops.get(name) or {})
        if name == "listener":
            s = {"ended_at": wa.get("at"), "result": "Watching: " + (wa.get("group") or "") if wa.get("state") == "ready"
                 else wa.get("detail") or wa.get("state") or "not started"}
        elif name == "awake":
            s = {"ended_at": aw.get("last_press"), "next_at": aw.get("next_press") if aw.get("running") else None,
                 "result": "On" if aw.get("running") else "Off (Members tab → Keep laptop awake)"}
        elif name == "warn" and _warn["running"]:
            s.update(running=1, done=_warn["done"], total=_warn["total"])
        out.append({"name": name, "label": label, "about": about, "process": proc, "every": every,
                    "alive": alive[proc], **s})
    return {"jobs": out, "bot_state": bot_state()}


@app.get("/api/wa/groups")
def wa_groups(request: Request):
    """For wa/listener.js: the WhatsApp groups to watch (every workshop's), newest first."""
    local_only(request)
    return {"groups": list(reversed(all_groups())), "main": main_group()}
