"""Facebook Messenger side of the bot: people who message the Facebook Page (e.g. from a click-to-Messenger ad)
get the same steps, buttons, answers and reminders as on Instagram, and the chat stays in Messenger.

Comments on the Page's posts and ads get the same as Instagram comments: a public "inbox check karo" reply and
one private Messenger reply (Facebook allows only one until they answer) with the intro and the buttons.

Facebook doesn't tell apps who follows a Page, so the "I'm following ✅" tap (or DONE) is trusted and gets the link.
Needs PAGE_ID, PAGE_ACCESS_TOKEN (with pages_messaging) and FB_APP_SECRET in .env; until then Page webhooks
are ignored. People are kept in fb_users; their join links are main.personal_link("fb:<psid>"), so taps land in
the same clicks table as Instagram's. Texts and workshop settings come from main (text(), meet_url(), ...).
"""
import logging
import os
import sqlite3
from datetime import datetime

import httpx

import main
from main import now

PAGE_ID = os.getenv("PAGE_ID", "").strip()
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
API = "https://graph.facebook.com/v23.0"
PAGE_URL = f"https://www.facebook.com/{PAGE_ID}"
VISIT_TITLE = "Visit Page"


def page_text(t, visit=None):
    """An Instagram message with the profile wording swapped for the Page, so Instagram text changes reach here.
    visit: the Instagram profile button's title as the message names it (default: main.VISIT_TITLE)."""
    return (t.replace(visit or main.VISIT_TITLE, VISIT_TITLE).replace("Profile pe", "Page pe")
            .replace("profile follow", "Page follow"))


# the default texts in Messenger's wording; the bot goes through text(), which picks up the workshop's edits
RULES_MSG, INTRO_MSG, FOLLOW_MSG, REMIND_MSG = map(page_text, (main.RULES_MSG, main.INTRO_MSG, main.FOLLOW_MSG,
                                                               main.REMIND_MSG))


def text(key):
    """main.text(key) in Messenger's wording."""
    return page_text(main.text(key), main.text("VISIT_TITLE"))


log = logging.getLogger("bot")


def ready():
    return bool(PAGE_ID and PAGE_ACCESS_TOKEN)


def key(psid):
    """The id Messenger people go by in the shared links/clicks tables (Instagram ids have no prefix)."""
    return f"fb:{psid}"


def db():
    conn = main.db()
    # last_msg_at opens the 24h window; asked_at = buttons sent; the reminder columns hold one reminder per person
    conn.execute("CREATE TABLE IF NOT EXISTS fb_users (psid TEXT PRIMARY KEY, name TEXT, first_seen TEXT, "
                 "last_msg_at TEXT, rules_sent_at TEXT, asked_at TEXT, link_sent_at TEXT, reminded_at TEXT, "
                 "link_reminded_at TEXT, meet_sent_at TEXT)")
    have = {row[1] for row in conn.execute("PRAGMA table_info(fb_users)")}
    for col in ("past_msg_at", "between_sent_at"):  # main.PAST_MSG / main.BETWEEN_MSG, like in users
        if col not in have:
            conn.execute(f"ALTER TABLE fb_users ADD COLUMN {col} TEXT")
    return conn


def user(psid):
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM fb_users WHERE psid = ?", (psid,)).fetchone()
    return dict(row) if row else {}


def update(psid, **cols):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO fb_users (psid, first_seen) VALUES (?, ?)", (psid, now().isoformat()))
        if cols:
            conn.execute(f"UPDATE fb_users SET {', '.join(f'{c} = ?' for c in cols)} WHERE psid = ?",
                         (*cols.values(), psid))


def api(method, path, **kwargs):
    r = httpx.request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {PAGE_ACCESS_TOKEN}"},
                      timeout=15, **kwargs)
    if r.is_error:
        log.error("Messenger API %s /%s failed: %s %s", method, path, r.status_code, r.text)
        main.set_error(r)
        return None
    main.set_error(None)
    return r.json()


def send(psid, message_text, buttons=None):
    message = {"text": message_text}
    if buttons:
        message = {"attachment": {"type": "template", "payload": {
            "template_type": "button", "text": message_text, "buttons": buttons}}}
    ok = api("POST", f"{PAGE_ID}/messages", json={"recipient": {"id": psid}, "messaging_type": "RESPONSE",
                                                  "message": message}) is not None
    main.note_send("fb", key(psid), message_text, buttons, ok, lambda t: page_text(t, main.text("VISIT_TITLE")))
    return ok


def lookup_name(psid):
    data = api("GET", psid, params={"fields": "first_name,last_name"}) or {}
    return " ".join(filter(None, (data.get("first_name"), data.get("last_name")))) or None


def follow_buttons():
    return [{"type": "web_url", "url": PAGE_URL, "title": VISIT_TITLE},
            {"type": "postback", "title": main.text("FOLLOWING_TITLE"), "payload": main.CHECK_FOLLOW}]


def send_rules(psid, first=False):
    """The plain-text steps (INTRO_MSG on a first message), at most once per main.RULES_EVERY."""
    at = user(psid).get("rules_sent_at")
    if at and datetime.fromisoformat(at) > now() - main.RULES_EVERY:
        return False
    if send(psid, text("INTRO_MSG") if first else text("RULES_MSG")):
        update(psid, rules_sent_at=now().isoformat())
        return True
    return False


def personal_link(psid, name=None):
    """main.personal_link for a Messenger person. Their name goes in ig_names too, so the members dashboard
    doesn't ask Instagram about a Facebook id."""
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ig_names (igsid TEXT PRIMARY KEY, name TEXT, at TEXT)")
        conn.execute("INSERT OR IGNORE INTO ig_names VALUES (?, ?, ?)",
                     (key(psid), f"{name or 'Someone'} (Facebook)", now().isoformat()))
    return main.personal_link(key(psid))


def send_meet(psid):
    url = main.meet_url()
    sent = send(psid, main.fill(text("MEET_MSG"), url))
    sent = send(psid, text("MEET_BUTTON_MSG"), [{"type": "web_url", "url": url,
                                                 "title": main.text("MEET_TITLE")}]) or sent
    if sent:
        update(psid, meet_sent_at=now().isoformat())
    return sent


def send_link(psid, name=None):
    """Like main.send_link in a DM: the Meet link first while the workshop is live, then the WhatsApp link."""
    if main.meet_url():
        send_meet(psid)
    url = personal_link(psid, name)
    sent = send(psid, main.fill(text("LINK_MSG"), url))
    return send(psid, text("LINK_BUTTON_MSG"), [{"type": "web_url", "url": url,
                                                 "title": main.text("JOIN_TITLE")}]) or sent


def past_attendee(u):
    """Got their join link for an earlier workshop and none yet for this one (main.past_before)."""
    return bool(u.get("link_sent_at")) and not main.this_workshop(u["link_sent_at"])


def between_buttons():
    return [{"type": "web_url", "url": PAGE_URL, "title": VISIT_TITLE}]


def send_between(psid, who):
    """No workshop running: main.BETWEEN_MSG with the Page button, at most once a day."""
    at = user(psid).get("between_sent_at")
    if at and datetime.fromisoformat(at) > now() - main.RULES_EVERY:
        log.info("messenger: skip %s: no workshop running, already told today", who)
        main.activity("skip", key(psid), "no workshop running, already told today: no reply", channel="fb")
        return False
    if send(psid, text("BETWEEN_MSG"), between_buttons()) or send(psid, text("BETWEEN_MSG")):
        update(psid, between_sent_at=now().isoformat())
        log.info("messenger: sent between-workshops message to %s", who)
        return True
    return False


def handle(psid, claimed=False):
    """Messenger version of main.handle; returns True if a message went out.
    claimed: they tapped "I'm following ✅" or wrote DONE, which Facebook gives no way to check, so it's trusted."""
    u = user(psid)
    first = not main.this_workshop(u.get("last_msg_at"))
    update(psid, last_msg_at=now().isoformat())
    name = u.get("name")
    if name is None:  # looked up once; "" = Facebook didn't share it
        name = lookup_name(psid) or ""
        update(psid, name=name)
    who = name or psid
    if main.workshop() is None:
        return send_between(psid, who)
    live = main.meet_url()
    past = past_attendee(u)  # an earlier workshop's link: the follow-gate again, then main.PAST_MSG + the new link
    if u.get("link_sent_at") and not past:
        if claimed and not live:  # e.g. tapped the buttons of a comment reply after getting the link
            if send_link(psid, name):
                log.info("messenger: link sent again to %s", who)
                return True
            return False
        # while live, link holders get the Meet link once (again whenever they tap "I'm following" or say DONE)
        if live and (claimed or not main.this_workshop(u.get("meet_sent_at"))):
            if send_meet(psid):
                log.info("messenger: meet link sent to %s", who)
                return True
            return False
        log.info("messenger: skip %s: already has the link", who)
        main.activity("skip", key(psid), "already has the link: no reply", channel="fb")
        return False
    if claimed:
        if past and main.latest_workshop()["past_offer"] and send(psid, text("PAST_MSG")):
            update(psid, past_msg_at=now().isoformat())
            log.info("messenger: sent past-attendee offer to %s", who)
        if send_link(psid, name):
            update(psid, link_sent_at=now().isoformat())
            log.info("messenger: link sent to %s", who)
            return True
        return False
    send_rules(psid, first)
    if send(psid, text("FOLLOW_MSG"), follow_buttons()) or send(psid, text("FOLLOW_TEXT")):
        update(psid, asked_at=now().isoformat())
        log.info("messenger: sent follow request to %s", who)
        return True
    return False


def comment(v):
    """A new comment on one of the Page's posts or ads (webhook field "feed")."""
    cid, sender = v.get("comment_id"), (v.get("from") or {}).get("id")
    if (v.get("item") != "comment" or v.get("verb") != "add" or not cid or sender == PAGE_ID
            or v.get("parent_id") not in (None, v.get("post_id"))):  # replies inside a thread don't count
        return
    if not main.first_time(f"fbc:{cid}"):
        return
    log.info("messenger: comment trigger from %s: %r", (v.get("from") or {}).get("name"), v.get("message"))
    main.activity("in", None, f"{(v.get('from') or {}).get('name') or 'Someone'} commented on a Facebook post",
                  v.get("message"), channel="fb")
    # the private reply is the only message allowed before they answer, so the intro and buttons go together
    between = main.workshop() is None
    reply = (text("BETWEEN_MSG"), between_buttons()) if between else (text("INTRO_MSG"), follow_buttons())
    r = api("POST", f"{PAGE_ID}/messages", json={"recipient": {"comment_id": cid}, "message": {"attachment": {
        "type": "template", "payload": {"template_type": "button", "text": reply[0], "buttons": reply[1]}}}})
    psid = (r or {}).get("recipient_id")
    main.note_send("fb", key(psid) if psid else None, reply[0], reply[1], bool(r),
                   lambda t: page_text(t, main.text("VISIT_TITLE")))
    if not r:
        return
    if psid:
        update(psid, **({"between_sent_at": now().isoformat()} if between else
                        {"asked_at": now().isoformat(), "rules_sent_at": now().isoformat()}))
    log.info("messenger: sent private reply to comment %s", cid)
    public = main.text("COMMENT_REPLY")
    if api("POST", f"{cid}/comments", data={"message": public}):
        log.info("messenger: replied publicly to comment %s", cid)
        main.activity("out", key(psid) if psid else None, "public reply under the comment", public,
                      {"key": "COMMENT_REPLY"}, channel="fb")


def process(payload):
    """Webhook events of object "page" (main.process hands them over)."""
    if not ready():
        log.warning("messenger webhook ignored: PAGE_ID or PAGE_ACCESS_TOKEN missing in .env")
        return
    for entry in payload.get("entry", []):
        log.info("messenger webhook received: messaging=%d changes=%s", len(entry.get("messaging", [])),
                 [c.get("field") for c in entry.get("changes", [])])
        for change in entry.get("changes", []):
            if change.get("field") == "feed" and main.current_event(
                    (change.get("value") or {}).get("created_time") or entry.get("time")):
                comment(change.get("value") or {})
        for m in entry.get("messaging", []):
            if not main.current_event(m.get("timestamp") or entry.get("time")):
                continue
            msg, pb = m.get("message", {}), m.get("postback", {})
            psid = m.get("sender", {}).get("id")
            if not psid or psid == PAGE_ID or msg.get("is_echo"):
                continue
            if pb:
                if pb.get("payload") == main.CHECK_FOLLOW and main.first_time(
                        pb.get("mid") or f"fbpb:{psid}:{m.get('timestamp')}"):
                    log.info("messenger: button tap from %s: %r", psid, pb.get("title"))
                    main.activity("in", key(psid), f"tapped “{pb.get('title') or 'a button'}”", channel="fb")
                    handle(psid, claimed=True)
                continue
            said = msg.get("text", "").strip()
            # click-to-Messenger ads attach a referral (inside the message, or alone as messaging_referrals)
            ad = (msg.get("referral") or m.get("referral") or {}).get("ad_id")
            if not (said or ad) or not main.first_time(msg.get("mid") or f"fbref:{psid}:{m.get('timestamp')}"):
                continue
            if ad or said.upper() == "DONE" or main.matches(said):
                log.info("messenger trigger from %s (ad=%s): %r", psid, ad, said)
                main.activity("in", key(psid), "opened the chat from an ad" if ad and not said else
                              "message from an ad" if ad else "message", said or None, channel="fb")
                hits = main.answers_for(said)
                for f, answer in hits:
                    u = user(psid)
                    if (f["id"] == "material" and u.get("link_sent_at") and not past_attendee(u)
                            and not main.link_used(key(psid))):
                        answer += "\n\n" + main.fill(text("MATERIAL_LINK_MSG"), personal_link(psid, u.get("name")))
                    answer, buttons = page_text(answer, main.text("VISIT_TITLE")), main.reply_buttons(f)
                    if (send(psid, answer, buttons) if buttons else send(psid, answer)) or (
                            buttons and send(psid, f"{answer}\n{f['button_url']}")):
                        log.info("messenger: answered question from %s", psid)
                if said.upper() != "DONE" and any(f.get("stop") for f, _ in hits):
                    update(psid, last_msg_at=now().isoformat())  # no follow step after a "stop here" reply
                    log.info("messenger: skip %s: a custom reply stops here", psid)
                    main.activity("skip", key(psid), "custom reply set to \"stop here\": no follow step", channel="fb")
                else:
                    handle(psid, claimed=said.upper() == "DONE")
            else:
                main.activity("in", key(psid), "message without the keyword: no reply", said, channel="fb")


def remind():
    """One reminder per person and workshop, like Instagram, only inside the 24h window and never while the
    workshop is live: REMIND_MSG with the buttons main.REMIND_AFTER after them if "I'm following" was never
    tapped, or LINK_REMIND_MSG main.LINK_REMIND_AFTER after the link if it was never really tapped.
    Returns {"follow": n, "link": n} (None when there's nothing to do)."""
    if not ready() or main.meet_url() or main.workshop() is None:
        return None
    from dashboard import real_taps  # same rules as the dashboard: Meta's scanners and the owner's phone don't count
    window = (now() - main.WINDOW).isoformat()
    start = main.utc_iso(main.past_before())  # reminders from earlier workshops don't count
    with db() as conn:
        asked = conn.execute("SELECT psid, name FROM fb_users WHERE (link_sent_at IS NULL OR link_sent_at < ?) "
                             "AND asked_at <= ? AND asked_at >= ? AND last_msg_at >= ? "
                             "AND (reminded_at IS NULL OR reminded_at < ?) "
                             "AND (link_reminded_at IS NULL OR link_reminded_at < ?)",
                             (start, (now() - main.REMIND_AFTER).isoformat(), window, window, start, start)).fetchall()
        linked = conn.execute("SELECT psid, name FROM fb_users WHERE link_sent_at <= ? AND link_sent_at >= ? "
                              "AND last_msg_at >= ? AND (reminded_at IS NULL OR reminded_at < ?) "
                              "AND (link_reminded_at IS NULL OR link_reminded_at < ?)",
                              ((now() - main.LINK_REMIND_AFTER).isoformat(), start, window, start, start)).fetchall()
        clicks = conn.execute("SELECT igsid, clicked_at, COALESCE(user_agent, ''), ip FROM clicks "
                              "WHERE igsid LIKE 'fb:%'").fetchall()
    done = {"follow": 0, "link": 0}
    for psid, name in asked:
        if send(psid, text("REMIND_MSG"), follow_buttons()):
            update(psid, reminded_at=now().isoformat())
            done["follow"] += 1
            log.info("messenger: sent reminder to %s", name or psid)
    tapped = real_taps(clicks)
    for psid, name in linked:
        if key(psid) not in tapped and send(psid, main.fill(text("LINK_REMIND_MSG"), personal_link(psid, name))):
            update(psid, link_reminded_at=now().isoformat())
            done["link"] += 1
            log.info("messenger: sent join-link reminder to %s (no tap yet)", name or psid)
    return done
