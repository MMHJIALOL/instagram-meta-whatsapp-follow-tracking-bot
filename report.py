"""Who interacted with the bot, and do they follow now? python report.py -> users_report.csv

status:
  followed          got the link and still follows
  unfollowed        got the link, no longer follows
  dm_only           messaged/commented but does not follow (never got the link)
  followed_no_link  follows now but never got the link (followed after the bot gave up)
  unknown           Instagram would not say (no follow lookup allowed, or comment with no user id)
"""
import csv
import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import main

main.logging.getLogger("httpx").setLevel(main.logging.WARNING)

LOG = "uvicorn.log"
OUT = "users_report.csv"
LINE = re.compile(r"^(\S+ \S+),\d+ INFO (DM trigger|button tap|comment trigger) from (\S+?)(?: \(.*?\))?: (.*)$")


def from_log():
    """igsid/username -> {first_seen, last_seen, messages, last_message}; comments are keyed by username."""
    people = {}
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line.lstrip("﻿"))
            if not m:
                continue
            when, kind, who, text = m.groups()
            key = ("u:" if kind == "comment trigger" else "") + who
            p = people.setdefault(key, {"first_seen": when, "messages": 0})
            p["last_seen"], p["last_message"] = when, re.sub(r"\\u([0-9a-f]{4})|\\U([0-9a-f]{8})", lambda e: chr(int(e[1] or e[2], 16)), text.strip("'\""))
            p["messages"] += 1
    return people


def main_():
    conn = sqlite3.connect(main.DB)
    main.db().close()  # make sure the users table exists
    sent = {igsid: (username, at) for igsid, username, at in conn.execute("SELECT igsid, username, sent_at FROM sent")}
    known = {igsid: username for igsid, username in conn.execute("SELECT igsid, username FROM users")}
    people = from_log()

    ids = set(sent) | set(known) | {k for k in people if not k.startswith("u:")}
    print(f"checking follow status of {len(ids)} users...")
    with ThreadPoolExecutor(8) as pool:
        checks = dict(zip(ids, pool.map(main.check_follow, ids)))

    rows = []
    for igsid in ids:
        follows, username = checks[igsid]
        username = username or known.get(igsid) or (sent.get(igsid) or (None,))[0]
        if follows is None:
            status = "unknown"
        elif igsid in sent:
            status = "followed" if follows else "unfollowed"
        else:
            status = "followed_no_link" if follows else "dm_only"
        # a comment logged under this username is the same person
        p = dict(people.get(igsid) or {})
        c = people.pop(f"u:{username}", None) if username else None
        if c:
            p["messages"] = p.get("messages", 0) + c["messages"]
            p["first_seen"] = min(p.get("first_seen", c["first_seen"]), c["first_seen"])
            if c["last_seen"] > p.get("last_seen", ""):
                p["last_seen"], p["last_message"] = c["last_seen"], c["last_message"]
        rows.append([status, username or "", igsid, (sent.get(igsid) or (None, ""))[1],
                     p.get("first_seen", ""), p.get("last_seen", ""), p.get("messages", 0), p.get("last_message", "")])

    sent_names = {u for u, _ in sent.values() if u}
    for key, p in people.items():  # commenters we never matched to a user id
        if key.startswith("u:"):
            name = key[2:]
            rows.append(["followed" if name in sent_names else "unknown", name, "", "", p["first_seen"],
                         p["last_seen"], p["messages"], p["last_message"]])

    order = ["followed", "unfollowed", "followed_no_link", "dm_only", "unknown"]
    rows.sort(key=lambda r: (order.index(r[0]), r[5]), reverse=False)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:  # -sig so Excel shows emoji/Hindi right
        w = csv.writer(f)
        w.writerow(["status", "username", "igsid", "link_sent_at", "first_seen", "last_seen", "messages", "last_message"])
        w.writerows(rows)

    counts = Counter(r[0] for r in rows)
    for s in order:
        print(f"{s:17} {counts[s]}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main_()
