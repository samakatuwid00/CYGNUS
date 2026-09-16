// Screenshots running_panel_mockup.html and exercises the stop-confirm flow.
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package'));
(async () => {
  let b;
  try { b = await chromium.launch(); } catch (e) { b = await chromium.launch({ channel: 'msedge' }); }
  const p = await b.newPage({ viewport: { width: 1360, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto('file://' + path.resolve(__dirname, '..', 'running_panel_mockup.html').replace(/\\/g, '/'));
  await p.waitForTimeout(1200);
  await p.screenshot({ path: path.join(__dirname, 'running_panel_mockup.png'), fullPage: true });
  await p.click('#desk [data-act="toggle"][data-id="s2"]');
  await p.click('#desk [data-act="stop"][data-id="s2"]');
  await p.waitForTimeout(200);
  await p.locator('#desk').screenshot({ path: path.join(__dirname, 'running_panel_mockup_stop.png') });
  console.log('errors:', JSON.stringify(errs));
  await b.close();
})();
