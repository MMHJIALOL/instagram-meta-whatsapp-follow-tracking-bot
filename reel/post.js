// After render.js: per-scene MP4s, last-frame PNG per scene, single-frame pop scan.
const { spawnSync } = require('child_process');
const fs = require('fs');
const ffmpeg = require('ffmpeg-static');
const { open } = require('./still.js');

const { scenes: SCENES } = require('./film.json');
const FPS = 60, ENC = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p', '-movflags', '+faststart'];

function ff(args) {
  const r = spawnSync(ffmpeg, args, { encoding: 'utf8', maxBuffer: 1 << 28 });
  if (r.status !== 0) throw new Error(r.stderr);
  return r.stderr;
}

(async () => {
  const scanOnly = process.argv[2] === 'scan';
  fs.mkdirSync('out/scenes', { recursive: true });
  if (!scanOnly) {
  for (const [n, a, b] of SCENES)
    ff(['-y', '-loglevel', 'error', '-i', 'out/film.mp4', '-ss', String(a), '-to', String(b), ...ENC, `out/scenes/${n}.mp4`]);

  const { srv, browser, page } = await open();
  for (const [n, , b] of SCENES) {
    await page.evaluate(t => seek(t), b - 1 / FPS);
    await page.locator('#stage').screenshot({ path: `out/scenes/${n}_last.png` });
  }
  await browser.close(); srv.close();
  }

  // mean abs luma difference between consecutive decoded frames (960x540 gray)
  const W = 540, H = 960, N = W * H;
  const src = process.argv[3] || 'out/film.mp4';
  const raw = spawnSync(ffmpeg, ['-loglevel', 'error', '-i', src, '-vf', `scale=${W}:${H},format=gray`,
    '-f', 'rawvideo', '-'], { maxBuffer: 2.5e9 }).stdout;
  const nf = raw.length / N, d = [];
  for (let f = 1; f < nf; f++) {
    let s = 0; const o = f * N, q = o - N;
    for (let i = 0; i < N; i++) { const v = raw[o + i] - raw[q + i]; s += v < 0 ? -v : v; }
    d.push(s / N);
  }
  if (!process.argv[3]) fs.writeFileSync('out/popscan.txt', d.map((v, i) => `${i + 1} ${((i + 1) / FPS).toFixed(3)} ${v.toFixed(4)}`).join('\n'));
  // spike = 3x both neighbours; below 0.25 mean it is a sub-pixel glyph snap at an ease tail, not a visible pop
  const pops = [], snaps = [];
  for (let i = 1; i < d.length - 1; i++)
    if (d[i] > 0.02 && d[i] > 3 * Math.max(d[i - 1], d[i + 1]))
      (d[i] > 0.25 ? pops : snaps).push(`frame ${i + 1} (t=${((i + 1) / FPS).toFixed(3)}s) diff ${d[i].toFixed(3)} vs ${d[i - 1].toFixed(3)}/${d[i + 1].toFixed(3)}`);
  console.log(`${d.length} frame diffs: ${pops.length} visible pops, ${snaps.length} sub-pixel snaps`);
  pops.forEach(p => console.log('  POP  ' + p));
  snaps.forEach(p => console.log('  snap ' + p));
})();
