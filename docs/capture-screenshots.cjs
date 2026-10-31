// Capture the real dashboard with synthetic API responses; never contacts local services.
// Run from the repository root after installing wa/ dependencies.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require(require.resolve('puppeteer', { paths: [path.resolve('wa')] }));
const root = path.resolve(__dirname, '..');
const out = path.join(__dirname, 'screenshots');
const at = new Date().toISOString();
const workshop = { id: 1, name: 'Resin art workshop · Demo', date: '2026-11-08',
  started_at: at, wa_group: 'Resin workshop · Demo group',
  wa_link: 'https://chat.whatsapp.com/DEMO_INVITE_ONLY', meet_url: '', past_offer: true,
  stats: { people: 128, links: 96, tapped: 82, joined: 74, in_group: 72, unfollowed: 2, removed: 0 } };
const history = { settings: { enabled: true }, group: workshop.wa_group,
  counts: { sent: 68, already_received: 4, pending: 2, skipped: 0 },
  listener: { state: 'ready', at }, repair: {}, error: null,
  jobs: [{ who: 'Demo member A', category: 'sent', result: { result: 'sent', count: 100 }, attempts: 1, done: true },
    { who: 'Demo member B', category: 'pending', result: { why: 'not_a_member_yet' }, attempts: 1, next: Date.now() + 60000 }] };
const responses = {
  '/api/data': { workshops: [workshop], running_id: 1, rows: [], wa_only: [], ws_stats: {},
    wa: { state: 'ready', me: 'Demo admin', group: workshop.wa_group }, audit_every_h: 2, warn: {} },
  '/api/wa/history': history,
  '/api/workshops': { settings: { keyword: 'workshop', ignore_media: 0, contact: 'Demo contact' } },
};
(async () => {
  fs.mkdirSync(out, { recursive: true });
  const options = { headless: true, args: ['--no-sandbox', '--disable-gpu'],
    defaultViewport: { width: 1440, height: 1060, deviceScaleFactor: 1 } };
  if (process.env.SCREENSHOT_BROWSER) options.executablePath = process.env.SCREENSHOT_BROWSER;
  const browser = await puppeteer.launch(options);
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.setRequestInterception(true);
    page.on('request', async req => {
      const url = new URL(req.url());
      if (url.hostname !== 'screenshot.invalid') {
        // Fonts and dashboard icon styles only; block any other remote requests.
        if (['fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net', 'cdnjs.cloudflare.com'].includes(url.hostname)) return req.continue();
        return req.abort();
      }
      const routes = { '/': ['dashboard.html', 'text/html'], '/shipping-label.js': ['shipping-label.js', 'text/javascript'],
        '/shipping-logo.jpeg': ['assets/shipping-logo.jpeg', 'image/jpeg'] };
      const file = routes[url.pathname];
      if (file) return req.respond({ status: 200, contentType: file[1], body: fs.readFileSync(path.join(root, file[0])) });
      return req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[url.pathname] || {}) });
    });
    await page.goto('http://screenshot.invalid/#workshop', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#wscurrent h2');
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(out, 'workshop.png'), fullPage: true });
    await page.click('[data-tab="members"]');
    await page.evaluate(() => { document.getElementById('history-details').open = true; });
    await (await page.$('#historycard')).screenshot({ path: path.join(out, 'history-sharing.png') });
    await page.click('[data-tab="shipping"]');
    const values = { name: 'Demo Customer', address: '12 Example Road\nSample Colony, New Delhi', pin: '110001',
      'from-name': 'Demo Studio', 'from-address': '24 Studio Lane\nExample Colony, Noida',
      'from-pin': '201304', 'from-phone': '9000000000' };
    await page.evaluate(fields => {
      for (const [key, value] of Object.entries(fields)) {
        const input = document.getElementById('shipping-' + key);
        input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }, values);
    await page.click('#shipping-create');
    await page.waitForFunction(() => !document.getElementById('shipping-png').disabled);
    assert.equal(await page.$eval('#shipping-canvas', c => `${c.width}x${c.height}`), '1800x1200');
    await page.screenshot({ path: path.join(out, 'shipping-label.png'), fullPage: true });
    // Editing must invalidate the rendered download; clearing keeps sender details.
    await page.type('#shipping-name', ' updated');
    assert.equal(await page.$eval('#shipping-png', el => el.disabled), true);
    await page.click('#shipping-clear');
    assert.equal(await page.$eval('#shipping-name', el => el.value), '');
    assert.equal(await page.$eval('#shipping-from-name', el => el.value), 'Demo Studio');
    assert.deepEqual(errors, []);
    console.log('Captured 3 screenshots with sample data; shipping preview and field checks passed.');
  } finally { await browser.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
