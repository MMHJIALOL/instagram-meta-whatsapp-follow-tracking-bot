// Live WhatsApp joins/leaves for the members dashboard. Read-only: never sends, adds or removes anyone.
// Logged in as a linked device of an admin number (scan the QR shown on http://127.0.0.1:8001 once).
// Started by members_keepalive.py; writes to the dashboard over 127.0.0.1 only.
const { Client, LocalAuth } = require('whatsapp-web.js');
const path = require('path');

const DASHBOARD = 'http://127.0.0.1:8001';
const GROUP = (process.env.WA_GROUP || 'Karwa Chauth special worshop 18th Oct').toLowerCase(); // chats whose name contains this
const BROWSER = process.env.WA_BROWSER || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const CATCH_UP = 500; // on start, re-read this many recent messages per chat to pick up joins missed while offline

const log = (...a) => console.log(new Date().toISOString(), ...a);

async function post(route, body) {
  try {
    await fetch(DASHBOARD + route, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-local': '1' }, body: JSON.stringify(body),
    });
  } catch (e) {
    log('dashboard unreachable:', e.message);
  }
}
let status = { state: 'starting' };
const setStatus = (s) => { status = s; return post('/api/wa/status', s); };

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(__dirname, '.wwebjs_auth') }),
  puppeteer: { headless: true, executablePath: BROWSER, args: ['--no-sandbox'] },
});

// With a saved login, WhatsApp Web reloads itself right after opening, which kills the library's setup
// script mid-way ("Execution context was destroyed"); wait for the reload and try again.
const inject = client.inject.bind(client);
client.inject = async () => {
  for (let i = 1; ; i++) {
    try {
      return await inject();
    } catch (e) {
      if (i >= 15 || !/context was destroyed|navigation/i.test(String((e && e.message) || e))) throw e;
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
};

const idOf = (x) => (typeof x === 'object' && x ? x._serialized : x);

async function number(id) {
  // ids are '919…@c.us' (phone) or '…@lid' (WhatsApp's privacy id); turn lids into phone numbers when allowed
  if (id.endsWith('@c.us')) return '+' + id.split('@')[0];
  try {
    const [r] = await client.getContactLidAndPhone([id]);
    if (r && r.pn) return '+' + r.pn.split('@')[0];
  } catch (e) { /* older WhatsApp Web build */ }
  try {
    const c = await client.getContactById(id);
    if (c.number) return '+' + c.number;
  } catch (e) { /* unknown contact */ }
  return id; // hidden id; stored as-is so it still shows up
}

// client.getChats() crashes on some chats in current WhatsApp Web builds, so read just ids and names
async function groupChats() {
  return client.pupPage.evaluate(() => window.require('WAWebCollections').Chat.getModelsArray()
    .filter((c) => c.id && c.id.server === 'g.us')
    .map((c) => ({ id: c.id._serialized, name: c.formattedTitle || c.name || (c.groupMetadata && c.groupMetadata.subject) || '' })));
}

const chatNames = new Map();
async function chatName(chatId) {
  if (!chatNames.has(chatId)) {
    try { for (const c of await groupChats()) chatNames.set(c.id, c.name); } catch (e) { return ''; }
  }
  return chatNames.get(chatId) || '';
}

async function report(chatId, subtype, recipients, author, t) {
  const kind = ['add', 'invite', 'linked_group_join'].includes(subtype) ? 'join'
    : ['leave', 'remove'].includes(subtype) ? 'leave' : null;
  if (!kind) return;
  const chat = await chatName(chatId);
  if (!chat.toLowerCase().includes(GROUP)) {
    log(`ignored ${kind} in "${chat}"`);
    return;
  }
  const people = (recipients && recipients.length ? recipients : [author]).map(idOf).filter(Boolean);
  for (const p of people) {
    const who = await number(p);
    log(kind, who, `"${chat}"`);
    await post('/api/wa/event', { who, kind, at: t, chat });
  }
}

// WhatsApp profile names of group members, for the dashboard's name check. WhatsApp Web only knows them for
// people who've posted something; read from its memory, no extra requests to WhatsApp.
async function postNames() {
  try {
    const people = await client.pupPage.evaluate((group) => {
      const C = window.require('WAWebCollections');
      const out = {};
      for (const g of C.Chat.getModelsArray()) {
        if (!g.id || g.id.server !== 'g.us' || !(g.formattedTitle || '').toLowerCase().includes(group)) continue;
        const parts = g.groupMetadata && g.groupMetadata.participants ? g.groupMetadata.participants.getModelsArray() : [];
        for (const p of parts) {
          const c = C.Contact.get(p.id._serialized);
          const pn = c && c.phoneNumber ? c.phoneNumber._serialized : p.id._serialized;
          if (c && pn.endsWith('@c.us') && (c.pushname || c.username)) {
            out[pn] = { who: '+' + pn.split('@')[0], name: c.pushname || '', username: c.username || '' };
          }
        }
      }
      return Object.values(out);
    }, GROUP);
    if (people.length) await post('/api/wa/names', { people });
  } catch (e) {
    log('reading names failed:', e.message);
  }
}

client.on('qr', (qr) => { log('scan the QR on the dashboard'); setStatus({ state: 'qr', qr }); });
client.on('authenticated', () => setStatus({ state: 'authenticated' }));
client.on('auth_failure', (m) => setStatus({ state: 'auth_failure', detail: String(m) }));
client.on('disconnected', (r) => { setStatus({ state: 'disconnected', detail: String(r) }); process.exit(1); });

client.on('group_join', (n) => report(n.chatId, n.type, n.recipientIds, n.author, n.timestamp));
client.on('group_leave', (n) => report(n.chatId, n.type, n.recipientIds, n.author, n.timestamp));

client.on('ready', async () => {
  const me = client.info && client.info.wid ? '+' + client.info.wid.user : '';
  let chats = [], all = [];
  try {
    all = await groupChats();
    chats = all.filter((c) => c.name.toLowerCase().includes(GROUP));
  } catch (e) {
    log('could not list chats:', e.message);
  }
  // community subgroups show up here too; when nothing matches, list every group name so the right one can be picked
  const seen = all.map((c) => c.name).filter(Boolean).sort().join(' | ');
  log('ready as', me, 'watching:', chats.map((c) => c.name).join(', ') || 'NO MATCHING GROUP', '| groups seen:', seen);
  await setStatus({ state: 'ready', me, group: chats.map((c) => c.name).join(', ') || `no chat named "${GROUP}". Groups seen: ${seen}` });
  for (const { id, name } of chats) { // catch up on joins/leaves while the listener was off (the dashboard skips duplicates)
    try {
      // getChatById/fetchMessages crash like getChats, so read the chat's loaded messages directly
      const msgs = await client.pupPage.evaluate((id, limit) => {
        const chat = window.require('WAWebCollections').Chat.get(id);
        const all = chat && chat.msgs ? chat.msgs.getModelsArray() : [];
        const ser = (x) => (x && x._serialized) || x;
        return all.filter((m) => m.type === 'gp2').slice(-limit).map((m) => ({
          subtype: m.subtype, recipients: (m.recipients || []).map(ser), author: ser(m.author), t: m.t }));
      }, id, CATCH_UP);
      let n = 0;
      for (const m of msgs) {
        await report(id, m.subtype, m.recipients, m.author, m.t);
        n++;
      }
      log(`caught up ${n} group events in "${name}"`);
    } catch (e) {
      log('catch-up failed for', name, e.message);
    }
  }
  await postNames();
});

setInterval(() => { if (status.state === 'ready') setStatus(status); }, 60000); // heartbeat for the dashboard
setInterval(() => { if (status.state === 'ready') postNames(); }, 5 * 60000);

// WhatsApp Web can freeze while the heartbeat above keeps saying "ready" (2026-10-07: 4h of joins lost).
// Ping the page every minute; after 3 misses in a row, exit so members_keepalive starts a fresh one.
let misses = 0;
setInterval(async () => {
  if (status.state !== 'ready' || !client.pupPage) return;
  try {
    await Promise.race([
      client.pupPage.evaluate(() => 1),
      new Promise((_, rej) => setTimeout(() => rej(new Error('no answer in 30s')), 30000)),
    ]);
    misses = 0;
  } catch (e) {
    misses++;
    log(`page check failed (${misses}/3):`, e.message);
    if (misses >= 3) {
      await setStatus({ state: 'disconnected', detail: 'WhatsApp Web froze, restarting' });
      try { client.pupBrowser.process().kill('SIGKILL'); } catch (_) { /* already gone */ }
      process.exit(1);
    }
  }
}, 60000);

process.on('unhandledRejection', (e) => log('error:', (e && e.stack) || e));
log('starting');
setStatus({ state: 'starting', detail: 'Opening WhatsApp Web…' });
client.initialize().catch((e) => { log("failed to start:", e.stack); setStatus({ state: 'disconnected', detail: e.message }); process.exit(1); });
