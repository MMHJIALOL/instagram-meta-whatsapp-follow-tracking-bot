/* Shared state and API helpers are initialized by dashboard.html. */
let chatRequest = 0, chatListDigest = '';
const chatTime = t => new Date(t * 1000).toLocaleTimeString('en-IN', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit'
});
const chatDayKey = t => new Date(t * 1000).toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'});
function chatDay(t) {
  const key = chatDayKey(t), now = Date.now() / 1000;
  if (key === chatDayKey(now)) return 'Today';
  if (key === chatDayKey(now - 86400)) return 'Yesterday';
  return new Date(t * 1000).toLocaleDateString('en-IN', {timeZone: 'Asia/Kolkata', day: 'numeric', month: 'long', year: 'numeric'});
}
function chatName(c) { return c.name || c.phone || c.chat_id.split('@')[0]; }
function chatAvatar(c) {
  const name = c.name || '';
  const letters = name ? name.trim().split(/\s+/).slice(0, 2).map(s => s[0]).join('') : '#';
  return '<span class="chat-avatar" aria-hidden="true">' + esc(letters.toUpperCase()) + '</span>';
}
function chatListTime(t) {
  if (!t) return '';
  if (chatDayKey(t) === chatDayKey(Date.now() / 1000)) return chatTime(t);
  if (chatDay(t) === 'Yesterday') return 'Yesterday';
  return new Date(t * 1000).toLocaleDateString('en-IN', {timeZone: 'Asia/Kolkata', day: '2-digit', month: '2-digit', year: '2-digit'});
}
async function loadChats() { allChats = await api('/api/chats'); renderChats(); }
function renderChats() {
  const query = $('chatSearch').value.toLowerCase(), review = $('reviewOnly').checked;
  const rows = allChats.filter(c => (!review || c.review) && [c.name, c.phone, c.last_message].join(' ').toLowerCase().includes(query));
  const digest = JSON.stringify([rows, currentChat, query, review]);
  if (digest === chatListDigest) return;
  chatListDigest = digest;
  const scroll = $('chatList').scrollTop;
  $('chatList').innerHTML = rows.length ? rows.map(c => {
    const sender = c.last_direction === 'out' ? 'Bot: ' : c.last_direction === 'manual' ? 'You: ' : '';
    return '<button type="button" class="chatitem ' + (c.chat_id === currentChat ? 'selected' : '') + '" data-chat="' + esc(c.chat_id) + '" aria-pressed="' + (c.chat_id === currentChat) + '">' + chatAvatar(c) +
      '<div class="chat-copy"><div class="chat-title-row"><strong>' + esc(chatName(c)) + '</strong><time>' + esc(chatListTime(c.last_at)) + '</time></div><p>' + esc(sender + (c.last_message || 'New enquiry')) + '</p><div class="badges">' +
      (c.review ? '<span class="badge">' + esc(c.review) + '</span>' : '') +
      (c.payment === 'paid' ? '<span class="badge paid">Verified paid</span>' : '') +
      (c.paused || c.manual_until > Date.now() / 1000 ? '<span class="badge">Paused</span>' : '') +
      (c.payment === 'opted_out' ? '<span class="badge">Stopped</span>' : '') + '</div></div></button>';
  }).join('') : '<div class="empty">No matching enquiries yet.</div>';
  $('chatList').scrollTop = scroll;
  $('chatList').querySelectorAll('[data-chat]').forEach(el => el.onclick = () => {
    loadChat(el.dataset.chat).catch(e => toast(e.message));
  });
}
function messageBubble(m) {
  const outgoing = m.direction === 'out' || m.direction === 'manual';
  const problem = outgoing && !['sent', 'sent from phone'].includes(m.status);
  const role = m.direction === 'out' ? 'Bot' : m.direction === 'manual' ? 'You' : '';
  const asset = m.asset === 'payment.jpg' ? '<a href="/assets/payment.jpg" target="_blank" rel="noopener"><img src="/assets/payment.jpg" alt="Payment QR"></a>' :
    m.asset === 'workshop.pdf' ? '<a href="/assets/workshop.pdf" target="_blank" rel="noopener">📄 Workshop PDF ↗</a>' : '';
  // A single tick means sent. Do not claim delivery/read without WhatsApp acknowledgements.
  return '<div class="bubble ' + (outgoing ? m.direction : 'in') + '" data-message="' + esc(m.id) + '"><div class="message-body">' + esc(m.body) + '</div>' + asset +
    '<small class="message-meta' + (problem ? ' delivery-problem' : '') + '" title="' + esc(stamp(m.at) + ' · ' + m.status) + '">' +
    (role ? '<span class="message-role">' + role + '</span>' : '') + '<time>' + esc(chatTime(m.at)) + '</time>' +
    (outgoing ? '<span class="message-status" aria-label="' + esc(m.status) + '">' + (problem ? '⚠ ' + esc(m.status) : '✓') + '</span>' : '') + '</small></div>';
}
async function loadChat(id) {
  const switching = currentChat !== id;
  currentChat = id;
  if (switching) {
    chatDigest = '';
    $('chatMain').innerHTML = '<div class="empty">Loading conversation…</div>';
  }
  document.querySelector('.inbox').classList.add('chat-open');
  renderChats();
  const request = ++chatRequest;
  const r = await api('/api/chat?chat_id=' + encodeURIComponent(id));
  if (request !== chatRequest || currentChat !== id) return;
  if (!r.contact) { $('chatMain').innerHTML = '<div class="empty">Conversation not found.</div>'; return; }
  const c = r.contact, paused = !!(c.paused || c.manual_until > Date.now() / 1000);
  const digest = JSON.stringify([r, paused, !!currentState?.settings.enabled]);
  if (digest === chatDigest) return;
  chatDigest = digest;
  const oldThread = $('thread'), oldScroll = oldThread?.scrollTop || 0;
  const atBottom = !oldThread || oldThread.scrollHeight - oldScroll - oldThread.clientHeight < 70;
  const actionsOpen = $('chatActions')?.open || false, alertsOpen = $('teamAlerts')?.open || false;
  let lastDay = '', previousDisplayAt = 0;
  const messages = r.messages.map(m => {
    // Keep date/time labels monotonic too when the phone clock runs ahead.
    const displayAt = Math.max(previousDisplayAt, m.at);
    previousDisplayAt = displayAt;
    const key = chatDayKey(displayAt), day = key !== lastDay ? '<div class="day-separator">' + esc(chatDay(displayAt)) + '</div>' : '';
    lastDay = key;
    return day + messageBubble({...m, at: displayAt});
  }).join('');
  const alerts = r.team_alerts || [];
  const alertNote = alerts.length ? '<details id="teamAlerts"' + (alertsOpen ? ' open' : '') + '><summary>Team notifications · ' + alerts.length + '</summary>' + alerts.map(a => '<p>' + esc(stamp(a.at)) + ' · Team alert ' + esc(a.status) + '</p>').join('') + '</details>' : '';
  const waLink = c.phone ? 'https://wa.me/' + encodeURIComponent(c.phone) : '';
  $('chatMain').innerHTML = '<div class="chathead"><button type="button" id="backToChats" class="chat-back" aria-label="Back to chats">←</button>' + chatAvatar(c) + '<div class="chat-identity"><strong>' + esc(chatName(c)) + '</strong><div class="chat-subtitle">' + esc(c.phone ? '+' + c.phone : id) + ' · ' + (paused ? 'Bot paused' : 'Business chat') + '</div></div>' +
    '<details class="chat-actions" id="chatActions"' + (actionsOpen ? ' open' : '') + '><summary aria-label="Conversation actions" title="Conversation actions">⋮</summary><div class="chat-action-menu"><button class="small" id="pauseChat">' + (paused ? 'Resume bot' : 'Pause bot') + '</button><button class="small" id="markPaid">' + (c.payment === 'paid' ? 'Payment verified ✓' : 'Mark payment verified') + '</button><button class="small" id="clearReview">Clear attention flag</button></div></details></div>' +
    '<div class="thread" id="thread" role="log" aria-label="Conversation messages" aria-live="off">' + (messages || '<div class="empty">No messages yet.</div>') + '</div><button type="button" class="jump-latest hide" id="jumpLatest">↓ Latest messages</button>' +
    '<div class="chatfoot"><div>' + (c.review ? 'Needs attention: ' + esc(c.review) + '. ' : '') + (paused ? 'Bot paused for this conversation.' : 'Bot replies ' + (currentState?.settings.enabled ? 'on' : 'paused globally') + '.') + ' Reply from WhatsApp to take over.' + alertNote + '</div>' + (waLink ? '<a class="reply-whatsapp" href="' + esc(waLink) + '" target="_blank" rel="noopener">Reply in WhatsApp ↗</a>' : '') + '</div>';
  const thread = $('thread');
  thread.scrollTop = atBottom ? thread.scrollHeight : oldScroll;
  const updateJump = () => $('jumpLatest').classList.toggle('hide', thread.scrollHeight - thread.scrollTop - thread.clientHeight < 70);
  thread.onscroll = updateJump;
  $('jumpLatest').onclick = () => { thread.scrollTop = thread.scrollHeight; updateJump(); };
  updateJump();
  $('backToChats').onclick = () => { document.querySelector('.inbox').classList.remove('chat-open'); currentChat = ''; chatDigest = ''; ++chatRequest; renderChats(); };
  $('pauseChat').onclick = () => chatAction(paused ? {resume: true} : {paused: true});
  $('clearReview').onclick = () => chatAction({clear_review: true});
  $('markPaid').onclick = () => {
    if (c.payment === 'paid') return;
    if (confirm('Have you checked the actual payment in your bank/UPI app? A screenshot alone is not confirmation.')) chatAction({payment: 'paid'});
  };
}
