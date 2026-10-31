const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { HistoryQueue } = require('./history_queue');

test('temporary Windows destination locks do not lose queued or completed shares', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-history-'));
  try {
    const file = path.join(dir, 'jobs.json');
    const rename = fs.renameSync;
    let locks = 2;
    const mocked = t.mock.method(fs, 'renameSync', (...args) => {
      if (locks-- > 0) throw Object.assign(new Error('locked by reader'), { code: 'EPERM' });
      return rename(...args);
    });
    const q = new HistoryQueue(file, async () => ({ result: 'sent' }), async () => {});
    q.add({ chatId: 'group', id: 'person', t: 1 });
    locks = 1;
    await q.run();
    assert.equal(Object.values(JSON.parse(fs.readFileSync(file, 'utf8')))[0].done, true);
    assert.equal(mocked.mock.callCount(), 5);
    mocked.mock.restore();
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('failed shares survive restart, retry and deduplicate; rejoining is a new job', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-history-'));
  try {
    const file = path.join(dir, 'jobs.json'); let now = 10000; let calls = 0;
    const job = { chatId: 'group', id: 'person', t: 1 };
    const q = new HistoryQueue(file, async () => { throw Error('offline'); }, async () => {}, () => now);
    q.add(job); await q.run();
    const restored = new HistoryQueue(file, async () => { calls++; return { result: 'sent' }; }, async () => {}, () => now);
    await restored.run(); assert.equal(calls, 0);
    now += 5000; await restored.run(); assert.equal(calls, 1);
    restored.add(job); await restored.run(); assert.equal(calls, 1);
    restored.add({ ...job, t: 2 }); await restored.run(); assert.equal(calls, 2);
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('member-list delay retries and an already received history is terminal', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-history-'));
  try {
    let now = 0; let calls = 0;
    const q = new HistoryQueue(path.join(dir, 'jobs.json'), async () => ++calls === 1
      ? { result: 'wait', why: 'not_a_member_yet' } : { result: 'skip', why: 'already_received' }, async () => {}, () => now);
    q.add({ chatId: 'group', id: 'person', t: 1 }); await q.run();
    now = 5000; await q.run(); now = 999999; await q.run(); assert.equal(calls, 2);
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('pausing stops pending jobs without dropping them', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-history-'));
  try {
    let enabled = false; let calls = 0;
    const q = new HistoryQueue(path.join(dir, 'jobs.json'), async () => {
      calls++; enabled = false; return { result: 'sent' };
    }, async () => {}, () => 10000, () => enabled);
    q.add({ chatId: 'group', id: 'one', t: 1 });
    q.add({ chatId: 'group', id: 'two', t: 1 });
    await q.run(); assert.equal(calls, 0);
    enabled = true; await q.run(); assert.equal(calls, 1);
    enabled = true; await q.run(); assert.equal(calls, 2);
  } finally { fs.rmSync(dir, { recursive: true }); }
});
