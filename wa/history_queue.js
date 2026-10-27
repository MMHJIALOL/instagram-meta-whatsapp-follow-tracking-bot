const fs = require('fs');
const TERMINAL = new Set(['already_received', 'window_expired', 'bot']);

class HistoryQueue {
  constructor(file, send, onResult, now = () => Date.now()) {
    this.file = file; this.send = send; this.onResult = onResult; this.now = now;
    this.busy = false;
    this.jobs = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  }
  save() {
    fs.writeFileSync(this.file + '.tmp', JSON.stringify(this.jobs));
    fs.renameSync(this.file + '.tmp', this.file);
  }
  add(job) {
    if (!job.id || !Number.isFinite(job.t) || job.t <= 0) return;
    const key = `${job.chatId} ${job.id} ${job.t}`;
    if (this.jobs[key]) return;
    this.jobs[key] = { ...job, attempts: 0, next: 0, done: false };
    this.save();
  }
  async run() {
    if (this.busy) return;
    this.busy = true;
    try {
      for (const job of Object.values(this.jobs)) {
        if (job.done || job.next > this.now()) continue;
        let result;
        try { result = await this.send(job); }
        catch (e) { result = { result: 'failed', why: e.message }; }
        job.attempts++;
        job.done = result.result === 'sent' || TERMINAL.has(result.why);
        job.next = this.now() + Math.min(300000, 5000 * 2 ** Math.min(job.attempts - 1, 6));
        const changed = job.last !== JSON.stringify(result);
        job.last = JSON.stringify(result);
        this.save();
        if (changed) await this.onResult(job, result);
      }
    } finally { this.busy = false; }
  }
}
module.exports = { HistoryQueue };
