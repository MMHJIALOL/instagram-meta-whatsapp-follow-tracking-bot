const {test}=require('node:test');
const assert=require('node:assert/strict');
const {isDirectMessage,isFreshMessage,messageId,isKnownBusinessGreeting,actionDestination}=require('./transport_rules');
const inbound={from:'919999999999@c.us',fromMe:false,isStatus:false,broadcast:false,type:'chat',timestamp:1000};
test('team queries go only to the configured operator; client answers stay in their chat',()=>{
 const team='919899676862',chat=inbound.from;
 assert.equal(actionDestination({kind:'text',audience:'team',destination:team+'@c.us'},chat,team),team+'@c.us');
 assert.equal(actionDestination({kind:'text',destination:team+'@c.us'},chat,team),chat);
 assert.throws(()=>actionDestination({kind:'text',audience:'team',destination:chat},chat,team),/Invalid/);
 assert.throws(()=>actionDestination({kind:'image',audience:'team',destination:team+'@c.us'},chat,team),/Invalid/);
});
test('the configured phone greeting does not masquerade as a manual reply',()=>{
 assert.equal(isKnownBusinessGreeting({body:'Thank you for contacting Kalakriti Resin Studio! Please let us know how we can help you.'}),true);
 assert.equal(isKnownBusinessGreeting({body:'Thank you! I will check your payment.'}),false);
 assert.equal(isKnownBusinessGreeting({body:'Hello, I will help you now'}),false);
});
test('IDs remain stable when WhatsApp omits the serialized key',()=>{
 const key='false_919999999999@c.us_ABC123';
 assert.equal(messageId({...inbound,id:{_serialized:key}}),key);
 assert.equal(messageId({...inbound,id:{$1:key+'_out'}}),key+'_out');
 assert.equal(messageId({...inbound,id:{fromMe:false,remote:'919999999999@c.us',id:'ABC123'}}),key);
 assert.equal(messageId({...inbound,id:{fromMe:false,remote:{_serialized:inbound.from},id:'ABC123'}}),key);
 assert.equal(messageId({...inbound,id:{id:'ABC123'}}),key);
 assert.equal(messageId({...inbound,id:key}),key);
 assert.notEqual(messageId({...inbound,id:{id:'ABC124'}}),key);
 assert.equal(messageId({fromMe:true,to:inbound.from,id:{id:'DEF'}}),'true_919999999999@c.us_DEF');
 assert.throws(()=>messageId({...inbound,id:{}}),/no usable ID/);
});
test('only incoming private messages are eligible',()=>{
 assert.equal(isDirectMessage(inbound),true);
 assert.equal(isDirectMessage({...inbound,from:'12345@lid'}),true);
 for(const change of [{fromMe:true},{isStatus:true},{broadcast:true},{from:'123@g.us'},{from:'status@broadcast'},{from:'123@newsletter'},{type:'gp2'}]) {
  assert.equal(isDirectMessage({...inbound,...change}),false,JSON.stringify(change));
 }
});
test('reconnect does not answer backlog or events before ready',()=>{
 assert.equal(isFreshMessage(inbound,999,1001),true);
 assert.equal(isFreshMessage(inbound,Infinity,1001),false);
 assert.equal(isFreshMessage(inbound,1005,1006),false);
 assert.equal(isFreshMessage(inbound,999,1300),false);
 assert.equal(isFreshMessage({...inbound,timestamp:1400},999,1001),false);
});
