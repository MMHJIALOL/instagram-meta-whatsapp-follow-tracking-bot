'use strict';
// Process-local fixes for whatsapp-web.js 1.34.7 on current WhatsApp Web.
// Does not change the shared installation used by the existing group listener.
function patchLoadUtils(source) {
  const replacements=[
    ['const msg = message.serialize();', 'const msg = message.serialize();\n          if (msg.id && !msg.id._serialized) msg.id._serialized = msg.id.$1;'],
    ['.Msg.get(newMsgKey._serialized)', '.Msg.get(newMsgKey._serialized ?? newMsgKey.$1)'],
    ["// Bot's won't reply if canonicalUrl is set (linking)", "delete message.__x_id;\n        // Bot's won't reply if canonicalUrl is set (linking)"],
  ];
  for (const [before,after] of replacements) {
    if (!source.includes(before)) throw new Error('WhatsApp compatibility patch needs review after library update');
    source=source.replace(before,after);
  }
  return source;
}
function install() {
  const utils=require('../wa/node_modules/whatsapp-web.js/src/util/Injected/Utils');
  // Compile only the installed library's own function plus the three edits above.
  utils.LoadUtils=Function(`return (${patchLoadUtils(utils.LoadUtils.toString())})`)();
}
module.exports={patchLoadUtils,install};
