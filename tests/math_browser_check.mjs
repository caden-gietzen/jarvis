// Real-browser check for the LaTeX math support (driven by tests/test_math.py; needs puppeteer-core + Chrome).
//   node math_browser_check.mjs <puppeteer-core dir> <chrome.exe> <previewUrl> <webUrl> [screenshotDir]
// Prints one JSON object with the findings; never touches the internet (the servers are on 127.0.0.1).
import { createRequire } from 'module';
import path from 'path';
const [, , pptrDir, chrome, previewUrl, webUrl, shotDir] = process.argv;
const require = createRequire(path.join(pptrDir, 'x.js'));
const puppeteer = require('puppeteer-core');

const browser = await puppeteer.launch({ executablePath: chrome, headless: 'new', args: ['--no-sandbox'] });
const out = {};

async function inspect(page, scope) {
  return page.evaluate((scope) => {
    const root = document.querySelector(scope) || document.body;
    const vis = (el) => el.checkVisibility({ visibilityProperty: true });
    // what the highlighter sees: visible text nodes, in order (same visibility test as content.js collectText)
    const texts = [];
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.parentElement && n.data && vis(n.parentElement)) texts.push(n.data);
    const math = [...root.querySelectorAll('.jv-math')];
    return {
      pwn: typeof window.__pwn !== 'undefined',
      katex: root.querySelectorAll('.katex').length,
      displays: root.querySelectorAll('.katex-display').length,
      mathml: root.querySelectorAll('.katex-mathml, math').length,
      errors: root.querySelectorAll('.jv-math-error').length,
      errorTexts: [...root.querySelectorAll('.jv-math-error')].map((e) => e.textContent),
      jsAnchors: [...root.querySelectorAll('a')].filter((a) => /^\s*javascript:/i.test(a.getAttribute('href') || '')).length,
      anchorsInMath: root.querySelectorAll('.jv-math a').length,
      injectedClass: root.querySelectorAll('.evilclass, [class*="evil"]').length,
      scripts: root.querySelectorAll('script').length,
      mathTexts: math.slice(0, 40).map((m) => m.textContent),
      visibleTextOfFirstMath: math.length ? (() => { const f = math.find((m) => m.querySelector('.katex')); return f ? [...(function* () { const t = document.createTreeWalker(f, NodeFilter.SHOW_TEXT); for (let n = t.nextNode(); n; n = t.nextNode()) if (vis(n.parentElement)) yield n.data; })()].join('') : null; })() : null,
      maxRulePx: Math.max(0, ...[...root.querySelectorAll('.katex .rule')].map((r) => Math.max(r.getBoundingClientRect().width, r.getBoundingClientRect().height))),
      roleMath: root.querySelectorAll('.jv-math[role="math"][aria-label]').length,
    };
  }, scope);
}

async function newPage(w, h) {
  const p = await browser.newPage();
  await p.setViewport({ width: w, height: h, deviceScaleFactor: 1 });
  p.__console = [];
  p.on('console', (m) => p.__console.push(m.type() + ': ' + m.text()));
  p.on('pageerror', (e) => p.__console.push('pageerror: ' + e.message));
  p.__requests = [];
  p.on('request', (r) => p.__requests.push(r.url()));
  p.__failed = [];
  p.on('requestfailed', (r) => p.__failed.push(r.url()));
  await p.evaluateOnNewDocument(() => { document.addEventListener('securitypolicyviolation', (e) => { (window.__csp = window.__csp || []).push(e.violatedDirective + ' ' + e.blockedURI); }); });
  return p;
}

// ---- preview page
{
  const p = await newPage(900, 1200);
  await p.goto(previewUrl, { waitUntil: 'load' });
  await new Promise((r) => setTimeout(r, 500));
  out.preview = await inspect(p, 'main');
  out.preview.csp = await p.evaluate(() => window.__csp || []);
  out.preview.console = p.__console;
  out.preview.external = p.__requests.filter((u) => !u.startsWith(new URL(previewUrl).origin));
  out.preview.failed = p.__failed;
  out.preview.fontsLoaded = await p.evaluate(() => [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family));
  if (shotDir) await p.screenshot({ path: path.join(shotDir, 'preview-study-document.png'), fullPage: false });
  await p.close();
}

// ---- web UI page (real Jarvis web server, real page, real CSP)
{
  const p = await newPage(420, 900);
  await p.goto(webUrl, { waitUntil: 'load' });
  await new Promise((r) => setTimeout(r, 800));  // SSE snapshot delivers the conversation
  out.web = await inspect(p, '#conv');
  out.web.csp = await p.evaluate(() => window.__csp || []);
  out.web.console = p.__console;
  out.web.external = p.__requests.filter((u) => !u.startsWith(new URL(webUrl).origin));
  out.web.fontsLoaded = await p.evaluate(() => [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family));
  // search: typing a word that sits in plain text next to math keeps the math rendered and marks the text
  await p.type('#search', 'convex');
  await new Promise((r) => setTimeout(r, 300));
  out.web.search = await p.evaluate(() => ({ katex: document.querySelectorAll('#conv .msg:not(.hidden) .katex').length,
                                             marks: document.querySelectorAll('#conv mark').length }));
  if (shotDir) { await p.evaluate(() => { const s = document.getElementById('searchClear'); if (s) s.click(); });
                 await new Promise((r) => setTimeout(r, 300));
                 await p.screenshot({ path: path.join(shotDir, 'web-ui-conversation.png') }); }
  await p.close();
}
await browser.close();
console.log(JSON.stringify(out));
