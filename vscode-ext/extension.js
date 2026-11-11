'use strict';
const vscode = require('vscode');
const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const PORT = 47821;
const TOKEN_PATH = path.join(os.homedir(), '.jarvis_showcode_token');
const HOSTS = [`127.0.0.1:${PORT}`, `localhost:${PORT}`];

let server;
let output;
let decoration;
let clearTimer;
let selListener;

function clearHighlight() {
  if (clearTimer) { clearTimeout(clearTimer); clearTimer = undefined; }
  if (selListener) { selListener.dispose(); selListener = undefined; }
  if (decoration) { decoration.dispose(); decoration = undefined; }
}

function send(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

async function show(data) {
  const file = data.file;
  if (typeof file !== 'string' || !path.isAbsolute(file)) {
    return [400, { ok: false, error: 'file must be an absolute path string' }];
  }
  const startIn = data.startLine;
  const endIn = data.endLine === undefined || data.endLine === null ? startIn : data.endLine;
  if (!Number.isInteger(startIn) || !Number.isInteger(endIn) || startIn < 1 || endIn < 1) {
    return [400, { ok: false, error: 'startLine/endLine must be integers >= 1' }];
  }
  if (data.note !== undefined && data.note !== null && typeof data.note !== 'string') {
    return [400, { ok: false, error: 'note must be a string' }];
  }
  let st;
  try { st = fs.statSync(file); } catch (e) { st = null; }
  if (!st || !st.isFile()) {
    return [404, { ok: false, error: 'file not found: ' + file }];
  }

  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  const total = doc.lineCount;
  let s = Math.min(startIn, total);
  let e = Math.min(endIn, total);
  if (e < s) { const t = s; s = e; e = t; }
  const active = vscode.window.activeTextEditor;
  const editor = await vscode.window.showTextDocument(doc, {
    preview: false,
    viewColumn: active ? active.viewColumn : undefined,
    preserveFocus: false
  });
  const range = new vscode.Range(s - 1, 0, e - 1, doc.lineAt(e - 1).text.length);
  editor.selection = new vscode.Selection(range.start, range.end);
  editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);

  clearHighlight();
  const myDeco = vscode.window.createTextEditorDecorationType({
    backgroundColor: new vscode.ThemeColor('editor.findMatchHighlightBackground'),
    isWholeLine: true
  });
  decoration = myDeco;
  editor.setDecorations(myDeco, [range]);
  // Clear on the next cursor move (ignore events where selection is still our range).
  selListener = vscode.window.onDidChangeTextEditorSelection(ev => {
    if (ev.textEditor !== editor) return;
    const sel = ev.selections[0];
    if (sel && sel.start.isEqual(range.start) && sel.end.isEqual(range.end)) return;
    if (decoration === myDeco) clearHighlight();
  });
  clearTimer = setTimeout(() => { if (decoration === myDeco) clearHighlight(); }, 60000);

  if (data.note) vscode.window.setStatusBarMessage(data.note, 30000);
  return [200, { ok: true, file, selected: { startLine: s, endLine: e }, totalLines: total }];
}

// Shared secret for /terminal (which executes commands). Created once, never overwritten
// ('wx'), so several windows activating at once agree on the same value.
function ensureToken() {
  try {
    if (fs.existsSync(TOKEN_PATH)) return;
    fs.writeFileSync(TOKEN_PATH, crypto.randomBytes(24).toString('hex'), { flag: 'wx', mode: 0o600 });
  } catch (e) {
    if (!e || e.code !== 'EEXIST') log('Could not create token file: ' + ((e && e.message) || e));
  }
}

function tokenOk(req) {
  let expected;
  try { expected = fs.readFileSync(TOKEN_PATH, 'utf8').trim(); } catch (_) { return false; }
  const given = req.headers['x-jarvis-token'];
  if (expected.length < 32 || typeof given !== 'string') return false;
  // Hash both so timingSafeEqual gets equal-length buffers and length is not leaked.
  const a = crypto.createHash('sha256').update(expected).digest();
  const b = crypto.createHash('sha256').update(given).digest();
  return crypto.timingSafeEqual(a, b);
}

// cwd is applied ONLY when a new terminal is created; an existing active terminal keeps
// whatever directory/shell state it already has.
async function runInTerminal(data) {
  const command = data.command;
  if (typeof command !== 'string' || command.trim() === '' || command.length >= 2000 || /[\r\n]/.test(command)) {
    return [400, { ok: false, error: 'command must be a non-empty single-line string under 2000 chars' }];
  }
  const cwd = data.cwd;
  if (cwd !== undefined && cwd !== null) {
    let st;
    try { st = typeof cwd === 'string' && path.isAbsolute(cwd) ? fs.statSync(cwd) : null; } catch (e) { st = null; }
    if (!st || !st.isDirectory()) {
      return [400, { ok: false, error: 'cwd must be an existing absolute directory' }];
    }
  }
  let term = vscode.window.activeTerminal;
  let created = false;
  if (!term) {
    const opts = { name: 'Jarvis' };
    if (typeof cwd === 'string') opts.cwd = cwd;
    term = vscode.window.createTerminal(opts);
    created = true;
  }
  term.show(true);
  term.sendText(command, true);
  return [200, { ok: true, terminal: term.name, created }];
}

function handler(req, res) {
  try {
    if (req.headers.origin !== undefined) return send(res, 403, { ok: false, error: 'Origin not allowed' });
    if (!HOSTS.includes(String(req.headers.host || '').toLowerCase())) {
      return send(res, 403, { ok: false, error: 'Bad Host header' });
    }
    const url = (req.url || '').split('?')[0];
    if (req.method === 'GET' && url === '/ping') return send(res, 200, { ok: true, name: 'jarvis-show-code' });
    if (req.method === 'POST' && url === '/release') {
      if (!tokenOk(req)) return send(res, 401, { ok: false, error: 'missing or invalid X-Jarvis-Token' });
      // A focused window never gives up the port; only a blurred owner hands it over.
      if (isFocused()) return send(res, 409, { ok: false, error: 'owner window is focused' });
      res.on('finish', () => { try { log('Released port to a newly focused window'); stopServer(); } catch (_) { /* ignore */ } });
      return send(res, 200, { ok: true, released: true });
    }
    if (req.method === 'POST' && (url === '/show' || url === '/terminal')) {
      if (url === '/terminal' && !tokenOk(req)) {
        return send(res, 401, { ok: false, error: 'missing or invalid X-Jarvis-Token' });
      }
      const chunks = [];
      let size = 0;
      req.on('data', c => {
        size += c.length;
        if (size > 1 << 20) { send(res, 400, { ok: false, error: 'body too large' }); req.destroy(); return; }
        chunks.push(c);
      });
      req.on('end', async () => {
        if (res.writableEnded) return;
        let data;
        try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (e) {
          return send(res, 400, { ok: false, error: 'invalid JSON' });
        }
        if (!data || typeof data !== 'object' || Array.isArray(data)) {
          return send(res, 400, { ok: false, error: 'body must be a JSON object' });
        }
        try {
          const [code, obj] = await (url === '/terminal' ? runInTerminal(data) : show(data));
          send(res, code, obj);
        } catch (e) {
          if (output) output.appendLine(url + ' error: ' + ((e && e.stack) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      });
      return;
    }
    return send(res, 404, { ok: false, error: 'not found' });
  } catch (e) {
    try { send(res, 500, { ok: false, error: String(e) }); } catch (_) { /* ignore */ }
  }
}

// Focus-follows-port: the most recently focused window owns the port and KEEPS it when
// blurred (so Jarvis still works while the user is in another app). A window that gains
// focus and hits EADDRINUSE asks the owner to let go via POST /release (token-protected;
// honored only if the owner is not focused), then retries listen.
const RETRY_ATTEMPTS = 5;
const RETRY_DELAY_MS = 300;
let retryTimer;
let disposed = false;

function log(msg) {
  try { if (output) output.appendLine(msg); } catch (_) { /* ignore */ }
}

function cancelRetry() {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = undefined; }
}

function stopServer() {
  cancelRetry();
  const s = server;
  server = undefined;
  if (s) {
    try { s.close(); } catch (_) { /* ignore */ }
    try { if (s.closeAllConnections) s.closeAllConnections(); } catch (_) { /* ignore */ }
    log('Stopped listening on 127.0.0.1:' + PORT);
  }
}

function requestRelease() {
  try {
    let token;
    try { token = fs.readFileSync(TOKEN_PATH, 'utf8').trim(); } catch (_) { return; }
    const r = http.request({
      host: '127.0.0.1', port: PORT, path: '/release', method: 'POST',
      headers: { 'X-Jarvis-Token': token, 'Content-Length': 0 }, timeout: 1000
    }, resp => { resp.resume(); });
    r.on('timeout', () => { try { r.destroy(); } catch (_) { /* ignore */ } });
    r.on('error', () => { /* owner gone or not ours; the retry will sort it out */ });
    r.end();
  } catch (_) { /* never throw out of the extension host */ }
}

function startServer(attempt) {
  if (disposed || server) return;
  cancelRetry();
  let s;
  try {
    s = http.createServer(handler);
    s.on('error', err => {
      try {
        if (server === s) server = undefined;
        try { s.close(); } catch (_) { /* ignore */ }
        if (disposed) return;
        if (err && err.code === 'EADDRINUSE' && attempt < RETRY_ATTEMPTS && isFocused()) {
          requestRelease();
          retryTimer = setTimeout(() => {
            retryTimer = undefined;
            if (isFocused()) startServer(attempt + 1);
          }, RETRY_DELAY_MS);
          return;
        }
        log('Server error: ' + ((err && err.code) || '') + ' ' + ((err && err.message) || err) +
          ((err && err.code === 'EADDRINUSE') ? ' (port ' + PORT + ' still in use; another window probably owns it)' : ''));
      } catch (_) { /* never throw out of the extension host */ }
    });
    server = s;
    s.listen(PORT, '127.0.0.1', () => log('Listening on 127.0.0.1:' + PORT));
  } catch (e) {
    if (server === s) server = undefined;
    log('Failed to start server: ' + ((e && e.message) || e));
  }
}

function isFocused() {
  try { return !!vscode.window.state.focused; } catch (_) { return false; }
}

function onFocusChange(focused) {
  try {
    if (focused) startServer(1);
    else cancelRetry(); // keep owning the port while blurred
  } catch (e) {
    log('Focus handler error: ' + ((e && e.message) || e));
  }
}

function activate(context) {
  disposed = false;
  output = vscode.window.createOutputChannel('Jarvis Show Code');
  context.subscriptions.push(output);
  ensureToken();
  context.subscriptions.push(vscode.window.onDidChangeWindowState(st => onFocusChange(!!(st && st.focused))));
  startServer(1); // focused: take over; unfocused: listen if nothing owns the port
  context.subscriptions.push({ dispose: deactivate });
}

function deactivate() {
  disposed = true;
  clearHighlight();
  stopServer();
}

module.exports = { activate, deactivate };
