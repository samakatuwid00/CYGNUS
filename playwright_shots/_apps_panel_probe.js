// Apps window probe: HUD + apps_panel.html over a stub server (no live Cygnus).
// Checks the HUD bridge (counts in the head, Escape from inside the iframe,
// refresh-not-reload on reopen, focus return), embedded mode, the
// "Search all detected" fallback, arrow-key row nav, and touch-visible actions.
const path = require('path');
const fs = require('fs');
const http = require('http');
const { chromium } = require(path.join(__dirname, '..', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package'));
const ROOT = path.join(__dirname, '..');
const OUT = path.join(__dirname, 'apps_panel');

const APPS = [
  { key: 'spotify', name: 'Spotify', kind: 'gui', category: 'media', registered: true, enabled: true, abilities: [{ id: 'a' }], compiled_rules: [{ rule_id: 'r1' }] },
  { key: 'chrome', name: 'Chrome', kind: 'gui', category: 'browser', registered: true, enabled: true },
  { key: 'code', name: 'VS Code', kind: 'gui', category: 'dev', registered: true, enabled: false },
  { key: 'obsidian', name: 'Obsidian', kind: 'gui', category: 'edit', registered: true, broken: true, bin: 'C:\\x\\obsidian.exe' },
  { key: 'youtube', name: 'YouTube', kind: 'website', category: 'website', host: 'youtube.com', url: 'https://youtube.com', registered: true },
  { key: 'hermes', name: 'Hermes', kind: 'capability', category: 'capability', description: 'General tasks', registered: true },
];
const RAW = APPS.concat([
  { key: 'randomtool', name: 'Random Tool', kind: 'gui', category: 'other', registered: false, enabled: true },
  { key: 'zebrautil', name: 'Zebra Util', kind: 'gui', category: 'util', registered: false, enabled: true },
]);

function serve() {
  const files = { '/': 'jarvis_hud_v3.html', '/jarvis_hud_v3.html': 'jarvis_hud_v3.html', '/apps.html': 'apps_panel.html' };
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      const u = req.url.split('?')[0];
      if (files[u]) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        return res.end(fs.readFileSync(path.join(ROOT, files[u])));
      }
      if (u === '/apps') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ curated: APPS, all: RAW, curated_count: APPS.length, all_count: RAW.length, hidden_count: 0 }));
      }
      if (u === '/apps/abilities/scan-all') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end('{"running":false}'); }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{}');
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

async function launch() {
  try { return await chromium.launch(); } catch (e) { return await chromium.launch({ channel: 'msedge' }); }
}

const UA_ANDROID = 'Mozilla/5.0 (Linux; Android 14; POCO X7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36';

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const srv = await serve();
  const base = 'http://127.0.0.1:' + srv.address().port;
  const browser = await launch();
  const fails = [];
  const check = (ok, label) => { console.log((ok ? 'ok:   ' : 'FAIL: ') + label); if (!ok) fails.push(label); };

  // ---------------------------------------------------------------- desktop
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e.message).slice(0, 160)));
    await page.goto(base + '/?demo=1');
    await page.waitForTimeout(800);
    const frame = () => page.frame({ url: /apps\.html/ });
    await page.waitForFunction(() => document.getElementById('jv-apps-count').textContent.length > 0, null, { timeout: 8000 }).catch(() => {});
    const count0 = await page.$eval('#jv-apps-count', e => e.textContent);
    check(/6 CURATED · 8 DETECTED · 1 BROKEN/.test(count0), 'HUD head shows counts from the panel: ' + JSON.stringify(count0));
    check(await frame().$eval('body', b => b.classList.contains('embedded')), 'panel knows it is embedded');
    check(await frame().$eval('.title', t => getComputedStyle(t).display === 'none'), 'embedded panel hides its duplicate page title');

    await page.click('#jv-apps');
    await page.waitForTimeout(400);
    check(await page.$eval('#jv-apps', b => b.getAttribute('aria-expanded') === 'true'), 'Apps button aria-expanded=true when open');
    check(await frame().evaluate(() => document.activeElement && document.activeElement.id === 'search'), 'open puts focus in the panel search box');
    await frame().evaluate(() => { window.__marker = 42; });
    await page.screenshot({ path: path.join(OUT, 'desktop-open.png') });

    // Search miss in curated, hit in raw -> fallback button switches the tab.
    await frame().fill('#search', 'zebra');
    await page.waitForTimeout(150);
    check(!(await frame().$eval('#search-clear', b => b.hidden)), 'search clear button appears while typing');
    const fb = await frame().$('[data-act="browse"]');
    check(!!fb, 'curated miss offers "Search all detected"');
    await page.screenshot({ path: path.join(OUT, 'desktop-search-fallback.png') });
    if (fb) { await fb.click(); await page.waitForTimeout(150); }
    check(await frame().$eval('#tab-all', t => t.classList.contains('active')), 'fallback switches to All Detected');
    check((await frame().$$('.row')).length === 1, 'All Detected shows the one match');
    await frame().click('#search-clear');
    await page.waitForTimeout(150);
    check(await frame().$eval('#search', s => s.value === '' && document.activeElement === s), 'clear button empties and refocuses the search');
    await frame().click('#tab-curated');
    await page.waitForTimeout(150);

    // Arrow keys walk the rows from the search box.
    await frame().focus('#search');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    const focused = await frame().evaluate(() => { const r = document.activeElement.closest('.row'); return r && r.dataset.key; });
    check(!!focused, 'ArrowDown from the search box steps into the rows (' + focused + ')');
    await page.keyboard.press('End');
    const last = await frame().evaluate(() => { const r = document.activeElement.closest('.row'); return r && r.dataset.key; });
    check(last === 'hermes', 'End jumps to the last row (' + last + ')');

    // Escape inside the iframe (nothing to dismiss) closes the HUD window.
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    check(!(await page.$eval('#jv-apps-modal', m => m.classList.contains('show'))), 'Escape pressed inside the iframe closes the Apps window');
    check(await page.evaluate(() => document.activeElement && document.activeElement.id === 'jv-apps'), 'focus returns to the Apps button on close');

    // Reopen: same document (no reload), data refetched.
    await page.click('#jv-apps');
    await page.waitForTimeout(400);
    check(await frame().evaluate(() => window.__marker === 42), 'reopen keeps the panel document (no reload)');

    // Escape with a drawer open closes the drawer first, not the window.
    await frame().click('.row[data-key="spotify"] .main');
    await page.waitForTimeout(300);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    check(await page.$eval('#jv-apps-modal', m => m.classList.contains('show')), 'Escape with the app drawer open closes only the drawer');
    check(!(await frame().$eval('#drawer', d => d.classList.contains('open'))), 'app drawer closed by Escape');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    check(!(await page.$eval('#jv-apps-modal', m => m.classList.contains('show'))), 'second Escape closes the window');
    check(errors.length === 0, 'no page errors on desktop: ' + JSON.stringify(errors));
    await ctx.close();
  }

  // ------------------------------------------------------------------ phone
  {
    const ctx = await browser.newContext({ viewport: { width: 400, height: 860 }, hasTouch: true, isMobile: true, userAgent: UA_ANDROID });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e.message).slice(0, 160)));
    await page.goto(base + '/?demo=1');
    await page.waitForTimeout(800);
    await page.click('#jv-apps');
    await page.waitForTimeout(500);
    const frame = page.frame({ url: /apps\.html/ });
    check(await frame.evaluate(() => document.activeElement === document.body || document.activeElement.id !== 'search'), 'phone: search not auto-focused (no keyboard pop-up)');
    check(await page.$eval('#jv-apps-count', c => getComputedStyle(c).display === 'none'), 'phone: HUD head count hidden so the title does not wrap');
    check(await page.$eval('#jv-apps-title', t => t.getBoundingClientRect().height < 30), 'phone: Apps title on one line');
    await page.screenshot({ path: path.join(OUT, 'phone-open.png') });
    check(errors.length === 0, 'no page errors on phone: ' + JSON.stringify(errors));
    await ctx.close();
  }
  // Tablet (touch, wide enough to keep the row buttons): Open must be visible without hover.
  {
    const ctx = await browser.newContext({ viewport: { width: 768, height: 1024 }, hasTouch: true, isMobile: true, userAgent: UA_ANDROID });
    const page = await ctx.newPage();
    await page.goto(base + '/?demo=1');
    await page.waitForTimeout(800);
    await page.click('#jv-apps');
    await page.waitForTimeout(500);
    const frame = page.frame({ url: /apps\.html/ });
    const op = await frame.$eval('.row[data-key="chrome"] .acts .btn', b => getComputedStyle(b).opacity);
    check(op === '1', 'tablet touch: row Open button visible without hover (opacity ' + op + ')');
    await page.screenshot({ path: path.join(OUT, 'tablet-open.png') });
    await ctx.close();
  }

  await browser.close();
  srv.close();
  console.log(fails.length ? `\n${fails.length} FAILED` : '\nALL PASS');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
