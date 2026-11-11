// Injected on demand by background.js (chrome.scripting.executeScript, isolated world).
// Read-only: finds text, scrolls, highlights, shows a caption. Never clicks, types or submits.
// Wrapped in an IIFE so injecting it twice into the same page does not redeclare anything.
(function () {
  'use strict';

  // ---- pure text matching (unit-tested in Node) -------------------------------------
  // items: array of strings, or {text, sep}. sep:true means "a block boundary comes before
  // this text", which counts as whitespace; plain adjacent items (inline elements) join with
  // no space, so "foo<b>bar</b>" matches "foobar".
  function buildIndex(items) {
    let norm = '';
    const mi = []; // normalized char -> item index (-1 = synthetic space)
    const mo = []; // normalized char -> offset in that item's text
    let lastSpace = true; // leading whitespace is dropped
    const push = (ch, i, o) => { norm += ch; mi.push(i); mo.push(o); };
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const text = typeof it === 'string' ? it : String(it.text || '');
      const sep = typeof it !== 'string' && !!it.sep;
      if (sep && !lastSpace) { push(' ', -1, it.hard === false ? 0 : 1); lastSpace = true; } // mo: 1 = hard block boundary
      for (let o = 0; o < text.length; o++) {
        const c = text[o];
        if (/\s/.test(c)) {
          if (!lastSpace) { push(' ', i, o); lastSpace = true; }
        } else {
          const l = c.toLowerCase();
          for (let k = 0; k < l.length; k++) push(l[k], i, o);
          lastSpace = false;
        }
      }
    }
    return { norm, mi, mo };
  }

  function normalizeNeedle(s) {
    return buildIndex([String(s)]).norm.trim();
  }

  // occurrence: 1-based number (or numeric string), 'last', or empty (= 1). Returns null if invalid.
  function parseOccurrence(v) {
    if (v === undefined || v === null || v === '') return 1;
    if (v === 'last') return 'last';
    const n = typeof v === 'number' ? v : (typeof v === 'string' && /^\d+$/.test(v.trim()) ? Number(v.trim()) : NaN);
    return Number.isInteger(n) && n >= 1 ? n : null;
  }

  // Non-overlapping start positions of n in norm.
  function allPositions(norm, n) {
    const out = [];
    for (let p = norm.indexOf(n); p >= 0; p = norm.indexOf(n, p + n.length)) out.push(p);
    return out;
  }

  // Index into a positions array for an occurrence, or -1 if there is no such occurrence.
  function pickIndex(count, occ) {
    if (occ === null) return -1;
    const k = occ === 'last' ? count - 1 : occ - 1;
    return k >= 0 && k < count ? k : -1;
  }

  // Returns {count, occurrence, start:{item,offset}, end:{item,offset}} (end offset exclusive),
  // {count:0}, or {count, outOfRange:true} when the requested occurrence does not exist.
  function findInTexts(items, needle, occurrence) {
    const n = normalizeNeedle(needle);
    if (!n) return { count: 0 };
    const idx = buildIndex(items);
    const pos = allPositions(idx.norm, n);
    if (!pos.length) return { count: 0 };
    const k = pickIndex(pos.length, parseOccurrence(occurrence));
    if (k < 0) return { count: pos.length, outOfRange: true };
    const first = pos[k];
    const last = first + n.length - 1;
    return {
      count: pos.length,
      occurrence: k + 1,
      start: { item: idx.mi[first], offset: idx.mo[first] },
      end: { item: idx.mi[last], offset: idx.mo[last] + 1 },
    };
  }

  // ---- passage matching (endText / expand sentence), pure -----------------------------
  const TERM = '.!?';
  const CLOSERS = '"\')]”’';

  // Is normalized position k the first char of a block (block boundary right before it)?
  function isBlockStart(idx, items, k) {
    if (k <= 0) return true;
    if (idx.mi[k - 1] === -1) return idx.mo[k - 1] === 1; // synthetic separator: hard or soft (inline formula)
    const it = idx.mi[k];
    return it >= 0 && it !== idx.mi[k - 1] && typeof items[it] !== 'string' && !!items[it].sep && items[it].hard !== false;
  }

  function isTermAt(norm, p) {
    if (p < 0) return false;
    if (TERM.includes(norm[p])) return true;
    return CLOSERS.includes(norm[p]) && p > 0 && TERM.includes(norm[p - 1]);
  }

  // Words (lowercase, no final period) after which a period does NOT end a sentence.
  // Keep in sync with ABBREVIATIONS in tests/test_sentence.py.
  const ABBREVIATIONS = new Set(['fig', 'figs', 'eq', 'eqs', 'sec', 'secs', 'no', 'nos', 'vs', 'cf',
    'al', 'dr', 'mr', 'mrs', 'ms', 'prof', 'ref', 'refs', 'vol', 'pp', 'ch', 'approx', 'resp', 'eg', 'ie']);
  const OPENERS = '"\'([“‘';

  // Abbreviation-aware sentence boundary. p = index of the last char of the sentence end (the
  // terminator, or a closing quote/bracket right after it) and norm[p + 1] must be a space.
  // A '.' is NOT a boundary when the word before it is a known abbreviation ("Fig.", "et al."),
  // a dotted abbreviation ("e.g.", "i.e.", "U.S.") or a single-letter initial ("D. Malyuta").
  // Decimals like "3.5" have no space after the period, so they never reach this test.
  function isSentenceEnd(norm, p) {
    if (!isTermAt(norm, p) || norm[p + 1] !== ' ') return false;
    const tp = TERM.includes(norm[p]) ? p : p - 1;
    if (norm[tp] !== '.') return true;
    let j = tp;
    while (j > 0 && norm[j - 1] !== ' ') j--;
    let w = norm.slice(j, tp);
    while (w.length && OPENERS.includes(w[0])) w = w.slice(1);
    if (!w) return true;
    if (w.length === 1 && /[a-z]/.test(w)) return false;
    if (/^([a-z]\.)+[a-z]$/.test(w)) return false;
    return !ABBREVIATIONS.has(w);
  }

  // Expand normalized range [s, e] (inclusive) to whole sentence(s), never crossing a block.
  function expandSentence(idx, items, s, e) {
    const norm = idx.norm;
    let a = s;
    while (a > 0) {
      if (isBlockStart(idx, items, a)) break;
      if (a >= 2 && norm[a - 1] === ' ' && isSentenceEnd(norm, a - 2)) break;
      a--;
    }
    let b = e;
    while (b < norm.length - 1) {
      if (isSentenceEnd(norm, b)) break;
      if (isBlockStart(idx, items, b + 1)) break;
      b++;
    }
    while (b > a && norm[b] === ' ') b--;
    return { s: a, e: b };
  }

  // Returns {count, s, e} (normalized inclusive positions, plus idx) or {count:0}.
  // endText: span runs to the end of the first match of endText found after the start match.
  // expand: 'sentence' expands the span to sentence boundaries (paragraph is handled by the DOM side).
  function findSpan(items, needle, endNeedle, expand, occurrence) {
    const n = normalizeNeedle(needle);
    if (!n) return { count: 0 };
    const idx = buildIndex(items);
    const pos = allPositions(idx.norm, n);
    if (!pos.length) return { count: 0 };
    const count = pos.length;
    const k = pickIndex(count, parseOccurrence(occurrence));
    if (k < 0) return { count, outOfRange: true };
    const first = pos[k];
    let s = first;
    let e = first + n.length - 1;
    if (endNeedle !== undefined && endNeedle !== null && String(endNeedle).trim()) {
      const en = normalizeNeedle(endNeedle);
      const at = en ? idx.norm.indexOf(en, first + n.length) : -1;
      if (at < 0) return { count, endMissing: true };
      e = at + en.length - 1;
    }
    if (expand === 'sentence') ({ s, e } = expandSentence(idx, items, s, e));
    return { count, occurrence: k + 1, s, e, idx };
  }

  // ---- smooth scroll profile (pure) ---------------------------------------------------
  // Trapezoidal velocity: accelerate for 30% of the time, cruise for 5%, decelerate for 65%.
  // The long, asymmetric tail makes the scroll crawl to a stop instead of braking late.
  // Returns progress (0..1) at normalized time t (0..1). The area under velocity is 1.
  const ACC = 0.3;
  const DEC = 0.65;
  const VMAX = 1 / (1 - (ACC + DEC) / 2);
  function scrollPosition(t) {
    if (!(t > 0)) return 0;
    if (t >= 1) return 1;
    if (t < ACC) return (VMAX / (2 * ACC)) * t * t;
    if (t <= 1 - DEC) return (VMAX * ACC) / 2 + VMAX * (t - ACC);
    const u = 1 - t;
    return 1 - (VMAX / (2 * DEC)) * u * u;
  }

  // Duration in ms for a scroll of dist px: ~1 ms per 2 px, at least 600 ms, up to 2400 ms at 4800 px.
  // Beyond that it keeps growing, but slowly (square root), up to 8000 ms, so a top-to-bottom trip on a
  // very long page does not whip past (e.g. 36000 px takes about 5.4 s).
  const LONG_START_PX = 4800;
  function scrollDuration(dist) {
    const d = Math.abs(dist);
    if (d <= LONG_START_PX) return Math.max(600, d / 2);
    return Math.min(8000, d / 2 > 2400 ? 2400 + 25 * (Math.sqrt(d) - Math.sqrt(LONG_START_PX)) : d / 2);
  }

  // ---- DOM side ---------------------------------------------------------------------
  let scrollAnim = null; // {raf, stop} of the running scroll animation, if any

  function cancelScroll() {
    if (!scrollAnim) return;
    const a = scrollAnim;
    scrollAnim = null;
    cancelAnimationFrame(a.raf);
    ['wheel', 'touchstart', 'keydown'].forEach(ev => window.removeEventListener(ev, a.stop));
  }

  function smoothScrollTo(targetY, onDone) {
    cancelScroll();
    const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    const to = Math.max(0, Math.min(targetY, maxY));
    const from = window.scrollY;
    const dist = to - from;
    const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (Math.abs(dist) < 40 || reduced) {
      window.scrollTo({ top: to, behavior: 'instant' });
      if (onDone) onDone();
      return;
    }
    const dur = scrollDuration(dist);
    const anim = { raf: 0, stop: null };
    anim.stop = cancelScroll; // user input: stop where we are, never fight the user
    scrollAnim = anim;
    ['wheel', 'touchstart', 'keydown'].forEach(ev => window.addEventListener(ev, anim.stop, { passive: true }));
    const t0 = performance.now();
    const step = (now) => {
      if (scrollAnim !== anim) return;
      const t = Math.min(1, (now - t0) / dur);
      window.scrollTo({ top: from + dist * scrollPosition(t), behavior: 'instant' });
      if (t < 1) anim.raf = requestAnimationFrame(step);
      else { cancelScroll(); if (onDone) onDone(); }
    };
    anim.raf = requestAnimationFrame(step);
  }

  // Tall ranges: top near the top of the viewport; otherwise centered vertically.
  function scrollToRange(range) {
    const r = range.getBoundingClientRect();
    if (!r || (!r.width && !r.height)) return;
    const top = r.top + window.scrollY;
    const tall = r.height > window.innerHeight * 0.8;
    smoothScrollTo(tall ? top - 24 : top + r.height / 2 - window.innerHeight / 2);
  }

  // Place the outline box around a range (document coordinates).
  function placeBox(d, r) {
    const rc = r.getBoundingClientRect();
    const pad = 4;
    d.style.left = (rc.left + window.scrollX - pad) + 'px';
    d.style.top = (rc.top + window.scrollY - pad) + 'px';
    d.style.width = (rc.width + 2 * pad) + 'px';
    d.style.height = (rc.height + 2 * pad) + 'px';
  }

  // Soft yellow band behind each visual line of the range. The CSS highlight does not paint every piece of an
  // inline formula (stretchy parentheses and the like), so this layer fills the gaps. One band per line.
  const BAND_ATTR = 'data-jarvis-band';
  function removeBands() {
    document.querySelectorAll('[' + BAND_ATTR + ']').forEach(d => d.remove());
  }
  function lineRects(range) {
    const rects = Array.from(range.getClientRects()).filter(r => r.width > 0 && r.height > 0);
    if (!rects.length || rects.length > 400) return [];
    rects.sort((a, b) => a.top - b.top || a.left - b.left);
    const lines = [];
    for (const r of rects) {
      const mid = r.top + r.height / 2;
      const L = lines.find(l => mid >= l.top && mid <= l.bottom);
      if (L) {
        L.left = Math.min(L.left, r.left); L.right = Math.max(L.right, r.right);
        L.top = Math.min(L.top, r.top); L.bottom = Math.max(L.bottom, r.bottom);
      } else lines.push({ left: r.left, right: r.right, top: r.top, bottom: r.bottom });
    }
    return lines;
  }
  function drawBands(range) {
    removeBands();
    try {
      for (const l of lineRects(range)) {
        const d = document.createElement('div');
        d.setAttribute(BAND_ATTR, '1');
        d.style.cssText = 'position:absolute;pointer-events:none;z-index:2147483645;border-radius:2px;' +
          'background:rgba(255,225,77,.38);left:' + (l.left + window.scrollX - 1) + 'px;top:' + (l.top + window.scrollY - 1) +
          'px;width:' + (l.right - l.left + 2) + 'px;height:' + (l.bottom - l.top + 2) + 'px;';
        document.documentElement.appendChild(d);
      }
    } catch (_) { /* decoration only */ }
  }

  // Pages with content-visibility:auto (long arXiv HTML) report estimated positions for sections that are
  // not drawn yet. Jump the range into view first so the browser lays it out for real, measure and draw the
  // box there, jump back, then run the eased scroll to the true position and re-check once it ends.
  function revealRange(r, withBox) {
    const el = r.startContainer.parentElement;
    const from = window.scrollY;
    try { if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (_) { /* ignore */ }
    const rc = r.getBoundingClientRect();
    const topDoc = rc.top + window.scrollY;
    const box = withBox ? drawBox(r) : null;
    drawBands(r);
    window.scrollTo({ top: from, behavior: 'instant' });
    if (!rc.width && !rc.height) return;
    const tall = rc.height > window.innerHeight * 0.8;
    smoothScrollTo(tall ? topDoc - 24 : topDoc + rc.height / 2 - window.innerHeight / 2, () => {
      const now = r.getBoundingClientRect();
      const mid = now.top + now.height / 2;
      if (el && !tall && (mid < 0 || mid > window.innerHeight)) {
        try { el.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (_) { /* ignore */ }
      }
      if (box && box.isConnected) placeBox(box, r);
      drawBands(r);
    });
  }

  const HL_NAME = 'jarvis-hl';
  const BOX_ID = 'jarvis-box';
  const BLOCK_SELECTOR = 'p,li,blockquote,dd,dt,td,th,pre,h1,h2,h3,h4,h5,h6,figcaption';
  const STYLE_ID = 'jarvis-hl-style';
  const CAPTION_ID = 'jarvis-caption';
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TEXTAREA', 'SELECT', 'OPTION']);

  function isVisible(el, cache) {
    if (cache.has(el)) return cache.get(el);
    let ok = true;
    if (SKIP_TAGS.has(el.tagName) || el.id === CAPTION_ID) ok = false;
    else if (typeof el.checkVisibility === 'function') ok = el.checkVisibility({ visibilityProperty: true });
    else {
      const cs = getComputedStyle(el);
      ok = cs.display !== 'none' && cs.visibility !== 'hidden';
    }
    cache.set(el, ok);
    return ok;
  }

  function blockOf(el, cache) {
    let cur = el;
    while (cur && cur.parentElement) {
      if (cache.has(cur)) return cache.get(cur);
      const d = getComputedStyle(cur).display;
      if (d !== 'inline' && d !== 'contents') { cache.set(el, cur); return cur; }
      cur = cur.parentElement;
    }
    cache.set(el, cur || el);
    return cur || el;
  }

  // Inline-level boxes (inline-block, inline math, ruby ...) are not paragraph-like blocks.
  const SOFT_DISPLAY = /inline|math|ruby/;
  function isHardDisplay(el) {
    try { return !SOFT_DISPLAY.test(getComputedStyle(el).display); } catch (_) { return true; }
  }

  // Nearest ancestor-or-self that is a real (non-inline) box: the paragraph/cell/list item a piece of text lives in.
  function hardAncestor(el, cache) {
    if (cache.has(el)) return cache.get(el);
    let cur = el;
    while (cur.parentElement && !isHardDisplay(cur)) cur = cur.parentElement;
    cache.set(el, cur);
    return cur;
  }

  function collectText() {
    const nodes = [];
    const items = [];
    const vis = new Map();
    const blk = new Map();
    const w = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    let prevBlock = null;
    const hbCache = new Map();
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const el = n.parentElement;
      if (!el || !n.data) continue;
      // visible if the element and all ancestors are (checkVisibility covers ancestors; fallback checks self only)
      if (!isVisible(el, vis)) continue;
      const b = blockOf(el, blk);
      const sep = b !== prevBlock;
      // A boundary is SOFT (still a space, but a sentence runs across it) only when both sides live in the same
      // paragraph-like box: e.g. a paragraph and an inline formula inside it, or two pieces of one formula.
      // Different paragraphs, list items or table cells stay HARD edges, even when the text there is a formula.
      let hard = true;
      if (sep && prevBlock && hardAncestor(b, hbCache) === hardAncestor(prevBlock, hbCache)) hard = false;
      items.push({ text: n.data, sep, hard: sep ? hard : false });
      nodes.push(n);
      prevBlock = b;
    }
    return { nodes, items };
  }

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = '::highlight(' + HL_NAME + '){background-color:#ffe14d;color:#000;}' +
      'mark[data-jarvis]{background-color:#ffe14d;color:#000;}';
    (document.head || document.documentElement).appendChild(st);
  }

  function clearAll() {
    cancelScroll();
    try { if (typeof CSS !== 'undefined' && CSS.highlights) CSS.highlights.delete(HL_NAME); } catch (_) { /* ignore */ }
    const marks = document.querySelectorAll('mark[data-jarvis]');
    const parents = new Set();
    marks.forEach(m => {
      const p = m.parentNode;
      if (!p) return;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      parents.add(p);
    });
    parents.forEach(p => { try { p.normalize(); } catch (_) { /* ignore */ } });
    const cap = document.getElementById(CAPTION_ID);
    if (cap) cap.remove();
    const box = document.getElementById(BOX_ID);
    if (box) box.remove();
    removeBands();
  }

  // Semi-transparent box with a visible outline around the whole passage (document coordinates).
  function drawBox(range) {
    const r = range.getBoundingClientRect();
    if (!r || (!r.width && !r.height)) return null;
    const d = document.createElement('div');
    d.id = BOX_ID;
    const pad = 4;
    d.style.cssText = 'position:absolute;pointer-events:none;z-index:2147483646;box-sizing:border-box;' +
      'left:' + (r.left + window.scrollX - pad) + 'px;top:' + (r.top + window.scrollY - pad) + 'px;' +
      'width:' + (r.width + 2 * pad) + 'px;height:' + (r.height + 2 * pad) + 'px;' +
      'background:rgba(255,225,77,.18);outline:3px solid #f5a300;border-radius:4px;' +
      'box-shadow:0 0 0 6px rgba(245,163,0,.25);';
    document.documentElement.appendChild(d);
    return d;
  }

  function showCaption(text) {
    const d = document.createElement('div');
    d.id = CAPTION_ID;
    d.textContent = text;
    d.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);max-width:70vw;' +
      'z-index:2147483647;pointer-events:none;background:rgba(20,20,20,.92);color:#fff;' +
      'font:14px/1.4 system-ui,sans-serif;padding:8px 14px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.4);';
    (document.body || document.documentElement).appendChild(d);
  }

  function wrapMarks(nodes, sn, so, en, eo) {
    let firstMark = null;
    for (let i = sn; i <= en; i++) {
      const node = nodes[i];
      const s = i === sn ? so : 0;
      const e = i === en ? eo : node.data.length;
      if (e <= s) continue;
      let mid = node;
      if (s > 0) mid = node.splitText(s);
      if (e - s < mid.data.length) mid.splitText(e - s);
      const m = document.createElement('mark');
      m.setAttribute('data-jarvis', '1');
      mid.parentNode.insertBefore(m, mid);
      m.appendChild(mid);
      if (!firstMark) firstMark = m;
    }
    return firstMark;
  }

  // Diagnostics: where the matched range really is and whether it can be seen.
  function diag(r) {
    try {
      const rc = r.getBoundingClientRect();
      const el = r.startContainer.parentElement;
      return {
        rect: [Math.round(rc.left), Math.round(rc.top), Math.round(rc.width), Math.round(rc.height)],
        scrollY: Math.round(window.scrollY),
        viewport: [innerWidth, innerHeight],
        highlightApi: !!(typeof CSS !== 'undefined' && CSS.highlights),
        registered: !!(typeof CSS !== 'undefined' && CSS.highlights && CSS.highlights.has(HL_NAME)),
        el: el ? el.tagName + '.' + String(el.className).slice(0, 60) : null,
        boxPresent: !!document.getElementById(BOX_ID),
        text: String(r.toString()).slice(0, 400), // what the highlight actually covers (debug only)
      };
    } catch (e) { return { diagError: String(e) }; }
  }

  // Passage mode: endText and/or expand. Highlights a multi-node range plus an outline box.
  function doShowPassage(text, endText, expand, caption, occurrence, debug) {
    clearAll();
    const { nodes, items } = collectText();
    const f = findSpan(items, text, endText, expand, occurrence);
    if (f.outOfRange) return outOfRangeResult(f.count, occurrence);
    if (!f.count || f.endMissing) {
      return { ok: true, found: false, matchCount: f.count || 0,
        message: f.endMissing
          ? 'Start text was found but endText was not found after it. Try a different end phrase copied exactly as it appears on screen.'
          : 'Text not found on this page. Try a shorter or differently worded phrase copied exactly as it appears on screen.' };
    }
    const { idx } = f;
    let sn = idx.mi[f.s], so = idx.mo[f.s];
    let en = idx.mi[f.e], eo = idx.mo[f.e] + 1;
    if (sn < 0 || en < 0) return { ok: true, found: false, matchCount: f.count, message: 'Could not map the match to page text.' };
    if (expand === 'paragraph') {
      const blockFor = (node) => {
        const el = node.parentElement;
        return (el && el.closest(BLOCK_SELECTOR)) || blockOf(el, new Map());
      };
      const sb = blockFor(nodes[sn]);
      const eb = blockFor(nodes[en]);
      sn = nodes.findIndex(n => sb.contains(n));
      for (en = nodes.length - 1; en >= 0 && !eb.contains(nodes[en]); en--) { /* find last node in end block */ }
      so = 0;
      eo = nodes[en].data.length;
    }
    ensureStyle();
    const r = document.createRange();
    r.setStart(nodes[sn], so);
    r.setEnd(nodes[en], eo);
    if (typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight === 'function') {
      CSS.highlights.set(HL_NAME, new Highlight(r));
      revealRange(r, true);
    } else {
      drawBox(r); // measure before any DOM mutation (mark fallback)
      wrapMarks(nodes, sn, so, en, eo);
      scrollToRange(r);
    }
    if (typeof caption === 'string' && caption.trim()) showCaption(caption.trim().slice(0, 300));
    const res = { ok: true, found: true, matchCount: f.count, occurrence: f.occurrence };
    if (debug === true) res.diag = diag(r);
    return res;
  }

  function outOfRangeResult(count, occurrence) {
    return { ok: true, found: false, matchCount: count,
      message: 'Only ' + count + ' match' + (count === 1 ? '' : 'es') + ' found on this page; occurrence ' +
        String(occurrence) + ' does not exist.' };
  }

  function doShow(text, caption, endText, expand, occurrence, debug) {
    if (typeof text !== 'string' || !text.trim()) return { ok: false, error: 'bad_args', message: 'text is required' };
    if (parseOccurrence(occurrence) === null) {
      return { ok: false, error: 'bad_args', message: 'occurrence must be a positive integer or "last"' };
    }
    const hasEnd = typeof endText === 'string' && endText.trim() !== '';
    if (expand !== undefined && expand !== null && expand !== '' && expand !== 'sentence' && expand !== 'paragraph') {
      return { ok: false, error: 'bad_args', message: 'expand must be "sentence" or "paragraph"' };
    }
    const mode = expand === 'sentence' || expand === 'paragraph' ? expand : null;
    if (hasEnd || mode) return doShowPassage(text, hasEnd ? endText : null, mode, caption, occurrence, debug);
    clearAll();
    const { nodes, items } = collectText();
    const f = findInTexts(items, text, occurrence);
    if (f.outOfRange) return outOfRangeResult(f.count, occurrence);
    if (!f.count) {
      return { ok: true, found: false, matchCount: 0,
        message: 'Text not found on this page. Try a shorter or differently worded phrase copied exactly as it appears on screen.' };
    }
    ensureStyle();
    const r = document.createRange();
    r.setStart(nodes[f.start.item], f.start.offset);
    r.setEnd(nodes[f.end.item], f.end.offset);
    if (typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight === 'function') {
      CSS.highlights.set(HL_NAME, new Highlight(r));
      revealRange(r, false);
    } else {
      const rect = r.getBoundingClientRect(); // measure before any DOM mutation (mark fallback)
      wrapMarks(nodes, f.start.item, f.start.offset, f.end.item, f.end.offset);
      scrollToRange({ getBoundingClientRect: () => rect });
    }
    if (typeof caption === 'string' && caption.trim()) showCaption(caption.trim().slice(0, 300));
    const res = { ok: true, found: true, matchCount: f.count, occurrence: f.occurrence };
    if (debug === true) res.diag = diag(r);
    return res;
  }

  // Page text for `read`, WITHOUT Jarvis's own overlays (caption, outline box, bands): they are not page content.
  function pageText() {
    const hidden = [];
    const hide = (el) => { if (el && el.style) { hidden.push([el, el.style.display]); el.style.display = 'none'; } };
    try {
      hide(document.getElementById(CAPTION_ID));
      hide(document.getElementById(BOX_ID));
      document.querySelectorAll('[' + BAND_ATTR + ']').forEach(hide);
      return (document.body && document.body.innerText) || '';
    } finally {
      hidden.forEach(([el, d]) => { el.style.display = d; });
    }
  }

  function doRead(maxChars) {
    const max = Math.max(1, Math.min(Number.isFinite(maxChars) ? maxChars : 20000, 200000));
    const full = pageText();
    return {
      ok: true, title: document.title, url: location.href,
      text: full.slice(0, max), truncated: full.length > max, totalChars: full.length,
    };
  }

  function run(op, args) {
    args = args || {};
    try {
      if (op === 'show') return doShow(args.text, args.caption, args.endText, args.expand, args.occurrence, args.debug);
      if (op === 'clear') { clearAll(); return { ok: true }; }
      if (op === 'read') return doRead(args.maxChars);
      return { ok: false, error: 'unknown_op' };
    } catch (e) {
      return { ok: false, error: 'script_error', message: String((e && e.message) || e) };
    }
  }

  const lib = { buildIndex, normalizeNeedle, findInTexts, findSpan, isSentenceEnd, parseOccurrence, pageText, scrollPosition, scrollDuration, run };
  if (typeof module !== 'undefined' && module.exports) module.exports = lib;
  else globalThis.__jarvisLib = lib;
})();
