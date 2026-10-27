# Kalakriti WhatsApp DM bot

Run `start_whatsapp_bot.bat` from the project root. Open http://127.0.0.1:8003/.
In WhatsApp Business on **9211664301**, open Linked devices → Link a device and
scan the QR. Check **Try a reply**, then click **Turn replies on**.

This is a separate whatsapp-web.js linked device and SQLite database. It does
not reuse or modify the existing group listener's authentication, Meta app,
Instagram/Messenger bot, member database, or ad manager. Only the new app listens
on port 8003. All dashboard data and login sessions stay in `wa_dm/data/`.

The computer must stay on and connected. The launcher keeps the server running
after the browser closes and restarts it if it crashes. Reopening the launcher
does not start another supervisor. No automatic startup-at-login is installed.

## Replies

- Inbox follows recorded receive/send order, so clock differences between the
  phone and computer cannot put a bot reply before the customer's message.
  This also fixes existing conversations without rewriting timestamps or resending.
  The chat list shows the latest customer/bot/phone message; team notifications
  appear separately below the conversation. The WhatsApp-style layout includes
  day separators, outgoing ticks, mobile back navigation and independent scrolling.
- Expanded Hinglish FAQ answers with English/Hindi/Hinglish phrases, common spelling
  corrections, multi-topic matching and short follow-up context. No external AI/API key.
  See `FAQ.md` for example questions and replies. This remains a local rule-based bot.
- Ad CTA enquiries such as “Hello! Can I get more info on this?” receive a complete
  workshop overview. A leading `Link:`/URL header is treated as ad context. Actual
  joining-link requests still go to the team. Saying “haan” after the overview starts
  registration and sends the payment QR.
- Workshop: 25 October 2026, 3 PM IST, 3 hours; workshop ₹99, separate kit ₹1,000
  + ₹200 shipping. Seeded from the supplied brochure. All values are editable.
- Price/payment/registration questions send the supplied payment QR. Material requests
  say the list is provided after workshop purchase. The supplied PDF contains the list,
  so it is sent only when payment is manually marked verified in Inbox.
- Small talk, greetings, thanks, emoji reactions, identity questions and obvious test/noise
  messages get short local answers without team alerts. Mixed messages containing a real
  question still use normal FAQ matching/handoff.
- Unknown questions and policies needing confirmation notify **+91 9899676862** on
  WhatsApp with the client's number, exact question/caption and recent chat context.
  The client gets a short team-follow-up reply. Voice notes/photos are flagged for
  the team; their contents are not transcribed/downloaded or forwarded automatically.
  If WhatsApp hides the phone number, the alert explicitly shows the chat ID instead.
- Team alerts have their own destination, persistent outbox and delivery state in Inbox.
  A duplicate incoming ID cannot produce another alert. Pending unsent alerts survive
  reconnect for up to 24 hours. Failed/uncertain sends require manual checking.
  The team number itself never receives automated FAQ replies, preventing loops.
- New private incoming messages only. Groups, broadcasts, status updates, old
  chats and self messages are excluded. Connection must match the expected number.
- New installation starts with replies OFF. Messages while paused are recorded
  but not queued to send later. Reconnection also does not catch up old messages.
- Duplicate incoming message IDs cannot generate repeated replies. Each outgoing
  action is claimed once. An uncertain send is shown for manual checking and is
  never retried automatically, because delivery cannot be proven in that case.
- Replying from the phone pauses that conversation for 30 minutes. Per-chat pause,
  global pause, manual resume, review flags and verified-payment marking are in Inbox.
  The observed phone greeting, “Thank you for contacting Kalakriti Resin Studio!
  Please let us know how we can help you.”, is excluded from this manual-pause rule.
- `STOP`, `unsubscribe`, `band karo` or `message mat karo` stop bot replies for
  that contact. The bot never verifies payment based on a screenshot or text.
- Incoming attachment content is viewed from the WhatsApp phone app; the dashboard
  records its type/caption without downloading customer files.

## Dependencies / development

Uses the root `.venv` and installed `wa/node_modules/whatsapp-web.js` (1.34.7).
Additional Python dependency: `qrcode[pil]`. See `requirements.txt` here.

`compatibility.js` applies three process-local fixes for the current WhatsApp Web:
renamed message keys (`$1`), sent-message lookup and private media IDs. It never
edits the shared library files. Review its guarded replacements if upgrading the library.

Start manually from the project root:

```
.venv/Scripts/python.exe -m uvicorn wa_dm.app:app --host 127.0.0.1 --port 8003 --no-access-log
```

Run tests:

```
.venv/Scripts/python.exe -m unittest wa_dm.test_bot
node --test wa_dm/transport_rules.test.js wa_dm/compatibility.test.js
```

The local HTTP API checks Host and same-origin admin requests. Bridge mutations
require a fresh per-server secret passed only via the child process environment.
QR codes are generated locally; the login QR is never sent to a third-party QR
service. `Reconnect` restarts only this app's bridge. It does not log out, migrate,
delete or unlink the WhatsApp account.

This is an unofficial WhatsApp Web integration. Session breakage or account
restriction remains possible; the user chose this route after discussion.
