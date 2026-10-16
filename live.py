"""One-time "workshop is LIVE" broadcast to the most recent people (by last DM or button tap).

python live.py --dry-run   only prints who would get what (follow lookups only, no DMs)
python live.py             sends for real, a few seconds apart

Followers get the Meet link. Everyone else gets LIVE_FOLLOW_MSG with the follow buttons; tapping
"I'm following" then gets them the Meet link through the normal bot flow.
Only people inside Instagram's 24h messaging window can be reached; the rest are skipped, not tried.
Each person gets this once (users.live_sent_at).
"""
import argparse
import time
from datetime import timedelta

import main
from nudge import last_messages

main.logging.getLogger("httpx").setLevel(main.logging.WARNING)

LATEST = 300
REACHABLE = timedelta(hours=23, minutes=50)  # a little inside Instagram's 24h window
GAP = 3  # seconds between people, so it doesn't look like a blast
LIVE_FOLLOW_MSG = ("🔴 Resin Pooja Thali workshop abhi LIVE hai!\n\n"
                   "Link ke liye profile follow karo, phir neeche \"I'm following ✅\" dabao, Meet link turant aa jayega 👇")


def candidates():
    latest = sorted(((at, igsid) for igsid, (at, _) in last_messages().items()), reverse=True)[:LATEST]
    cutoff = main.now() - REACHABLE
    with main.db() as conn:
        done = {r[0] for r in conn.execute("SELECT igsid FROM users WHERE live_sent_at IS NOT NULL")}
        names = dict(conn.execute("SELECT igsid, username FROM users"))
    reachable = [(at, igsid, names.get(igsid)) for at, igsid in latest if at >= cutoff and igsid not in done]
    return latest, reachable


def run():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="only print who would be messaged")
    dry = p.parse_args().dry_run

    latest, rows = candidates()
    print(f"latest {len(latest)} people; {len(rows)} inside the 24h window and not yet messaged")
    counts = {}
    for at, igsid, username in rows:
        follows, api_username = main.check_follow(igsid)
        username = api_username or username
        if dry:
            result = "would send Meet link" if follows else "would send live + follow buttons"
        elif follows:
            result = "Meet link sent" if main.send_meet(igsid, {"id": igsid}) else "Meet link FAILED"
        else:
            ok = main.send({"id": igsid}, LIVE_FOLLOW_MSG, main.follow_buttons())
            if ok:
                main.update_user(igsid, asked_at=main.now().isoformat())
            result = "live + follow buttons sent" if ok else "live msg FAILED"
        if not dry and "FAILED" not in result:
            main.update_user(igsid, live_sent_at=main.now().isoformat())
        counts[result] = counts.get(result, 0) + 1
        hours = (main.now() - at).total_seconds() / 3600
        print(f"{hours:5.1f}h ago  {username or igsid:24} {result}", flush=True)
        if not dry:
            time.sleep(GAP)
    print(counts)


if __name__ == "__main__":
    run()
