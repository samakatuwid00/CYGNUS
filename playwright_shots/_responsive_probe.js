// RESPONSIVE_PLAN.md matrix probe. Loads the HUD from disk in demo mode.
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package'));
const url = 'file:///' + path.join(__dirname, '..', 'jarvis_hud_v3.html').replace(/\\/g, '/') + '?demo=1';
const OUT = path.join(__dirname, 'responsive');
const UA_ANDROID = 'Mozilla/5.0 (Linux; Android 14; POCO X7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36';
const SIZES = [
  [360, 780, true], [400, 860, true], [860, 400, true], [915, 412, true],
  [768, 1024, true], [1024, 768, true], [1024, 700, false],
  [1280, 800, false], [1400, 900, false], [1920, 1080, false],
];

async function launch() {
  try { return await chromium.launch(); } catch (e) { return await chromium.launch({ channel: 'msedge' }); }
}

(async () => {
  require('fs').mkdirSync(OUT, { recursive: true });
  const browser = await launch();
  const report = [];
  for (const [w, h, touch] of SIZES) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, hasTouch: touch, isMobile: touch,
      userAgent: touch ? UA_ANDROID : undefined, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(String(e.message).slice(0, 120)));
    await page.goto(url);
    await page.waitForTimeout(1200);
    const tag = `${w}x${h}${touch ? '-touch' : ''}`;
    await page.screenshot({ path: path.join(OUT, tag + '.png') });
    const r = await page.evaluate(({ touch }) => {
      const vw = innerWidth, vh = innerHeight, issues = [];
      const box = s => { const el = document.querySelector(s); if (!el) return null;
        const b = el.getBoundingClientRect(); return { x: b.left, y: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height }; };
      const inside = (b) => b && b.x >= -0.5 && b.y >= -0.5 && b.r <= vw + 0.5 && b.b <= vh + 0.5;
      const vis = el => { const s = getComputedStyle(el); const b = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0; };
      if (document.documentElement.scrollWidth > vw + 1) issues.push('h-overflow ' + document.documentElement.scrollWidth);
      if (document.body.scrollWidth > vw + 1) issues.push('body h-overflow ' + document.body.scrollWidth);
      for (const s of ['#jv-topbar', '#jv-feed', '#jv-input-wrap', '#jv-mic']) {
        const b = box(s); if (!b) { issues.push('missing ' + s); continue; }
        if (!inside(b)) issues.push(`${s} out of viewport ${JSON.stringify(b)}`);
      }
      // Every visible control in the top bar stays on-screen.
      document.querySelectorAll('#jv-topbar button, #jv-topbar .jv-pill').forEach(el => {
        if (!vis(el)) return;
        const b = el.getBoundingClientRect();
        if (b.right > vw + 0.5 || b.left < -0.5) issues.push('topbar item off-screen ' + (el.id || el.className));
      });
      const status = document.getElementById('jv-status');
      if (status && vis(status) && status.getBoundingClientRect().height > parseFloat(getComputedStyle(status).lineHeight || 20) * 1.6)
        issues.push('status wraps');
      const small = [];
      if (touch) {
        document.querySelectorAll('#jv-shell button, #jv-shell .vf-opt, #jv-shell [role=button]').forEach(el => {
          if (!vis(el)) return; const b = el.getBoundingClientRect();
          if (b.width < 43.5 || b.height < 43.5) small.push(`${el.id || el.className || el.tagName} ${Math.round(b.width)}x${Math.round(b.height)}`);
        });
        const fs = parseFloat(getComputedStyle(document.getElementById('jv-input')).fontSize);
        if (fs < 16) issues.push('input font ' + fs);
      }
      const feed = box('#jv-feed');
      return { issues, small, feedH: feed && Math.round(feed.h), inputY: Math.round(box('#jv-input-wrap').b),
        placeholder: document.getElementById('jv-input').placeholder,
        panelsBtn: vis(document.getElementById('jv-panels')),
        cols: ['#jv-col-left', '#jv-col-right'].map(s => { const e = document.querySelector(s);
          return e && vis(e) ? { sh: e.scrollHeight, ch: e.clientHeight, oy: getComputedStyle(e).overflowY } : null; }) };
    }, { touch });

    // Drawer: every tab's section reachable and on-screen.
    if (r.panelsBtn) {
      await page.click('#jv-panels');
      await page.waitForTimeout(300);
      const tabs = await page.$$eval('#jv-drawer-tabs [data-tab]', els => els.map(e => e.dataset.tab));
      r.drawer = [];
      for (const t of tabs) {
        await page.click(`#jv-drawer-tabs [data-tab="${t}"]`);
        await page.waitForTimeout(120);
        const d = await page.evaluate((t) => {
          const body = document.getElementById('jv-drawer-body');
          const on = [...body.children].filter(c => getComputedStyle(c).display !== 'none');
          const b = body.getBoundingClientRect();
          const card = document.getElementById('jv-drawer-card').getBoundingClientRect();
          const closeB = document.getElementById('jv-drawer-close').getBoundingClientRect();
          const tabB = document.querySelector(`#jv-drawer-tabs [data-tab="${t}"]`).getBoundingClientRect();
          return { t, shown: on.map(c => c.id || c.className), bodyH: Math.round(b.height),
            cardOk: card.left >= -0.5 && card.right <= innerWidth + 0.5 && card.bottom <= innerHeight + 0.5,
            tabOn: tabB.right <= innerWidth + 0.5 && tabB.left >= -0.5, tabH: Math.round(tabB.height),
            close: Math.round(closeB.width) + 'x' + Math.round(closeB.height) };
        }, t);
        r.drawer.push(d);
      }
      await page.screenshot({ path: path.join(OUT, tag + '-drawer.png') });
      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);
      r.drawerClosedByEsc = await page.evaluate(() => !document.getElementById('jv-drawer').classList.contains('show'));
    }
    // Apps modal fits the screen.
    const appsSel = await page.evaluate(() => {
      const b = document.querySelector('[aria-controls="jv-apps-modal"], #jv-apps-btn, button[aria-label="Apps"]');
      return b ? (b.id ? '#' + b.id : null) : null;
    });
    if (appsSel) {
      await page.click(appsSel);
      await page.waitForTimeout(300);
      r.apps = await page.evaluate(() => {
        const c = document.getElementById('jv-apps-card').getBoundingClientRect();
        const x = document.getElementById('jv-apps-close').getBoundingClientRect();
        return { card: [Math.round(c.left), Math.round(c.top), Math.round(c.width), Math.round(c.height)],
          inside: c.left >= -0.5 && c.top >= -0.5 && c.right <= innerWidth + 0.5 && c.bottom <= innerHeight + 0.5,
          close: Math.round(x.width) + 'x' + Math.round(x.height) };
      });
      await page.screenshot({ path: path.join(OUT, tag + '-apps.png') });
    } else r.apps = 'no apps button found';
    r.errors = errors;
    report.push({ tag, ...r });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(report, null, 1));
})().catch(e => { console.error(e); process.exit(1); });
