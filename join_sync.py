"""Durable public join service bridge. Identity mappings stay in bot.db."""
from contextlib import closing
import logging
import os
import time
import httpx

log = logging.getLogger("join_sync")
_pushed = {}
_registered_at = {}


def configured():
    return bool(os.getenv("JOIN_SERVICE_BASE") and os.getenv("JOIN_SYNC_TOKEN"))


def api(path, payload=None):
    url = os.environ["JOIN_SERVICE_BASE"].rstrip("/") + "/api/sync/" + path
    with httpx.Client(timeout=15) as client:
        kwargs = {"headers": {"Authorization": "Bearer " + os.environ["JOIN_SYNC_TOKEN"]}}
        response = client.get(url, **kwargs) if payload is None else client.post(url, json=payload, **kwargs)
        response.raise_for_status()
        return response.json()


def register(code, target, state="active"):
    api("links", {"links": [{"code": code, "target": target, "state": state}]})
    _pushed[code] = (target, state)
    _registered_at[code] = time.monotonic()


def pull_events(bot):
    # Cursor and click inserts commit atomically. Retrying cannot duplicate imported events.
    with closing(bot.db()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS join_sync_state (source TEXT PRIMARY KEY, cursor INTEGER NOT NULL)")
        source = os.environ["JOIN_SERVICE_BASE"].rstrip("/")
        row = conn.execute("SELECT cursor FROM join_sync_state WHERE source=?", (source,)).fetchone()
        cursor = row[0] if row else 0
        events = api(f"events?after={cursor}")["events"]
        for event in events:
            normal = conn.execute("SELECT igsid FROM links WHERE code=?", (event["code"],)).fetchone()
            if normal:
                conn.execute("INSERT INTO clicks(code,igsid,clicked_at,user_agent,ip) VALUES(?,?,?,?,?)",
                             (event["code"], normal[0], event["at"], event["ua"], event["ip"]))
            else:
                rp = conn.execute("SELECT reel_id,igsid FROM reel_people WHERE code=?", (event["code"],)).fetchone()
                if rp:
                    conn.execute("INSERT INTO reel_clicks VALUES(?,?,?,?,?,?)",
                                 (event["code"], *rp, event["at"], event["ua"], event["ip"]))
            cursor = max(cursor, event["id"])
        conn.execute("INSERT INTO join_sync_state VALUES(?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor", (source, cursor))
    return len(events)


def push_links(bot):
    with closing(bot.db()) as conn, conn:
        rows = conn.execute("SELECT l.code,l.igsid,s.sent_at,u.igsid FROM links l LEFT JOIN sent s ON s.igsid=l.igsid LEFT JOIN used_links u ON u.igsid=l.igsid").fetchall()
        reel_rows = conn.execute("SELECT p.code,r.wa_link FROM reel_people p JOIN reels r ON r.id=p.reel_id WHERE p.code IS NOT NULL").fetchall()
    target = bot.workshop_link()
    links = [{"code":code,"target":target,"state":"expired" if sent and not bot.this_workshop(sent) else "used" if used else "active"} for code,ig,sent,used in rows]
    links.extend({"code":code,"target":url,"state":"active"} for code,url in reel_rows if url)
    # Avoid rewriting unchanged rows and let record_sent finish after a freshly issued link.
    links = [l for l in links if time.monotonic() - _registered_at.get(l['code'], -1000) > 60
             and _pushed.get(l['code']) != (l['target'], l['state'])]
    for offset in range(0,len(links),100):
        batch = links[offset:offset+100]
        api("links", {"links":batch})
        _pushed.update({l['code']:(l['target'],l['state']) for l in batch})


def loop(bot):
    last_push = 0
    while True:
        try:
            if configured():
                pull_events(bot)
                if time.monotonic() - last_push > 60:
                    push_links(bot)
                    last_push = time.monotonic()
        except Exception as exc:
            log.warning("Join service sync failed (%s); will retry", type(exc).__name__)
        time.sleep(10)
