'use strict';
// Jarvis Browser service worker. Long-polls the local helper for commands, runs them, posts results.
// Never clicks, types or submits anything on a page.

const BASE = 'http://127.0.0.1:47822';
const DEFAULT_ALLOWED = ['arxiv.org', 'www.arxiv.org', 'ar5iv.org', 'ar5iv.labs.arxiv.org', 'example.com', 'localhost'];
const POLL_ABORT_MS = 35000;
const MAX_BACKOFF_MS = 15000;

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- storage -------------------------------------------------------------------------

async function getToken() {
  const { token } = await chrome.storage.local.get('token');
  return typeof token === 'string' ? token.trim() : '';
}

async function getAllowed() {
  const { allowed } = await chrome.storage.local.get('allowed');
  if (Array.isArray(allowed)) return allowed;
  await chrome.storage.local.set({ allowed: DEFAULT_ALLOWED });
  return DEFAULT_ALLOWED.slice();
}

let lastStatus = '';
async function setStatus(s) {
  if (s === lastStatus) return;
  lastStatus = s;
  try { await chrome.storage.local.set({ status: { state: s, at: Date.now() } }); } catch (_) { /* ignore */ }
}

function normalizeHost(input) {
  if (typeof input !== 'string') return '';
  let h = input.trim().toLowerCase();
  if (!h) return '';
  if (h.includes('://')) {
    try { h = new URL(h).hostname; } catch (_) { return ''; }
  }
  h = h.replace(/\.$/, '');
  if (h.length > 253 || !/^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/.test(h) || h.includes('..')) return '';
  return h;
}

// ---- tab helpers ---------------------------------------------------------------------

async function activeTab() {
  const win = await chrome.windows.getLastFocused({ populate: true });
  const tab = (win.tabs || []).find(t => t.active);
  return tab || null;
}

function parseHttpUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url : null;
  } catch (_) { return null; }
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise(resolve => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(onUpd);
      clearTimeout(timer);
      resolve();
    };
    const onUpd = (id, info) => { if (id === tabId && info.status === 'complete') finish(); };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpd);
  });
}

// Returns {tab} or {error: result-object}. Enforces the allowlist before touching the page.
async function allowedActiveTab() {
  const tab = await activeTab();
  if (!tab) return { error: { ok: false, error: 'no_active_tab' } };
  const url = parseHttpUrl(tab.url || '');
  if (!url) return { error: { ok: false, error: 'site_not_allowed', host: '', message: 'The active tab is not a normal web page.' } };
  const host = url.hostname.toLowerCase();
  const allowed = (await getAllowed()).map(h => String(h).toLowerCase());
  if (!allowed.includes(host)) return { error: { ok: false, error: 'site_not_allowed', host } };
  return { tab, host };
}

async function runInTab(tabId, op, args) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    const res = await chrome.scripting.executeScript({
      target: { tabId },
      func: (o, a) => globalThis.__jarvisLib.run(o, a),
      args: [op, args],
    });
    return (res && res[0] && res[0].result) || { ok: false, error: 'no_result' };
  } catch (e) {
    return { ok: false, error: 'script_failed', message: String((e && e.message) || e) };
  }
}

// ---- commands ------------------------------------------------------------------------

async function execute(cmd) {
  switch (cmd && cmd.action) {
    case 'open': {
      const url = typeof cmd.url === 'string' ? parseHttpUrl(cmd.url.trim()) : null;
      if (!url) return { ok: false, error: 'bad_url', message: 'Only http and https URLs can be opened.' };
      let tab;
      if (cmd.newTab) {
        tab = await chrome.tabs.create({ url: url.href, active: true });
      } else {
        const cur = await activeTab();
        tab = cur ? await chrome.tabs.update(cur.id, { url: url.href }) : await chrome.tabs.create({ url: url.href, active: true });
      }
      await waitForLoad(tab.id, 8000);
      const final = await chrome.tabs.get(tab.id).catch(() => tab);
      return { ok: true, tabId: tab.id, url: final.url || url.href, title: final.title || '', loaded: final.status === 'complete' };
    }
    case 'show':
    case 'clear':
    case 'read': {
      const r = await allowedActiveTab();
      if (r.error) return r.error;
      return runInTab(r.tab.id, cmd.action, cmd);
    }
    case 'allow': {
      const host = normalizeHost(cmd.host);
      if (!host) return { ok: false, error: 'bad_host' };
      const list = await getAllowed();
      if (!list.includes(host)) list.push(host);
      await chrome.storage.local.set({ allowed: list });
      return { ok: true, host, allowed: list };
    }
    case 'list_allowed':
      return { ok: true, allowed: await getAllowed() };
    default:
      return { ok: false, error: 'unknown_action' };
  }
}

// ---- poll loop -----------------------------------------------------------------------

async function postResult(token, id, result) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(BASE + '/result', {
        method: 'POST',
        headers: { 'X-Jarvis-Token': token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, result }),
      });
      if (r.ok || r.status === 404) return;
    } catch (_) { /* retry once */ }
    await sleep(300);
  }
}

let loopRunning = false;
async function pollLoop() {
  if (loopRunning) return;
  loopRunning = true;
  let backoff = 1000;
  try {
    for (;;) {
      const token = await getToken();
      if (!token) { await setStatus('no_token'); await sleep(3000); continue; }
      try {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), POLL_ABORT_MS);
        let resp;
        try {
          resp = await fetch(BASE + '/poll', { headers: { 'X-Jarvis-Token': token }, signal: ctl.signal, cache: 'no-store' });
        } finally { clearTimeout(timer); }
        if (resp.status === 401) { await setStatus('bad_token'); await sleep(5000); continue; }
        if (resp.status === 204) { backoff = 1000; await setStatus('connected'); continue; }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        backoff = 1000;
        await setStatus('connected');
        const job = await resp.json();
        let result;
        try { result = await execute(job.command); } catch (e) {
          result = { ok: false, error: 'exception', message: String((e && e.message) || e) };
        }
        await postResult(token, job.id, result);
      } catch (_) {
        await setStatus('disconnected');
        await sleep(backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    }
  } finally {
    loopRunning = false;
  }
}

// ---- lifecycle -----------------------------------------------------------------------

chrome.alarms.create('jarvis-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'jarvis-keepalive') pollLoop(); });
chrome.runtime.onStartup.addListener(() => { pollLoop(); });
chrome.runtime.onInstalled.addListener(async d => {
  await getAllowed();
  if (d.reason === 'install' && !(await getToken())) chrome.runtime.openOptionsPage();
  pollLoop();
});
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'local' && ch.token) pollLoop(); });

pollLoop();
