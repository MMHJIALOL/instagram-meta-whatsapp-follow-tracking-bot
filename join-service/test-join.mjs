import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, readdirSync } from 'node:fs';
import { serveJoin } from './lib/join-service.ts';

test('authenticated registration, durable taps, previews, replay and invite safety', async () => {
  const sql = new DatabaseSync(':memory:');
  for (const file of readdirSync('drizzle').filter(f=>f.endsWith('.sql'))) sql.exec(readFileSync(`drizzle/${file}`,'utf8'));
  const env = { SYNC_TOKEN:'test-secret', DB:{
    prepare(query) { return { bind(...params) { const stmt=sql.prepare(query); return {
      async run(){return stmt.run(...params)}, async first(){return stmt.get(...params)}, async all(){return {results:stmt.all(...params)}}
    }}} }, async batch(stmts){return Promise.all(stmts.map(s=>s.run()))}
  }};
  const request = (path, options={})=>new Request(`https://join.example.com${path}`,options);
  const post = links=>serveJoin(request('/api/sync/links',{method:'POST',headers:{authorization:'Bearer test-secret','content-type':'application/json'},body:JSON.stringify({links})}),env);
  assert.equal((await serveJoin(request('/api/sync/events'),env)).status,401);
  assert.equal((await post([{code:'sample123',target:'https://evil.example/',state:'active'}])).status,400);
  const link={code:'sample123',target:'https://chat.whatsapp.com/testinvite',state:'active'};
  assert.equal((await post([link])).status,200);
  for (const headers of [{'user-agent':'facebookexternalhit'}, {'user-agent':'WhatsApp/2'}]) await serveJoin(request('/j/sample123',{headers}),env);
  await serveJoin(request('/j/sample123',{method:'HEAD'}),env);
  assert.equal(sql.prepare('select count(*) n from join_events').get().n,0);
  const response=await serveJoin(request('/j/sample123',{headers:{'user-agent':'Mobile Instagram test','cf-connecting-ip':'203.0.113.1'}}),env);
  assert.equal(response.status,302); assert.equal(response.headers.get('location'),link.target);
  assert.equal(response.headers.get('cache-control'),'no-store');
  const events=await (await serveJoin(request('/api/sync/events?after=0',{headers:{authorization:'Bearer test-secret'}}),env)).json();
  assert.equal(events.events.length,1); assert.equal(events.events[0].code,'sample123');
  await post([{...link,state:'used'}]);
  assert.equal((await serveJoin(request('/j/sample123'),env)).status,302);
  assert.equal(sql.prepare('select count(*) n from join_events').get().n,1);
  await post([{...link,state:'expired'}]);
  assert.equal((await serveJoin(request('/j/sample123'),env)).status,410);
  assert.equal((await serveJoin(request('/j/unknown123'),env)).status,404);
  sql.close();
});
