// Live WhatsApp joins/leaves for the members dashboard. Never adds or removes anyone; the only thing it sends is
// the current workshop group's message history to people who just joined (what the "Share message history"
// button under their join does), see shareHistory below.
// Logged in as a linked device of an admin number (scan the QR shown on http://127.0.0.1:8001 once).
// Started by members_keepalive.py; writes to the dashboard over 127.0.0.1 only.
const { Client, LocalAuth } = require('whatsapp-web.js');
const path = require('path');

const DASHBOARD = 'http://127.0.0.1:8001';
// Watch the same groups the dashboard counts as main: WA_MAIN_CHAT from ../.env (so switching workshops there
// switches the listener too) plus past workshops' groups, whose leaves still matter. Keep PAST in sync with
// dashboard.PAST_MAIN_CHATS.
function envVar(name) {
  if (process.env[name]) return process.env[name];
  try {
    const m = require('fs').readFileSync(path.join(__dirname, '..', '.env'), 'utf8').match(new RegExp(`^${name}=(.*)$`, 'm'));
    return m ? m[1].trim().replace(/^["']|["']$/g, '') : '';
  } catch (e) { return ''; }
}
const PAST = ['Karwa Chauth special worshop 18th Oct'];
// Until the dashboard answers (and if it has no workshops set up): the groups above. After that, every minute,
// the groups of all workshops from the dashboard's Workshop tab, so starting a new workshop needs no restart here.
let GROUPS = [envVar('WA_GROUP') || envVar('WA_MAIN_CHAT'), ...PAST].filter(Boolean).map((g) => g.toLowerCase());
const watched = (name) => GROUPS.some((g) => (name || '').toLowerCase().includes(g)); // chats whose name contains one
// The current workshop's group (the dashboard's "main"): people who join it get its message history.
let MAIN = (envVar('WA_GROUP') || envVar('WA_MAIN_CHAT')).toLowerCase();
const isMain = (name) => !!MAIN && (name || '').toLowerCase().includes(MAIN);
// WhatsApp Web hasn't got "send message history" switched on for this number yet (the phone has). The user chose on
// 2026-10-21 to force it on here, accepting the risk to the number. WA_FORCE_HISTORY=0 in ../.env turns that off.
const FORCE_HISTORY = envVar('WA_FORCE_HISTORY') !== '0';
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

async function refreshGroups() {
  try {
    const r = await fetch(DASHBOARD + '/api/wa/groups', { headers: { 'x-local': '1' } });
    if (!r.ok) return;
    const j = await r.json();
    const groups = (j.groups || []).filter(Boolean).map((g) => g.toLowerCase());
    if (j.main && j.main.toLowerCase() !== MAIN) {
      MAIN = j.main.toLowerCase();
      log('sharing message history in:', j.main);
    }
    if (groups.length && groups.join('|') !== GROUPS.join('|')) {
      log('watching groups:', groups.join(' | '));
      GROUPS = groups;
      if (status.state === 'ready') {
        const names = (await groupChats()).map((c) => c.name).filter(watched);
        setStatus({ ...status, group: names.join(', ') || `no chat named "${GROUPS.join('" or "')}"` });
      }
    }
  } catch (e) { /* dashboard restarting; keep the groups we have */ }
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(__dirname, '.wwebjs_auth') }),
  // Trimmed for RAM: nothing here needs images, the GPU or extensions. (Merging renderers with
  // --renderer-process-limit=1 / no site-per-process made the WhatsApp page bigger, not smaller.)
  puppeteer: {
    headless: true, executablePath: BROWSER, args: [
      '--no-sandbox', '--disable-gpu', '--disable-software-rasterizer', '--disable-extensions',
      '--blink-settings=imagesEnabled=false',
      '--disable-features=Translate,OptimizationHints,MediaRouter,BackForwardCache',
    ],
  },
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

async function report(chatId, subtype, recipients, author, t, catchUp = false) {
  const kind = ['add', 'invite', 'linked_group_join'].includes(subtype) ? 'join'
    : ['leave', 'remove'].includes(subtype) ? 'leave' : null;
  if (!kind) return;
  const chat = await chatName(chatId);
  if (!watched(chat)) {
    log(`ignored ${kind} in "${chat}"`);
    return;
  }
  const people = (recipients && recipients.length ? recipients : [author]).map(idOf).filter(Boolean);
  for (const p of people) {
    const who = await number(p);
    log(kind, who, `"${chat}"`);
    await post('/api/wa/event', { who, kind, at: t, chat });
    if (kind === 'join' && isMain(chat)) shareHistory(chatId, chat, p, who, t, catchUp);
  }
}

// WhatsApp doesn't show a group's earlier messages to people who join; an admin has to tap "Share message history"
// under their join. This does the same through WhatsApp Web's own code (WAWebGroupHistoryPostJoinSendFlow, which
// that button runs): up to 100 messages from the 14 days before they joined, with WhatsApp's own checks that we're
// admin, sharing isn't turned off for the group, they joined recently and haven't got it yet (e.g. from the phone).
// Returns {result: sent|skip|failed|wait, why?, count?}; 'wait' = WhatsApp Web hasn't added them to the member list yet.
function sendHistory(chatId, id, t) {
  return client.pupPage.evaluate(async (chatId, id, t, force) => {
    let C, W, J, G, E, EF, X, S;
    try {
      [C, W, J, G, E, EF, X, S] = ['WAWebCollections', 'WAWebWidFactory', 'WAWebWidToJid', 'WAWebGroupHistoryGating',
        'WAWebGroupHistoryPostJoinEligibility', 'WAWebGroupHistoryPostJoinEligibilityFull',
        'WAWebGroupHistoryRestrictionHelper', 'WAWebSendHistoryBundleAction'].map((m) => window.require(m));
    } catch (e) {
      return { result: 'skip', why: 'no_feature' };
    }
    const gid = W.createWid(chatId);
    const chat = C.Chat.get(chatId);
    const meta = chat && chat.groupMetadata;
    if (!meta || !meta.participants) return { result: 'wait', why: 'group_not_loaded' };
    // WhatsApp rolls this out per number and per app: on 2026-10-21 the phone had it, WhatsApp Web didn't
    // ({send: false, prerequisites: true, afterJoin: false}). With FORCE_HISTORY those two switches (only read by this
    // gating module) are turned on in this page; everything else, and every check below, stays WhatsApp's own.
    const flagsNow = () => ({ send: G.isGroupHistorySenderEnabled(gid), prerequisites: G.isGroupHistoryPostJoinSenderPrerequisitesEnabled(),
      afterJoin: G.isGroupHistoryPostJoinSenderOrInternalTesterEnabled(gid) });
    const real = G.__real || flagsNow();
    if (force && !G.__real && !(real.send && real.afterJoin)) {
      G.__real = real;
      for (const k of ['isGroupHistorySenderEnabled', 'isGroupHistoryPostJoinSenderEnabled', 'isGroupHistoryPostJoinSenderOrInternalTesterEnabled']) {
        G[k] = () => true;
      }
    }
    const flags = { ...flagsNow(), forced: !!G.__real, real };
    if (!flags.send || !flags.prerequisites || !flags.afterJoin) return { result: 'skip', why: 'not_enabled', flags };
    if (!E.isPostJoinHistoryGroupStructurallyEligible(E.groupContextFromMetadata(meta))) return { result: 'skip', why: 'group_ineligible' };
    if (!id) { // just checking, at start
      const ab = (k) => window.require('WAWebABProps').getABPropConfigValue(k);
      return { result: 'ready', flags, limits: { messages: ab('group_history_message_count_limit'),
        days: ab('group_history_messages_time_limit_secs') / 86400, joinWindowHours: ab('group_history_new_user_threshold_secs') / 3600 } };
    }
    const is = (w) => !!w && String(w._serialized || w) === id;
    const p = meta.participants.getModelsArray().find((x) => is(x.id) || is(x.lid) || is(x.contact && x.contact.phoneNumber));
    if (!p) return { result: 'wait', why: 'not_a_member_yet' };
    const own = E.getParticipantPostJoinEligibility(p, t);
    if (!own.eligible) return { result: own.reason === 'no_join_time' ? 'wait' : 'skip', why: own.reason };
    const full = await EF.isEligibleForPostJoinHistoryFull({ groupMetadata: meta, groupWid: gid, joinTimeFallback: t, participant: p });
    if (!full.eligible) return { result: 'skip', why: full.reason };
    const { historyReceivers, nonHistoryReceivers } = X.filterParticipants([p.contact.id]);
    const sent = await S.sendHistoryBundleAction(J.widToGroupJid(gid), historyReceivers, nonHistoryReceivers, null,
      p.joinTime != null ? p.joinTime : t);
    return sent.bundleAcked ? { result: 'sent', count: full.messageCount } : { result: 'failed', why: 'not_accepted' };
  }, chatId, id, t, FORCE_HISTORY);
}

const WHY = {
  no_feature: "this WhatsApp Web version can't share history",
  not_enabled: "WhatsApp hasn't turned on sending history in WhatsApp Web for this number yet (only on the phone)",
  group_ineligible: 'not allowed in this group (not admin, or sharing is off in group settings)',
  group_not_loaded: 'group not loaded', not_a_member_yet: 'not in the member list', no_join_time: 'join time unknown',
  already_received: 'already has it', window_expired: 'joined too long ago', no_available_messages: 'no recent messages to share',
  bot: 'a bot', ineligible: 'WhatsApp says not eligible', not_accepted: "WhatsApp didn't accept it",
};
let sending = Promise.resolve(); // one at a time: each send uploads a file
const offered = new Set(); // group+person already handled since start
let notEnabledSaid = false;
function shareHistory(chatId, chat, id, who, t, catchUp) {
  const key = chatId + ' ' + id;
  // after a restart: only joins from while it was off, not everyone the phone didn't share with days ago
  if (offered.has(key) || (catchUp && Date.now() / 1000 - t > 2 * 3600)) return;
  offered.add(key);
  sending = sending.then(async () => {
    let r;
    for (let i = 0; i < 6; i++) { // the member list can lag the join notice by a few seconds
      try {
        r = await sendHistory(chatId, id, t);
      } catch (e) {
        r = { result: 'failed', why: e.message };
      }
      if (r.result !== 'wait') break;
      await new Promise((res) => setTimeout(res, 5000));
    }
    if (r.result === 'wait') r.result = 'skip';
    if (catchUp && r.result === 'skip') return; // old joins: being too late, or done on the phone already, is normal
    if (r.why === 'not_enabled') { // said once at start; it switches itself on when WhatsApp turns it on
      if (!notEnabledSaid) log('history not shared (not on in WhatsApp Web yet); tap it on the phone for now');
      notEnabledSaid = true;
      return;
    }
    notEnabledSaid = false;
    const why = WHY[r.why] || r.why;
    log(`history ${r.result}:`, who, `"${chat}"`, r.result === 'sent' ? `${r.count} messages` : why);
    if (r.why !== 'already_received') await post('/api/wa/history', { who, chat, result: r.result, count: r.count, why });
  });
}

// WhatsApp profile names of group members, for the dashboard's name check. WhatsApp Web only knows them for
// people who've posted something; read from its memory, no extra requests to WhatsApp.
async function postNames() {
  try {
    const people = await client.pupPage.evaluate((groups) => {
      const C = window.require('WAWebCollections');
      const out = {};
      for (const g of C.Chat.getModelsArray()) {
        if (!g.id || g.id.server !== 'g.us' || !groups.some((x) => (g.formattedTitle || '').toLowerCase().includes(x))) continue;
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
    }, GROUPS);
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
  await refreshGroups();
  const me = client.info && client.info.wid ? '+' + client.info.wid.user : '';
  let chats = [], all = [];
  try {
    all = await groupChats();
    chats = all.filter((c) => watched(c.name));
  } catch (e) {
    log('could not list chats:', e.message);
  }
  // community subgroups show up here too; when nothing matches, list every group name so the right one can be picked
  const seen = all.map((c) => c.name).filter(Boolean).sort().join(' | ');
  log('ready as', me, 'watching:', chats.map((c) => c.name).join(', ') || 'NO MATCHING GROUP', '| groups seen:', seen);
  await setStatus({ state: 'ready', me, group: chats.map((c) => c.name).join(', ') || `no chat named "${GROUPS.join('" or "')}". Groups seen: ${seen}` });
  for (const c of chats.filter((x) => isMain(x.name))) { // a community and its announcements share the name
    try {
      const r = await sendHistory(c.id, null, 0);
      log(`message history sharing in "${c.name}" (${c.id}):`, r.result === 'ready' ? 'on' : `off, ${WHY[r.why] || r.why}`,
        r.flags ? JSON.stringify(r.flags) : '', r.limits ? JSON.stringify(r.limits) : '');
    } catch (e) {
      log('message history check failed:', e.message);
    }
  }
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
        await report(id, m.subtype, m.recipients, m.author, m.t, true);
        n++;
      }
      log(`caught up ${n} group events in "${name}"`);
    } catch (e) {
      log('catch-up failed for', name, e.message);
    }
  }
  await postNames();
  await freeMemory();
});

// WhatsApp Web keeps ~700 MB after loading every chat but only needs a fraction once settled; Chromium gives
// it back when told memory is short. Same as what Windows does on its own after hours, just sooner.
async function freeMemory() {
  try {
    const cdp = await client.pupPage.target().createCDPSession();
    await cdp.send('Memory.simulatePressureNotification', { level: 'critical' });
    await cdp.send('HeapProfiler.collectGarbage');
    await cdp.detach();
  } catch (e) { /* page restarting */ }
}
setInterval(() => { if (status.state === 'ready') freeMemory(); }, 10 * 60000);

setInterval(() => { if (status.state === 'ready') setStatus(status); }, 60000); // heartbeat for the dashboard
setInterval(() => { if (status.state === 'ready') postNames(); }, 5 * 60000);
setInterval(refreshGroups, 60000);

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
