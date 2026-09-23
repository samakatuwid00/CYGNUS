"""ui_audit.py - measure the HUD and the Apps panel as they render.

Not an opinion about the design: every check below is something a person
using a keyboard, a screen reader or a phone runs into, measured in the real
page through a browser.

  names        buttons and links a screen reader would read as nothing
  contrast     text against what is actually behind it (WCAG 2.2: 4.5:1,
               or 3:1 for large text)
  targets      controls smaller than 24x24 CSS px (WCAG 2.2 AA minimum)
  focus        whether keyboard focus is visible at all
  alt          images with no alternative text
  live         whether replies arrive in a region a screen reader announces
  motion       whether the page honours "reduce motion"
  phone        horizontal scrolling at phone width, and the viewport meta
  lang         the page's language, for the screen reader's pronunciation

  python scripts/bench/ui_audit.py
  python scripts/bench/ui_audit.py --shots      also save screenshots

Read-only: it opens pages and looks at them.
"""

import argparse
import asyncio
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import paths  # noqa: E402
import cygnus_client as cc  # noqa: E402

PAGES = {"hud": "/", "apps": "/apps.html"}
SIZES = {"desktop": (1440, 900), "phone": (390, 844)}

# Everything below runs inside the page.
SCRIPT = r"""
() => {
  const out = {names: [], contrast: [], targets: [], alt: [], focus: [], notes: {}};
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
           && s.opacity !== '0';
  };
  const label = el => (el.getAttribute('aria-label') || el.getAttribute('title')
      || (el.getAttribute('aria-labelledby')
          ? (document.getElementById(el.getAttribute('aria-labelledby'))?.innerText || '') : '')
      || el.innerText || el.textContent || el.value || el.getAttribute('alt') || '').trim();
  // NB: a placeholder is not an accessible name; a screen reader may not read it.
  const where = el => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
        ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    return (el.tagName.toLowerCase() + id + cls).slice(0, 60);
  };
  const controls = [...document.querySelectorAll(
      'button, a[href], input, select, textarea, [role=button], [role=link], [role=tab], [onclick]')]
      .filter(vis);
  for (const el of controls) {
    if (!label(el) && !el.querySelector('img[alt]:not([alt=""])')) {
      out.names.push({where: where(el), html: el.outerHTML.slice(0, 120)});
    }
    const r = el.getBoundingClientRect();
    if (Math.min(r.width, r.height) < 24) {
      out.targets.push({where: where(el), size: Math.round(r.width) + 'x' + Math.round(r.height),
                        text: label(el).slice(0, 30)});
    }
  }
  for (const img of [...document.querySelectorAll('img')].filter(vis)) {
    if (!img.hasAttribute('alt')) out.alt.push({where: where(img), src: (img.src || '').slice(-50)});
  }
  // contrast
  const lum = c => {
    const [r, g, b] = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  // rgb(12, 34, 56) and color(srgb 0.8 0.54 0.43): the second is 0..1.
  const rgb = s => {
    const v = (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    return (/^color\(/.test(s) || v.every(x => x <= 1)) && v.length === 3
        ? v.map(x => Math.round(x * 255)) : v;
  };
  // rgba(r,g,b,a) and the modern color(srgb r g b / a) / rgb(r g b / a).
  const alpha = s => {
    let m = s.match(/rgba?\([^)]*,\s*([\d.]+)\)/);
    if (m) return Number(m[1]);
    m = s.match(/\/\s*([\d.]+)(%?)\s*\)/);
    if (m) return m[2] ? Number(m[1]) / 100 : Number(m[1]);
    return s === 'transparent' ? 0 : 1;
  };
  const behind = el => {
    let n = el;
    while (n && n !== document.documentElement) {
      const s = getComputedStyle(n);
      if (alpha(s.backgroundColor) > 0.5) return rgb(s.backgroundColor);
      if (s.backgroundImage && s.backgroundImage !== 'none') return null;   // gradient/image: skip
      n = n.parentElement;
    }
    const s = getComputedStyle(document.body);
    return alpha(s.backgroundColor) > 0.5 ? rgb(s.backgroundColor) : [0, 0, 0];
  };
  const seen = new Set();
  for (const el of [...document.querySelectorAll('body *')].filter(vis)) {
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim())
        .join(' ').trim();
    if (text.length < 2) continue;
    const s = getComputedStyle(el);
    const fg = rgb(s.color), bg = behind(el);
    if (!bg || fg.length < 3) continue;
    const L1 = lum(fg) + 0.05, L2 = lum(bg) + 0.05;
    const ratio = L1 > L2 ? L1 / L2 : L2 / L1;
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight || '400', 10) >= 700;
    const large = size >= 24 || (size >= 18.66 && bold);
    const need = large ? 3 : 4.5;
    const key = where(el) + '|' + text.slice(0, 20);
    if (ratio < need && !seen.has(key)) {
      seen.add(key);
      out.contrast.push({where: where(el), text: text.slice(0, 40), ratio: Math.round(ratio * 100) / 100,
                         need, size: Math.round(size), color: s.color, background: 'rgb(' + bg.join(',') + ')'});
    }
  }
  // Something you can click that the keyboard cannot reach. Both pages
  // delegate clicks through data- attributes, so that is the signal: an
  // element carrying one, with a pointer cursor, that is neither a native
  // control nor inside one and has no role or tabindex. (A pointer cursor
  // alone is not enough: decorative icons carry one too.)
  const ACT = '[data-act], [data-mode], [data-filter], [data-tab], [data-ab], [data-answer], ' +
              '[data-key], [data-rid], [data-verdict], [data-s], [data-desk]';
  const native = 'button, a[href], input, select, textarea, [role=button], [role=link], [role=tab], [role=switch]';
  out.keyboard = [...document.querySelectorAll(ACT)].filter(el =>
      vis(el) && getComputedStyle(el).cursor === 'pointer'
      && !el.matches(native) && !el.closest(native)
      && !el.hasAttribute('role') && !el.hasAttribute('tabindex')
      && !el.querySelector(native))
      .slice(0, 40).map(el => ({where: where(el),
          text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30)}));
  out.notes.controls = controls.length;
  out.notes.lang = document.documentElement.getAttribute('lang') || '';
  out.notes.title = document.title || '';
  out.notes.viewport = document.querySelector('meta[name=viewport]')?.getAttribute('content') || '';
  out.notes.live_regions = [...document.querySelectorAll('[aria-live], [role=status], [role=log], [role=alert]')]
      .map(where);
  out.notes.horizontal_scroll = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  out.notes.reduced_motion_css = [...document.styleSheets].some(sh => {
    try { return [...sh.cssRules].some(r => (r.conditionText || '').includes('prefers-reduced-motion')); }
    catch (e) { return false; }
  });
  out.notes.focusable = controls.filter(el => el.tabIndex >= 0).length;
  return out;
}
"""

FOCUS_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const s = getComputedStyle(el);
  return {tag: el.tagName.toLowerCase(), id: el.id || '', outline: s.outlineStyle + ' ' + s.outlineWidth,
          boxShadow: (s.boxShadow || 'none').slice(0, 40)};
}
"""


async def audit(base, shots):
    from playwright.async_api import async_playwright
    findings = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for page_name, path in PAGES.items():
            for size_name, (w, h) in SIZES.items():
                ctx = await browser.new_context(viewport={"width": w, "height": h},
                                                device_scale_factor=1)
                page = await ctx.new_page()
                await page.goto(base + path, wait_until="networkidle")
                await page.wait_for_timeout(1500)
                data = await page.evaluate(SCRIPT)
                focus = []
                for _ in range(6):
                    await page.keyboard.press("Tab")
                    got = await page.evaluate(FOCUS_SCRIPT)
                    if got:
                        focus.append(got)
                data["focus"] = focus
                if shots:
                    os.makedirs(paths.LOG_DIR, exist_ok=True)
                    await page.screenshot(path=paths.log(f"ui-{page_name}-{size_name}.png"), full_page=False)
                findings.append({"page": page_name, "size": size_name, **data})
                await ctx.close()
        await browser.close()
    return findings


def verdicts(findings):
    out = []
    for f in findings:
        tag = f"{f['page']}/{f['size']}"
        if f["names"]:
            out.append(f"{tag}: {len(f['names'])} control(s) a screen reader reads as nothing, "
                       f"e.g. {f['names'][0]['where']}")
        bad = [c for c in f["contrast"] if c["ratio"] < c["need"]]
        if bad:
            worst = min(bad, key=lambda c: c["ratio"])
            out.append(f"{tag}: {len(bad)} text colour(s) under contrast, worst {worst['ratio']}:1 "
                       f"(needs {worst['need']}) on {worst['where']} “{worst['text'][:24]}”")
        if f["targets"]:
            out.append(f"{tag}: {len(f['targets'])} control(s) under 24x24 px, "
                       f"e.g. {f['targets'][0]['where']} {f['targets'][0]['size']}")
        if f["alt"]:
            out.append(f"{tag}: {len(f['alt'])} image(s) with no alt text")
        if f.get("keyboard"):
            out.append(f"{tag}: {len(f['keyboard'])} thing(s) you can click but cannot reach by "
                       f"keyboard, e.g. {f['keyboard'][0]['where']} “{f['keyboard'][0]['text'][:20]}”")
        if not f["focus"]:
            out.append(f"{tag}: Tab reaches nothing - the page cannot be used from the keyboard")
        elif not any("none" not in x["outline"] or x["boxShadow"] != "none" for x in f["focus"]):
            out.append(f"{tag}: keyboard focus is not visible on the first controls")
        n = f["notes"]
        if not n["lang"]:
            out.append(f"{tag}: no lang on <html> - a screen reader guesses the language")
        if not n["live_regions"]:
            out.append(f"{tag}: no live region - new replies are not announced")
        if f["size"] == "phone" and n["horizontal_scroll"]:
            out.append(f"{tag}: the page scrolls sideways at phone width")
        if f["size"] == "phone" and not n["viewport"]:
            out.append(f"{tag}: no viewport meta - the phone renders it as a desktop page")
        if not n["reduced_motion_css"]:
            out.append(f"{tag}: nothing honours “reduce motion”")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--shots", action="store_true", help="save screenshots next to the report")
    args = ap.parse_args(argv)
    base = cc.find_server(args.url)
    print(f"ui audit: {base}, {len(PAGES)} page(s) x {len(SIZES)} size(s)")
    findings = asyncio.run(audit(base, args.shots))
    found = verdicts(findings)
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"bench-ui-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": base,
                   "findings": found, "detail": findings}, f, indent=2, ensure_ascii=False)
    for f in findings:
        n = f["notes"]
        print(f"  {f['page']}/{f['size']}: {n['controls']} controls, {len(f['names'])} unnamed, "
              f"{len(f['contrast'])} low contrast, {len(f['targets'])} small targets, "
              f"live regions: {len(n['live_regions'])}")
    print("\n" + ("\n".join("  - " + x for x in found) if found else "  nothing to report"))
    print(f"report: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
