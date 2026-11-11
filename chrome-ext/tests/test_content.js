// Run: node tests/test_content.js  (pure matching logic only; no DOM)
const assert = require('assert');
const lib = require('../extension/content.js');
const items = [
  { text: 'Intro heading', sep: true },
  { text: 'Models are good. The dominant ', sep: true }, { text: 'sequence', sep: false },
  { text: ' models rely on RNNs. They work! Next one.', sep: false },
  { text: 'Another paragraph here.', sep: true },
];
const cut = (f) => f.idx.norm.slice(f.s, f.e + 1);
let f = lib.findSpan(items, 'dominant sequence', null, null);
assert.strictEqual(cut(f), 'dominant sequence');
f = lib.findSpan(items, 'dominant', 'rely on', null);
assert.strictEqual(cut(f), 'dominant sequence models rely on');
f = lib.findSpan(items, 'dominant', 'nothing here', null);
assert.ok(f.endMissing);
f = lib.findSpan(items, 'rely', null, 'sentence');
assert.strictEqual(cut(f), 'the dominant sequence models rely on rnns.');
f = lib.findSpan(items, 'next', null, 'sentence');
assert.strictEqual(cut(f), 'next one.');
f = lib.findSpan(items, 'intro', null, 'sentence');
assert.strictEqual(cut(f), 'intro heading');
assert.strictEqual(lib.findSpan(items, 'zzz', null, null).count, 0);

// ---- abbreviation-aware sentence boundaries (mirrors tests/test_sentence.py; NOT yet run, no Node here) ----
{
  const sent = (text, needle) => {
    const f = lib.findSpan([text], needle, null, 'sentence');
    return f.idx.norm.slice(f.s, f.e + 1);
  };
  const cases = [
    ['Models rely on RNNs. They work! Next one.', 'rely', 'models rely on rnns.'],
    ['Models rely on RNNs. They work! Next one.', 'work', 'they work!'],
    ['Intro. See Fig. 3 for the setup. Then more.', 'setup', 'see fig. 3 for the setup.'],
    ['Intro. See Fig. 3 for the setup. Then more.', 'see', 'see fig. 3 for the setup.'],
    ['Many methods, e.g. SCvx and GuSTO, work. Next.', 'scvx', 'many methods, e.g. scvx and gusto, work.'],
    ['That is, i.e. the cost, is convex. Next.', 'cost', 'that is, i.e. the cost, is convex.'],
    ['As shown by Szmuk et al. the method converges. Done.', 'converges', 'as shown by szmuk et al. the method converges.'],
    ['Substituting into Eq. 5 gives the bound. Done.', 'bound', 'substituting into eq. 5 gives the bound.'],
    ['This is covered in Sec. 4 of the paper. Done.', 'paper', 'this is covered in sec. 4 of the paper.'],
    ['See No. 7 in the list. Done.', 'list', 'see no. 7 in the list.'],
    ['Ours is faster vs. the baseline. Done.', 'faster', 'ours is faster vs. the baseline.'],
    ['Compare cf. the earlier result. Done.', 'earlier', 'compare cf. the earlier result.'],
    ['The value is 3.5 percent here. Next.', 'percent', 'the value is 3.5 percent here.'],
    ['Tutorial by D. Malyuta and B. Acikmese is good. Next.', 'malyuta', 'tutorial by d. malyuta and b. acikmese is good.'],
    ['He said "stop." Then he left. Next.', 'left', 'then he left.'],
    ['He said "stop." Then he left. Next.', 'stop', 'he said "stop."'],
    ['Is it convex? Yes, it is. Done.', 'yes', 'yes, it is.'],
    ['We use the U.S. data set here. Next.', 'data', 'we use the u.s. data set here.'],
    ['The test ended in 5 min. The next began.', 'began', 'the next began.'],
  ];
  for (const [t, n, want] of cases) assert.strictEqual(sent(t, n), want, t + ' / ' + n);
}

// ---- occurrence ----
{
  const its = ['the cat and the dog and the end'];
  const cut2 = (f) => f.idx.norm.slice(f.s, f.e + 1);
  let f = lib.findSpan(its, 'the', null, null);
  assert.strictEqual(f.count, 3); assert.strictEqual(f.occurrence, 1);
  f = lib.findSpan(its, 'the', 'dog', null, 2);
  assert.strictEqual(cut2(f), 'the dog'); assert.strictEqual(f.occurrence, 2);
  f = lib.findSpan(its, 'the', null, null, 'last');
  assert.strictEqual(f.occurrence, 3); assert.strictEqual(cut2(f), 'the');
  assert.strictEqual(f.idx.norm.slice(f.s), 'the end');
  f = lib.findSpan(its, 'the', null, null, 4);
  assert.ok(f.outOfRange); assert.strictEqual(f.count, 3);
  assert.ok(lib.findSpan(its, 'the', null, null, 0).outOfRange);
  const g = lib.findInTexts(its, 'the', 'last');
  assert.strictEqual(g.occurrence, 3); assert.strictEqual(g.start.offset, 24);
  assert.ok(lib.findInTexts(its, 'the', 9).outOfRange);
  assert.strictEqual(lib.parseOccurrence(undefined), 1);
  assert.strictEqual(lib.parseOccurrence('2'), 2);
  assert.strictEqual(lib.parseOccurrence('last'), 'last');
  assert.strictEqual(lib.parseOccurrence('x'), null);
  assert.strictEqual(lib.parseOccurrence(0), null);
}

// ---- smooth scroll profile ----
{
  const p = lib.scrollPosition;
  const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, a + ' vs ' + b);
  close(p(0), 0); close(p(1), 1);
  close(p(-1), 0); close(p(2), 1);
  let prev = -1;
  for (let i = 0; i <= 1000; i++) { const v = p(i / 1000); assert.ok(v >= prev - 1e-12); prev = v; }
  const h = 1e-6;
  assert.ok((p(h) - p(0)) / h < 1e-3);      // velocity ~0 at start
  assert.ok((p(1) - p(1 - h)) / h < 1e-3);  // velocity ~0 at end
  const VM = 1 / (1 - (0.3 + 0.65) / 2);
  const v1 = (p(0.31) - p(0.30)) / 0.01, v2 = (p(0.35) - p(0.34)) / 0.01;
  close(v1, v2, 1e-6); close(v1, VM, 1e-6); // cruise is linear
  for (const j of [0.3, 0.35]) close((p(j) - p(j - h)) / h, (p(j + h) - p(j)) / h, 1e-4); // no jump at joins
  assert.ok((p(1) - p(0.95)) / 0.05 < 0.25 * VM); // long, slow tail
  assert.strictEqual(lib.scrollDuration(100), 600);
  assert.strictEqual(lib.scrollDuration(2000), 1000);
  assert.strictEqual(lib.scrollDuration(4800), 2400);
  assert.ok(Math.abs(lib.scrollDuration(36000) - 5410) < 5); // long page: slower, sublinear
  assert.ok(lib.scrollDuration(10000) > 2400 && lib.scrollDuration(10000) < lib.scrollDuration(36000));
  assert.strictEqual(lib.scrollDuration(100000), 8000);
}
// ---- read must not include Jarvis's caption overlay / outline box / bands ----
{
  const mk = (text) => ({ text, style: { display: '' } });
  const caption = mk('THE KEY CLAIM caption overlay'), box = mk(''), band = mk('band text');
  const page = mk('Real page text. Figure 1: a real figure caption.');
  const parts = [page, caption, box, band];
  const doc = {
    title: 'T',
    getElementById: (id) => ({ 'jarvis-caption': caption, 'jarvis-box': box }[id] || null),
    querySelectorAll: () => [band],
  };
  Object.defineProperty(doc, 'body', { value: { get innerText() {
    return parts.filter(p => p.style.display !== 'none').map(p => p.text).filter(Boolean).join('\n');
  } } });
  globalThis.document = doc;
  globalThis.location = { href: 'https://example.com/' };
  const r = lib.run('read', {});
  assert.ok(r.ok);
  assert.ok(r.text.includes('Real page text. Figure 1: a real figure caption.'));
  assert.ok(!r.text.includes('THE KEY CLAIM') && !r.text.includes('band text'), r.text);
  assert.strictEqual(caption.style.display, '');   // overlay restored afterwards
  assert.strictEqual(band.style.display, '');
  assert.strictEqual(r.totalChars, r.text.length);
  caption.style.display = 'block';                  // previous inline display value is restored too
  lib.run('read', {});
  assert.strictEqual(caption.style.display, 'block');
  delete globalThis.document; delete globalThis.location;
}
console.log('ok');
