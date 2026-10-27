# Dashboard upgrade: warnings, workshops, editable messages, activity

Spec for four additions to the members dashboard (http://127.0.0.1:8001) and the bot behind it.
Written 2026-10-20, while the 22 Oct workshop is running and the bot is live.

| # | Feature | In one line |
|---|---------|-------------|
| 1 | Unfollower warnings | A **Send warning** button on each "To remove" person (and **Warn all**) that DMs them "you unfollowed, follow again or you'll be removed from the group" |
| 2 | Workshops | **End workshop** / **Start next workshop** (a 3-step wizard), **Go live** with a class link, and a history of past workshops. Each workshop has its own group, link, messages and counters; nobody's data is deleted |
| 3 | Bot messages | Every message the bot can send, **what triggers it**, and an editor with a phone preview. Question keywords are editable too, you can add your own questions, and there's a tester |
| 4 | Activity | A live feed of everything the bot does (every DM in and out, link taps, WhatsApp joins, unfollows, failures), plus the state of every background job |

The page gets four tabs: **Members** (what's there today, plus the warning buttons), **Workshop**, **Messages**, **Activity**.

---

## 0. How things work today (what the design has to respect)

- `main.py` is the bot (port 8000, behind the Cloudflare tunnel). It's started by `keepalive.py`. Its texts are
  Python constants (`INTRO_MSG`, `FOLLOW_MSG`, ...), so a text change needs a code edit plus a restart.
- `dashboard.py` + `dashboard.html` is this dashboard (127.0.0.1 only, every API call needs the `x-local: 1` header).
  It imports `main`, so it can already send DMs through the bot's functions (that's how "Check missed DMs" works).
- What makes it the 22 Oct workshop is spread over `.env` and code:
  `WORKSHOP_SINCE` (who's current, who's a past attendee), `WORKSHOP_LINK` (where `/j/` links go), `WA_MAIN_CHAT`
  (which WhatsApp group counts), `MEET_URL` (live mode), `dashboard.PAST_MAIN_CHATS` and its copy in
  `wa/listener.js`, and the dates written into about eight messages. Switching workshops meant editing all of these
  and restarting three processes. Memory notes from 18 Oct record two outages that came from missing one of them.
- **Instagram's 24-hour rule.** A business account can only DM someone within 24 h of that person's last message
  to it. Outside the window Instagram refuses with error code 10 / subcode **2534022**. The bot already hit this
  twice (uvicorn.log). Subcode **2534014** means the user can't be found (deleted account or blocked us).
  This matters for unfollowers: most of them last messaged days ago.
- Facebook gives no way to check Page follows, so unfollowers only exist on the Instagram side.
- The bot never removes anyone from WhatsApp. Removing stays a manual step on the phone.

---

## 1. Unfollower warnings

### What you see
Each row in the red **To remove** card (Members tab) gets:

- **Send warning** button. After sending it changes to `Warned 2 h ago` with a small **Send again** link.
- A line under the name saying whether Instagram will let the message through:
  - `Can message for 9 more h`: they DMed the bot within the last 24 h.
  - `Last DM 3 d ago: Instagram will probably refuse (24 h rule)`: the button turns into **Try anyway**.
    It's a secondary button, because Instagram is the one that decides and we only know about the DMs the bot saw.
- **Re-check**: looks up their follow status right now, instead of waiting for the 2-hourly check.
- After a warning: `Warned 5 h ago · still not following`. After 24 h with no re-follow the pill turns red:
  `No response in 24 h`. Re-followers drop out of the card on their own.
- If sending failed, the reason in plain words: `Couldn't send: 24 h window closed` or `Couldn't send: account not found`.

At the top of the card: **Warn all (N)**. It sends to everyone in the card not yet warned in this workshop,
2 s apart, with a progress counter (`Warning 3/7…`). It asks before sending.

### What the person gets
`UNFOLLOW_WARN_MSG` with the **Visit Profile** and **I'm following ✅** buttons (editable in the Messages tab):

> Hi! 🙏 Humne dekha ki aapne hamara Instagram account unfollow kar diya hai.
>
> Workshop ka WhatsApp group sirf followers ke liye hai. Agar aap wapas follow nahi karte, toh aapko group se remove kar diya jayega 😕
>
> Wapas follow karke neeche "I'm following ✅" dabao 👇

### What happens next
- They follow again and tap **I'm following ✅** (or write DONE). The bot re-checks with the same 2 s/3 s retries as
  for the link. If the follow is there: `REFOLLOW_MSG` ("Thank you! 🙏 Follow wapas mil gaya ✅ ...") goes out,
  `unfollowed_at` is cleared, and they leave the To remove card. If not: the usual "Follow detect nahi hua" message
  with buttons.
  Today a link holder tapping that button gets **no reply at all**, so this reply is new. It only applies to people
  warned in the current workshop.
- They re-follow without tapping: the 2-hourly unfollow check (or **Re-check**) notices and clears them.
- They don't re-follow: they stay in the card marked "No response", and you remove them on the phone and tick
  **Removed**, same as today.

### Data
New `users` columns: `warned_at` (last successful warning), `warn_tried_at`, `warn_error` (last failure reason).
A warning only counts for the workshop it was sent in (`warned_at >= workshop start`).

### API
- `POST /api/warn` `{igsids: [...]}`: starts a background send and returns at once. Progress comes back in
  `/api/data` → `warn`. Each row in `/api/data` also has `warned_at`, `warn_tried_at`, `warn_error` and
  `last_msg_at`.
- `POST /api/follow/{igsid}`: re-checks one person's follow status now.

### Not included
- **Automatic** warnings. You asked for a button. An "auto-warn when an unfollow is detected" switch would be
  easy to add later.
- Messages outside the 24 h window. Meta's `HUMAN_AGENT` tag allows 7 days, but it needs App Review, and it's only
  for replying to a question someone asked. A warning isn't that.

---

## 2. Workshops

### The idea
A **workshop** is a stretch of time with its own settings. Everything that used to be scattered over `.env` and
code now belongs to it:

| Setting | Was | Now |
|---|---|---|
| Name (e.g. "22 Oct workshop") | none | Workshop tab |
| Class date | written into messages | Workshop tab (also used to spot old dates in messages) |
| Start: who counts as current, who's a past attendee | `.env WORKSHOP_SINCE` | set when you start it |
| WhatsApp invite link (`/j/` links forward here) | `.env WORKSHOP_LINK` | Workshop tab |
| WhatsApp group name (for join matching + listener) | `.env WA_MAIN_CHAT`, `PAST_MAIN_CHATS`, `listener.js PAST` | Workshop tab. Earlier workshops' groups stay watched automatically |
| Live class link (live mode) | `.env MEET_URL` + restart | **Go live** / **End live** buttons, no restart |
| Offer for people from earlier workshops | `PAST_MSG`, always on | on/off switch + editable text |
| Every bot message and question keyword | Python constants | Messages tab, saved per workshop |

`.env` keeps the secrets and the bot-wide settings: `KEYWORD`, `START_AFTER`, `CONTACT_NUMBER`, `IGNORE_MEDIA`.
The Workshop tab shows these read-only. The `.env` workshop values are only read once, to set up the first
workshop.

All changes apply **within a few seconds**, without restarting anything. The bot reads the workshop settings from
the database, cached for 2 s.

### Workshop tab
**Current workshop card**
- Name, a status pill (`Running` · `Live now` · `No workshop running`), and the class date with a countdown ("in 2 days").
- Editable fields with **Save**: name, class date, WhatsApp group name, WhatsApp invite link, past-attendee offer switch.
- Numbers for this workshop: people, got link, tapped link, joined WhatsApp, unfollowed, removed.
- Buttons:
  - **Go live**: paste the Google Meet link. From then on followers get the Meet link, the welcome and "when"
    answers switch to their live versions, and reminders stop (the existing live mode). **End live** clears it.
  - **End workshop** (see "Between workshops" below).
  - **Start next workshop** opens the wizard.

**Start next workshop wizard** (3 steps; nothing changes until the last button)
1. **Basics**: name, class date, WhatsApp group name (exactly as in WhatsApp), invite link, and the offer for
   earlier workshops' people (switch + text, prefilled from the current one).
2. **Messages**: every message, copied from the current workshop. The ones that still mention the old date (e.g.
   "22 Oct", "22 October", "Thursday") are flagged at the top. There's a **Find & replace** bar
   ("22 Oct" → "29 Oct", with a count of the messages it hits), and each message can be edited directly.
3. **Review**: a plain list of what's about to happen, then **Start "<name>"**:
   - The current workshop is closed and its numbers are frozen. Its people move under "Previous workshops".
     Nothing is deleted.
   - The bot uses the new messages from the next DM on.
   - Everyone from earlier workshops is a past attendee. They go through the follow step again, then get the
     offer (if switched on) and their new link. Their old link shows the "pichle workshop ka tha" page until then
     (same as today).
   - The WhatsApp listener starts watching the new group within a minute. Older groups stay watched, so leaves
     still count.

**Previous workshops**: a list (newest first). Each one is collapsible and shows its dates, group and numbers.
"Show people" jumps to the Members tab filtered to that workshop. The last one gets a **Reopen** button, in case
you ended it by mistake.

### What "fresh" means: counters that reset per workshop
| Counter | Rule now (per workshop) |
|---|---|
| Join link (`sent`) | Already the case: a link from an earlier workshop doesn't count (past attendee flow) |
| Welcome message (`INTRO_MSG` on the first DM) | First DM **in this workshop**. Returning people get the new workshop's intro instead of the bare steps |
| Follow reminder / link reminder (one per person) | One per person **per workshop** |
| Meet link while live | Once per person **per workshop** |
| Warnings | Per workshop |
| To remove / Check | Only people of the running workshop (as today) |
| Used-up links | Only joins into the running workshop's group (as today) |

**Kept forever:** every user, link, tap, WhatsApp join and leave, match you settled by hand, the removed ticks,
the activity history, and each closed workshop's frozen numbers.

### Between workshops
After **End workshop**, if you haven't started the next one yet:
- Any DM or comment gets `BETWEEN_MSG` (editable), at most once a day per person:
  "Abhi koi workshop scheduled nahi hai 🙏 Agla workshop jaldi announce hoga! Profile follow karke rakho..."
  with a **Visit Profile** button. No follow check and no link.
- Reminders and the Meet link stop.
- `/j/` links still forward to the last workshop's group (people may want the recording).
- The To remove card is empty, because old workshops' unfollowers are ignored (as today).

### Members tab
Rows are grouped by workshop. The running workshop is on top, and each earlier workshop has its own collapsible
line (`▼ 18 Oct workshop (ended 18 Oct) · 845 people`), replacing today's single "Previous workshop" line.
A person belongs to the workshop their latest activity falls in (link, tap, DM, join). The tiles count the running
workshop.

### Setting up history
The first start of the new dashboard creates the workshop list from what's known:
- **11 Oct workshop**: everything before 12 Oct 17:50 IST (its group isn't tracked for matching, same as today).
- **18 Oct workshop**: 12 Oct 17:50 to 18 Oct 22:17 IST, group "Karwa Chauth special worshop 18th Oct".
- **22 Oct workshop** (running): from `WORKSHOP_SINCE`, with today's `WORKSHOP_LINK`, `WA_MAIN_CHAT` and `MEET_URL`.
  Its messages are exactly the ones in `main.py` today, so nothing the bot sends changes.

### Data
```sql
workshops(id, name, date, started_at, closed_at,   -- closed_at NULL = running
          wa_group, wa_link, meet_url, past_offer,
          texts JSON,   -- edited messages {KEY: text}; missing keys = main.py default
          faqs JSON,    -- question list [{id, label, keywords[], on}], NULL = defaults
          stats JSON)   -- numbers frozen when it closed
```
Rules: at most one row has `closed_at IS NULL`, and they're ordered by `started_at`. `texts` holds only the
messages that differ from main.py's defaults. With no rows at all (fresh
install, offline tests) the bot falls back to `.env` exactly as before.

### API
- `GET /api/workshops`: the list, plus the bot-wide settings.
- `POST /api/workshops/{id}`: edit fields. `meet_url` set means Go live; `meet_url` empty means End live.
- `POST /api/workshops`: start the next workshop (closes the running one).
- `POST /api/workshops-end` / `POST /api/workshops-reopen`.
- `GET /api/wa/groups`: for the listener.
- `/api/data` also returns `workshops`, `running_id`, `ws_stats` and each row's `ws`.

---

## 3. Bot messages (Messages tab)

### Layout
- A line at the top: "Texts for **22 Oct workshop** · changes go live within a few seconds · Messenger gets the
  same texts with Profile → Page".
- **What starts the bot**, read-only: every DM (KEYWORD `*`), comments on ads and on posts after START_AFTER,
  story replies, DMs from ads, and "DONE".
- Message cards, in groups. Each card has:
  - Its name and **when it's sent**, in plain words.
  - Instagram/Messenger chips.
  - The buttons it comes with.
  - A text box with a character/byte counter.
  - A phone-style preview bubble with the buttons under it.
  - **Save** / **Undo**.
- **Try a message**: type what someone might DM and see which answers would fire.

### Every message and its trigger
| Group | Message | Sent when | Buttons |
|---|---|---|---|
| First contact | `INTRO_MSG` Welcome | Their first DM / button tap in this workshop (plain text before the buttons) | none |
| | `INTRO_LIVE_MSG` Welcome while live | Same, while a class link is set | none |
| | `RULES_MSG` 3 steps | Before the follow buttons when they've DMed before (max once a day) | none |
| | `OPENER_MSG` | Follow status unknown yet (Instagram shows it after the first interaction), also the comment private reply | Visit Profile · I'm following ✅ |
| | `FOLLOW_MSG` Follow request | They don't follow yet | Visit Profile · I'm following ✅ |
| | `FOLLOW_TEXT` | Instagram refused the buttons (rare): plain-text fallback | none |
| | `COMMENT_REPLY` | Public reply under their comment | none |
| Follow check | `NOT_FOLLOWING_MSG` | Tapped I'm following ✅ / wrote DONE, but no follow found | Visit Profile · I'm following ✅ |
| | `NOT_FOLLOWING_TEXT` | Fallback of the above | none |
| Join link | `LINK_MSG` ({link} = their personal link) | They follow → their WhatsApp link | none |
| | `LINK_BUTTON_MSG` | Right after, the same link as a button | Join WhatsApp group |
| | `PAST_MSG` | People from an earlier workshop, once, before their new link (offer switch on) | none |
| Questions | `WHEN_MSG` / `WHEN_LIVE_MSG` | DM contains when/date/time/kab/कब... | none |
| | `RECORDING_MSG` | record*/replay/baad mein dekh*/रिकॉर्ड | none |
| | `MATERIAL_MSG` (+ `MATERIAL_LINK_MSG` {link} for link holders) | material/saman/list/सामान... | none |
| | `FREE_MSG` | free/fees/cost/price/फ्री... (skipped if the material answer already went) | none |
| | your own questions | your keywords | none |
| Reminders (the bot sends these on its own) | `REMIND_MSG` | 3 h after the follow request, still not following, inside 24 h, once per workshop | Visit Profile · I'm following ✅ |
| | `LINK_REMIND_MSG` ({link}) | 3 h after the link with no real tap, inside 24 h, once per workshop | none |
| Live class | `MEET_MSG` ({link} = class link) | While live: followers / link holders, once per workshop | none |
| | `MEET_BUTTON_MSG` | Right after, as a button | Join workshop 🔴 |
| Unfollowers | `UNFOLLOW_WARN_MSG` | You press Send warning | Visit Profile · I'm following ✅ |
| | `REFOLLOW_MSG` | A warned person follows again and taps I'm following ✅ | none |
| Between workshops | `BETWEEN_MSG` | No workshop running: any DM or comment, once a day | Visit Profile |
| Link pages (web) | `USED_LINK_PAGE_TEXT` | Their link is opened after they joined (first line = heading) | n/a |
| | `PAST_LINK_PAGE_TEXT` | An earlier workshop's link is opened before they got the new one | n/a |
| Buttons | `VISIT_TITLE`, `FOLLOWING_TITLE`, `JOIN_TITLE`, `MEET_TITLE` | Button labels (max 20 characters) | n/a |

### Rules the editor enforces
- No empty messages.
- Link messages must contain `{link}`. It's filled in per person. The old `{}` still works.
- Instagram limits: plain text ≤ 1000 bytes, text above buttons ≤ 640 characters, button titles ≤ 20 characters
  on one line.
- Button titles must still appear in the welcome and 3-steps texts. The editor warns you if you rename a button
  but not the steps that mention it.

### Questions (FAQ)
- Each question is a list of keywords plus an answer, with an on/off switch.
- A keyword matches as a whole word, case doesn't matter, and `*` at the end matches any ending (`record*` →
  recording). Hindi keywords match anywhere in the message.
- The four built-in questions keep their exact behaviour. The regexes become plain word lists (`materials?` →
  material, materials), and the existing tests check every example phrase still matches.
- **+ Add a question** creates your own (e.g. "location", "certificate"). It goes out like the built-in answers,
  before the follow step.

### Storage
Saved into the current workshop's `texts`/`faqs`. Between workshops, edits go to the last workshop, which is where
`BETWEEN_MSG` is read from. The bot reads `text(KEY)` = the workshop's version, or the `main.py` default.

---

## 4. Activity (Activity tab)

### Live feed
Newest first, updates every few seconds. Filters: **All · In · Out · Links & WhatsApp · Follows · Background ·
Problems**, a search box, and a person filter. The clock icon on any member opens the feed for just that person.

| Kind | Examples |
|---|---|
| In | `DM from @asha: "workshop kab hai?"` · comment on an ad · button tap · story reply · Messenger message |
| Out | `→ @asha: When is it? (answer)` with the full text when expanded and the message name it matched |
| Skip | `@asha has the link already: no reply` · `comment on an old post ignored` |
| Problems | `Couldn't send to @x: 24 h window closed` · `account not found` · other API errors |
| Links | `@asha opened her link` (real taps only, scanners left out) · used-link page · old-link page |
| WhatsApp | `+91 98… joined "22 Oct …"` · left · `matched to @asha (sure)` |
| Follows | `@x unfollowed` · `@x followed again` · `warning sent` |
| Background | `Unfollow check: 1187 checked, 3 unfollowed` · `Follow re-check: 12 waiting, 1 link sent, 2 reminders` |
| You | workshop started / ended / went live · message edited · match settled · marked removed · bot restarted |

Stored in an `activity` table (`id, at, kind, who, channel, summary, text, detail JSON`), written by both the bot
and the dashboard. Names are looked up when the feed is shown, so a username that changes later shows correctly.

### Background jobs
A card per job: what it does, how often, last run, next run, its result, a progress bar while it runs, and a red
note if it failed or the process behind it is down.

| Job | Process | Every |
|---|---|---|
| Follow re-check + reminders (Instagram, Messenger) | bot | 10 min |
| Unfollow check (everyone with a link) | bot | 2 h (takes ~40 min) |
| Matching, used-up links, popups | dashboard | 1 min |
| Instagram name lookup | dashboard | 5 min |
| Bot & tunnel health | dashboard | 30 s |
| WhatsApp listener | listener | heartbeat every minute |
| Warnings being sent | dashboard | when you press Warn |
| Keep laptop awake | keep_awake.ps1 | 30 min |

Stored in a `jobs` table (`name, started_at, ended_at, running, progress, total, result, error`).

---

## 5. Files that change

| File | Change |
|---|---|
| `bot_messages.py` (new) | Message catalog (name, group, trigger, buttons, limits), default question list, keyword → regex |
| `main.py` | `workshop()` / `text()` / `meet_url()` / `workshop_link()` read from the DB (2 s cache, `.env` fallback). Per-workshop counters. Between-workshops mode. Warning reply. `activity()` + `jobs` records. Failure reason kept from API errors |
| `messenger.py` | Same texts and workshop settings through `main`. Between-workshops mode. Activity records |
| `dashboard.py` | Workshop list + seeding, rows grouped by workshop, warning sender, follow re-check, messages/FAQ API with validation, activity/jobs API, groups for the listener |
| `dashboard.html` | Tabs, warning UI, Workshop tab + wizard, Messages tab, Activity tab |
| `wa/listener.js` | Asks the dashboard which groups to watch every minute (falls back to `.env`), so a new workshop needs no restart |
| `backfill.py` | Uses the question matcher's ids instead of comparing texts |
| `seed_workshops.py` (new, one-off) | Writes the 11/18/22 Oct history into the workshop list. Already run on bot.db on 2026-10-20 |
| `test_workshops.py` (new) | 65 offline checks for all of the above |
| `nudge.py`, `test_filters.py` | Use `main.text()`; test_filters now uses a throwaway database from the start |

## 6. Testing
- The existing offline tests (`test_filters`, `test_past_attendees`, `test_messenger`, `test_used_links`,
  `test_matching`) must pass unchanged. They use a fresh database with no workshop rows, so they also check the
  `.env` fallback.
- New `test_workshops.py`:
  - edited texts reach the DMs;
  - `{link}` is filled in;
  - FAQ keyword edits and custom questions work;
  - start next workshop: past attendees and per-workshop reminders, intro and Meet link;
  - between-workshops mode, go live / end live without a restart;
  - warning: sending, the 24 h refusal, and the re-follow reply;
  - activity rows written;
  - seeding a database that has no workshops;
  - message validation.
- The dashboard is checked in the browser: every tab, a warning sent to a test id (stubbed), the wizard run
  against a copy of the database, and dark mode plus phone width.

## 7. Rollout
1. Run all tests.
2. Restart the **dashboard** (members_keepalive brings it back in ~10 s). It sets up the workshop list on start.
3. Restart the **bot**: only the `uvicorn main:app` processes. keepalive restarts them in ~3 s and the tunnel
   address stays the same, so nothing needs changing in Meta.
4. Restart the **WhatsApp listener** once for the new group polling (~40 s to reconnect; it catches up on missed
   joins).
5. Watch uvicorn.log and the Activity tab for the next real DMs.

Rollback: the database changes only add things (new tables and columns). Reverting the code files brings back
today's behaviour from `.env`.

**Done 2026-10-20 ~01:20 IST.** Tests passed, the history was seeded, and the dashboard, the bot and the listener
were restarted (the tunnel address didn't change). Real DMs showed up in the Activity tab within a minute.

---

## 8. Custom replies (added 2026-10-20)
Messages tab → **Custom replies** (top of the tab) → **Add custom reply**:
- **Name**: only you see it.
- **Words to watch for**: same rules as the questions. Whole words, any case, `*` at the end matches any ending,
  and Hindi matches anywhere.
- **The reply**.
- **Optional link button**: a title of at most 20 characters, plus an https:// link.
- **After this reply**: *carry on as usual* (the follow step, then their link) or *stop here* (only this reply).

Custom replies work in Instagram and Messenger DMs, and between workshops too. Comments still get the usual reply.
The "Try a message" box shows what a DM would get. Custom replies are stored with the questions (ids `faq_…`,
plus `button_title`, `button_url` and `stop`), and Start next workshop carries them over. Rules are in
`bot_messages.check_custom`, and they're checked in `test_workshops.py`.
