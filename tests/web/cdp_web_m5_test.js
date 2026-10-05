#!/usr/bin/env node
// Phase 4 M5: network model calls on the web target, verified in-page.
//   1. OpenAI-protocol POST to the local stub -> validated data ("net ok: four")
//   2. Dead-port base URL -> catchable error ("caught E..."), page stays alive
//   3. Missing key -> catchable E401 (config), page stays alive
// Prerequisites: stub_server.py on 8471, Chromium --remote-debugging-port=9222.
// Usage: node cdp_web_m5_test.js   (uses file:// base, like the main harness)
'use strict';

// Node <22 has no global WebSocket (CI runs Node 20); fall back to the
// `ws` package installed alongside these tests (npm install ws).
const WebSocket = globalThis.WebSocket ?? require('ws');

const BASE = process.env.MISA_WEB_BASE || 'file:///home/hatch/workspace/misa/web';
const CDP = 'http://localhost:9222';
let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`PASS ${name}`); }
  else { failures++; console.log(`FAIL ${name}${detail ? ' :: ' + detail : ''}`); }
}

async function main() {
  const targets = await (await fetch(`${CDP}/json/list`)).json();
  const page = targets.find(t => t.type === 'page');
  if (!page) throw new Error('no page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl, { maxPayload: 256 * 1024 * 1024 });
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params) => new Promise((resolve) => {
    const i = ++id; pending.set(i, resolve);
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  const evalJs = async (fn, awaitPromise) => {
    const r = await send('Runtime.evaluate', {
      expression: `(${fn})()`, awaitPromise: !!awaitPromise, returnByValue: true });
    if (r.result && r.result.exceptionDetails)
      throw new Error('evaluate threw: ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300));
    const v = r.result && r.result.result; return v ? v.value : undefined;
  };
  const waitReady = () => evalJs(`function() {
    return new Promise((res) => {
      const t = setInterval(() => {
        if (window.misaReady) { clearInterval(t); res('ready'); }
        if (window.misaBootError) { clearInterval(t); res('booterror:' + window.misaBootError); }
      }, 100);
      setTimeout(() => { clearInterval(t); res('timeout'); }, 25000);
    });
  }`, true);
  const clickNetModel = () => evalJs(`function() {
    [...document.querySelectorAll('button')].find(x => x.textContent === 'Net model').click();
  }`);
  const rootText = () => evalJs(`function() {
    return document.getElementById('misa-root').textContent;
  }`);
  try {
    await send('Page.enable'); await send('Runtime.enable');

    // ---- 1. network model call -> stub -> validated data ----
    await send('Page.navigate', { url:
      `${BASE}/shell.html?app=webmodel.js&env_TESTKEY=dummy` +
      `&env_MISA_OPENAI_BASE_URL=${encodeURIComponent('http://127.0.0.1:8471/api')}` });
    check('net app boots', await waitReady() === 'ready');
    await clickNetModel();
    await new Promise(r => setTimeout(r, 4000));
    const t1 = await rootText();
    check('network model call returns stub-validated data',
          /net ok: four/.test(t1), t1.slice(0, 160));

    // ---- 2. dead port -> catchable error, page alive ----
    await send('Page.navigate', { url:
      `${BASE}/shell.html?app=webmodel.js&env_TESTKEY=dummy` +
      `&env_MISA_OPENAI_BASE_URL=${encodeURIComponent('http://127.0.0.1:9/api')}` });
    check('dead-port app boots', await waitReady() === 'ready');
    await clickNetModel();
    await new Promise(r => setTimeout(r, 6000));
    const t2 = await rootText();
    check('transport failure is catchable in-page (no crash)',
          /caught E(400|401)/.test(t2), t2.slice(0, 160));
    const alive2 = await evalJs(`function() { return !!window.misaReady; }`);
    check('page alive after transport failure', alive2 === true);

    // ---- 3. missing key -> catchable E401 ----
    await send('Page.navigate', { url:
      `${BASE}/shell.html?app=webmodel.js` +
      `&env_MISA_OPENAI_BASE_URL=${encodeURIComponent('http://127.0.0.1:8471/api')}` });
    check('no-key app boots', await waitReady() === 'ready');
    await clickNetModel();
    await new Promise(r => setTimeout(r, 4000));
    const t3 = await rootText();
    check('missing key degrades to catchable E401',
          /caught E401/.test(t3), t3.slice(0, 160));
  } finally { ws.close(); }
  console.log(failures === 0 ? 'ALL_M5_TESTS_PASS' : `${failures} FAILURES`);
  process.exit(failures === 0 ? 0 : 1);
}
main().catch(e => { console.error('HARNESS_ERROR', e); process.exit(2); });
