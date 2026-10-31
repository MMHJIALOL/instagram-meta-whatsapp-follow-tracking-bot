/* Shipping stationery is rendered locally: exact text, no external generation service. */
(() => {
  'use strict';
  const el = id => document.getElementById('shipping-' + id);
  const form = el('form');
  const canvas = el('canvas');
  const ctx = canvas.getContext('2d');
  const fields = ['name', 'address', 'pin'].map(el);
  const senderFields = ['from-name', 'from-address', 'from-pin', 'from-phone'].map(el);
  const downloads = [el('png'), el('pdf')];
  const teal = '#005463', gold = '#c9942b', ink = '#111715';
  let ready = false, busy = false, revision = 0, filename = 'shipping-label';
  let logoPromise;

  function loadLogo() {
    if (!logoPromise) logoPromise = new Promise((resolve, reject) => {
      const logo = new Image();
      logo.onload = () => resolve(logo);
      logo.onerror = () => { logoPromise = null; reject(new Error('The logo could not load. Please try again.')); };
      logo.src = '/shipping-logo.jpeg';
    });
    return logoPromise;
  }
  function status(message, error = false) {
    el('status').textContent = message;
    el('status').className = error ? 'err' : 'sub';
  }
  function invalidate() {
    revision++;
    ready = false;
    downloads.forEach(button => { button.disabled = true; });
    status('Create the label to preview and download these details.');
  }
  function setFont(size, weight = 600, family = 'Arial, sans-serif') {
    ctx.font = `${weight} ${size}px ${family}`;
  }
  // Preserve paragraphs and split unusually long words without losing any characters.
  function wrap(text, width) {
    const lines = [];
    for (const paragraph of text.split(/\r?\n/)) {
      let line = '';
      for (const word of paragraph.trim().split(/\s+/).filter(Boolean)) {
        if (ctx.measureText(line ? line + ' ' + word : word).width <= width) {
          line = line ? line + ' ' + word : word;
          continue;
        }
        if (line) { lines.push(line); line = ''; }
        for (const char of Array.from(word)) {
          if (line && ctx.measureText(line + char).width > width) {
            lines.push(line); line = '';
          }
          line += char;
        }
      }
      if (line) lines.push(line);
    }
    return lines;
  }
  function fit(text, width, height, max, min, weight = 600) {
    for (let size = max; size >= min; size--) {
      setFont(size, weight);
      const lines = wrap(text, width);
      if (lines.length * size * 1.22 <= height) return { lines, size, weight };
    }
    throw new Error('This address is too long to fit clearly. Please shorten it or remove extra line breaks.');
  }
  function drawLines(block, x, y) {
    setFont(block.size, block.weight);
    ctx.fillStyle = ink;
    for (const line of block.lines) {
      ctx.fillText(line, x, y);
      y += block.size * 1.22;
    }
    return y;
  }
  function render(logo, recipient, address, pin, from) {
    // Fit every block before touching the preview, so invalid input never partially replaces it.
    const name = fit(recipient, 950, 190, 92, 52, 700);
    const nameHeight = name.lines.length * name.size * 1.22;
    const body = fit(address, 950, 590 - nameHeight, 61, 38);
    const senderText = `${from.name}\n${from.address}\nPIN: ${from.pin}\n${from.phone}`;
    const sender = fit(senderText, 580, 660, 53, 38);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 1800, 1200);
    ctx.strokeStyle = teal; ctx.lineWidth = 18; ctx.strokeRect(24, 24, 1752, 1152);
    ctx.strokeStyle = gold; ctx.lineWidth = 5; ctx.strokeRect(42, 42, 1716, 1116);
    // Use the supplied logo's original droplet artwork, accompanied by its compact wordmark.
    ctx.drawImage(logo, 460, 100, 330, 420, 528, 66, 118, 150);
    ctx.textBaseline = 'top'; ctx.fillStyle = ink;
    setFont(79, 700, 'Georgia, serif'); ctx.fillText('KALAKRITI', 665, 83);
    ctx.fillStyle = '#003b47'; setFont(45, 700, 'Georgia, serif');
    ctx.fillText('Resin Art Studio', 670, 168);
    ctx.strokeStyle = gold; ctx.lineWidth = 5;
    ctx.beginPath(); ctx.moveTo(76, 264); ctx.lineTo(1724, 264); ctx.stroke();
    ctx.strokeStyle = teal; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(1070, 310); ctx.lineTo(1070, 1105); ctx.stroke();
    ctx.fillStyle = teal; setFont(48, 700);
    ctx.fillText('SHIP TO', 90, 315); ctx.fillText('FROM', 1115, 315);
    let y = drawLines(name, 90, 401);
    y = drawLines(body, 90, y + 22);
    setFont(62, 700); ctx.fillStyle = ink; ctx.fillText('PIN: ' + pin, 90, Math.max(1010, y + 26));
    drawLines(sender, 1115, 412);
    canvas.setAttribute('aria-label', `Shipping label. Ship to ${recipient}, ${address}, PIN ${pin}. From ${from.name}, ${from.address}, PIN ${from.pin}, ${from.phone}.`);
  }

  // A small image-only PDF writer avoids any network/CDN dependency. PDF uses points:
  // 432 x 288 points = exactly 6 x 4 inches, centered on a plain white A4 page.
  function pdfBlob(jpeg) {
    const encode = value => new TextEncoder().encode(value);
    const binary = atob(jpeg.split(',')[1]);
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
    const parts = [], offsets = [0];
    let length = 0;
    function push(part) { const data = typeof part === 'string' ? encode(part) : part; parts.push(data); length += data.length; }
    function object(id, content) { offsets[id] = length; push(`${id} 0 obj\n${content}\nendobj\n`); }
    push('%PDF-1.4\n');
    object(1, '<< /Type /Catalog /Pages 2 0 R >>');
    object(2, '<< /Type /Pages /Kids [3 0 R] /Count 1 >>');
    object(3, '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.275591 841.889764] /Resources << /XObject << /Label 4 0 R >> >> /Contents 5 0 R >>');
    offsets[4] = length;
    push(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width 1800 /Height 1200 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${bytes.length} >>\nstream\n`);
    push(bytes); push('\nendstream\nendobj\n');
    const stream = 'q\n432 0 0 288 81.6377955 276.944882 cm\n/Label Do\nQ\n';
    object(5, `<< /Length ${encode(stream).length} >>\nstream\n${stream}endstream`);
    const start = length;
    push('xref\n0 6\n0000000000 65535 f \n');
    for (let i = 1; i <= 5; i++) push(`${String(offsets[i]).padStart(10, '0')} 00000 n \n`);
    push(`trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF\n`);
    return new Blob(parts, { type: 'application/pdf' });
  }
  function download(blob, suffix) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename + suffix;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  form.addEventListener('input', invalidate);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    [...fields, ...senderFields].forEach(field => field.value = field.value.trim());
    if (!form.reportValidity()) return;
    const version = revision;
    ready = false;
    downloads.forEach(button => { button.disabled = true; });
    busy = true; el('create').disabled = true; status('Creating label…');
    try {
      const logo = await loadLogo();
      if (version !== revision) return;
      render(logo, fields[0].value, fields[1].value, fields[2].value, {
        name: senderFields[0].value, address: senderFields[1].value,
        pin: senderFields[2].value, phone: senderFields[3].value
      });
      filename = (fields[0].value.replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '') || 'recipient') + '-shipping-label-6x4';
      ready = true;
      downloads.forEach(button => { button.disabled = false; });
      status('Label ready. Download the image or the A4 PDF.');
    } catch (error) { status(error.message || 'Could not create the label. Please try again.', true); }
    finally { busy = false; el('create').disabled = false; }
  });
  el('clear').addEventListener('click', () => {
    fields.forEach(field => { field.value = ''; });
    invalidate(); placeholder(); fields[0].focus();
    status('Your label will appear here.');
  });
  el('png').addEventListener('click', () => {
    if (!ready) return;
    canvas.toBlob(blob => {
      if (blob) download(blob, '.png');
      else status('Could not save the image. Please try again.', true);
    }, 'image/png');
  });
  el('pdf').addEventListener('click', () => {
    if (!ready) return;
    try { download(pdfBlob(canvas.toDataURL('image/jpeg', 0.98)), '-A4.pdf'); }
    catch (_) { status('Could not save the PDF. Please try again.', true); }
  });
  function placeholder() {
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1800, 1200);
    ctx.strokeStyle = teal; ctx.lineWidth = 12; ctx.strokeRect(24, 24, 1752, 1152);
    ctx.fillStyle = teal; setFont(65, 700); ctx.textBaseline = 'top';
    ctx.fillText('Your shipping label', 100, 440);
    ctx.fillStyle = '#666'; setFont(42, 400);
    ctx.fillText('Enter the delivery details and click Create Shipping Label.', 100, 550);
    canvas.setAttribute('aria-label', 'Empty shipping label preview. Enter recipient details to create a label.');
  }
  placeholder();
})();
