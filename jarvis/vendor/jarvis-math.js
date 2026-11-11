/* Jarvis math renderer: safe LaTeX math for the web UI conversation and the Markdown preview.
 *
 * Loaded as a plain external script (CSP script-src 'self') after the vendored katex.min.js.
 * Never builds HTML from message text: text becomes text nodes, math is rendered by katex.render into
 * elements created here, with trust denied (no \href / \url / \htmlClass / \htmlStyle / \includegraphics),
 * limited macro expansion and size, a length cap, a nesting cap, and output "html" only (the MathML twin
 * would double the text the page highlighter matches; role=math + aria-label carry the TeX for readers).
 * Anything KaTeX rejects, or that is too big, is shown as its plain source text.
 *
 * Delimiters: $...$ and \(...\) inline; $$...$$ and \[...\] display. "\$" is a literal dollar.
 * An opening $ must be followed by non-space; the closing $ must follow non-space and must not be
 * followed by a digit, so "$5 and $10" stays text. Keep in sync with jarvis/mathsupport.py.
 *
 * API: JarvisMath.hasMath(text), .split(text), .render(tex, display, el, raw), .fill(container, text, query),
 *      .renderAll(root) (renders every <span class="jv-math" data-tex="..."> the preview server emitted).
 */
(function () {
  "use strict";
  var MAX_TEX = 2000, MAX_DEPTH = 40, MAX_SEGMENTS = 200, MAX_TEXT = 40000;
  // trust is a function that throws: KaTeX asks it before honouring \href, \url, \htmlClass/Id/Style/Data and
  // \includegraphics, so every such command makes the whole expression fail (shown as plain source) instead of
  // KaTeX's default of quietly drawing the command name in red.
  function distrust() { throw new Error("untrusted command"); }
  var OPTS = { throwOnError: true, trust: distrust, strict: "ignore", output: "html", maxExpand: 200, maxSize: 10, displayMode: false };

  function isSpace(ch) { return ch === "" || /\s/.test(ch); }
  function isDigit(ch) { return ch >= "0" && ch <= "9" && ch !== ""; }

  function delim(s, i, openLen, close, display) {
    var j = s.indexOf(close, i + openLen);
    if (j === -1) return null;
    var tex = s.slice(i + openLen, j);
    if (!tex.trim()) return null;
    return { end: j + close.length, tex: tex, display: display };
  }

  // The math starting at s[i] ("$" or "\"), or null.
  function mathAt(s, i) {
    var c = s.charAt(i);
    if (c === "\\") {
      var n = s.charAt(i + 1);
      if (n === "(") return delim(s, i, 2, "\\)", false);
      if (n === "[") return delim(s, i, 2, "\\]", true);
      return null;
    }
    if (c !== "$") return null;
    if (s.charAt(i + 1) === "$") return delim(s, i, 2, "$$", true);
    if (isSpace(s.charAt(i + 1))) return null;
    for (var j = i + 1; j < s.length; j++) {
      var ch = s.charAt(j);
      if (ch === "\\") { j++; continue; }
      if (ch === "\n" && s.charAt(j + 1) === "\n") return null;
      if (ch === "$") {
        if (isSpace(s.charAt(j - 1)) || isDigit(s.charAt(j + 1))) return null;
        return { end: j + 1, tex: s.slice(i + 1, j), display: false };
      }
    }
    return null;
  }

  // Text -> [{t:"text", s}, {t:"math", tex, display, raw}]. Over-long texts and surplus segments stay plain text.
  function split(text) {
    text = String(text === undefined || text === null ? "" : text);
    if (text.length > MAX_TEXT) return [{ t: "text", s: text }];
    var segs = [], buf = "", count = 0, i = 0;
    function flush() { if (buf) { segs.push({ t: "text", s: buf }); buf = ""; } }
    while (i < text.length) {
      var c = text.charAt(i);
      if (c === "\\" && text.charAt(i + 1) === "$") { buf += "$"; i += 2; continue; }
      if ((c === "$" || c === "\\") && count < MAX_SEGMENTS) {
        var m = mathAt(text, i);
        if (m) {
          flush();
          segs.push({ t: "math", tex: m.tex, display: m.display, raw: text.slice(i, m.end) });
          count++; i = m.end; continue;
        }
      }
      buf += c; i++;
    }
    flush();
    return segs;
  }

  function hasMath(text) {
    var s = String(text === undefined || text === null ? "" : text);
    if (s.length > MAX_TEXT || (s.indexOf("$") === -1 && s.indexOf("\\(") === -1 && s.indexOf("\\[") === -1)) return false;
    var segs = split(s);
    for (var i = 0; i < segs.length; i++) if (segs[i].t === "math") return true;
    return false;
  }

  function depthOK(tex) {
    var d = 0;
    for (var i = 0; i < tex.length; i++) {
      var ch = tex.charAt(i);
      if (ch === "\\") { i++; continue; }
      if (ch === "{") { if (++d > MAX_DEPTH) return false; } else if (ch === "}") d--;
    }
    return true;
  }

  // Render tex into el (replacing its content). On any problem el shows `raw` as plain text and the call returns false.
  function render(tex, display, el, raw) {
    el.textContent = "";
    el.classList.add(display ? "jv-display" : "jv-inline");
    var ok = false;
    if (window.katex && typeof tex === "string" && tex.length <= MAX_TEX && depthOK(tex)) {
      try {
        var o = {}; for (var k in OPTS) o[k] = OPTS[k];
        o.displayMode = !!display;
        window.katex.render(tex, el, o);
        el.setAttribute("role", "math");
        el.setAttribute("aria-label", tex);
        ok = true;
      } catch (e) { ok = false; }   // ParseError (untrusted command, too much expansion, ...) or stack overflow
    }
    if (!ok) {
      el.textContent = raw !== undefined ? raw : tex;
      el.removeAttribute("role"); el.removeAttribute("aria-label");
      el.classList.add("jv-math-error");
    }
    return ok;
  }

  function mathEl(seg) {
    var e = document.createElement("span");
    e.className = "jv-math";
    render(seg.tex, seg.display, e, seg.raw);
    return e;
  }

  // Fill container with text and rendered math (text nodes only; query highlights text parts with <mark>).
  function fill(container, text, query) {
    container.textContent = "";
    var segs = split(text), q = (query || "").toLowerCase();
    for (var i = 0; i < segs.length; i++) {
      var g = segs[i];
      if (g.t === "math") { container.appendChild(mathEl(g)); continue; }
      var s = g.s, low = s.toLowerCase(), pos = 0, j;
      if (!q || low.length !== s.length) { container.appendChild(document.createTextNode(s)); continue; }
      while ((j = low.indexOf(q, pos)) !== -1) {
        if (j > pos) container.appendChild(document.createTextNode(s.slice(pos, j)));
        var mk = document.createElement("mark"); mk.textContent = s.slice(j, j + q.length);
        container.appendChild(mk);
        pos = j + q.length;
      }
      if (pos < s.length) container.appendChild(document.createTextNode(s.slice(pos)));
    }
  }

  // The preview server marks math as <span class="jv-math [jv-display]" data-tex="...">raw source</span>.
  function renderAll(root) {
    var list = (root || document).querySelectorAll("span.jv-math[data-tex]");
    for (var i = 0; i < list.length; i++) {
      var e = list[i], raw = e.textContent;
      render(e.getAttribute("data-tex") || "", e.classList.contains("jv-display"), e, raw);
    }
  }

  window.JarvisMath = { hasMath: hasMath, split: split, render: render, fill: fill, renderAll: renderAll };
  if (document.querySelector && document.querySelector("span.jv-math[data-tex]")) renderAll(document);
})();
