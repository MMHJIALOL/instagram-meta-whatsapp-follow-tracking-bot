"""Local self-check: run while uvicorn is up on :8000. python check_webhook.py"""
import hashlib
import hmac
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()
URL = "http://localhost:8000/webhook"
token, secret = os.environ["VERIFY_TOKEN"], os.environ["APP_SECRET"]

r = httpx.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "12345"})
assert r.status_code == 200 and r.text == "12345", f"verify failed: {r.status_code} {r.text}"
print("OK  GET verification echoes challenge")

r = httpx.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
assert r.status_code == 403, r.status_code
print("OK  wrong verify token -> 403")

body = json.dumps({"object": "instagram", "entry": []}).encode()
r = httpx.post(URL, content=body, headers={"X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"})
assert r.status_code == 403, r.status_code
print("OK  bad signature -> 403")

sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
r = httpx.post(URL, content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
assert r.status_code == 200, r.status_code
print("OK  valid signature -> 200")
