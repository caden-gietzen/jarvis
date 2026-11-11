'use strict';
const DEFAULT_ALLOWED = ['arxiv.org', 'www.arxiv.org', 'ar5iv.org', 'ar5iv.labs.arxiv.org', 'example.com', 'localhost'];
const $ = id => document.getElementById(id);
const LABELS = {
  connected: 'connected to the helper',
  disconnected: 'helper not reachable (is it running?)',
  no_token: 'no token saved yet',
  bad_token: 'helper rejected the token',
};

function msg(t) { $('msg').textContent = t; setTimeout(() => { $('msg').textContent = ''; }, 3000); }

async function refresh() {
  const { token, allowed, status } = await chrome.storage.local.get(['token', 'allowed', 'status']);
  $('token').value = token || '';
  $('hosts').value = (Array.isArray(allowed) ? allowed : DEFAULT_ALLOWED).join('\n');
  $('status').textContent = status ? (LABELS[status.state] || status.state) : 'starting';
}

$('saveToken').onclick = async () => {
  await chrome.storage.local.set({ token: $('token').value.trim() });
  msg('Token saved.');
};
$('saveHosts').onclick = async () => {
  const hosts = $('hosts').value.split(/\s+/).map(h => h.trim().toLowerCase()).filter(Boolean);
  await chrome.storage.local.set({ allowed: Array.from(new Set(hosts)) });
  msg('Sites saved.');
};
$('reset').onclick = async () => {
  await chrome.storage.local.set({ allowed: DEFAULT_ALLOWED });
  await refresh();
  msg('Reset.');
};
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === 'local' && ch.status && ch.status.newValue) {
    $('status').textContent = LABELS[ch.status.newValue.state] || ch.status.newValue.state;
  }
});
refresh();
