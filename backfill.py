"""Reply to DMs the bot never received (webhook was down, e.g. the tunnel address changed and Meta still had
the old one). Runs each person through the bot's normal flow, once.

Only messages whose id isn't in `seen` count (main.process records every DM it handles there), and only from
the last 23 h, since Instagram allows replies for 24 h. Also on the dashboard (127.0.0.1:8001): Instagram bot card -> "Check missed DMs".

python backfill.py           -> list who would get a reply, sends nothing
python backfill.py --send    -> actually replies
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

import main

OWN_NAME = "kalakritistudio_parul"


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def conversations(since):
    url = f"{main.API}/me/conversations"
    params = {"platform": "instagram", "limit": 50,
              "fields": "updated_time,messages.limit(10){from,message,created_time,id}"}
    while url:
        r = httpx.get(url, params=params, headers={"Authorization": f"Bearer {main.IG_ACCESS_TOKEN}"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        for c in d.get("data", []):
            if ts(c["updated_time"]) < since:
                return
            yield c
        url, params = d.get("paging", {}).get("next"), None


def mine(m):
    return m["from"]["id"] == main.IG_USER_ID or m["from"].get("username") == OWN_NAME


def find_missed():
    """People with text DMs the bot never saw and we haven't answered since: [{igsid, username, texts, mids, ...}]."""
    since = main.now() - main.WINDOW
    fresh = main.now() - timedelta(minutes=2)  # the live bot may still be on these (seen is written ~1 s later)
    with main.db() as conn:
        seen = {r[0] for r in conn.execute("SELECT id FROM seen")}
    people = []
    for c in conversations(since):
        msgs = sorted(c.get("messages", {}).get("data", []), key=lambda m: m["created_time"])
        last_out = max((ts(m["created_time"]) for m in msgs if mine(m)), default=None)
        pending = [m for m in msgs if not mine(m) and (m.get("message") or "").strip() and m["id"] not in seen
                   and since <= ts(m["created_time"]) < fresh and (last_out is None or ts(m["created_time"]) > last_out)]
        if pending:
            sid = pending[0]["from"]["id"]
            people.append({"igsid": sid, "username": pending[0]["from"].get("username"),
                           "texts": [m["message"].strip() for m in pending], "mids": [m["id"] for m in pending],
                           "last_at": pending[-1]["created_time"], "has_link": main.already_sent(sid)})
    return people


def reply(person):
    """What the live bot would have done, but once for all their messages (one follow request, not one each).
    Returns False if the bot handled them meanwhile (e.g. Meta re-delivered the messages)."""
    if not all([main.first_time(mid) for mid in person["mids"]]):
        return False
    sid, texts = person["igsid"], person["texts"]
    claimed = any(t.upper() == "DONE" or t.startswith("I'm following") for t in texts)
    answers = []
    for t in texts:
        answers += [a for a in main.faq_answers(t) if a not in answers]
    for a in answers:
        if a == main.MATERIAL_MSG and main.already_sent(sid):
            a += "\n\n" + main.MATERIAL_LINK_MSG.format(main.personal_link(sid))
        main.send({"id": sid}, a)
    main.handle(sid, person["username"], {"id": sid}, claimed=claimed)
    main.log.info("caught up missed DM from %s", person["username"] or sid)
    return True


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    send = "--send" in sys.argv
    missed = find_missed()
    print(f"{len(missed)} people with DMs the bot never got ({'SENDING' if send else 'dry run'})\n")
    for p in missed:
        print(f"{p['username'] or p['igsid']:28} {p['texts']!r:.80}  has_link={p['has_link']}")
        if send and not reply(p):
            print("  skipped: the bot handled it meanwhile")
