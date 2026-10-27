'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {patchLoadUtils}=require('./compatibility');
const {LoadUtils}=require('../wa/node_modules/whatsapp-web.js/src/util/Injected/Utils');

test('current library sends and finds text/media with renamed message keys',async()=>{
 const store=new Map();
 let sequence=0;
 class MsgKey {
  constructor({to,id}) { this.$1=`true_${to.value}_${id}_out`; }
  static async newId() { return `test${++sequence}`; }
 }
 const modules={
  WAWebChatGetters:{getIsNewsletter:()=>false,getIsBroadcast:()=>false},
  WALinkify:{findLink:()=>null,findLinks:()=>[]},
  WAWebUserPrefsMeUser:{getMaybeMeLidUser:()=>({}),getMaybeMePnUser:()=>({})},
  WAWebMsgKey:MsgKey,
  WAWebGetEphemeralFieldsMsgActionsUtils:{getEphemeralFields:()=>({})},
  WAWebSendMsgChatAction:{addAndSendMsgToChat:(_chat,data)=>{
   assert.equal(Object.hasOwn(data,'__x_id'),false,'media must not overwrite message ID');
   const model={id:data.id,serialize:()=>({...data})};
   store.set(data.id.$1,model);
   return [Promise.resolve(model),Promise.resolve()];
  }},
  WAWebCollections:{Msg:{get:key=>store.get(key)}},
 };
 const window={require:name=>{
  assert.ok(modules[name],`unexpected dependency ${name}`);
  return modules[name];
 }};
 vm.runInNewContext(`(${patchLoadUtils(LoadUtils.toString())})()`,{window});
 const api=window.WWebJS;
 api.processMediaData=async()=>({__x_id:'private-media-id',toJSON:()=>({mimetype:'image/jpeg'})});
 const chat={id:{value:'123@lid',isLid:()=>true}};
 for (const options of [{},{media:{data:'test'},caption:'payment QR'}]) {
  const sent=await api.sendMessage(chat,'hello',options);
  assert.ok(sent,'sent message must be retrieved with its new key');
  const serialized=api.getMessageModel(sent);
  assert.equal(serialized.id._serialized,sent.id.$1);
 }
 assert.equal(store.size,2);
});

test('compatibility edits stop if an upgraded library no longer matches',()=>{
 assert.throws(()=>patchLoadUtils('() => {}'),/needs review/);
});
