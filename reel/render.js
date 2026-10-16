// Full render: 60 fps, 4 subframes per frame blended by ffmpeg tmix, H.264 yuv420p crf 16.
// usage: node render.js [start=0] [end=DUR] [out=out/film.mp4]
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const ffmpeg = require('ffmpeg-static');
const { open } = require('./still.js');

const FPS = 60, SUB = 4;
const t0 = +(process.argv[2] || 0), t1 = +(process.argv[3] || require('./film.json').duration);
const out = process.argv[4] || path.join('out', 'film.mp4');

(async () => {
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const { srv, browser, page } = await open();
  const ff = spawn(ffmpeg, ['-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(FPS * SUB), '-c:v', 'png', '-i', '-',
    '-vf', `tmix=frames=${SUB},select=not(mod(n+1\\,${SUB})),setpts=N/(${FPS}*TB)`,
    '-r', String(FPS), '-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise(r => ff.on('close', r));
  const nF = Math.round((t1 - t0) * FPS), start = Date.now();
  for (let f = 0; f < nF; f++) {
    for (let s = 0; s < SUB; s++) {
      // subframes centred on the frame time, spread over half a frame (180-degree shutter)
      const t = Math.max(0, t0 + (f + (s - (SUB - 1) / 2) / (2 * SUB)) / FPS);
      await page.evaluate(t => seek(t), t);
      const buf = await page.screenshot({ type: 'png' });
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    }
    if (f % 300 === 299) console.log(`frame ${f + 1}/${nF}  ${((Date.now() - start) / 1000).toFixed(0)} s`);
  }
  ff.stdin.end();
  const code = await done;
  await browser.close(); srv.close();
  console.log(code === 0 ? `wrote ${out}` : `ffmpeg exited ${code}`);
})();
