// Render stills at given times and run the layout audit on each.
// usage: node still.js 6.9 27.9 40.9 ...
const http = require('http'), fs = require('fs'), path = require('path');
const { chromium } = require('playwright');

const ROOT = __dirname;
const TYPES = { '.html': 'text/html', '.woff2': 'font/woff2', '.js': 'text/javascript' };

function serve() {
  const srv = http.createServer((req, res) => {
    const f = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
    if (!f.startsWith(ROOT) || !fs.existsSync(f)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(f)] || 'application/octet-stream' });
    fs.createReadStream(f).pipe(res);
  });
  return new Promise(r => srv.listen(0, '127.0.0.1', () => r(srv)));
}

async function open() {
  const srv = await serve();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  page.on('pageerror', e => console.error('PAGE ERROR', e.message));
  await page.goto(`http://127.0.0.1:${srv.address().port}/film.html`);
  await page.waitForFunction(() => window.READY === true);
  return { srv, browser, page };
}

module.exports = { open };

if (require.main === module) (async () => {
  const times = process.argv.slice(2).map(Number);
  const { srv, browser, page } = await open();
  fs.mkdirSync(path.join(ROOT, 'stills'), { recursive: true });
  for (const t of times) {
    const issues = await page.evaluate(t => { seek(t); return audit(); }, t);
    const out = path.join(ROOT, 'stills', `t${t.toFixed(2).padStart(5, '0')}.png`);
    await page.locator('#stage').screenshot({ path: out });
    console.log(`t=${t}  ${issues.length ? issues.join(' | ') : 'ok'}`);
  }
  await browser.close(); srv.close();
})();
