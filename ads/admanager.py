"""My own small Meta Ads Manager: create ads from photos/videos, turn them on and off, change dates and budgets,
and see how they're doing.

.venv/Scripts/python.exe -m uvicorn admanager:app --app-dir ads --host 127.0.0.1 --port 8002   (start_admanager.bat)

Only listens on 127.0.0.1, so it never goes through the Cloudflare tunnel (that one points at port 8000).
Credentials come from ads/.env. The token goes to graph.facebook.com in a header and nowhere else (not in URLs,
logs or pages). Everything it creates starts PAUSED: only a click on the page (behind a confirmation) turns
anything on. It never touches billing or payment settings, it only reads them for the warning banner.
"""
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
from dotenv import dotenv_values
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

HERE = Path(__file__).resolve().parent
ENV = dotenv_values(HERE / ".env")
TOKEN = ENV.get("META_ACCESS_TOKEN") or ""
ACCOUNT = (ENV.get("AD_ACCOUNT_ID") or "").removeprefix("act_")
ACT = "act_" + ACCOUNT
PAGE_ID = ENV.get("PAGE_ID") or ""
IG_ID = ENV.get("IG_ACCOUNT_ID") or ""
GRAPH = "https://graph.facebook.com/v23.0"
TEMPLATE_CAMPAIGN = "120254958322650581"  # "again new main": the click-to-DM setup known to work; read-only here
IST = timezone(timedelta(hours=5, minutes=30))
UPLOADS = HERE / "uploads"  # files wait here only until they're sent to Meta
BILLING_URL = f"https://business.facebook.com/billing_hub/payment_settings?asset_id={ACCOUNT}"
ACCESS_URL = "https://business.facebook.com/latest/settings/system_users"
DM = "onsite_conversion.messaging_conversation_started_7d"


class TokenFilter(logging.Filter):
    """Belt and braces: the token never ends up in a log line, whatever logs it."""
    def filter(self, record):
        if TOKEN and TOKEN in record.getMessage():
            record.msg, record.args = record.getMessage().replace(TOKEN, "<token>"), ()
        return True


log = logging.getLogger("admanager")
log.setLevel(logging.INFO)
_handler = logging.FileHandler(HERE / "admanager.log", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_handler.addFilter(TokenFilter())
log.addHandler(_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI()


# ---------- talking to Meta ----------

class MetaError(Exception):
    """A Graph API error, with Meta's message in plain words plus what to do about it when we know."""

    def __init__(self, message, code=None, subcode=None, status=400, hint=None, link=None, trace=None):
        super().__init__(message)
        self.message, self.code, self.subcode, self.status = message, code, subcode, status
        self.hint, self.link, self.trace = hint, link, trace

    def info(self):
        return {"message": self.message, "hint": self.hint, "link": self.link, "code": self.code,
                "subcode": self.subcode, "trace": self.trace, "rate_limited": self.status == 429}


RATE_CODES = {4, 17, 32, 613, 80000, 80003, 80004, 80014}  # "asking too often" (#17 user, #80004 ad account)
_rate = {"until": 0.0}
client = httpx.Client(base_url=GRAPH, headers={"Authorization": f"Bearer {TOKEN}"},
                      timeout=httpx.Timeout(90, connect=20))


def explain(err, status):
    """Meta's error -> MetaError with plain words. Meta's own error_user_msg is usually readable already."""
    code, sub = err.get("code"), err.get("error_subcode")
    title, msg = err.get("error_user_title"), err.get("error_user_msg")
    text = f"{title}: {msg}" if title and msg else msg or err.get("message") or f"Meta said no (HTTP {status})"
    text = text.replace(TOKEN, "<token>") if TOKEN else text
    hint = link = None
    if code == 200 and sub == 2490585 or "No write permission on ad account" in text:
        text = "Meta won't let the ad-bot system user create or change ads in this ad account yet."
        hint = ("Business settings → Users → System users → ad-bot → Assign assets → Ad accounts → tick "
                f"{ACCOUNT} → turn on 'Manage campaigns' (full control) → Assign. No new token needed.")
        link = ACCESS_URL
    elif "pages_manage_ads" in text or (code in (10, 200) and "page" in text.lower() and "permission" in text.lower()):
        hint = ("The token needs the pages_manage_ads permission. Business settings → System users → ad-bot → "
                "Generate token → tick pages_manage_ads (keep the others) → paste the new token into ads/.env "
                "and restart the ad manager.")
        link = ACCESS_URL
    elif code == 190:
        text = "The token in ads/.env doesn't work any more."
        hint = "Business settings → System users → ad-bot → Generate token, then paste it into ads/.env."
        link = ACCESS_URL
    elif "instagram" in text.lower() and ("account" in text.lower() or "actor" in text.lower()) and code != 100 \
            or sub in (2446886, 1772103):
        hint = ("Add the Instagram account to the ad account: Business settings → Accounts → Instagram accounts "
                "→ @kalakritistudio_parul → Assign assets / Connected assets → add the ad account.")
        link = "https://business.facebook.com/latest/settings/instagram_account_v2"
    elif "whatsapp" in text.lower():
        hint = ("WhatsApp ads need a WhatsApp Business number linked to the Facebook Page: Facebook → your Page → "
                "Settings → Linked accounts → WhatsApp. Or untick WhatsApp in Message destinations.")
    if code in RATE_CODES:
        return MetaError("Meta is limiting how often we can ask or change things right now (too many requests). "
                         "Wait a few minutes, then try again.", code, sub, 429, trace=err.get("fbtrace_id"))
    return MetaError(text, code, sub, 502 if status >= 500 else 400, hint, link, err.get("fbtrace_id"))


def graph(method, path, params=None, data=None, files=None, timeout=None, retries=2, quiet=False):
    """One Graph API call. A rate-limit error is retried once after a pause when Meta says it's brief (Meta
    rejected the call, so repeating it can't create anything twice); network errors on POSTs are not retried for
    the same reason."""
    for attempt in range(retries + 1):
        try:
            r = client.request(method, path, params=params, data=data, files=files,
                               timeout=timeout or client.timeout)
        except httpx.HTTPError as e:
            if method == "GET" and attempt < retries:
                time.sleep(2)
                continue
            raise MetaError(f"Couldn't reach Meta ({type(e).__name__}). Is the internet on?", status=502)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.is_success and not (isinstance(body, dict) and "error" in body):
            return body
        err = explain(body.get("error") or {}, r.status_code)
        if err.status == 429:
            wait = regain_minutes(r) * 60
            _rate["until"] = time.time() + max(wait, 60)
            if attempt == 0 and retries and wait == 0:
                time.sleep(5)
                continue
        if not quiet:
            log.warning("%s %s failed: %s (code %s/%s, trace %s)", method, path, err.message, err.code,
                        err.subcode, err.trace)
        raise err
    raise MetaError("Meta kept refusing. Try again in a minute.", status=429)


def regain_minutes(r):
    """Meta's own estimate of how long the rate limit lasts, from the usage headers (0 if it doesn't say)."""
    try:
        usage = json.loads(r.headers.get("x-business-use-case-usage") or "{}")
        return max(u.get("estimated_time_to_regain_access", 0) for v in usage.values() for u in v)
    except (ValueError, TypeError, AttributeError):
        return 0


def form(d):
    """Graph API POST fields: nested values as JSON, None left out."""
    return {k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in d.items() if v is not None}


def prune(v):
    """Drop None/empty values from nested dicts so optional bits of a spec just aren't sent."""
    if isinstance(v, dict):
        return {k: prune(x) for k, x in v.items() if x not in (None, "", [], {})}
    if isinstance(v, list):
        return [prune(x) for x in v]
    return v


def get_all(path, params, limit=200):
    """Every page of a list. Follows the 'after' cursor ourselves instead of Meta's 'next' URL."""
    out, params = [], {**params, "limit": limit}
    while True:
        r = graph("GET", path, params=params)
        out += r.get("data", [])
        paging = r.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after or len(out) > 5000:
            return out
        params = {**params, "after": after}


_cache, _cache_lock = {}, threading.Lock()


def cached(key, ttl, fn, fresh=False):
    """fn() at most every ttl seconds. While Meta is rate-limiting us, the last answer is served instead (the
    page shows the limit banner), so a busy moment doesn't blank the page."""
    with _cache_lock:
        hit = _cache.get(key)
    limited = time.time() < _rate["until"]
    if hit and (limited or not fresh and time.time() - hit[0] < ttl):
        return hit[1]
    try:
        value = fn()
    except MetaError as e:
        if e.status == 429 and hit:
            return hit[1]
        raise
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


_pool = ThreadPoolExecutor(8)


def parallel(*fns):
    """Run independent Graph calls at the same time (the page waits for the slowest, not the sum)."""
    return [f.result() for f in [_pool.submit(fn) for fn in fns]]


def forget(*prefixes):
    with _cache_lock:
        for k in [k for k in _cache if k.startswith(prefixes)]:
            del _cache[k]


def fail(e: MetaError):
    raise HTTPException(e.status, e.info())


# ---------- account, billing and access ----------

def money(paise):
    return round(int(paise or 0) / 100, 2)


def billing(a):
    """Plain-words problems that stop ads from delivering. Read-only: this never changes billing."""
    problems = []
    fs = a.get("funding_source_details") or {}
    shown = fs.get("display_string") or ""
    balance = None
    if "balance" in shown.lower():  # prepaid: "Available balance (₹0.00 INR)"
        m = re.search(r"([\d,]+(?:\.\d+)?)", shown)
        balance = float(m[1].replace(",", "")) if m else None
        if balance is not None and balance <= 0:
            problems.append(f"Your prepaid balance is ₹{balance:,.2f}. Ads can't spend until you add money.")
    cap, spent = int(a.get("spend_cap") or 0), int(a.get("amount_spent") or 0)
    if cap and spent >= cap:
        problems.append(f"The account spending limit (₹{money(cap):,.2f}) has been reached. "
                        "Raise or remove the limit, or no ads will run.")
    status = a.get("account_status")
    if status not in (1, None):
        why = {2: "disabled", 3: "unsettled (a payment failed)", 7: "under risk review", 8: "pending settlement",
               9: "in a grace period", 100: "pending closure", 101: "closed"}.get(status, f"status {status}")
        problems.append(f"The ad account is {why}.")
    return {"problems": problems, "balance": balance, "funding": shown, "spend_cap": money(cap),
            "amount_spent": money(spent), "cap_left": money(cap - spent) if cap else None, "url": BILLING_URL}


def can_edit():
    """Whether Meta lets this token create things in the ad account: a validate-only campaign (creates nothing)."""
    hit = _cache.get("access")
    if hit and time.time() - hit[0] < (6 * 3600 if hit[1].get("ok") else 600):  # once it works, it keeps working
        return hit[1]
    try:
        graph("POST", f"{ACT}/campaigns", data=form({
            "name": "access check", "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED",
            "special_ad_categories": [], "is_adset_budget_sharing_enabled": False,
            "execution_options": ["validate_only"]}), retries=0, quiet=True)
        result = {"ok": True}
    except MetaError as e:
        if e.status == 429:
            return hit[1] if hit else {"ok": None}
        result = {"ok": False, **e.info()}
    with _cache_lock:
        _cache["access"] = (time.time(), result)
    return result


def names():
    """The Page's and Instagram account's names (they hardly ever change)."""
    page = graph("GET", PAGE_ID, params={"fields": "name"}) if PAGE_ID else {}
    try:
        ig = graph("GET", IG_ID, params={"fields": "username,profile_picture_url"}) if IG_ID else {}
    except MetaError:
        ig = {}
    return page, ig


def account_info(fresh=False):
    def load():
        a = graph("GET", ACT, params={"fields": "name,currency,timezone_name,account_status,spend_cap,amount_spent,"
                                                 "funding_source_details,min_daily_budget,business"})
        page, ig = cached("names", 86400, names)
        return {"id": ACCOUNT, "name": a.get("name"), "currency": a.get("currency"), "tz": a.get("timezone_name"),
                "min_daily_budget": money(a.get("min_daily_budget")), "billing": billing(a),
                "page": {"id": PAGE_ID, "name": page.get("name")},
                "ig": {"id": IG_ID, "username": ig.get("username"), "picture": ig.get("profile_picture_url")},
                "business_id": (a.get("business") or {}).get("id")}
    return cached("account", 300, load, fresh)


# ---------- the template campaign (read-only) ----------

def template():
    """Settings of the known-good click-to-DM campaign, used as defaults for new ads."""
    def load():
        out = {"daily_budget": 500, "caption": "", "headline": "", "greeting": "Hi! Please let us know how we can help you.",
               "questions": ["I'm interested! Send me the workshop link", "I'm interested, how do I join?"],
               "welcome_json": None, "age_min": 18, "age_max": 65, "advantage_audience": True, "countries": ["IN"],
               "value_rule_set_id": None}
        try:
            adsets = graph("GET", f"{TEMPLATE_CAMPAIGN}/adsets", params={
                "fields": "daily_budget,targeting,optimization_goal,destination_type,value_rule_set_id,"
                          "value_rules_applied"})["data"]
            if adsets:
                s, t = adsets[0], adsets[0].get("targeting") or {}
                out["daily_budget"] = money(s.get("daily_budget")) or out["daily_budget"]
                if s.get("value_rules_applied"):
                    out["value_rule_set_id"] = s.get("value_rule_set_id")
                out["age_min"], out["age_max"] = t.get("age_min", 18), t.get("age_max", 65)
                out["countries"] = (t.get("geo_locations") or {}).get("countries") or ["IN"]
                out["advantage_audience"] = bool((t.get("targeting_automation") or {}).get("advantage_audience"))
            ads = graph("GET", f"{TEMPLATE_CAMPAIGN}/ads", params={"fields": "creative{object_story_spec,body,title}"})["data"]
            if ads:
                cr = ads[0].get("creative") or {}
                story = cr.get("object_story_spec") or {}
                spec = story.get("video_data") or story.get("link_data") or {}
                out["caption"], out["headline"] = cr.get("body") or "", cr.get("title") or ""
                if spec.get("page_welcome_message"):
                    w = json.loads(spec["page_welcome_message"])
                    msg = ((w.get("text_format") or {}).get("message") or {})
                    out["welcome_json"] = w
                    out["greeting"] = msg.get("text") or out["greeting"]
                    # questions with a canned answer were for that one workshop; the rest go to the bot as DMs
                    qs = [q["title"] for q in msg.get("ice_breakers") or [] if q.get("title") and not q.get("response")]
                    out["questions"] = qs or out["questions"]
        except MetaError:
            log.exception("couldn't read the template campaign")
            out["partial"] = True
        return out
    t = cached("template", 3600, load)
    if t.get("partial"):
        forget("template")  # Meta was busy: these fallbacks are for this once, ask again next time
    return t


# ---------- ads list ----------

CAMPAIGN_FIELDS = ("name,status,effective_status,configured_status,objective,daily_budget,lifetime_budget,"
                   "budget_remaining,start_time,stop_time,created_time,issues_info")
ADSET_FIELDS = ("name,status,effective_status,campaign_id,daily_budget,lifetime_budget,budget_remaining,start_time,"
                "end_time,optimization_goal,destination_type,targeting,issues_info,created_time,bid_strategy,"
                "value_rule_set_id,value_rules_applied")
AD_FIELDS = ("name,status,effective_status,campaign_id,adset_id,created_time,issues_info,ad_review_feedback,"
             "preview_shareable_link,creative.thumbnail_width(200).thumbnail_height(200)"
             "{thumbnail_url,image_url,object_type,body,title,instagram_permalink_url}")
GONE = {"DELETED", "ARCHIVED"}
RESULT_LABELS = {
    DM: "DMs started", "onsite_conversion.total_messaging_connection": "Messaging contacts",
    "link_click": "Link clicks", "landing_page_view": "Page views", "post_engagement": "Engagements",
    "video_view": "3-sec video views", "video_thruplay_watched_actions": "ThruPlays", "thruplay": "ThruPlays",
    "reach": "People reached", "impressions": "Impressions", "page_engagement": "Engagements",
    "like": "Page likes", "post_reaction": "Reactions", "lead": "Leads",
}


def result_of(row):
    """(count, label) of an insights row's 'results' (whatever the ad was optimised for)."""
    for r in row.get("results") or []:
        ind = r.get("indicator") or ""
        total = sum(float(v.get("value") or 0) for v in r.get("values") or [])
        key = ind.split(":", 1)[-1]
        return total, RESULT_LABELS.get(key, RESULT_LABELS.get(ind, key.replace("_", " ").replace(".", " ")))
    return 0.0, None


def acts(row, key="actions"):
    return {a["action_type"]: float(a.get("value") or 0) for a in row.get(key) or []}


def first(row, key):
    v = row.get(key) or []
    return float(v[0].get("value") or 0) if v else None


def audience_text(t):
    """A targeting spec in a few plain words."""
    g = t.get("geo_locations") or {}
    places = [c.get("name") or c.get("key") for c in g.get("cities") or []] + \
             [r.get("name") or r.get("key") for r in g.get("regions") or []] + \
             [{"IN": "India"}.get(c, c) for c in g.get("countries") or []]
    sex = {(1,): "men", (2,): "women"}.get(tuple(t.get("genders") or []), "everyone")
    lo, hi = t.get("age_range") or (t.get("age_min", 18), t.get("age_max", 65))
    interests = [i.get("name") for f in t.get("flexible_spec") or [] for i in f.get("interests") or []]
    where = t.get("publisher_platforms")
    km = {c.get("radius") for c in g.get("cities") or []}
    where_text = ", ".join(p for p in places if p) or "anywhere"
    if len(km) == 1 and None not in km:
        where_text += f" +{km.pop()} km"
    bits = [where_text, f"{lo}–{'65+' if hi == 65 else hi}", sex]
    if interests:
        bits.append("into " + ", ".join(interests[:3]) + (" …" if len(interests) > 3 else ""))
    if (t.get("targeting_automation") or {}).get("advantage_audience"):
        bits.append("+ similar people")
    names = {"instagram": "Instagram", "facebook": "Facebook", "messenger": "Messenger", "threads": "Threads",
             "audience_network": "other apps"}
    apps = [names.get(w, w) for w in sorted(where or [], key=lambda w: list(names).index(w) if w in names else 9)]
    bits.append(" + ".join(apps) + (" only" if len(apps) == 1 else "") if apps else "automatic placements")
    return " · ".join(bits)


def placements_from(t):
    """A targeting spec's placements in the page's shape (which previews an existing ad set can show)."""
    plats = t.get("publisher_platforms")
    if not plats:
        return {"auto": True}
    return {"auto": False, **{k: (t.get(f"{k}_positions") or POSITIONS[k]) if k in plats else [] for k in POSITIONS}}


def tree(fresh=False):
    """Campaigns → ad sets → ads, with lifetime spend and results on each."""
    def load():
        lifetime = lambda level: lambda: get_all(f"{ACT}/insights", {
            "level": level, "date_preset": "maximum",
            "fields": f"{level}_id,adset_id,spend,impressions,results,actions" if level == "ad" else
            f"{level}_id,spend,impressions,results,actions"})
        # the whole tree in one nested call: this app is on Meta's development tier, where every ads call counts
        camps, per_camp, per_ad = parallel(
            lambda: get_all(f"{ACT}/campaigns", {"fields": f"{CAMPAIGN_FIELDS},adsets.limit(100){{{ADSET_FIELDS},"
                                                           f"ads.limit(100){{{AD_FIELDS}}}}}"}, limit=50),
            lifetime("campaign"), lifetime("ad"))
        adsets = [s for c in camps for s in (c.get("adsets") or {}).get("data", [])]
        ads = [a for s in adsets for a in (s.get("ads") or {}).get("data", [])]
        stats = {}
        for level, rows in (("campaign", per_camp), ("ad", per_ad)):
            for r in rows:
                n, label = result_of(r)
                one = {"spend": float(r.get("spend") or 0), "results": n, "result_label": label,
                       "dms": acts(r).get(DM, 0), "impressions": int(r.get("impressions") or 0)}
                stats[r[f"{level}_id"]] = one
                if level == "ad":  # an ad set's totals are its ads' totals added up
                    s = stats.setdefault(r.get("adset_id"), {"spend": 0.0, "results": 0.0, "result_label": label,
                                                             "dms": 0.0, "impressions": 0})
                    for k in ("spend", "results", "dms", "impressions"):
                        s[k] += one[k]
        by_adset = {}
        for a in ads:
            if a.get("effective_status") in GONE:
                continue
            cr = a.get("creative") or {}
            by_adset.setdefault(a.get("adset_id"), []).append({
                "id": a["id"], "name": a.get("name"), "status": a.get("status"),
                "effective_status": a.get("effective_status"), "issues": a.get("issues_info") or [],
                "review": a.get("ad_review_feedback") or {}, "preview": a.get("preview_shareable_link"),
                "thumb": cr.get("thumbnail_url") or cr.get("image_url"), "type": cr.get("object_type"),
                "caption": cr.get("body"), "permalink": cr.get("instagram_permalink_url"),
                "created": a.get("created_time"), "stats": stats.get(a["id"])})
        by_camp = {}
        for s in adsets:
            if s.get("effective_status") in GONE:
                continue
            by_camp.setdefault(s.get("campaign_id"), []).append({
                "id": s["id"], "name": s.get("name"), "status": s.get("status"),
                "effective_status": s.get("effective_status"), "issues": s.get("issues_info") or [],
                "daily_budget": money(s.get("daily_budget")), "lifetime_budget": money(s.get("lifetime_budget")),
                "budget_remaining": money(s.get("budget_remaining")),
                "start": s.get("start_time"), "end": s.get("end_time"), "goal": s.get("optimization_goal"),
                "destination": s.get("destination_type"), "audience": audience_text(s.get("targeting") or {}),
                "value_rule_set": s.get("value_rule_set_id") if s.get("value_rules_applied") else None,
                "placements": placements_from(s.get("targeting") or {}),
                "bid_strategy": s.get("bid_strategy"),
                "ads": sorted(by_adset.get(s["id"], []), key=lambda x: x["created"] or "", reverse=True),
                "created": s.get("created_time"), "stats": stats.get(s["id"])})
        out = []
        for c in camps:
            if c.get("effective_status") in GONE:
                continue
            out.append({
                "id": c["id"], "name": c.get("name"), "status": c.get("status"),
                "effective_status": c.get("effective_status"), "objective": c.get("objective"),
                "issues": c.get("issues_info") or [], "daily_budget": money(c.get("daily_budget")),
                "lifetime_budget": money(c.get("lifetime_budget")), "start": c.get("start_time"),
                "end": c.get("stop_time"), "created": c.get("created_time"), "template": c["id"] == TEMPLATE_CAMPAIGN,
                "adsets": sorted(by_camp.get(c["id"], []), key=lambda x: x["created"] or "", reverse=True),
                "stats": stats.get(c["id"])})
        out.sort(key=lambda c: c["created"] or "", reverse=True)
        return {"campaigns": out, "at": time.time()}
    return cached("tree", 300, load, fresh)


def ours(obj_id):
    """Ids come from the page: only digits, and only objects that belong to our ad account can be changed."""
    if not re.fullmatch(r"\d{6,25}", obj_id or ""):
        raise HTTPException(400, {"message": "That isn't a valid id."})
    try:
        owner = cached(f"owner:{obj_id}", 86400,
                       lambda: graph("GET", obj_id, params={"fields": "account_id"}, quiet=True).get("account_id"))
    except MetaError as e:
        if e.code != 100:  # 100: it has no ad account at all (a page, a post, ...)
            fail(e)
        owner = None
    if owner != ACCOUNT:
        raise HTTPException(403, {"message": "That isn't in your ad account."})


def ist(s):
    """'2026-10-13T10:00' typed on the page (always IST) -> aware datetime."""
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, {"message": f"Couldn't read the date '{s}'."})
    return d if d.tzinfo else d.replace(tzinfo=IST)


def meta_time(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------- uploads (photos/videos go to Meta's ad library as soon as they're dropped) ----------

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".webm", ".3gp", ".mpeg", ".mpg"}
_jobs = {}  # upload and create jobs by id (in memory: a restart forgets them, the media stays in Meta's library)


def new_job(kind, **kw):
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"id": jid, "kind": kind, "at": time.time(), **kw}
    return _jobs[jid]


def send_image(job, path):
    job.update(phase="uploading", pct=10)
    with open(path, "rb") as f:
        r = graph("POST", f"{ACT}/adimages", files={"filename": (job["name"], f)}, timeout=300, retries=0)
    img = next(iter((r.get("images") or {}).values()), {})
    if not img.get("hash"):
        raise MetaError("Meta didn't return the photo after uploading it.")
    try:
        size = graph("GET", f"{ACT}/adimages", params={"hashes": json.dumps([img["hash"]]),
                                                       "fields": "width,height"}).get("data") or [{}]
        job.update(width=size[0].get("width"), height=size[0].get("height"))
    except MetaError:
        pass
    job.update(phase="ready", pct=100, hash=img["hash"], thumb=img.get("url"), thumbs=[])


def send_video(job, path):
    """Resumable upload in the chunks Meta asks for, then wait until Meta has processed it."""
    size = path.stat().st_size
    job.update(phase="uploading", pct=0)
    start = graph("POST", f"{ACT}/advideos", data={"upload_phase": "start", "file_size": str(size)}, retries=0)
    session, job["video_id"] = start["upload_session_id"], start["video_id"]
    lo, hi = int(start["start_offset"]), int(start["end_offset"])
    with open(path, "rb") as f:
        while lo < hi:
            f.seek(lo)
            chunk = f.read(hi - lo)
            for attempt in range(3):  # a chunk can be sent again safely: Meta tracks the offsets
                try:
                    r = graph("POST", f"{ACT}/advideos", data={"upload_phase": "transfer", "upload_session_id": session,
                                                               "start_offset": str(lo)},
                              files={"video_file_chunk": ("chunk", chunk)}, timeout=600, retries=0)
                    break
                except MetaError:
                    if attempt == 2:
                        raise
                    time.sleep(3)
            lo, hi = int(r["start_offset"]), int(r["end_offset"])
            job["pct"] = round(100 * lo / size) if size else 100
    graph("POST", f"{ACT}/advideos", data={"upload_phase": "finish", "upload_session_id": session,
                                           "title": job["name"]}, retries=0)
    job.update(phase="processing", pct=0)
    for _ in range(600):  # up to ~30 min
        v = graph("GET", job["video_id"], params={"fields": "status,length"})
        st = v.get("status") or {}
        job["pct"] = int(st.get("processing_progress") or job["pct"])
        if st.get("video_status") == "ready":
            job["length"] = v.get("length")
            break
        if st.get("video_status") == "error":
            raise MetaError("Meta couldn't process this video. Try exporting it again as MP4 (H.264).")
        time.sleep(3)
    else:
        raise MetaError("Meta is still processing this video after 30 minutes. Try a smaller file.")
    thumbs = []
    for i in range(20):  # cover pictures show up a little after 'ready', one by one
        thumbs = graph("GET", f"{job['video_id']}/thumbnails",
                       params={"fields": "uri,is_preferred,width,height"}).get("data") or []
        if len(thumbs) >= 5 or thumbs and i >= 4:
            break
        time.sleep(3)
    if not thumbs:
        raise MetaError("Meta processed the video but made no thumbnail. Try uploading it again.")
    best = next((t for t in thumbs if t.get("is_preferred")), thumbs[0])
    job.update(phase="ready", pct=100, thumb=best["uri"], thumbs=[t["uri"] for t in thumbs[:10]],
               width=best.get("width"), height=best.get("height"))


def send_to_meta(job, path):
    try:
        (send_video if job["type"] == "video" else send_image)(job, path)
        log.info("uploaded %s %s -> %s", job["type"], job["name"], job.get("video_id") or job.get("hash"))
    except MetaError as e:
        job.update(phase="error", error=e.info())
    except Exception as e:
        log.exception("upload of %s failed", job["name"])
        job.update(phase="error", error={"message": f"Upload failed: {e}"})
    finally:
        path.unlink(missing_ok=True)


# ---------- business assets: Pages, their Instagram accounts and WhatsApp numbers ----------

def pages():
    """Pages this ad account can advertise for, each with its Instagram account and WhatsApp number (if Meta
    shows one). The main Page from ads/.env comes first."""
    def load():
        fields = "id,name,picture{url},instagram_business_account{id,username,profile_picture_url}"
        try:
            rows = get_all(f"{ACT}/promote_pages", {"fields": fields + ",whatsapp_number"})
        except MetaError as e:
            if e.code != 100:
                raise
            rows = get_all(f"{ACT}/promote_pages", {"fields": fields})  # this token may not see WhatsApp numbers
        out = []
        for p in rows:
            ig = p.get("instagram_business_account") or {}
            out.append({"id": p["id"], "name": p.get("name"),
                        "picture": ((p.get("picture") or {}).get("data") or {}).get("url"),
                        "ig": {"id": ig["id"], "username": ig.get("username"), "picture": ig.get("profile_picture_url")}
                        if ig.get("id") else None,
                        "whatsapp": p.get("whatsapp_number")})
        out.sort(key=lambda p: p["id"] != PAGE_ID)
        return out
    return cached("pages", 3600, load)


def main_page():
    """The Page and Instagram account from ads/.env, for when Meta can't be asked right now."""
    acc = (_cache.get("account") or (0, {}))[1]
    return {"id": PAGE_ID, "name": (acc.get("page") or {}).get("name") or "your Page", "picture": None,
            "ig": {"id": IG_ID, "username": (acc.get("ig") or {}).get("username"), "picture": None} if IG_ID else None,
            "whatsapp": None}


def page_of(page_id):
    try:
        found = next((p for p in pages() if p["id"] == page_id), None)
    except MetaError as e:
        if page_id != PAGE_ID:
            fail(e)
        found = main_page()
    if not found:
        raise HTTPException(400, {"message": "That Facebook Page isn't one this ad account can advertise for."})
    return found


# ---------- value rules: bid more or less for some placements / ages / genders / places / devices ----------

VR_FIELDS = ("name,status,rules{name,adjust_sign,adjust_value,status,"
             "criterias{criteria_type,criteria_values,criteria_value_types}}")
VR_VALUES = {  # what each kind of rule can be about (Ads Manager stores placements in lower case, e.g. ig_reels)
    "PLACEMENT": ["ig_feed", "ig_stories", "ig_reels", "fb_feed", "fb_stories", "fb_reels",
                  "fb_marketplace", "fb_search", "fb_video", "audience_network"],
    "AGE": ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"],
    "GENDER": ["MALE", "FEMALE"], "OS_TYPE": ["ANDROID", "IOS"], "DEVICE_PLATFORM": ["MOBILE", "DESKTOP"],
    "LOCATION": None,  # place keys from the location search
}
VR_LOCATION = {"country": "LOCATION_COUNTRY", "region": "LOCATION_REGION", "city": "LOCATION_CITY"}
MAX_RULE_SETS = 6  # Meta's limit per ad account


def rule_sets(fresh=False):
    def load():
        rows = get_all(f"{ACT}/value_rule_set", {"fields": VR_FIELDS}, limit=25)
        return [{"id": r["id"], "name": r.get("name"), "status": r.get("status"),
                 "rules": [{"name": x.get("name"), "sign": x.get("adjust_sign"), "value": x.get("adjust_value"),
                            "criteria": [{"type": c.get("criteria_type"), "values": c.get("criteria_values") or [],
                                          "value_types": c.get("criteria_value_types") or []}
                                         for c in (x.get("criterias") or {}).get("data", [])]}
                           for x in (r.get("rules") or {}).get("data", [])]}
                for r in rows if r.get("status") not in ("DELETED", "ARCHIVED")]
    return cached("rulesets", 300, load, fresh)


def make_rule_set(body):
    """{name, rules: [{type, values | places, sign: INCREASE|DECREASE, pct, label}]} -> a new rule set in Meta."""
    bad = lambda m: HTTPException(400, {"message": m})
    name = (body.get("name") or "").strip()[:100]
    if not name:
        raise bad("Give the rule set a name.")
    rules = []
    for r in body.get("rules") or []:
        kind = r.get("type")
        if kind not in VR_VALUES:
            raise bad("Pick what each rule is about.")
        if kind == "LOCATION":
            places = [p for p in r.get("places") or [] if p.get("type") in VR_LOCATION and p.get("key")]
            values, types = [str(p["key"]) for p in places], [VR_LOCATION[p["type"]] for p in places]
        else:
            values = [v for v in r.get("values") or [] if v in VR_VALUES[kind]]
            types = ["NONE"] * len(values)
        if not values:
            raise bad("Each rule needs at least one choice ticked.")
        sign = "DECREASE" if r.get("sign") == "DECREASE" else "INCREASE"
        try:
            pct = int(r.get("pct"))
        except (TypeError, ValueError):
            raise bad("Each rule needs a percentage.")
        if not (1 <= pct <= (90 if sign == "DECREASE" else 1000)):
            raise bad("Bid more: 1–1000%. Bid less: 1–90%.")
        label = (r.get("label") or f"{kind.title()} ({'+' if sign == 'INCREASE' else '-'}{pct}%)").strip()[:100]
        rules.append({"name": label, "adjust_sign": sign, "adjust_value": pct,
                      "criterias": [{"criteria_type": kind, "operator": "CONTAINS", "criteria_values": values,
                                     "criteria_value_types": types}]})
    if not rules:
        raise bad("Add at least one rule.")
    if len(rules) > 10:
        raise bad("Meta allows up to 10 rules in a set.")
    if len(rule_sets(fresh=True)) >= MAX_RULE_SETS:
        raise bad(f"Meta allows {MAX_RULE_SETS} value rule sets per ad account, and you have that many. "
                  "Delete one in Ads Manager first, or use one you already have.")
    r = graph("POST", f"{ACT}/value_rule_set", data=form({"name": name, "rules": rules}), retries=0)
    log.info("created value rule set %s (%s): %s", r.get("id"), name, rules)
    forget("rulesets")
    return r.get("id")


# ---------- creating an ad: campaign → ad set → creative(s) → ad(s), all PAUSED ----------

OBJECTIVES = {"engagement": "OUTCOME_ENGAGEMENT", "traffic": "OUTCOME_TRAFFIC"}
DESTINATIONS = {  # message app -> button, app_destination, the link Meta itself puts on such creatives
    "instagram": ("INSTAGRAM_MESSAGE", "INSTAGRAM_DIRECT", "https://www.instagram.com/"),
    "messenger": ("MESSAGE_PAGE", "MESSENGER", "https://fb.com/messenger_doc/"),
    "whatsapp": ("WHATSAPP_MESSAGE", "WHATSAPP", "https://api.whatsapp.com/send"),
}
DEST_TYPES = {("instagram",): "INSTAGRAM_DIRECT", ("messenger",): "MESSENGER", ("whatsapp",): "WHATSAPP",
              ("instagram", "messenger"): "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
              ("messenger", "whatsapp"): "MESSAGING_MESSENGER_WHATSAPP",
              ("instagram", "whatsapp"): "MESSAGING_INSTAGRAM_DIRECT_WHATSAPP",
              ("instagram", "messenger", "whatsapp"): "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP"}
WEB_BUTTONS = ["LEARN_MORE", "BOOK_NOW", "SIGN_UP", "CONTACT_US", "APPLY_NOW", "GET_OFFER", "SHOP_NOW", "SUBSCRIBE"]
LOCATIONS = {
    # conversion location -> performance goals per campaign objective (first is the default) and button choices
    "messages": {"goals": {"engagement": ["CONVERSATIONS", "LINK_CLICKS"],
                           "traffic": ["CONVERSATIONS", "LINK_CLICKS"]}, "buttons": None},
    "on_ad": {"goals": {"engagement": ["POST_ENGAGEMENT", "THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS",
                                       "REACH"]}, "buttons": [""]},
    # Meta refuses website and profile visits under Engagement ("Performance goal isn't available")
    "website": {"goals": {"traffic": ["LINK_CLICKS", "LANDING_PAGE_VIEWS", "REACH"]},  # IMPRESSIONS is retired
                "buttons": WEB_BUTTONS},
    "profile": {"goals": {"traffic": ["PROFILE_VISIT"]}, "buttons": ["VIEW_INSTAGRAM_PROFILE"]},
    "calls": {"goals": {"engagement": ["QUALITY_CALL"], "traffic": ["QUALITY_CALL"]}, "buttons": ["CALL_NOW"]},
}
VIDEO_GOALS = {"THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"}
# goals Meta takes a cost per result goal (COST_CAP) with (checked 2026-10-12); it refuses one on CONVERSATIONS,
# PROFILE_VISIT and QUALITY_CALL
COST_CAP_GOALS = {"LINK_CLICKS", "LANDING_PAGE_VIEWS", "POST_ENGAGEMENT", "THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"}
IG_LINK = DESTINATIONS["instagram"][2]  # what Meta itself puts on click-to-DM creatives (see the template)
POSITIONS = {  # publisher platform -> the positions Meta takes (Instagram Explore and Facebook video feeds are retired)
    "facebook": ["feed", "profile_feed", "marketplace", "right_hand_column", "story", "facebook_reels",
                 "facebook_reels_overlay", "instream_video", "search", "notification"],
    "instagram": ["stream", "profile_feed", "story", "reels", "profile_reels", "ig_search"],
    "messenger": ["story"],
    "threads": ["threads_stream"],
    "audience_network": ["classic", "rewarded_video"],
}
FB_NEEDS_FEED = {"marketplace", "search", "profile_feed", "notification"}


def welcome_message(greeting, questions):
    """The chat welcome screen (greeting + tap-to-send questions, each with an optional automatic answer), built
    on the template's own JSON so the shape is one Meta already accepted. A tapped question also arrives as a
    normal DM, so the bot sees it either way."""
    base = json.loads(json.dumps(template().get("welcome_json") or {
        "type": "VISUAL_EDITOR", "version": 2, "landing_screen_type": "welcome_message", "media_type": "text",
        "user_edit": True, "surface": "visual_editor_new"}))
    base.setdefault("text_format", {})["customer_action_type"] = "ice_breakers"
    base["text_format"]["message"] = {
        "ice_breakers": [{"title": q["q"], **({"response": q["a"]} if q["a"] else {})} for q in questions],
        "quick_replies": [], "text": greeting}
    base["media_type"] = "text"
    base["landing_screen_type"] = "welcome_message"
    base.pop("template_id", None)  # a saved template of the old ad would win over these questions
    return json.dumps(base)


def tall_media(m):
    """Taller than 4:5 (e.g. a 9:16 reel), which Instagram carousels don't take."""
    w, h = m.get("width"), m.get("height")
    return bool(w and h and h / w > 1.3)


def placements_of(s):
    """{auto, devices, facebook: [...], instagram: [...], ...} from the page's choices."""
    p = s.get("placements") or {}
    out = {"auto": bool(p.get("auto")), "devices": p.get("devices") if p.get("devices") in ("mobile", "desktop")
           else "all"}
    for plat, allowed in POSITIONS.items():
        out[plat] = [x for x in allowed if x in (p.get(plat) or [])]
    return out


def placement_problem(p):
    """Meta's rules about which placements need which others, in plain words (None if fine)."""
    if p["auto"]:
        return None
    fb, ig = p["facebook"], p["instagram"]
    if not any(p[k] for k in POSITIONS):
        return "Pick at least one place to show the ad."
    if p["audience_network"] and not any(p[k] for k in POSITIONS if k != "audience_network"):
        return "Audience Network can't be the only placement. Tick a Facebook or Instagram place too."
    if FB_NEEDS_FEED & set(fb) and "feed" not in fb:
        return "Facebook Marketplace, search results, profile feed and notifications only run with Facebook Feed. " \
               "Tick Facebook Feed too."
    if "story" in fb and "feed" not in fb and "story" not in ig:
        return "Facebook Stories needs Facebook Feed or Instagram Stories ticked too."
    if p["messenger"] and "feed" not in fb and "story" not in ig:
        return "Messenger Stories needs Facebook Feed or Instagram Stories ticked too."
    if p["threads"] and "stream" not in ig:
        return "Threads only runs together with the Instagram feed. Tick Instagram feed too."
    return None


def phone_number(raw):
    digits = re.sub(r"[\s\-()]", "", raw or "")
    if re.fullmatch(r"\d{10}", digits):
        digits = "+91" + digits
    elif re.fullmatch(r"\d{11,15}", digits):
        digits = "+" + digits
    return digits if re.fullmatch(r"\+\d{11,15}", digits) else None


def check_creative(s, preview=False):
    """The part of the form people see (and where they go), checked before any call to Meta. With preview=True
    it's lenient about what a preview doesn't need (a caption, a name)."""
    bad = lambda m: HTTPException(400, {"message": m})
    objective, loc = s.get("objective"), s.get("location")
    if objective not in OBJECTIVES:
        raise bad("Pick what the ad is for.")
    goals = (LOCATIONS.get(loc) or {}).get("goals", {}).get(objective)
    if not goals:
        raise bad("Pick where people go (the conversion location).")
    perf = s.get("perf") or goals[0]
    if perf not in goals:
        raise bad("That performance goal doesn't fit this conversion location.")
    page = page_of(s.get("page_id") or PAGE_ID)
    ig = page.get("ig")
    dests = [d for d in DESTINATIONS if d in (s.get("destinations") or [])] if loc == "messages" else []
    if loc == "messages" and not dests:
        raise bad("Tick at least one message destination: Instagram, Messenger or WhatsApp.")
    if ("instagram" in dests or loc == "profile") and not ig:
        raise bad(f"The Page “{page['name']}” has no Instagram account linked, so it can't get Instagram DMs or "
                  "profile visits. Pick another Page, or link the Instagram account to it in Meta first.")
    media = []
    for jid in s.get("media") or []:
        j = _jobs.get(jid)
        if not j or j["kind"] != "upload":
            raise bad("One of the files was lost (the app restarted?). Remove it and add it again.")
        if j["phase"] != "ready":
            raise bad(f"{j['name']} is still uploading. Wait until it says Ready.")
        media.append(dict(j))
    if not media:
        raise bad("Add at least one photo or video.")
    carousel = bool(s.get("carousel")) and len(media) > 1
    if carousel and len(media) > 10:
        raise bad("A carousel can have at most 10 photos/videos.")
    tall = [m["name"] for m in media if tall_media(m)] if carousel else []
    if tall:
        # Meta rejects these with an unhelpful "Instagram format validation error"
        raise bad(f"Instagram carousels only take square or 4:5 photos/videos, and {', '.join(tall)} "
                  f"{'is' if len(tall) == 1 else 'are'} vertical (like a reel). Untick 'carousel' so each one "
                  "becomes its own ad, or use a square / 4:5 version.")
    if perf in VIDEO_GOALS and (carousel or any(m["type"] != "video" for m in media)):
        raise bad("Video goals (ThruPlay, 2-second plays) need videos only, one per ad (no photos, no carousel).")
    for m in media:
        thumb = (s.get("thumbs") or {}).get(m["id"])
        if thumb and thumb in (m.get("thumbs") or []):
            m["chosen_thumb"] = thumb
    caption = (s.get("caption") or "").strip()
    if not caption and not preview:
        raise bad("Write a caption.")
    primary = ("messenger" if "messenger" in dests else dests[0]) if dests else None
    buttons = LOCATIONS[loc]["buttons"]
    button = DESTINATIONS[primary][0] if primary else (s.get("button") or buttons[0])
    if buttons is not None and button not in buttons:
        raise bad("That button doesn't fit this conversion location.")
    link = (s.get("link") or "").strip()
    if loc == "website" and not re.match(r"https?://\S+\.\S+", link):
        raise bad("Paste the full link, starting with https:// (e.g. https://wa.me/91XXXXXXXXXX).")
    phone = phone_number(s.get("phone")) if loc == "calls" else None
    if loc == "calls" and not phone:
        raise bad("Type the phone number people should call, e.g. +91 98XXXXXXXX.")
    questions = []
    for x in s.get("questions") or []:
        q, a = (x.get("q"), x.get("a")) if isinstance(x, dict) else (x, "")
        q, a = (q or "").strip()[:80], (a or "").strip()[:300]
        if q:
            questions.append({"q": q, "a": a})
        elif a:
            raise bad("An answer needs its question: fill in the question next to it, or clear the answer.")
    return {**s, "objective": objective, "location": loc, "perf": perf, "page_id": page["id"],
            "page_name": page.get("name"), "ig_id": ig["id"] if ig else None,
            "ig_username": ig.get("username") if ig else None, "destinations": dests, "primary": primary,
            "media": media, "carousel": carousel, "caption": caption, "headline": (s.get("headline") or "").strip()[:255],
            "button": button, "link": link, "phone": phone, "questions": questions[:4],
            "greeting": (s.get("greeting") or "").strip()[:300]}


def check_spec(s):
    """Everything the page sent, checked before any call to Meta. Returns a cleaned copy."""
    bad = lambda m: HTTPException(400, {"message": m})
    c = check_creative(s)
    name = (s.get("name") or "").strip()
    if not name:
        raise bad("Give the ad a name (only you see it).")
    lifetime = s.get("budget_type") == "lifetime"
    try:
        budget = float(s.get("budget") or 0)
        cost_goal = float(s.get("cost_goal") or 0)
    except (TypeError, ValueError):
        raise bad("Budgets and the cost per result goal have to be numbers.")
    if cost_goal < 0:
        raise bad("The cost per result goal can't be below ₹0.")
    if cost_goal and c["perf"] not in COST_CAP_GOALS:
        raise bad("Meta doesn't take a cost per result goal with this performance goal. Leave it empty.")
    now = datetime.now(IST)
    start = ist(s["start"]) if s.get("start") else None
    end = ist(s["end"]) if s.get("end") else None
    if start and start < now - timedelta(minutes=5):
        raise bad("The start time is in the past. Pick a later time, or tick 'Start as soon as it's live'.")
    if end and end <= (start or now) + timedelta(hours=1):
        raise bad("The end has to be at least an hour after the start.")
    if lifetime and not end:
        raise bad("A total budget needs an end date.")
    days = max(1, round(((end - (start or now)).total_seconds()) / 86400)) if end else None
    min_daily = account_info().get("min_daily_budget") or 0
    if not lifetime and budget < min_daily:
        raise bad(f"Meta's minimum for this account is ₹{min_daily:,.0f} per day.")
    if lifetime and budget < min_daily * (days or 1):
        raise bad(f"For {days} days, Meta needs at least ₹{min_daily * days:,.0f} in total.")
    geo = s.get("geo") or []
    if not geo:
        raise bad("Add at least one location.")
    age_min, age_max = int(s.get("age_min") or 18), int(s.get("age_max") or 65)
    if not 18 <= age_min <= age_max <= 65:
        raise bad("Ages go from 18 to 65, and the minimum can't be above the maximum.")
    p = placements_of(s)
    problem = placement_problem(p)
    if problem:
        raise bad(problem)
    if not p["auto"]:
        if (p["instagram"] or p["threads"]) and not c["ig_id"]:
            raise bad("This Page has no Instagram account, so untick the Instagram and Threads places.")
        if c["destinations"] == ["instagram"] and not p["instagram"]:
            raise bad("Instagram DM ads need at least one Instagram place. For Facebook, add Messenger as a "
                      "message destination.")
        if c["location"] == "profile" and not p["instagram"]:
            raise bad("Instagram profile visits only make sense on Instagram. Tick at least one Instagram place.")
    vr = str(s.get("value_rule_set_id") or "")
    if vr:
        try:
            known = {r["id"]: r["name"] for r in rule_sets()}
        except MetaError:
            known = {vr: None}  # can't check right now; Meta will refuse it if it's wrong
        if vr not in known:
            raise bad("That value rule set doesn't exist any more. Pick another one.")
    return {**c, "name": name[:200], "lifetime": lifetime, "budget": budget, "start": start, "end": end,
            "days": days, "geo": geo, "age_min": age_min, "age_max": age_max, "placements": p,
            "value_rule_set_id": vr or None, "value_rule_name": known.get(vr) if vr else None,
            "cost_goal": cost_goal or None}


def targeting(s):
    geo = {"countries": [], "regions": [], "cities": []}
    for g in s["geo"]:
        if g.get("type") == "country":
            geo["countries"].append(g["key"])
        elif g.get("type") == "region":
            geo["regions"].append({"key": str(g["key"])})
        elif g.get("type") == "city":
            geo["cities"].append({"key": str(g["key"]), "radius": int(g.get("radius") or 25),
                                  "distance_unit": "kilometer"})
    geo = {k: v for k, v in geo.items() if v}
    geo["location_types"] = ["home", "recent"]
    t = {"geo_locations": geo, "age_min": s["age_min"], "age_max": s["age_max"]}
    if s.get("gender") in ("male", "female"):
        t["genders"] = [1 if s["gender"] == "male" else 2]
    interests = [{"id": str(i["id"]), "name": i.get("name")} for i in s.get("interests") or [] if i.get("id")]
    if interests:
        t["flexible_spec"] = [{"interests": interests}]
    p = s["placements"]
    if not p["auto"]:
        t["publisher_platforms"] = [k for k in POSITIONS if p[k]]
        for k in POSITIONS:
            if p[k]:
                t[f"{k}_positions"] = p[k]
        if p["devices"] != "all":
            t["device_platforms"] = [p["devices"]]
    if s.get("advantage", True):
        # Advantage+ audience: location and a minimum age up to 25 stay strict; the rest become suggestions.
        t["targeting_automation"] = {"advantage_audience": 1}
        if (s["age_min"], s["age_max"]) != (18, 65):
            t["age_range"] = [s["age_min"], s["age_max"]]
            t["age_min"], t["age_max"] = min(s["age_min"], 25), 65
    else:
        t["targeting_automation"] = {"advantage_audience": 0}
    return t


def shown_targeting(s):
    """targeting(s) with the place names from the page, for the summary (Meta only needs the keys)."""
    t = targeting(s)
    names = {str(g["key"]): g.get("name") for g in s["geo"]}
    for kind in ("cities", "regions"):
        for x in t["geo_locations"].get(kind, []):
            x["name"] = names.get(x["key"])
    return t


def destination_type(s):
    if s["location"] == "messages":
        return DEST_TYPES[tuple(s["destinations"])]
    if s["location"] == "on_ad":
        return "ON_VIDEO" if s["perf"] in VIDEO_GOALS else "ON_POST"
    return {"website": "WEBSITE", "profile": "INSTAGRAM_PROFILE", "calls": "PHONE_CALL"}[s["location"]]


def profile_url(s):
    return f"https://www.instagram.com/{s['ig_username']}" if s.get("ig_username") else IG_LINK


def cta(s):
    loc = s["location"]
    if loc == "messages":
        button, app, _ = DESTINATIONS[s["primary"]]
        return {"type": button, "value": {"app_destination": app}}
    if loc == "website":
        return {"type": s["button"], "value": {"link": s["link"]}}
    if loc == "calls":
        return {"type": "CALL_NOW", "value": {"link": "tel:" + s["phone"]}}
    if loc == "profile":
        return {"type": "VIEW_INSTAGRAM_PROFILE", "value": {"link": profile_url(s)}}
    return None


def creative_spec(s, items, label):
    loc = s["location"]
    story = {"page_id": s["page_id"], "instagram_user_id": s["ig_id"]}
    welcome = welcome_message(s["greeting"] or template()["greeting"], s["questions"]) \
        if loc == "messages" and s["questions"] else None
    link = {"messages": DESTINATIONS[s["primary"]][2] if s["primary"] else IG_LINK, "website": s["link"],
            "calls": f"https://www.facebook.com/{s['page_id']}", "profile": profile_url(s)}.get(loc, IG_LINK)
    thumb = lambda m: m.get("chosen_thumb") or m.get("thumb")
    if len(items) > 1:
        cards = [{"link": link, "name": s["headline"], "call_to_action": cta(s),
                  **({"video_id": m["video_id"], "picture": thumb(m)} if m["type"] == "video" else
                     {"image_hash": m["hash"]})} for m in items]
        story["link_data"] = {"link": link, "message": s["caption"], "child_attachments": cards,
                              "multi_share_optimized": False, "multi_share_end_card": False,
                              "call_to_action": cta(s), "page_welcome_message": welcome}
    elif items[0]["type"] == "video":
        m = items[0]
        story["video_data"] = {"video_id": m["video_id"], "image_url": thumb(m), "message": s["caption"],
                               "title": s["headline"], "call_to_action": cta(s), "page_welcome_message": welcome}
    elif loc == "on_ad":
        story["photo_data"] = {"image_hash": items[0]["hash"], "caption": s["caption"]}
    else:
        story["link_data"] = {"image_hash": items[0]["hash"], "link": link, "message": s["caption"],
                              "name": s["headline"], "call_to_action": cta(s), "page_welcome_message": welcome}
    spec = {"name": label[:200], "object_story_spec": prune(story)}
    if len(s["destinations"]) > 1:
        # several message apps: Meta sends each person to the one they're most likely to answer in
        spec["asset_feed_spec"] = {"optimization_type": "DOF_MESSAGING_DESTINATION", "call_to_actions": [
            {"type": DESTINATIONS[d][0], "value": {"app_destination": DESTINATIONS[d][1], "link": DESTINATIONS[d][2]}}
            for d in s["destinations"]]}
    return spec


def cleanup(made):
    """Delete what a failed create left behind, newest first. Returns what happened to each."""
    out = []
    for kind, oid in reversed(made):
        try:
            graph("DELETE", oid, retries=1)
            out.append({"kind": kind, "id": oid, "deleted": True})
        except MetaError as e:
            out.append({"kind": kind, "id": oid, "deleted": False, "why": e.message})
    log.info("cleanup after failed create: %s", out)
    return out


def run_create(job, s):
    made = []
    steps = job["steps"]

    def step(text):
        if steps and steps[-1]["state"] == "running":
            steps[-1]["state"] = "done"
        steps.append({"text": text, "state": "running"})

    try:
        step("Creating the campaign")
        camp = graph("POST", f"{ACT}/campaigns", retries=1, data=form({
            "name": s["name"], "objective": OBJECTIVES[s["objective"]], "status": "PAUSED", "buying_type": "AUCTION",
            "special_ad_categories": [], "is_adset_budget_sharing_enabled": False}))["id"]
        made.append(("campaign", camp))

        step("Creating the ad set (budget, dates, audience, placements)")
        budget = int(round(s["budget"] * 100))
        website_traffic = s["objective"] == "traffic" and s["location"] == "website"
        adset = graph("POST", f"{ACT}/adsets", retries=1, data=form(prune({
            "name": f"{s['name']} · audience", "campaign_id": camp, "status": "PAUSED",
            "billing_event": "IMPRESSIONS", "optimization_goal": s["perf"], "destination_type": destination_type(s),
            "bid_strategy": "COST_CAP" if s["cost_goal"] else "LOWEST_COST_WITHOUT_CAP",
            "bid_amount": int(round(s["cost_goal"] * 100)) if s["cost_goal"] else None,
            "promoted_object": None if website_traffic else {"page_id": s["page_id"]},
            "lifetime_budget" if s["lifetime"] else "daily_budget": budget,
            "start_time": meta_time(s["start"]) if s["start"] else None,
            "end_time": meta_time(s["end"]) if s["end"] else None,
            "value_rule_set_id": s["value_rule_set_id"],
            "value_rules_applied": True if s["value_rule_set_id"] else None,
            "targeting": targeting(s)})))["id"]
        made.append(("ad set", adset))

        groups = [s["media"]] if s["carousel"] else [[m] for m in s["media"]]
        many = len(groups) > 1
        ads = []
        for i, items in enumerate(groups, 1):
            label = f"{s['name']} · {Path(items[0]['name']).stem}" if many else s["name"]
            step(f"Creating ad {i} of {len(groups)}" if many else "Creating the ad")
            cr = graph("POST", f"{ACT}/adcreatives", retries=1, data=form(creative_spec(s, items, label)))["id"]
            made.append(("creative", cr))
            ad = graph("POST", f"{ACT}/ads", retries=1, data=form({
                "name": label, "adset_id": adset, "creative": {"creative_id": cr}, "status": "PAUSED"}))["id"]
            made.append(("ad", ad))
            ads.append({"id": ad, "name": label, "creative_id": cr, "thumb": items[0].get("chosen_thumb") or
                        items[0].get("thumb"), "files": [m["name"] for m in items]})

        step("Getting previews")
        for a in ads:
            try:
                a["preview"] = graph("GET", a["id"], params={"fields": "preview_shareable_link"}).get(
                    "preview_shareable_link")
            except MetaError:
                a["preview"] = None
        steps[-1]["state"] = "done"
        daily = None if s["lifetime"] else s["budget"]
        job.update(state="done", result={
            "campaign_id": camp, "adset_id": adset, "ads": ads, "name": s["name"], "objective": s["objective"],
            "location": s["location"], "perf": s["perf"], "destinations": s["destinations"],
            "page": s["page_name"], "ig": s["ig_username"], "value_rules": s["value_rule_name"],
            "cost_goal": s["cost_goal"], "placements": s["placements"],
            "budget": s["budget"], "lifetime": s["lifetime"], "daily": daily, "days": s["days"],
            "total": s["budget"] if s["lifetime"] else (s["budget"] * s["days"] if s["days"] else None),
            "start": s["start"].isoformat() if s["start"] else None, "end": s["end"].isoformat() if s["end"] else None,
            "audience": audience_text(shown_targeting(s)), "carousel": s["carousel"]})
        log.info("created PAUSED campaign %s (ad set %s, ads %s)", camp, adset, [a["id"] for a in ads])
    except MetaError as e:
        steps[-1]["state"] = "failed"
        job.update(state="error", error=e.info(), cleanup=cleanup(made) if made else [])
    except Exception as e:
        log.exception("create failed")
        steps[-1]["state"] = "failed"
        job.update(state="error", error={"message": f"Something broke on our side: {e}"},
                   cleanup=cleanup(made) if made else [])
    finally:
        forget("tree")


# ---------- stats ----------

PRESETS = {"today": "today", "yesterday": "yesterday", "7d": "last_7d", "30d": "last_30d", "lifetime": "maximum"}
SUMMARY_FIELDS = ("spend,reach,impressions,frequency,clicks,ctr,cpm,cpc,inline_link_clicks,actions,results,"
                  "video_play_actions,video_thruplay_watched_actions,video_p25_watched_actions,"
                  "video_p50_watched_actions,video_p75_watched_actions,video_p95_watched_actions,"
                  "video_p100_watched_actions,video_avg_time_watched_actions")
BREAKDOWNS = {"age_gender": "age,gender", "placement": "publisher_platform,platform_position", "region": "region",
              "hour": "hourly_stats_aggregated_by_audience_time_zone"}
INSIGHTS_TTL = 300


def period(preset, since, until):
    if preset == "custom":
        try:
            a, b = date.fromisoformat(since), date.fromisoformat(until)
        except (TypeError, ValueError):
            raise HTTPException(400, {"message": "Pick both dates."})
        if a > b:
            a, b = b, a
        return {"time_range": json.dumps({"since": a.isoformat(), "until": b.isoformat()})}
    if preset not in PRESETS:
        raise HTTPException(400, {"message": "Unknown date range."})
    return {"date_preset": PRESETS[preset]}


def increment(preset, since, until):
    """Days for short ranges; months for lifetime and long custom ranges (Meta adds them up itself, so reach
    per month is real reach, not a sum of days)."""
    if preset == "lifetime":
        return "monthly"
    if preset == "custom":
        try:
            return "monthly" if abs((date.fromisoformat(until) - date.fromisoformat(since)).days) > 92 else 1
        except (TypeError, ValueError):
            return 1
    return 1


def target(level, obj_id):
    if level == "account":
        return ACT
    if level not in ("campaign", "adset", "ad"):
        raise HTTPException(400, {"message": "Unknown level."})
    ours(obj_id)
    return obj_id


def kpis(row):
    a = acts(row)
    spend = float(row.get("spend") or 0)
    res, label = result_of(row)
    dms = a.get(DM, 0)
    visits = sum(v for k, v in a.items() if "profile_visit" in k)
    follows = sum(v for k, v in a.items() if "follow" in k and "unfollow" not in k)
    num = lambda k, f=float: f(row[k]) if row.get(k) not in (None, "") else None
    return {
        "spend": spend, "reach": num("reach", int), "impressions": num("impressions", int),
        "frequency": num("frequency"), "clicks": num("clicks", int), "ctr": num("ctr"), "cpm": num("cpm"),
        "cpc": num("cpc"), "link_clicks": num("inline_link_clicks", int),
        "results": res, "result_label": label, "cost_per_result": spend / res if res else None,
        "dms": dms, "cost_per_dm": spend / dms if dms else None,
        "video_plays": first(row, "video_play_actions"), "thruplays": first(row, "video_thruplay_watched_actions"),
        "views_3s": a.get("video_view"), "avg_watch": first(row, "video_avg_time_watched_actions"),
        "p25": first(row, "video_p25_watched_actions"), "p50": first(row, "video_p50_watched_actions"),
        "p75": first(row, "video_p75_watched_actions"), "p95": first(row, "video_p95_watched_actions"),
        "p100": first(row, "video_p100_watched_actions"),
        "profile_visits": visits or None, "follows": follows or None,
        "conversations_replied": a.get("onsite_conversion.messaging_conversation_replied_7d"),
        "engagements": a.get("post_engagement"), "comments": a.get("comment"), "saves": a.get("onsite_conversion.post_save"),
    }


def insights(level, obj_id, preset, since, until, fresh):
    path, when, inc = target(level, obj_id), period(preset, since, until), increment(preset, since, until)

    def load():
        total, daily = parallel(
            lambda: get_all(f"{path}/insights", {"fields": SUMMARY_FIELDS, **when}),
            lambda: get_all(f"{path}/insights", {"fields": "spend,reach,impressions,clicks,actions,results",
                                                 "time_increment": inc, **when}))
        head = total[0] if total else {}
        return {"totals": kpis(head), "bucket": "month" if inc == "monthly" else "day",
                # lifetime reaches back 37 months; charts start at the first month that had any spend
                "since": (daily[0]["date_start"] if daily and preset == "lifetime" else head.get("date_start"))
                or (daily[0]["date_start"] if daily else None),
                "until": head.get("date_stop") or (daily[-1]["date_stop"] if daily else None),
                "daily": [{"date": r["date_start"], **{k: v for k, v in kpis(r).items()
                                                       if k in ("spend", "reach", "impressions", "clicks", "results",
                                                                "dms", "cost_per_result", "cost_per_dm",
                                                                "result_label")}} for r in daily],
                "at": time.time()}
    return cached(f"ins:{path}:{json.dumps(when)}", INSIGHTS_TTL, load, fresh)


def breakdown(level, obj_id, by, preset, since, until, fresh):
    path, when = target(level, obj_id), period(preset, since, until)
    if by not in BREAKDOWNS:
        raise HTTPException(400, {"message": "Unknown breakdown."})

    def load():
        fields = "spend,impressions,clicks,actions,results" + ("" if by == "hour" else ",reach")
        rows = get_all(f"{path}/insights", {"fields": fields, "breakdowns": BREAKDOWNS[by], **when})
        out = []
        for r in rows:
            k = kpis(r)
            key = {"age_gender": lambda: {"age": r.get("age"), "gender": r.get("gender")},
                   "placement": lambda: {"platform": r.get("publisher_platform"), "position": r.get("platform_position")},
                   "region": lambda: {"region": r.get("region")},
                   "hour": lambda: {"hour": int((r.get("hourly_stats_aggregated_by_audience_time_zone") or "0")[:2])},
                   }[by]()
            out.append({**key, **{x: k[x] for x in ("spend", "impressions", "reach", "clicks", "results", "dms",
                                                    "cost_per_result", "cost_per_dm", "result_label")}})
        return {"rows": out, "at": time.time()}
    return cached(f"bd:{by}:{path}:{json.dumps(when)}", INSIGHTS_TTL, load, fresh)


def per_ad(level, obj_id, preset, since, until, fresh):
    path, when, inc = target(level, obj_id), period(preset, since, until), increment(preset, since, until)

    def load():
        rows, daily = parallel(
            lambda: get_all(f"{path}/insights", {"level": "ad", **when, "fields":
                            "ad_id,ad_name,adset_name,campaign_name,spend,impressions,reach,clicks,ctr,cpm,actions,"
                            "results,video_thruplay_watched_actions"}),
            lambda: get_all(f"{path}/insights", {"level": "ad", "time_increment": inc, **when,
                                                 "fields": "ad_id,spend,actions,results"}))
        thumbs = {}
        try:
            for c in tree()["campaigns"]:
                for s in c["adsets"]:
                    for a in s["ads"]:
                        thumbs[a["id"]] = a["thumb"]
        except MetaError:
            pass
        series = {}
        for r in daily:
            k = kpis(r)
            series.setdefault(r["ad_id"], []).append({"date": r["date_start"], "spend": k["spend"],
                                                      "results": k["results"], "dms": k["dms"]})
        return {"ads": [{"id": r["ad_id"], "name": r.get("ad_name"), "adset": r.get("adset_name"),
                         "campaign": r.get("campaign_name"), "thumb": thumbs.get(r["ad_id"]),
                         **{x: v for x, v in kpis(r).items() if x in (
                             "spend", "impressions", "reach", "clicks", "ctr", "cpm", "results", "result_label",
                             "cost_per_result", "dms", "cost_per_dm", "thruplays")},
                         "daily": series.get(r["ad_id"], [])} for r in rows],
                "at": time.time()}
    return cached(f"ads:{path}:{json.dumps(when)}", INSIGHTS_TTL, load, fresh)


# ---------- HTTP ----------

def local_only(request: Request):
    """Pages on other sites can't send custom headers to us without a CORS preflight, which we never allow."""
    if request.headers.get("x-local") != "1":
        raise HTTPException(403)


def call(fn, *args):
    try:
        return fn(*args)
    except MetaError as e:
        fail(e)


@app.get("/")
def page():
    return FileResponse(HERE / "admanager.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/account")
def api_account(request: Request, fresh: bool = False):
    local_only(request)
    acc = call(account_info, fresh)
    if fresh:
        forget("access")
    left = _rate["until"] - time.time()
    return {**acc, "access": can_edit(), "rate_limited": round(left) if left > 0 else 0}


@app.get("/api/defaults")
def api_defaults(request: Request):
    local_only(request)
    t = template()
    return {**{k: v for k, v in t.items() if k != "welcome_json"},
            "locations": LOCATIONS, "positions": POSITIONS, "vr_values": VR_VALUES, "max_rule_sets": MAX_RULE_SETS,
            "cost_cap_goals": sorted(COST_CAP_GOALS)}


@app.get("/api/assets")
def api_assets(request: Request, fresh: bool = False):
    """Business assets for the form: Pages (with their Instagram account / WhatsApp number) and value rule sets.
    Either half can fail on its own (e.g. while Meta is rate-limiting) without blanking the other."""
    local_only(request)
    t = template()
    out = {"default_rule_set": t.get("value_rule_set_id"), "template_partial": bool(t.get("partial"))}
    try:
        out["pages"] = pages()
    except MetaError as e:
        out["pages"], out["pages_error"] = [main_page()], e.message
    try:
        out["rule_sets"] = rule_sets(fresh)
    except MetaError as e:
        out["rule_sets"], out["rule_sets_error"] = [], e.message
    return out


@app.post("/api/value_rules")
def api_value_rules(request: Request, body: dict = Body(...)):
    """Make a new value rule set (it does nothing until an ad set uses it)."""
    local_only(request)
    rid = call(make_rule_set, body)
    return {"id": rid, "rule_sets": call(rule_sets, True)}


@app.get("/api/tree")
def api_tree(request: Request, fresh: bool = False):
    local_only(request)
    return call(tree, fresh)


@app.post("/api/status/{obj_id}")
def api_status(obj_id: str, request: Request, body: dict = Body(...)):
    """Toggle: {status: ACTIVE|PAUSED, confirmed: true}. Turning on must come from a confirmed click."""
    local_only(request)
    status = body.get("status")
    if status not in ("ACTIVE", "PAUSED"):
        raise HTTPException(400, {"message": "Status must be on or off."})
    if status == "ACTIVE" and body.get("confirmed") is not True:
        raise HTTPException(400, {"message": "Turning an ad on needs your confirmation."})
    ours(obj_id)
    call(graph, "POST", obj_id, None, {"status": status})
    log.info("status of %s set to %s from the page", obj_id, status)
    forget("tree")
    return {"ok": True}


@app.post("/api/edit/{obj_id}")
def api_edit(obj_id: str, request: Request, body: dict = Body(...)):
    """Dates and budgets: {kind: campaign|adset, start?, end? ('' = no end), daily_budget?, lifetime_budget?}
    in IST and rupees."""
    local_only(request)
    ours(obj_id)
    kind = body.get("kind")
    data = {}
    if kind == "adset":
        if body.get("start"):
            data["start_time"] = meta_time(ist(body["start"]))
        if "end" in body:
            data["end_time"] = meta_time(ist(body["end"])) if body["end"] else "0"
        if "value_rule_set_id" in body:  # '' = stop using value rules
            vr = str(body["value_rule_set_id"] or "")
            if vr and not re.fullmatch(r"\d{6,25}", vr):
                raise HTTPException(400, {"message": "That isn't a valid value rule set."})
            data.update({"value_rule_set_id": vr, "value_rules_applied": "true"} if vr else
                        {"value_rules_applied": "false"})
    elif kind != "campaign":
        raise HTTPException(400, {"message": "Only campaigns and ad sets have budgets and dates."})
    for k in ("daily_budget", "lifetime_budget"):
        if body.get(k) not in (None, ""):
            try:
                rupees = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(400, {"message": "The budget has to be a number."})
            if rupees <= 0:
                raise HTTPException(400, {"message": "The budget has to be more than ₹0."})
            data[k] = str(int(round(rupees * 100)))
    if not data:
        raise HTTPException(400, {"message": "Nothing to change."})
    call(graph, "POST", obj_id, None, data)
    log.info("edited %s %s: %s", kind, obj_id, data)
    forget("tree")
    return {"ok": True}


@app.post("/api/upload")
async def api_upload(request: Request):
    """Raw file body (x-filename header). Saved to ads/uploads, then sent to Meta in the background."""
    local_only(request)
    name = unquote(request.headers.get("x-filename") or "file")
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)[:120] or "file"
    ext = Path(name).suffix.lower()
    ctype = request.headers.get("content-type") or ""
    kind = "video" if ext in VIDEO_EXT or ctype.startswith("video/") else \
        "image" if ext in IMAGE_EXT else None
    if not kind:
        if ext in (".heic", ".heif", ".webp"):
            raise HTTPException(400, {"message": f"{name}: Meta doesn't take {ext} photos. Save it as JPG first."})
        raise HTTPException(400, {"message": f"{name}: only photos (JPG, PNG) and videos (MP4, MOV) work."})
    UPLOADS.mkdir(exist_ok=True)
    job = new_job("upload", name=name, type=kind, phase="receiving", pct=0)
    path = UPLOADS / f"{job['id']}{ext}"
    size = 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    if not size:
        path.unlink(missing_ok=True)
        job.update(phase="error", error={"message": "The file was empty."})
        return job
    job["size"] = size
    threading.Thread(target=send_to_meta, args=(job, path), daemon=True).start()
    return job


@app.get("/api/job/{jid}")
def api_job(jid: str, request: Request):
    local_only(request)
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, {"message": "Not found (the app may have restarted)."})
    return job


@app.post("/api/create")
def api_create(request: Request, body: dict = Body(...)):
    local_only(request)
    s = check_spec(body)
    job = new_job("create", state="running", steps=[])
    threading.Thread(target=run_create, args=(job, s), daemon=True).start()
    return job


@app.post("/api/golive/{campaign_id}")
def api_golive(campaign_id: str, request: Request, body: dict = Body(...)):
    """Turn on a campaign with its ad sets and ads (the summary card's Go live button, after a confirmation).
    The campaign goes last: it's the main switch, so nothing spends until everything under it is on."""
    local_only(request)
    if body.get("confirmed") is not True:
        raise HTTPException(400, {"message": "Going live needs your confirmation."})
    ours(campaign_id)
    try:
        ads = get_all(f"{campaign_id}/ads", {"fields": "id,status"})
        adsets = get_all(f"{campaign_id}/adsets", {"fields": "id,status"})
        for obj in ads + adsets:
            if obj.get("status") == "PAUSED":
                graph("POST", obj["id"], data={"status": "ACTIVE"}, retries=1)
        graph("POST", campaign_id, data={"status": "ACTIVE"}, retries=1)
    except MetaError as e:
        forget("tree")
        fail(e)
    log.info("went live from the page: campaign %s (%d ad sets, %d ads)", campaign_id, len(adsets), len(ads))
    forget("tree")
    return {"ok": True}


@app.delete("/api/campaign/{campaign_id}")
def api_discard(campaign_id: str, request: Request):
    """Discard a draft: only a paused campaign that has never spent anything, and never the template."""
    local_only(request)
    ours(campaign_id)
    if campaign_id == TEMPLATE_CAMPAIGN:
        raise HTTPException(400, {"message": "That's your main campaign, it can't be deleted from here."})
    try:
        c = graph("GET", campaign_id, params={"fields": "status"})
        spent = get_all(f"{campaign_id}/insights", {"fields": "spend", "date_preset": "maximum"})
        if c.get("status") != "PAUSED" or (spent and float(spent[0].get("spend") or 0) > 0):
            raise HTTPException(400, {"message": "Only paused drafts that never spent anything can be deleted."})
        creatives = {(a.get("creative") or {}).get("id") for a in get_all(f"{campaign_id}/ads", {"fields": "creative{id}"})}
        graph("DELETE", campaign_id, retries=1)
    except MetaError as e:
        fail(e)
    for cr in creatives - {None}:  # creatives outlive their campaign; they're only this draft's, so tidy them up
        try:
            graph("DELETE", cr, retries=0)
        except MetaError:
            pass
    log.info("draft campaign %s deleted from the page (and creatives %s)", campaign_id, sorted(creatives - {None}))
    forget("tree")
    return {"ok": True}


@app.get("/api/insights")
def api_insights(request: Request, level: str = "account", id: str = "", preset: str = "7d", since: str = "",
                 until: str = "", fresh: bool = False):
    local_only(request)
    return call(insights, level, id, preset, since, until, fresh)


@app.get("/api/insights/breakdown")
def api_breakdown(request: Request, by: str, level: str = "account", id: str = "", preset: str = "7d",
                  since: str = "", until: str = "", fresh: bool = False):
    local_only(request)
    return call(breakdown, level, id, by, preset, since, until, fresh)


@app.get("/api/insights/ads")
def api_per_ad(request: Request, level: str = "account", id: str = "", preset: str = "7d", since: str = "",
               until: str = "", fresh: bool = False):
    local_only(request)
    return call(per_ad, level, id, preset, since, until, fresh)


PREVIEW_FORMATS = ("INSTAGRAM_STANDARD", "INSTAGRAM_REELS", "INSTAGRAM_STORY", "MOBILE_FEED_STANDARD",
                   "FACEBOOK_STORY_MOBILE", "FACEBOOK_REELS_MOBILE")


def iframe_src(rows):
    src = re.search(r'src="([^"]+)"', (rows[0].get("body") if rows else "") or "")
    return src[1].replace("&amp;", "&") if src else None


@app.get("/api/previews/{ad_id}")
def api_previews(ad_id: str, request: Request, fmt: str = "INSTAGRAM_STANDARD"):
    local_only(request)
    if fmt not in PREVIEW_FORMATS:
        raise HTTPException(400, {"message": "Unknown preview format."})
    ours(ad_id)
    body = call(lambda: cached(f"preview:{ad_id}:{fmt}", 600, lambda: graph(
        "GET", f"{ad_id}/previews", params={"ad_format": fmt}).get("data") or []))
    return {"src": iframe_src(body)}


@app.post("/api/preview_draft")
def api_preview_draft(request: Request, body: dict = Body(...)):
    """Meta's own preview of the ad being made, before anything is created (generatepreviews)."""
    local_only(request)
    fmt = body.get("fmt")
    if fmt not in PREVIEW_FORMATS:
        raise HTTPException(400, {"message": "Unknown preview format."})
    s = check_creative(body, preview=True)
    items = s["media"] if s["carousel"] else s["media"][:1]
    creative = {k: v for k, v in creative_spec(s, items, "preview").items() if k != "name"}
    key = "draft:" + hashlib.sha1(json.dumps([creative, fmt], sort_keys=True).encode()).hexdigest()
    rows = call(lambda: cached(key, 600, lambda: graph("GET", f"{ACT}/generatepreviews", params={
        "creative": json.dumps(creative), "ad_format": fmt}).get("data") or []))
    return {"src": iframe_src(rows)}


@app.get("/api/search/geo")
def api_geo(request: Request, q: str):
    local_only(request)
    if len(q.strip()) < 2:
        return {"data": []}
    rows = call(lambda: cached(f"geo:{q.lower()}", 86400, lambda: graph("GET", "search", params={
        "type": "adgeolocation", "q": q, "location_types": json.dumps(["city", "region", "country"]),
        "limit": 12}).get("data") or []))
    rows.sort(key=lambda r: r.get("country_code") != "IN")  # Indian places first
    return {"data": [{"key": r["key"], "name": r["name"], "type": r["type"], "region": r.get("region"),
                      "country": r.get("country_name"), "country_code": r.get("country_code")} for r in rows]}


@app.get("/api/search/interest")
def api_interest(request: Request, q: str):
    local_only(request)
    if len(q.strip()) < 2:
        return {"data": []}
    def load():
        rows = graph("GET", "search", params={"type": "adinterest", "q": q, "limit": 15}).get("data") or []
        if not rows:
            return []
        # Meta's search still offers interests it has retired (e.g. "Resin", "Epoxy"); they make the ad set fail
        # with "Broaden your audience", so only the ones Meta calls valid are offered
        valid = graph("GET", "search", params={"type": "adinterestvalid",
                                                "interest_list": json.dumps([r["name"] for r in rows])}).get("data") or []
        ok = {v["id"] for v in valid if v.get("valid")}
        return [r for r in rows if r["id"] in ok][:10]
    rows = call(lambda: cached(f"int:{q.lower()}", 86400, load))
    return {"data": [{"id": r["id"], "name": r["name"], "topic": r.get("topic"),
                      "size": r.get("audience_size_upper_bound")} for r in rows]}


@app.post("/api/audience_size")
def api_audience_size(request: Request, body: dict = Body(...)):
    """Meta's estimate of how many people the audience reaches (nothing is created)."""
    local_only(request)
    p = placements_of(body)
    if placement_problem(p):
        p = {**p, "auto": False, **{k: [] for k in POSITIONS}, "instagram": ["stream"]}
    s = {**body, "placements": p,
         "age_min": int(body.get("age_min") or 18), "age_max": int(body.get("age_max") or 65),
         "geo": body.get("geo") or [{"type": "country", "key": "IN"}]}
    goals = (LOCATIONS.get(body.get("location")) or {}).get("goals", {}).get(body.get("objective")) or ["CONVERSATIONS"]
    goal = body.get("perf") if body.get("perf") in goals else goals[0]
    t = targeting(s)
    key = "size:" + json.dumps([t, goal], sort_keys=True)
    try:
        rows = cached(key, 3600, lambda: graph("GET", f"{ACT}/delivery_estimate", params={
            "optimization_goal": goal, "targeting_spec": json.dumps(t)}).get("data") or [])
    except MetaError as e:
        return {"error": e.message}
    r = rows[0] if rows else {}
    return {"lower": r.get("estimate_mau_lower_bound"), "upper": r.get("estimate_mau_upper_bound")}
