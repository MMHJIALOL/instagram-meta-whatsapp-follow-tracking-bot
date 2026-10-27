"""Separate local WhatsApp DM dashboard. Start with: uvicorn wa_dm.app:app --host 127.0.0.1 --port 8003."""
import asyncio
import contextlib
import copy
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .replies import DEFAULTS, plan_reply

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("WA_DM_DATA", str(HERE / "data")))
ASSETS = HERE / "assets"
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = secrets.token_urlsafe(32)
PORT = int(os.environ.get("WA_DM_PORT", "8003"))
bridge_state = {"state": "starting", "me": "", "detail": "WhatsApp connection starting", "qr": "", "at": 0}
bridge_process = None
reconnect_requested = False
reconnect_at = 0.0


@contextlib.contextmanager
def db():
    c = sqlite3.connect(DATA / "messages.db", timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts (
            chat_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            paused INTEGER NOT NULL DEFAULT 0, manual_until REAL NOT NULL DEFAULT 0,
            payment TEXT NOT NULL DEFAULT 'new', review TEXT NOT NULL DEFAULT '',
            last_intent TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, direction TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'chat', at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'received', intent TEXT NOT NULL DEFAULT '', asset TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS message_chat_time ON messages(chat_id,at);
        CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, inbound_id TEXT NOT NULL, chat_id TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, detail TEXT NOT NULL DEFAULT '',
            wa_id TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, text TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO settings VALUES (1,?)", (json.dumps(DEFAULTS, ensure_ascii=False),))
        saved = json.loads(c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        if saved.get("reply_revision", 1) < 2:
            saved["answers"] = {**DEFAULTS["answers"], **saved.get("answers", {})}
            for key in ("materials", "unknown", "pdf", "human", "media", "audio", "recording"):
                saved["answers"][key] = DEFAULTS["answers"][key]
            saved.update(reply_revision=2, handoff_number=DEFAULTS["handoff_number"])
            c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(saved, ensure_ascii=False),))
        # A crash during a send is ambiguous. Do not retry and risk double payment requests.
        c.execute("UPDATE outbox SET status='uncertain',detail='Connection restarted during send; check WhatsApp before retrying' WHERE status='sending'")


def config():
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
    value = copy.deepcopy(DEFAULTS)
    if row:
        saved = json.loads(row[0])
        value.update(saved)
        value["answers"] = {**DEFAULTS["answers"], **saved.get("answers", {})}
    return value


def activity(text):
    with db() as c:
        c.execute("INSERT INTO activity(at,text) VALUES (?,?)", (time.time(), text[:500]))


def connected(cfg):
    return bridge_state["state"] == "ready" and bridge_state["me"] == cfg["expected_number"] and time.time() - bridge_state["at"] < 50


def blocked(contact, cfg):
    if not cfg["enabled"]:
        return "Bot paused"
    if not connected(cfg):
        return "WhatsApp is not connected to the expected business number"
    if contact and contact["paused"]:
        return "Chat paused"
    if contact and contact["manual_until"] > time.time():
        return "You are handling this chat (30-minute automatic pause)"
    if contact and contact["payment"] == "opted_out":
        return "Customer opted out"
    return ""


async def bridge_supervisor():
    global bridge_process, reconnect_requested
    node = os.environ.get("WA_DM_NODE", r"C:\Program Files\nodejs\node.exe")
    delay = 4
    while True:
        try:
            bridge_state.update(state="starting", detail="Opening WhatsApp Web…", qr="", at=time.time())
            env = dict(os.environ, WA_DM_BRIDGE_TOKEN=TOKEN, WA_DM_PORT=str(PORT), WA_DM_DATA=str(DATA))
            with open(DATA / "bridge.log", "ab") as logfile:
                bridge_process = subprocess.Popen([node, str(HERE / "bridge.js")], cwd=HERE, env=env,
                    stdout=logfile, stderr=logfile, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = time.time()
            while bridge_process.poll() is None:
                await asyncio.sleep(1)
                if reconnect_requested:
                    if time.time() - reconnect_at > 25:
                        kill_bridge_tree(bridge_process)
            if bridge_state["state"] not in ("error", "auth_failure"):
                bridge_state.update(state="disconnected", qr="", detail="Connection closed. Reconnecting…", at=time.time())
            reconnect_requested = False
            if time.time() - started > 90:
                delay = 4
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            bridge_state.update(state="error", detail=str(exc)[:250], at=time.time())
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


def kill_bridge_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        process.terminate()


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = None
    if os.environ.get("WA_DM_NO_BRIDGE") != "1":
        task = asyncio.create_task(bridge_supervisor())
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if bridge_process and bridge_process.poll() is None:
        kill_bridge_tree(bridge_process)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.url.path.startswith("/internal/"):
            if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + TOKEN):
                return Response("Forbidden", status_code=403)
        else:
            origin = request.headers.get("origin")
            if request.headers.get("x-requested-with") != "kalakriti-dashboard" or (origin and urlparse(origin).netloc != request.headers.get("host")):
                return Response("Open the local dashboard to make changes", status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.get("/")
def dashboard():
    return FileResponse(HERE / "dashboard.html")


@app.get("/inbox.css")
def inbox_styles():
    return FileResponse(HERE / "inbox.css", media_type="text/css")


@app.get("/inbox.js")
def inbox_script():
    return FileResponse(HERE / "inbox.js", media_type="text/javascript")


@app.get("/assets/{name}")
def asset(name: str):
    if name not in ("payment.jpg", "workshop.pdf") or not (ASSETS / name).is_file():
        raise HTTPException(404)
    return FileResponse(ASSETS / name)


@app.get("/api/qr")
def qr():
    if bridge_state["state"] != "qr" or not bridge_state.get("qr"):
        raise HTTPException(404, "QR is not available")
    buf = io.BytesIO()
    qrcode.make(bridge_state["qr"]).save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/api/state")
def state():
    cfg = config()
    connection = {k: v for k, v in bridge_state.items() if k != "qr"}
    connection.update(connected=connected(cfg), has_qr=bool(bridge_state.get("qr")), expected=cfg["expected_number"])
    with db() as c:
        counts = {"contacts": c.execute("SELECT count(*) FROM contacts").fetchone()[0],
            "replies": c.execute("SELECT count(*) FROM outbox WHERE status='sent' AND coalesce(json_extract(payload,'$.audience'),'client')<>'team'").fetchone()[0],
            "review": c.execute("SELECT count(*) FROM contacts WHERE review<>''").fetchone()[0],
            "paid": c.execute("SELECT count(*) FROM contacts WHERE payment='paid'").fetchone()[0],
            "alerts": c.execute("SELECT count(*) FROM outbox WHERE status='sent' AND json_extract(payload,'$.audience')='team'").fetchone()[0]}
        events = [dict(r) for r in c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 12")]
        problems = [dict(r) for r in c.execute("SELECT id,chat_id,status,detail,created FROM outbox WHERE status IN ('failed','uncertain') ORDER BY created DESC LIMIT 10")]
    return {"connection": connection, "settings": cfg, "counts": counts, "activity": events, "problems": problems}


@app.get("/api/chats")
def chats():
    with db() as c:
        return [dict(r) for r in c.execute("""SELECT c.*,
            m.body AS last_message, m.at AS last_at, m.direction AS last_direction,
            m.status AS last_status
            FROM contacts c LEFT JOIN messages m ON m.rowid=(
                SELECT rowid FROM messages WHERE chat_id=c.chat_id AND direction<>'team'
                ORDER BY rowid DESC LIMIT 1)
            ORDER BY m.rowid DESC, c.updated DESC LIMIT 400""")]


@app.get("/api/chat")
def chat(chat_id: str):
    with db() as c:
        person = c.execute("SELECT * FROM contacts WHERE chat_id=?", (chat_id,)).fetchone()
        # WhatsApp timestamps and this computer's clock can differ by seconds.
        # The insertion sequence records the actual receive/send order, including
        # existing chats, without changing original timestamps or replaying sends.
        messages = [dict(r) for r in c.execute("""SELECT * FROM (
            SELECT rowid AS sequence, * FROM messages WHERE chat_id=? AND direction<>'team'
            ORDER BY rowid DESC LIMIT 150) ORDER BY sequence""", (chat_id,))]
        alerts = [dict(r) for r in c.execute("""SELECT id,at,status FROM messages
            WHERE chat_id=? AND direction='team' ORDER BY rowid DESC LIMIT 10""", (chat_id,))]
    return {"contact": dict(person) if person else None, "messages": messages, "team_alerts": alerts}


class Preview(BaseModel):
    text: str = Field(max_length=6000)
    last_intent: str = ""


@app.post("/api/preview")
def preview(body: Preview):
    return plan_reply(body.text, config(), last_intent=body.last_intent)


@app.post("/api/settings")
async def save_settings(request: Request):
    incoming = await request.json()
    cfg = config()
    for key in ("business_name", "date", "time", "duration", "upi", "payee", "website"):
        if key in incoming:
            if not isinstance(incoming[key], str) or not 1 <= len(incoming[key].strip()) <= 400:
                raise HTTPException(400, "Invalid " + key)
            cfg[key] = incoming[key].strip()
    for key in ("workshop_fee", "kit_price", "shipping"):
        if key in incoming:
            if type(incoming[key]) is not int or not 0 <= incoming[key] <= 100000:
                raise HTTPException(400, "Invalid " + key)
            cfg[key] = incoming[key]
    if "answers" in incoming:
        if not isinstance(incoming["answers"], dict):
            raise HTTPException(400, "Invalid answers")
        for key, value in incoming["answers"].items():
            if key not in DEFAULTS["answers"] or not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
                raise HTTPException(400, "Invalid answer: " + key)
            cfg["answers"][key] = value.strip()
    if "enabled" in incoming:
        if type(incoming["enabled"]) is not bool:
            raise HTTPException(400, "enabled must be true or false")
        if incoming["enabled"] and not connected(cfg):
            raise HTTPException(409, "Pehle +91 9211664301 ko QR scan karke connect karo.")
        cfg["enabled"] = incoming["enabled"]
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(cfg, ensure_ascii=False),))
    activity("Saved reply settings" if "enabled" not in incoming else ("Bot replies turned ON" if cfg["enabled"] else "Bot replies paused"))
    return {"ok": True}


class ChatUpdate(BaseModel):
    chat_id: str = Field(max_length=120)
    paused: bool | None = None
    payment: str | None = None
    clear_review: bool = False
    resume: bool = False


@app.post("/api/chat")
def update_chat(body: ChatUpdate):
    with db() as c:
        if not c.execute("SELECT 1 FROM contacts WHERE chat_id=?", (body.chat_id,)).fetchone():
            raise HTTPException(404, "Chat not found")
        if body.paused is not None:
            c.execute("UPDATE contacts SET paused=? WHERE chat_id=?", (int(body.paused), body.chat_id))
        if body.resume:
            c.execute("UPDATE contacts SET paused=0,manual_until=0 WHERE chat_id=?", (body.chat_id,))
        if body.payment is not None:
            if body.payment not in ("new", "payment_review", "paid", "opted_out"):
                raise HTTPException(400, "Invalid payment status")
            c.execute("UPDATE contacts SET payment=? WHERE chat_id=?", (body.payment, body.chat_id))
        if body.clear_review or body.payment == "paid":
            c.execute("UPDATE contacts SET review='' WHERE chat_id=?", (body.chat_id,))
    return {"ok": True}


@app.post("/api/reconnect")
def reconnect():
    global reconnect_requested, reconnect_at
    reconnect_requested = True
    reconnect_at = time.time()
    return {"ok": True}


class Incoming(BaseModel):
    id: str = Field(min_length=1, max_length=250)
    chat_id: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=150)
    phone: str = Field(default="", max_length=40)
    body: str = Field(default="", max_length=12000)
    kind: str = Field(default="chat", max_length=40)
    has_media: bool = False
    timestamp: float


def direct_chat(chat_id):
    return bool(re.fullmatch(r"\d+@(?:c\.us|lid)", chat_id))


def team_chat(body, cfg):
    return body.phone == cfg["handoff_number"] or body.chat_id == cfg["handoff_number"] + "@c.us"


def team_alert(c, body, person, cfg, reason):
    phone = person["phone"] or (body.chat_id.split("@")[0] if body.chat_id.endswith("@c.us") else "")
    client = "+" + phone if phone else "Number unavailable; WhatsApp ID: " + body.chat_id
    history = c.execute("SELECT direction,body FROM messages WHERE chat_id=? AND id<>? AND direction IN ('in','out','manual') ORDER BY rowid DESC LIMIT 4", (body.chat_id, body.id)).fetchall()
    context = "\n".join(("Client: " if r["direction"] == "in" else "Reply: ") + r["body"][:250] for r in reversed(history))
    query = body.body or f"[{body.kind}: please check the attachment/voice note in WhatsApp]"
    text = f"Kalakriti · Team help needed\nClient: {person['name'] or 'Customer'}\nNumber: {client}\n"
    if phone:
        text += f"Reply: https://wa.me/{phone}\n"
    text += f"Reason: {reason}\n\nClient query:\n{query[:2200]}"
    if len(query) > 2200:
        text += "\n[Long query: rest is in the local Inbox]"
    if context:
        text += "\n\nRecent context:\n" + context[:1100]
    return {"kind": "text", "body": text, "audience": "team", "destination": cfg["handoff_number"] + "@c.us"}


@app.post("/internal/status")
async def status_update(request: Request):
    incoming = await request.json()
    old = bridge_state["state"]
    me = re.sub(r"\D", "", str(incoming.get("me", "")))
    new_state = str(incoming.get("state", "error"))[:40]
    if new_state == "ready" and me != config()["expected_number"]:
        new_state = "wrong_number"
    bridge_state.update(state=new_state, me=me, qr=str(incoming.get("qr", ""))[:12000], detail=str(incoming.get("detail", ""))[:350], at=time.time())
    if old != new_state:
        activity("WhatsApp: " + new_state.replace("_", " "))
    cfg = config()
    return {"ok": True, "expected_number": cfg["expected_number"], "handoff_number": cfg["handoff_number"], "restart": reconnect_requested}


@app.post("/internal/incoming")
def incoming_message(body: Incoming):
    if not direct_chat(body.chat_id) or body.timestamp < time.time() - 120 or body.timestamp > time.time() + 120:
        return {"actions": [], "reason": "Ignored group, broadcast or old message"}
    cfg = config()
    if team_chat(body, cfg):
        return {"actions": [], "reason": "Team inbox; automatic replies disabled"}
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM messages WHERE id=?", (body.id,)).fetchone():
            return {"actions": [], "reason": "Duplicate message"}
        c.execute("INSERT INTO contacts(chat_id,name,phone,updated) VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE contacts.name END,phone=CASE WHEN excluded.phone<>'' THEN excluded.phone ELSE contacts.phone END,updated=excluded.updated", (body.chat_id, body.name, body.phone, time.time()))
        person = c.execute("SELECT * FROM contacts WHERE chat_id=?", (body.chat_id,)).fetchone()
        text = body.body.strip()
        answer = plan_reply(text, cfg, kind=body.kind, has_media=body.has_media, last_intent=person["last_intent"], payment_status=person["payment"])
        c.execute("INSERT INTO messages(id,chat_id,direction,body,kind,at,intent) VALUES (?,?,?,?,?,?,?)", (body.id, body.chat_id, "in", text or f"[{body.kind}]", body.kind, body.timestamp, ",".join(answer["intents"])))
        if re.fullmatch(r"(?:stop|unsubscribe|band karo|message mat karo|mat bhejo)[.! ]*", text.lower()):
            c.execute("UPDATE contacts SET payment='opted_out',review='Customer asked to stop' WHERE chat_id=?", (body.chat_id,))
            return {"actions": [], "reason": "Customer opted out"}
        if person["payment"] == "opted_out" and text.lower() in ("start", "resume", "shuru karo"):
            c.execute("UPDATE contacts SET payment='new',review='' WHERE chat_id=?", (body.chat_id,))
            person = c.execute("SELECT * FROM contacts WHERE chat_id=?", (body.chat_id,)).fetchone()
        if answer["review"]:
            c.execute("UPDATE contacts SET review=? WHERE chat_id=?", (answer["review"], body.chat_id))
        if answer["payment_review"] and person["payment"] not in ("paid", "opted_out"):
            c.execute("UPDATE contacts SET payment='payment_review' WHERE chat_id=?", (body.chat_id,))
        c.execute("UPDATE contacts SET last_intent=? WHERE chat_id=?", (answer["intents"][-1], body.chat_id))
        reason = blocked(person, cfg)
        sent_recently = c.execute("SELECT count(*) FROM outbox WHERE chat_id=? AND created>? AND status IN ('pending','sending','sent') AND coalesce(json_extract(payload,'$.audience'),'client')<>'team'", (body.chat_id, time.time() - 300)).fetchone()[0]
        if sent_recently >= 12:
            reason = "Reply limit reached; check chat manually"
            c.execute("UPDATE contacts SET review=? WHERE chat_id=?", (reason, body.chat_id))
        if reason:
            c.execute("UPDATE messages SET status=? WHERE id=?", (reason, body.id))
            return {"actions": [], "reason": reason}
        actions = []
        payloads = list(answer["actions"])
        if answer["handoff"]:
            payloads.append(team_alert(c, body, person, cfg, answer["review"]))
        for index, payload in enumerate(payloads):
            action_id = f"{body.id}:{index}"
            c.execute("INSERT INTO outbox(id,inbound_id,chat_id,payload,created) VALUES (?,?,?,?,?)", (action_id,body.id,body.chat_id,json.dumps(payload,ensure_ascii=False),time.time()))
            actions.append({**payload, "id": action_id, "chat_id": body.chat_id})
    return {"actions": actions, "intents": answer["intents"], "handoff": answer["handoff"]}


class ActionId(BaseModel):
    id: str = Field(max_length=300)


@app.post("/internal/claim")
def claim(body: ActionId):
    cfg = config()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM outbox WHERE id=?", (body.id,)).fetchone()
        if not row or row["status"] != "pending":
            return {"allowed": False}
        person = c.execute("SELECT * FROM contacts WHERE chat_id=?", (row["chat_id"],)).fetchone()
        payload = json.loads(row["payload"])
        is_team = payload.get("audience") == "team"
        reason = blocked(None if is_team else person,cfg)
        if is_team and payload.get("destination") != cfg["handoff_number"] + "@c.us":
            reason = "Invalid team destination"
        if reason or time.time() - row["created"] > (86400 if is_team else 120):
            c.execute("UPDATE outbox SET status='cancelled',detail=? WHERE id=?", (reason or "Reply expired",body.id))
            return {"allowed": False}
        c.execute("UPDATE outbox SET status='sending' WHERE id=?", (body.id,))
    return {"allowed": True}


class Delivery(BaseModel):
    id: str = Field(max_length=300)
    status: str
    wa_id: str = Field(default="",max_length=300)
    detail: str = Field(default="",max_length=500)


@app.post("/internal/result")
def result(body: Delivery):
    if body.status not in ("sent","uncertain","failed"):
        raise HTTPException(400)
    with db() as c:
        row = c.execute("SELECT * FROM outbox WHERE id=?",(body.id,)).fetchone()
        if not row or row["status"] != "sending":
            return {"ok": True}
        payload = json.loads(row["payload"])
        c.execute("UPDATE outbox SET status=?,detail=?,wa_id=? WHERE id=?",(body.status,body.detail,body.wa_id,body.id))
        direction = "team" if payload.get("audience") == "team" else "out"
        c.execute("INSERT OR IGNORE INTO messages(id,chat_id,direction,body,kind,at,status,asset) VALUES (?,?,?,?,?,?,?,?)", (body.id,row["chat_id"],direction,payload.get("body",""),payload["kind"],time.time(),body.status,payload.get("asset","")))
        if body.status != "sent":
            c.execute("UPDATE contacts SET review='Reply delivery needs checking' WHERE chat_id=?",(row["chat_id"],))
    return {"ok": True}


@app.get("/internal/pending-alerts")
def pending_alerts(request: Request):
    if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + TOKEN):
        raise HTTPException(403)
    with db() as c:
        rows = c.execute("SELECT id,chat_id,payload FROM outbox WHERE status='pending' AND created>? AND json_extract(payload,'$.audience')='team' ORDER BY created LIMIT 20", (time.time()-86400,)).fetchall()
    return [{**json.loads(r["payload"]), "id": r["id"], "chat_id": r["chat_id"]} for r in rows]


@app.post("/internal/manual")
def manual(body: Incoming):
    if not direct_chat(body.chat_id) or body.timestamp < time.time()-120:
        return {"ok": True}
    if team_chat(body, config()):
        return {"ok": True}
    with db() as c:
        c.execute("INSERT INTO contacts(chat_id,updated,manual_until) VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET manual_until=excluded.manual_until,updated=excluded.updated",(body.chat_id,time.time(),time.time()+1800))
        c.execute("INSERT OR IGNORE INTO messages(id,chat_id,direction,body,kind,at,status) VALUES (?,?,?,?,?,?,?)",(body.id,body.chat_id,"manual",body.body or f"[{body.kind}]",body.kind,body.timestamp,"sent from phone"))
    return {"ok": True}
