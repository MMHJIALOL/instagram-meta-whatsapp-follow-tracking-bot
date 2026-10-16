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
import main

main.logging.getLogger("httpx").setLevel(main.logging.WARNING)
log = main.log
HERE = Path(__file__).resolve().parent
SAME_EVENT = timedelta(seconds=90)  # live event and export line this close are the same join/leave
# The community's main chat. The personal link joins people here; joins of its other groups (e.g. "free resin
# pooja thali workshop 11th Oct") are members who are already in, so they aren't matched to taps.
# Exports (chat '') are of the main chat.
MAIN_CHAT = os.getenv("WA_MAIN_CHAT", "Karwa Chauth special worshop 18th Oct")
# People whose link, tap or join is at or after this time belong to the current workshop; the rest sit below a line.
WORKSHOP_SINCE = datetime.fromisoformat(os.getenv("WORKSHOP_SINCE", "2026-10-12T17:50:00+05:30"))
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


def main_chat(events):
    """(time, who, kind, source, chat) events -> (the main chat's events without chat, {who: (kind, time)}).
    Membership comes from the main chat, or from the other groups for people we've never seen there."""
    is_main = lambda chat: not chat or " ".join(chat.lower().split()) == " ".join(MAIN_CHAT.lower().split())
    state = {}
    for at, who, kind, _, chat in events:
        if is_main(chat) or who not in state or not state[who][2]:
            state[who] = (kind, at, is_main(chat))
    return ([e[:4] for e in events if is_main(e[4])],
            {who: (kind, at) for who, (kind, at, _) in state.items()})


def build():
    """Everything the page shows, recomputed from the database on each request."""
    with db() as conn:
        users = conn.execute("SELECT igsid, username, follows, follow_checked_at, unfollowed_at, first_seen, last_msg_at "
                             "FROM users").fetchall()
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
    users += [(ig, None, None, None, None, None, None) for ig in last_click if ig not in names]  # e.g. test links
    label = lambda ig: names.get(ig) or ig
    in_group = lambda who: state.get(who, ("join",))[0] == "join"

    rows = []
    for igsid, username, follows, checked, unfollowed_at, first_seen, last_msg in users:
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
        cleared = lambda at: igsid in removed and at and removed[igsid] >= at
        to_remove = f == "unfollowed" and group == "in" and not cleared(m["joined"])
        # unfollowed, and maybe one of these numbers: worth a look before removing anyone
        to_check = (f == "unfollowed" and bool(m) and m["conf"] == "maybe"
                    and sum(o["p"] for o in options if o["in_group"]) >= 0.2 and not cleared(m["options"][0]["joined"]))
        # DM-only people have no link or tap, so their last DM counts too
        stamps = [sent.get(igsid), last_msg, first_seen, last_click[igsid].isoformat() if igsid in last_click else None,
                  m["joined"].isoformat() if m and m["joined"] else None]
        current = any(datetime.fromisoformat(s) >= WORKSHOP_SINCE for s in stamps if s)
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
        })
    # WhatsApp members nobody's tap explains (mostly people who joined before personal links existed)
    linked = {m["who"] for m in matches.values() if m["who"]} | {w for w, opts in per_who.items() if opts}
    wa_only = [{"who": who, "group": "in" if kind == "join" else "left", "at": at.isoformat(),
                "wa_name": wa_names.get(who, ("",))[0]} for who, (kind, at) in state.items() if who not in linked]
    return {"rows": rows, "wa_only": wa_only, "export": get_meta("export"), "recheck": get_meta("recheck"),
            "wa": get_meta("wa", {"state": "not started"}), "audit_every_h": main.AUDIT_EVERY / 3600,
            "now": main.now().isoformat()}


def ig_names_loop():
    """Instagram display names of everyone who tapped a link, for the name check (one lookup per person)."""
    while True:
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
        except Exception:
            log.exception("Instagram name lookup failed")
        time.sleep(300)


# ---------- popup when someone new needs removing ----------

def notify_loop():
    while True:
        try:
            todo = {r["igsid"]: r for r in build()["rows"] if r["to_remove"] or r["to_check"]}
            told = set(get_meta("notified", []))
            new = [r for ig, r in todo.items() if ig not in told]
            if new:
                names = "\n".join(f"@{r['username'] or r['igsid']}  " + (r["who"] if r["to_remove"] else "check: " + " / ".join(
                    f"{o['who']} {o['p']:.0%}" for o in r["options"])) for r in new[:10])
                threading.Thread(target=ctypes.windll.user32.MessageBoxW, daemon=True, args=(
                    0, f"Unfollowed on Instagram but still in WhatsApp:\n\n{names}\n\nOpen http://127.0.0.1:8001",
                    "Resin members", 0x40000)).start()
            set_meta("notified", sorted(told | set(todo)))
        except Exception:
            log.exception("notify check failed")
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
            if marker in line:
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
        return "down"
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


def processes():
    """Running keepalive.py, bot (uvicorn main:app) and cloudflared processes: {name: [pid, ...]}."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or "
                          "Name='cloudflared.exe'\" | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW).stdout.strip()
    rows = json.loads(out) if out else []
    found = {"keepalive": [], "bot": [], "tunnel": []}
    for r in [rows] if isinstance(rows, dict) else rows:
        cmd = r["CommandLine"] or ""
        if r["Name"] == "cloudflared.exe":
            found["tunnel"].append(r["ProcessId"])
        elif KEEPALIVE_CMD.search(cmd):
            found["keepalive"].append(r["ProcessId"])
        elif BOT_CMD.search(cmd):
            found["bot"].append(r["ProcessId"])
    return found


def kill(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)


def start_keepalive():
    """Through WMI, so keepalive doesn't count as a child of the dashboard and outlives its restarts."""
    cmd = f'"{PYW}" "{HERE / "keepalive.py"}"'
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Invoke-CimMethod Win32_Process -MethodName Create -Arguments "
                    f"@{{CommandLine='{cmd}'; CurrentDirectory='{HERE}'}}"],
                   capture_output=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


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
    return {"found": found, "added": added}


@app.post("/api/recheck")
def recheck(request: Request):
    local_only(request)
    if (get_meta("recheck") or {}).get("running"):
        return {"started": False}
    threading.Thread(target=recheck_everyone, daemon=True).start()
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


@app.post("/api/removed/{igsid}")
def mark_removed(igsid: str, request: Request):
    local_only(request)
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO removed VALUES (?, ?)", (igsid, main.now().isoformat()))
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
    return {"ok": True}


@app.delete("/api/removed/{igsid}")
def unmark_removed(igsid: str, request: Request):
    local_only(request)
    with db() as conn:
        conn.execute("DELETE FROM removed WHERE igsid = ?", (igsid,))
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
