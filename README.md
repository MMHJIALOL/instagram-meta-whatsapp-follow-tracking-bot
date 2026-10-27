# Meta ecosystem workshop bot

Instagram / Facebook DM bot built for a resin art online workshop (Karva Chauth pooja thali batch).

- `main.py` — FastAPI webhook: replies to ad DMs, comments and story replies with the workshop link
- `messenger.py` — same flow for Facebook Messenger
- `dashboard.py` + `dashboard.html` — local dashboard (127.0.0.1:8001) with tabs: Members (who joined, who to
  remove, unfollow warnings), Workshop (end / start the next workshop, go live), Messages (edit every bot text and
  question), Activity (live feed of everything the bot does, background jobs). See `DASHBOARD_FEATURES.md`
- `bot_messages.py` — the list of bot messages, what triggers each, and the editing rules
- `wa/listener.js` — WhatsApp group listener (joins/leaves) that feeds the dashboard
- `ads/` — a small Meta Ads Manager for creating and toggling ads
- `reel/` — script that rendered the promo reel

Copy `.env.example` to `.env`, fill in your Meta app credentials, then:

```
pip install -r requirements.txt
uvicorn main:app --port 8000
```

WhatsApp community history sharing runs through `members_keepalive.py` at Windows logon.
The current workshop is read from the dashboard. Join events trigger an immediate
share; membership reconciliation every 15 seconds catches community joins that
WhatsApp does not emit as events, including eligible missed joins after downtime.
Pending shares are stored in `wa/history-jobs.json` and retried with backoff up to
five minutes. WhatsApp eligibility and already-received checks remain in force.
The PC must be awake, online, and linked to WhatsApp. The supervisor prevents duplicate
launches and clears orphaned browsers belonging only to the listener's profile.
Run `node --test wa/history_queue.test.js` to check retry and restart behavior.
