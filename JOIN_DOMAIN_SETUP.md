# Branded workshop join service

Hosted service: https://kalakriti-workshop-join.nimblepond4.chatgpt.site

Desired public link: `https://join.kalakritistudio.com/j/<personal-code>`

Site project: `appgprj_6adfa58a05c88191a922fa37d47007de`
Version 1 commit: `b2953fa71ccd4423731034aaac5891929d5dd272`
Deployment: `appgdep_6adfa6ff40b081919f4fcc029fa94754` (succeeded)
Domain: `appgdom_6adfa7194d708191aa008f7e9775d65a` (active; HTTPS active)

## GoDaddy DNS records

Add these records in the kalakritistudio.com zone. Do not change apex, www, mail, or nameservers.

| Type | Name | Value |
| --- | --- | --- |
| CNAME | join | custom-domains.chatgpt.site |
| TXT | _openai-site-verification.join | openai-site-verification=-4n3U0Pt0oFqGEaZZWRZITDDeUIVEEb_4dl24Nj03go |
| TXT | _cf-custom-hostname.join | 092d4766-5230-46f7-8034-a2ca2eb26b8c |

Use the default TTL. Wait for the Sites domain and certificate to become active, and verify HTTPS before enabling the new base.

## Cutover

`JOIN_SERVICE_BASE` and secret `JOIN_SYNC_TOKEN` are configured in the local ignored .env. The same secret is stored in Sites runtime secrets. Never commit or print it.

Cutover completed on 27 October 2026 (IST). DNS, TLS, branded redirect and automatic background click import verified; synthetic test data removed. The bot is running with the new base.

For future cutovers, set `PUBLIC_LINK_BASE=https://join.kalakritistudio.com` in .env. Restart only the main:app uvicorn process; keepalive restarts it. Do not restart cloudflared, since that changes the Instagram webhook address. Verify a synthetic link and its imported event before reporting completion.

`join_sync.py` registers codes, refreshes link states every minute, and imports cloud events every ten seconds while the bot runs. Cursor updates and imports are atomic. Local identity mappings never leave the dashboard. The cloud stores invite codes, destination, state, timestamp, user agent, and IP for existing matching logic. It does not receive Instagram IDs or phone numbers. Old QA events without a local identity are skipped.

Valid links use an uncached HTTPS 302 redirect. Preview requests and HEAD requests do not create clicks. Used links continue to open the invite without adding matching events. Expired links return 410. Failed new-link registration falls back to the direct WhatsApp invite, sacrificing tracking to keep joining possible. Click recording failure also preserves the redirect where the destination remains readable.

The custom domain removes reliance on the temporary tunnel for customer link clicks. Bot messages and the WhatsApp listener still require the existing local processes. The redirect cannot extract a phone number: number matching still depends on WhatsApp join events, and is not guaranteed attribution.

## Validation

- Service unit test passed: authorized writes, unauthorized reads rejected, safe destinations, durable event, preview/HEAD filtering, used and expired links.
- Python import test passed: correct local identity, atomic cursor, replay without duplicate clicks.
- Live service: health 200, unauthenticated event API 401, registered test redirect 302 to the correct community, event recorded, test invite then expired (410).
- Existing used-link and past-attendee suites passed when run as standalone scripts. Reel suite has one unrelated disabled-reel fixture failure; do not claim all suites passed.
- User added GoDaddy records. Authoritative/public DNS and Sites validation passed. Live branded HTTPS redirect and running-bot click import passed. Browser visual QA remains unavailable.

Rollback: unset PUBLIC_LINK_BASE and restart only the bot process. Existing branded links remain served by the hosted service.
