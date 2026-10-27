# Resin workshop bot

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
