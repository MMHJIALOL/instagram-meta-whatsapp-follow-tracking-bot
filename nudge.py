"""One-time nudge for people still inside Instagram's 24h messaging window who never got the link.

python nudge.py --dry-run   only prints who would get what (follow lookups only, no DMs)
python nudge.py             sends for real

Followers get the link (recorded in `sent`, like handle()); everyone else gets RULES_MSG and the
follow-request buttons, after which the bot's background re-check sends the link if they follow.
Anyone whose last DM or button tap is older than main.WINDOW (23h) is skipped; no message tags.
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import main

main.logging.getLogger("httpx").setLevel(main.logging.WARNING)

LOG = "uvicorn.log"
LINE = re.compile(r"^(\S+ \S+),\d+ INFO (?:DM trigger|button tap) from (\d+)(?: \(.*?\))?: (.*)$")


def last_messages():
    """igsid -> (last DM/tap time in UTC, last text). The users table only exists since 2026-10-05,
    so uvicorn.log ("DM trigger from" / "button tap from" lines) fills in everyone before that."""
    last = {}
    with main.db() as conn:
        for igsid, at in conn.execute("SELECT igsid, last_msg_at FROM users WHERE last_msg_at IS NOT NULL"):
            last[igsid] = (datetime.fromisoformat(at), "")
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line.lstrip("﻿"))
            if m:
                at = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)  # log is local time
                if m[2] not in last or at >= last[m[2]][0]:
                    last[m[2]] = (at, m[3])
    return last


def candidates():
    cutoff = main.now() - main.WINDOW
    with main.db() as conn:
        sent = {row[0] for row in conn.execute("SELECT igsid FROM sent")}
        names = dict(conn.execute("SELECT igsid, username FROM users"))
    rows = [(at, igsid, names.get(igsid), text) for igsid, (at, text) in last_messages().items()
            if at >= cutoff and igsid not in sent]
    return sorted(rows)


def nudge(igsid, username, at, follows):
    with main.db() as conn:  # make sure log-only users have a row, without touching last_seen of known ones
        conn.execute("INSERT OR IGNORE INTO users (igsid, username, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                     (igsid, username, at.isoformat(), at.isoformat()))
        conn.execute("UPDATE users SET username = COALESCE(?, username), last_msg_at = MAX(COALESCE(last_msg_at, ''), ?), "
                     "follows = COALESCE(?, follows) WHERE igsid = ?",
                     (username, at.isoformat(), None if follows is None else int(follows), igsid))
    if main.already_sent(igsid):  # the live bot may have sent it since the dry run
        return "already has the link"
    if follows:
        if main.send_link(igsid, {"id": igsid}):
            main.record_sent(igsid, username)
            return "link sent"
        return "link FAILED"
    if main.ask_to_follow(igsid, {"id": igsid}, main.text("FOLLOW_MSG"), main.text("FOLLOW_TEXT")):
        return "rules + follow request sent"
    return "follow request FAILED"


def run():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="only print who would be messaged")
    dry = p.parse_args().dry_run

    rows = candidates()
    print(f"{len(rows)} users messaged within the last {main.WINDOW} and have no link; checking follows...")
    with ThreadPoolExecutor(4) as pool:
        checks = list(pool.map(main.check_follow, [igsid for _, igsid, _, _ in rows]))

    now = main.now()
    for (at, igsid, username, text), (follows, api_username) in zip(rows, checks):
        username = api_username or username
        status = {True: "follows", False: "not following", None: "unknown"}[follows]
        if dry:
            result = "would send link" if follows else "would send rules + follow request"
        else:
            result = nudge(igsid, username, at, follows)
        hours = (now - at).total_seconds() / 3600
        print(f"{hours:5.1f}h ago  {igsid:>17}  {username or '?':24} {status:14} {result:34} {text[:60]}")


if __name__ == "__main__":
    run()
