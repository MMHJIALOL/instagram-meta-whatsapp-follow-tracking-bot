'use strict';
function messageId(message) {
  const id=message.id;
  if (typeof id==='string' && id) return id;
  if (typeof id?._serialized==='string' && id._serialized) return id._serialized;
  if (typeof id?.$1==='string' && id.$1) return id.$1;
  // Newer WhatsApp Web serializers retain the key fields but omit _serialized.
  const remote=typeof id?.remote==='string' ? id.remote : id?.remote?._serialized;
  const chat=remote || (message.fromMe ? message.to : message.from);
  if (typeof id?.id!=='string' || !id.id || !/^\d+@(?:c\.us|lid)$/.test(chat||'')) {
    throw new Error('WhatsApp message has no usable ID');
  }
  return `${id.fromMe ?? !!message.fromMe}_${chat}_${id.id}`;
}
function isDirectMessage(message) {
  return !message.fromMe && !message.isStatus && !message.broadcast &&
    /^\d+@(?:c\.us|lid)$/.test(message.from||'') &&
    ['chat','image','document','video','audio','ptt','sticker','location','vcard','multi_vcard'].includes(message.type);
}
function isFreshMessage(message,readyAt,now=Date.now()/1000) {
  return Number.isFinite(message.timestamp) && message.timestamp>=readyAt-1 &&
    message.timestamp>=now-120 && message.timestamp<=now+120;
}
function isKnownBusinessGreeting(message) {
  return String(message.body||'').trim().replace(/\s+/g,' ').toLowerCase() ===
    'thank you for contacting kalakriti resin studio! please let us know how we can help you.';
}
function actionDestination(action,chatId,teamNumber) {
  if (action.audience==='team') {
    if (action.kind!=='text' || action.destination!==teamNumber+'@c.us') throw new Error('Invalid team alert destination');
    return action.destination;
  }
  return chatId;
}
module.exports={isDirectMessage,isFreshMessage,messageId,isKnownBusinessGreeting,actionDestination};
