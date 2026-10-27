# 31 October free workshop campaign

Created through the existing Meta API integration; verified in the ad dashboard.

- Campaign: 120255353801660581
- Ad set: 120255353803050581
- Ad: 120255353812750581
- Source campaign: 120255227520700581 (22nd oct karwa chauth free)
- Status: PAUSED at all three levels; no spend authorized by this script.
- Budget: INR 400/day; India, ages 18–65+, Instagram placements, conversations optimization.
- Stop: 31 October 2026, 12:00 IST. Class: 12:00–15:00 IST.
- Ad created / bot workshop started: 26 October 2026, 23:55:17 IST.
- 4:5 feeds, 9:16 Stories/Reels, 1:1 default (including search). Original supplied PNGs uploaded unchanged.
- Questions: Workshop kab hai? / Kya ye free hai? / Recording available hogi?
- Workshop: 31st Diwali Special Workshop. The supplied new invite is configured in dashboard workshop 4.

The recording reply confirms availability and directs people to the WhatsApp group for details. Old dated reminders and expired paid-workshop offers are removed from this workshop.

## Join link improvements

Known personal group links now show a mobile page with a deliberate Open WhatsApp tap, HTTPS invite fallback, copy link, WhatsApp Web, and in-app browser instructions. Used links retain a recovery option without recording a misleading extra matching click. Old-workshop links remain gated until the user gets the new workshop link. Invalid codes return 404. Link pages use no-store and no-referrer headers.

The link itself does not extract a phone number. The dashboard correlates real link taps with WhatsApp membership events; ambiguous matches remain ambiguous. The temporary tunnel can still change or go offline. PUBLIC_LINK_BASE allows a permanent address once an always-on host/named tunnel is provisioned. Do not set it to a hostname that does not serve this bot.

## Verification

Offline checks passed: test_filters, test_campaign_start, test_used_links, test_past_attendees, test_workshops, test_reels, test_messenger. Tests use temporary databases and fake sends. Meta readback confirmed dates, INR 400 budget, original targeting, all three assets and placement labels, and the three exact questions. Feed and Reels previews were inspected in the existing dashboard. The live dashboard recording-answer tester returns the new answer.

The user updated Instagram webhook settings; the local dashboard observed Meta verification at 27 October 2026, 00:01:27 IST. No real customer test DM was sent by the agent.

## Delivery blockers observed

Meta reports prepaid balance INR 0 and account spend cap INR 10,511.95 fully consumed. The campaign is paused. WhatsApp listener readiness must be checked separately before relying on automatic phone-number matching.
