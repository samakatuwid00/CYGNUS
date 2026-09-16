// Apps panel (apps_panel.html) header probe. Loads the panel from disk with a
// mocked /apps payload, screenshots the top of the page at the widths the HUD
// modal gives it (960 card on desktop; phone widths dock it full-width) and
// reports header-row layout issues.
//   node playwright_shots/_apps_panel_shot.js [outdir-suffix]
const path = require('path');
const fs = require('fs');
const { chromium } = require(path.join(__dirname, '..', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package'));
const http = require('http');
const PANEL = path.join(__dirname, '..', 'apps_panel.html');
const OUT = path.join(__dirname, 'apps_panel' + (process.argv[2] ? '-' + process.argv[2] : ''));
const SIZES = [[360, 720, true], [400, 780, true], [700, 600, true], [860, 700, false], [958, 720, false], [1200, 800, false]];

const app = (key, name, category, extra) => Object.assign({
  key, name, category, kind: 'gui', enabled: true, broken: false, registered: true,
  compiled_rules: [], abilities: [], bin: 'C:\\Program Files\\' + name + '\\' + name + '.exe'
}, extra || {});
const APPS = [
  app('code', 'VS Code', 'dev', { abilities: [1, 2, 3, 4], compiled_rules: [{ rule_id: 'r1' }] }),
  app('chrome', 'Chrome', 'browser', { abilities: [1, 2], compiled_rules: [{ rule_id: 'r2' }, { rule_id: 'r3' }] }),
  app('brave', 'Brave', 'browser'),
  app('spotify', 'Spotify', 'media', { abilities: [1, 2, 3, 4, 5, 6] }),
  app('vlc', 'VLC', 'media', { enabled: false }),
  app('obsidian', 'Obsidian', 'edit', { broken: true, bin: 'C:\\missing\\Obsidian.exe' }),
  app('excel', 'Excel', 'office'),
  { key: 'site:youtube.com', name: 'YouTube', kind: 'website', category: 'website', host: 'youtube.com', url: 'https://youtube.com', enabled: true, broken: false, registered: true, visits: 12, compiled_rules: [] },
  { key: 'site:github.com', name: 'GitHub', kind: 'website', category: 'website', host: 'github.com', url: 'https://github.com', enabled: true, broken: false, registered: true, visits: 4, compiled_rules: [] },
  { key: 'cap:web_search', name: 'Web search', kind: 'capability', category: 'capability', description: 'Search the web and read pages', adapter: 'local', enabled: true, broken: false, compiled_rules: [] },
];
const RAW = APPS.concat([app('asusfeatureservice', 'ASUS Feature Service', 'other'), app('epsonscan2', 'Epson Scan 2', 'util')]);

async function launch() {
  try { return await chromium.launch(); } catch (e) { return await chromium.launch({ channel: 'msedge' }); }
}

// Chromium will not fetch("/apps") from a file:// page, so serve the panel and
// a fake /apps from a throwaway HTTP server instead.
function serve() {
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      const p = req.url.split('?')[0];
      const json = (o) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); };
      if (p === '/apps.html' || p === '/') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(fs.readFileSync(PANEL)); }
      // The HUD shows the panel in an iframe (body.embedded); this mimics that.
      else if (p === '/frame.html') { res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><body style="margin:0;background:#000"><iframe src="/apps.html" style="border:0;width:100vw;height:100vh"></iframe>'); }
      else if (p === '/apps') json({ curated: APPS, all: RAW });
      else if (p === '/apps/abilities/scan-all') json({ running: false });
      else json({});
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const srv = await serve();
  const base = 'http://127.0.0.1:' + srv.address().port;
  const browser = await launch();
  const report = [];
  const RUNS = [];
  for (const s of SIZES) { RUNS.push([...s, false]); RUNS.push([...s, true]); }
  for (const [w, h, touch, embed] of RUNS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, hasTouch: touch, isMobile: touch, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e.message).slice(0, 160)));
    await page.goto(base + (embed ? '/frame.html' : '/apps.html'));
    await page.waitForTimeout(600);
    const f = embed ? page.frames().find(x => /apps\.html/.test(x.url())) : page.mainFrame();
    const tag = `${w}x${h}${touch ? '-touch' : ''}${embed ? '-embedded' : ''}`;
    await page.screenshot({ path: path.join(OUT, tag + '.png') });
    const r = await f.evaluate(() => {
      const vw = innerWidth, issues = [], small = [];
      const vis = el => { const s = getComputedStyle(el); const b = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0; };
      if (document.documentElement.scrollWidth > vw + 1) issues.push('h-overflow ' + document.documentElement.scrollWidth);
      const head = document.querySelector('header.top');
      const headB = head.getBoundingClientRect();
      const visible = [];
      const tabs = document.querySelector('.tabs');
      const tabsScroll = tabs.scrollWidth > tabs.clientWidth + 1;   // phones: tabs scroll sideways by design
      head.querySelectorAll('button, input, .stat').forEach(el => {
        if (!vis(el)) return;
        const b = el.getBoundingClientRect();
        visible.push((el.id || el.className.split(' ')[0] || el.tagName) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height));
        if ((b.right > vw + 0.5 || b.left < -0.5) && !(tabsScroll && el.classList.contains('tab'))) issues.push('off-screen ' + (el.id || el.className));
        if (el.tagName === 'BUTTON' && el.scrollWidth > el.clientWidth + 1) issues.push('label clipped ' + (el.id || el.textContent.trim()));
      });
      // Header rows must not wrap onto extra lines unexpectedly: compare the
      // top-row's height with its tallest child.
      const topRow = document.querySelector('.top-row');
      const kids = [...topRow.children].filter(vis).map(k => k.getBoundingClientRect());
      const tallest = Math.max(...kids.map(k => k.height));
      if (topRow.getBoundingClientRect().height > tallest + 24) issues.push('top-row wraps ' + Math.round(topRow.getBoundingClientRect().height));
      return { headerHeight: Math.round(headB.height), visible, issues };
    });
    // Touch: every visible header button is at least 40px tall.
    if (touch) {
      const smalls = await f.evaluate(() => {
        const out = [];
        document.querySelectorAll('header.top button').forEach(el => {
          const s = getComputedStyle(el); const b = el.getBoundingClientRect();
          if (s.display === 'none' || !b.width) return;
          if (b.height < 39.5) out.push((el.id || el.textContent.trim()) + ' ' + Math.round(b.width) + 'x' + Math.round(b.height));
        });
        return out;
      });
      if (smalls.length) r.issues.push('small touch targets: ' + smalls.join(', '));
    }
    // Open the ⋯ menu if it is shown, and the Add row, and shoot those too.
    if (await f.isVisible('#h-more')) {
      await f.click('#h-more');
      await page.waitForTimeout(200);
      await page.screenshot({ path: path.join(OUT, tag + '-menu.png') });
      const menuOk = await f.evaluate(() => {
        const m = document.getElementById('h-menu'), b = m.getBoundingClientRect();
        return !m.hidden && b.right <= innerWidth + 0.5 && b.left >= -0.5 && [...m.querySelectorAll('[data-for]')].length === 3;
      });
      if (!menuOk) r.issues.push('menu missing or off-screen');
      await page.keyboard.press('Escape');
      const closed = await f.evaluate(() => document.getElementById('h-menu').hidden);
      if (!closed) r.issues.push('Escape did not close menu');
    }
    await f.evaluate(() => document.getElementById('h-add').click());
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(OUT, tag + '-add.png') });
    const addOk = await f.evaluate(() => {
      const row = document.getElementById('add-row'), q = document.getElementById('add-q');
      const b = row.getBoundingClientRect();
      return !row.hidden && document.activeElement === q && b.right <= innerWidth + 0.5 &&
        document.getElementById('h-add').getAttribute('aria-expanded') === 'true';
    });
    if (!addOk) r.issues.push('add row not shown/focused');
    // Busy state: a scanning button shows its ring and stays on one line.
    await f.evaluate(() => { const b = document.getElementById('h-scan'); b.disabled = true; b.classList.add('is-busy'); });
    await page.waitForTimeout(100);
    await page.screenshot({ path: path.join(OUT, tag + '-busy.png') });
    r.errors = errors;
    report.push([tag, r]);
    await ctx.close();
  }
  await browser.close();
  srv.close();
  for (const [tag, r] of report) console.log(tag, JSON.stringify(r));
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1));
})();
