#!/usr/bin/env node
/*
  Video in loop delle schermate TV e fotogrammi dell'avvio (Plymouth), da graphics/src/tv-loops.html.

      node graphics/src/render_loops.mjs             # tutto
      node graphics/src/render_loops.mjs idle boot   # solo alcune

  Si lancia sul computer, non su Cucù (servono Google Chrome, ffmpeg, Python con Pillow e
  Node ≥ 22, che ha WebSocket di serie: nessuna dipendenza npm). Chrome headless viene
  guidato con il protocollo DevTools: per ogni fotogramma la pagina va all'istante giusto
  con seek() (10 fps, come tutte le animazioni di Cucù) e si cattura lo schermo.

  - Schermate: ffmpeg monta i fotogrammi in H.264 1080p a 8 bit, quello che il decoder
    hardware del Pi Zero 2 W regge → graphics/loops/*.mp4
  - Avvio: Plymouth non riproduce video e i suoi file finiscono nell'initramfs, quindi
    il fondo (sole e nuvole) è a tutto schermo in 3 varianti (passo uno) e il gufetto è
    ritagliato da solo, su fondo trasparente, in un riquadro piccolo → plymouth/boot_*.png.
    Da un fotogramma dell'avvio esce anche graphics/splash.png.
  Tutto va nel repository e arriva ai dispositivi con l'OTA.
*/
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, statSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SRC = dirname(fileURLToPath(import.meta.url));
const ROOT = join(SRC, '..', '..');
const LOOPS = join(ROOT, 'graphics', 'loops');
const PLYMOUTH = join(ROOT, 'plymouth');
const HTML = pathToFileURL(join(SRC, 'tv-loops.html')).href;
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9333, FPS = 10;

// schermata (id nell'HTML) → video usato da read_nfc.py
const SCREENS = { idle: 'idle', end: 'end', next: 'wait_next', rest: 'rest', listen: 'listen' };

// Avvio: deve combaciare con plymouth/cucu.script
const BOOT = {
  intro: 24,                                     // fotogrammi di entrata (2,4 s), poi il loop
  owl: { x: 800, y: 330, width: 330, height: 420 },   // riquadro del gufetto, salti e ala compresi
  variants: 3, boil: 4,                          // fondo: 3 ritagli, uno ogni 4 fotogrammi
  splash: 30,                                    // fotogramma per graphics/splash.png
};

const wanted = process.argv.slice(2).length ? process.argv.slice(2) : [...Object.keys(SCREENS), 'boot'];
for (const id of wanted) if (!SCREENS[id] && id !== 'boot') { console.error(`Schermata sconosciuta: ${id}`); process.exit(1); }

const profile = mkdtempSync(join(tmpdir(), 'cucu-chrome-'));
const chrome = spawn(CHROME, ['--headless', '--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1920,1080',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, '--allow-file-access-from-files', 'about:blank'], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function target() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find(t => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* Chrome non è ancora pronto */ }
    await sleep(200);
  }
  throw new Error('Chrome non risponde');
}

let ws, nextId = 0;
const pending = new Map(), waiters = [];
function cdp(method, params = {}) {
  const id = ++nextId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}
const once = name => new Promise(res => waiters.push({ name, res }));

// apre una schermata e ne restituisce la durata del loop (s)
async function open(id, query = '') {
  // passando da una schermata all'altra cambierebbe solo l'hash, senza ricaricare: prima una pagina vuota
  await cdp('Page.navigate', { url: 'about:blank' });
  const loaded = once('Page.loadEventFired');
  await cdp('Page.navigate', { url: `${HTML}?fermo${query}#${id}` });
  await loaded;
  await sleep(300);
  return (await cdp('Runtime.evaluate', { expression: `DUR['${id}']`, returnByValue: true })).result.value;
}

const FULL = { x: 0, y: 0, width: 1920, height: 1080 };
async function shot(id, frame, file, clip = FULL) {
  // due rAF: lo stile delle animazioni è applicato e dipinto prima della cattura
  await cdp('Runtime.evaluate', {
    expression: `seek('${id}', ${frame / FPS}); new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`,
    awaitPromise: true,
  });
  const { data } = await cdp('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 } });
  writeFileSync(file, Buffer.from(data, 'base64'));
}

async function renderLoop(id) {
  const dur = await open(id);
  const frames = Math.round(dur * FPS);
  const dir = mkdtempSync(join(tmpdir(), `cucu-${id}-`));
  process.stdout.write(`${id}: ${frames} fotogrammi `);
  for (let f = 0; f < frames; f++) {
    await shot(id, f, join(dir, `${String(f).padStart(4, '0')}.png`));
    if (f % 20 === 0) process.stdout.write('.');
  }
  const dest = join(LOOPS, `${SCREENS[id]}.mp4`);
  // B-frame attivi (default di x264): sul Pi i video senza B-frame vanno a metà dei fotogrammi
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(FPS), '-i', join(dir, '%04d.png'),
    '-c:v', 'libx264', '-preset', 'slow', '-tune', 'animation', '-crf', '20', '-pix_fmt', 'yuv420p',
    '-profile:v', 'high', '-level', '4.0', '-r', String(FPS), '-movflags', '+faststart', dest]);
  rmSync(dir, { recursive: true, force: true });
  console.log(` ${relative(ROOT, dest)} (${Math.round(statSync(dest).size / 1024)} KB, ${dur} s)`);
}

async function renderBoot() {
  const dir = mkdtempSync(join(tmpdir(), 'cucu-boot-'));
  // splash: l'avvio intero, fermo su un fotogramma
  await open('boot');
  await shot('boot', BOOT.splash, join(dir, 'splash.png'));
  // il fondo senza gufetto, una cattura per ogni variante del passo uno
  await open('boot', '&parte=fondo');
  for (let v = 0; v < BOOT.variants; v++) await shot('boot', v * BOOT.boil, join(dir, `boot_bg_${v}.png`));
  // il gufetto da solo, su fondo trasparente: colori piatti, pochi KB a fotogramma
  const frames = Math.round(await open('boot', '&parte=gufo') * FPS);
  await cdp('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
  process.stdout.write(`boot: ${frames} fotogrammi del gufetto + ${BOOT.variants} fondi `);
  for (let f = 0; f < frames; f++) {
    await shot('boot', f, join(dir, `boot_owl_${f}.png`), BOOT.owl);
    if (f % 20 === 0) process.stdout.write('.');
  }
  await cdp('Emulation.setDefaultBackgroundColorOverride', {});

  for (const f of readdirSync(PLYMOUTH)) if (/^boot_.*\.png$/.test(f)) rmSync(join(PLYMOUTH, f));
  // fondi a 256 colori con dithering: metà del peso (i fotogrammi finiscono nell'initramfs)
  execFileSync('python3', ['-c', `
import sys, os
from PIL import Image
src, dst, splash = sys.argv[1:4]
for f in sorted(os.listdir(src)):
    im = Image.open(os.path.join(src, f))
    if f == 'splash.png':
        im.convert('RGB').save(splash, optimize=True)
    elif f.startswith('boot_owl_'):
        im.convert('RGBA').save(os.path.join(dst, f), optimize=True)   # trasparente: Plymouth lo posa sul fondo
    else:
        im = im.convert('RGB').quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        im.save(os.path.join(dst, f), optimize=True)
`, dir, PLYMOUTH, join(ROOT, 'graphics', 'splash.png')]);
  rmSync(dir, { recursive: true, force: true });
  const kb = readdirSync(PLYMOUTH).filter(f => f.startsWith('boot_')).reduce((s, f) => s + statSync(join(PLYMOUTH, f)).size, 0) / 1024;
  console.log(` plymouth/boot_*.png (${Math.round(kb)} KB in tutto), graphics/splash.png`);
}

async function main() {
  ws = new WebSocket(await target());
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
    } else if (m.method) {
      for (let i = waiters.length - 1; i >= 0; i--) if (waiters[i].name === m.method) waiters.splice(i, 1)[0].res(m.params);
    }
  });
  await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });

  mkdirSync(LOOPS, { recursive: true });
  for (const id of wanted) id === 'boot' ? await renderBoot() : await renderLoop(id);
}

main()
  .catch(err => { console.error(err.message); process.exitCode = 1; })
  .finally(() => { ws?.close(); chrome.kill(); setTimeout(() => rmSync(profile, { recursive: true, force: true }), 500); });
