'use strict';
// Own linked-device session. Never opens or changes wa/.wwebjs_auth.
const path = require('path');
const fs = require('fs');
require('./compatibility').install();
const { Client, LocalAuth, MessageMedia } = require('../wa/node_modules/whatsapp-web.js');
const { isDirectMessage, isFreshMessage, messageId, isKnownBusinessGreeting, actionDestination } = require('./transport_rules');
const PORT = Number(process.env.WA_DM_PORT || 8003);
const TOKEN = process.env.WA_DM_BRIDGE_TOKEN;
if (!TOKEN) throw new Error('Start the bridge from the local dashboard server');
const BASE = `http://127.0.0.1:${PORT}`;
const dataPath = process.env.WA_DM_DATA || path.join(__dirname, 'data');
const browser = process.env.WA_DM_BROWSER || [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find(p => fs.existsSync(p));
if (!browser) throw new Error('Chrome or Edge is required');
let state = {state:'starting',me:'',detail:'Opening WhatsApp Web…'};
let phaseStarted=Date.now();
let readyAt = Infinity;
let expectedNumber = '919211664301';
let teamNumber = '919899676862';
const phoneCache = new Map();
const queues = new Map();
const expectedSends = new Map();
const ownIds = new Set();

async function post(route, body) {
  const response = await fetch(BASE + route, {
    method:'POST', headers:{'content-type':'application/json',authorization:`Bearer ${TOKEN}`},
    body:JSON.stringify(body), signal:AbortSignal.timeout(12000),
  });
  if (!response.ok) {
    // Record field/type diagnostics only; never log customer text or credentials.
    const details=await response.json().catch(()=>null);
    const fields=Array.isArray(details?.detail) ? details.detail.map(e=>`${(e.loc||[]).join('.')}: ${e.type}`).join(', ') : '';
    throw new Error(`Local dashboard ${route} failed (${response.status})${fields?': '+fields:''}`);
  }
  return response.json();
}
async function status(update) {
  if(update.state!==state.state) phaseStarted=Date.now();
  state = {...update};
  try {
    const result = await post('/internal/status',state);
    expectedNumber = result.expected_number || expectedNumber;
    teamNumber = result.handoff_number || teamNumber;
    if(result.restart) await shutdown(0);
  } catch (err) { console.error(new Date().toISOString(),err.message); }
}
function log(message) { console.log(new Date().toISOString(),message); }
const client = new Client({
  authStrategy:new LocalAuth({clientId:'kalakriti-dm',dataPath:path.join(dataPath,'auth')}),
  webVersionCache:{type:'local',path:path.join(dataPath,'web-cache')},
  deviceName:'Kalakriti DM Bot', browserName:'Chrome',
  takeoverOnConflict:false,
  puppeteer:{headless:true,executablePath:browser,args:[
    '--disable-gpu','--disable-extensions','--disable-dev-shm-usage',
    '--disable-features=Translate,OptimizationHints,MediaRouter,BackForwardCache',
  ]},
});
// WhatsApp sometimes reloads itself during injection after a saved login.
const originalInject = client.inject.bind(client);
client.inject = async () => {
  for (let attempt=0;;attempt++) {
    try { return await originalInject(); }
    catch (err) {
      if (attempt >= 8 || !/context was destroyed|navigation/i.test(String(err))) throw err;
      log('WhatsApp reloaded during initialization; retrying injection');
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
  }
};

client.on('qr',qr=>{log('QR ready on the local dashboard');status({state:'qr',qr,me:'',detail:'Phone → Linked devices → Link a device'});});
client.on('authenticated',()=>status({state:'authenticated',me:'',detail:'Linked. Loading chats…'}));
client.on('loading_screen',percent=>{
  if (state.state !== 'ready') status({...state,detail:`Loading WhatsApp (${percent}%)…`});
});
client.on('auth_failure',detail=>status({state:'auth_failure',me:'',detail:String(detail)}));
client.on('disconnected',async detail=>{
  readyAt=Infinity;
  await status({state:'disconnected',me:'',detail:String(detail)});
  await shutdown(1);
});
client.on('ready',async()=>{
  let me = client.info?.wid?.user || '';
  if (client.info?.wid?._serialized?.endsWith('@lid')) {
    try {
      const result = await client.getContactLidAndPhone([client.info.wid._serialized]);
      if (result[0]?.pn) me = result[0].pn.split('@')[0];
    } catch (_) {}
  }
  me=String(me).replace(/\D/g,'');
  readyAt=Date.now()/1000;
  await status({state:'ready',me,detail:me===expectedNumber?'WhatsApp connected':'Different number linked. Use +91 9211664301.'});
  log(me===expectedNumber?'Expected business account connected':'Wrong account linked; all replies blocked');
});

function ownExpected(message) {
  const msgId=messageId(message);
  if (ownIds.has(msgId)) return true;
  const expected=expectedSends.get(message.to) || [];
  const now=Date.now();
  const index=expected.findIndex(item=>item.expires>now &&
    (item.kind==='text' ? message.body===item.body :
      message.type===item.kind && (!message.body || message.body===item.body || message.body===item.filename)));
  if (index<0) return false;
  expected.splice(index,1);
  if (msgId) ownIds.add(msgId);
  return true;
}

async function limited(promise) {
  let timer;
  try { return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Contact lookup timed out')),5000);})]); }
  finally { clearTimeout(timer); }
}
async function resolvePhone(chatId) {
  if (chatId.endsWith('@c.us')) return chatId.split('@')[0];
  if (phoneCache.has(chatId)) return phoneCache.get(chatId);
  let phone='';
  try { const rows=await limited(client.getContactLidAndPhone([chatId])); phone=(rows[0]?.pn||'').split('@')[0]; } catch (_) {}
  if (!phone) {
    try {
      const contact=await limited(client.getContactById(chatId));
      if(contact.id?.server==='c.us' || contact.id?._serialized?.endsWith('@c.us')) phone=contact.number||contact.id.user||'';
    } catch (_) {}
  }
  if (!/^\d{10,15}$/.test(phone)) return '';
  phoneCache.set(chatId,phone);
  return phone;
}
async function handle(message) {
  if (state.state!=='ready' || state.me!==expectedNumber || !isFreshMessage(message,readyAt)) return;
  const chatId=message.from;
  const phone=await resolvePhone(chatId);
  const result=await post('/internal/incoming',{
    id:messageId(message),chat_id:chatId,phone,
    name:String(message._data?.notifyName||'').slice(0,150),
    body:String(message.body||'').slice(0,12000),kind:message.type||'chat',
    has_media:!!message.hasMedia,timestamp:message.timestamp,
  });
  await sendActions(chatId,result.actions||[]);
}

async function sendActions(chatId,actions) {
  let clientFailed=false;
  for (const action of actions) {
    if (state.state!=='ready' || state.me!==expectedNumber) break;
    if (clientFailed && action.audience!=='team') continue;
    const target=actionDestination(action,chatId,teamNumber);
    // Claim is transactional and rechecks global/chat pause immediately before sending.
    const claim=await post('/internal/claim',{id:action.id});
    if (!claim.allowed) continue;
    let attempted=false;
    try {
      let content=action.body;
      const options={sendSeen:false};
      if (action.kind!=='text') {
        if (!['payment.jpg','workshop.pdf'].includes(action.asset)) throw new Error('Unknown approved attachment');
        content=MessageMedia.fromFilePath(path.join(__dirname,'assets',action.asset));
        options.caption=action.body;
        if (action.kind==='document') options.sendMediaAsDocument=true;
      }
      const expected=expectedSends.get(target)||[];
      expected.push({...action,filename:action.asset,expires:Date.now()+60000});
      expectedSends.set(target,expected.filter(item=>item.expires>Date.now()));
      attempted=true;
      const sent=await client.sendMessage(target,content,options);
      if (!sent) throw new Error('WhatsApp did not return a message; check delivery manually');
      const sentId=messageId(sent);
      ownIds.add(sentId);
      await post('/internal/result',{id:action.id,status:'sent',wa_id:sentId});
    } catch (err) {
      // Never automatically retry an ambiguous network/WhatsApp send.
      await post('/internal/result',{id:action.id,status:attempted?'uncertain':'failed',detail:String(err.message||err).slice(0,500)}).catch(()=>{});
      log('A reply needs a manual delivery check in the dashboard');
      if (action.audience!=='team') clientFailed=true;
    }
    await new Promise(resolve=>setTimeout(resolve,600));
  }
}

client.on('message',message=>{
  if (!isDirectMessage(message) || !isFreshMessage(message,readyAt)) return;
  const previous=queues.get(message.from)||Promise.resolve();
  const next=previous.catch(()=>{}).then(()=>handle(message)).catch(err=>log(`Message handling: ${err.message}`));
  queues.set(message.from,next);
  next.finally(()=>{if(queues.get(message.from)===next)queues.delete(message.from);});
});
client.on('message_create',async message=>{
 try {
  if (!message.fromMe || !/^\d+@(?:c\.us|lid)$/.test(message.to||'') || !isFreshMessage(message,readyAt)) return;
  if (ownExpected(message)) return;
  // The phone's configured automatic welcome is not a human taking over the chat.
  if (isKnownBusinessGreeting(message)) return;
  const phone=await resolvePhone(message.to);
  if (phone===teamNumber) return;
  await post('/internal/manual',{
    id:messageId(message),chat_id:message.to,phone,body:String(message.body||'').slice(0,12000),
    kind:message.type||'chat',timestamp:message.timestamp,
  });
 } catch(err) { log(err.message); }
});

let checking=false,misses=0;
setInterval(async()=>{
  if(checking)return;
  checking=true;
  try {
    if (['starting','authenticated'].includes(state.state) && Date.now()-phaseStarted>60000) {
      log('WhatsApp startup stalled; restarting the saved linked-device session');
      await status({state:'disconnected',me:'',detail:'WhatsApp loading stalled. Reconnecting…'});
      await shutdown(1);
      return;
    }
    if(state.state==='ready') {
      await Promise.race([client.pupPage.evaluate(()=>1),new Promise((_,reject)=>setTimeout(()=>reject(new Error('WhatsApp page not responding')),15000))]);
    }
    await status(state);
    if(state.state==='ready' && state.me===expectedNumber) {
      const response=await fetch(BASE+'/internal/pending-alerts',{headers:{authorization:`Bearer ${TOKEN}`},signal:AbortSignal.timeout(12000)});
      if(!response.ok) throw new Error('Could not check pending team alerts');
      for(const alert of await response.json()) await sendActions(alert.chat_id,[alert]);
    }
    misses=0;
    if(ownIds.size>3000)ownIds.clear();
  } catch(err) {
    if(++misses>=3) {
      await status({state:'disconnected',me:'',detail:'WhatsApp stopped responding. Reconnecting…'});
      await shutdown(1);
    }
  } finally { checking=false; }
},12000);

let stopping=false;
async function shutdown(code=0) {
  if(stopping)return;
  stopping=true;
  const exit=setTimeout(()=>process.exit(code),5000);
  try{await client.destroy();}catch(_){}
  clearTimeout(exit);
  process.exit(code);
}
process.on('SIGTERM',()=>shutdown());
process.on('SIGINT',()=>shutdown());
process.on('uncaughtException',err=>{log(`Bridge stopped: ${err.message}`);shutdown(1);});
process.on('unhandledRejection',err=>log(`Bridge error: ${err?.message||err}`));
log('Starting separate WhatsApp DM linked device');
client.initialize().then(()=>{
  client.pupPage.on('pageerror',err=>log(`WhatsApp page error: ${String(err.message||err).slice(0,400)}`));
}).catch(async err=>{
  log(`Initialization failed: ${String(err.message||err)}`);
  await status({state:'error',me:'',detail:String(err.message||err).slice(0,350)});
  await shutdown(1);
});
