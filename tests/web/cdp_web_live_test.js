#!/usr/bin/env node
// Optional live-provider check for the web target (CI only, needs a
// MISA_LIVE_API_KEY secret). Boots webmodel.js against the REAL OpenAI
// API and asserts a validated model answer renders in-page.
// The key comes from the environment (GitHub secret); it is passed via
// the page's JS env bridge and is NEVER baked into the wasm binary.
// Usage: MISA_LIVE_API_KEY=sk-... node cdp_web_live_test.js
'use strict';

const BASE = process.env.MISA_WEB_BASE || 'file:///home/hatch/workspace/misa/web';
const CDP = 'http://localhost:9222';
const KEY = process.env.MISA_LIVE_API_KEY;
if (!KEY) { console.log('SKIP: no MISA_LIVE_API_KEY'); process.exit(0); }

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
  try {
    await send('Page.enable'); await send('Runtime.enable');
    const url = `${BASE}/shell.html?app=webmodel_live.js` +
      `&env_TESTKEY=${encodeURIComponent(KEY)}` +
      `&env_MISA_OPENAI_BASE_URL=${encodeURIComponent('https://api.openai.com/v1')}`;
    await send('Page.navigate', { url });
    check('live app boots', await waitReady() === 'ready');
    await evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Net model').click();
    }`);
    // Real provider call: allow up to 90s.
    const text = await evalJs(`function() {
      return new Promise((res) => {
        const t0 = Date.now();
        const t = setInterval(() => {
          const txt = document.getElementById('misa-root').textContent;
          if (/net ok:|caught E/.test(txt) || Date.now() - t0 > 90000) {
            clearInterval(t); res(txt);
          }
        }, 1000);
      });
    }`, true);
    check('live provider call returns validated data in-page',
          /net ok:/.test(text), String(text).slice(0, 200));
  } finally { ws.close(); }
  console.log(failures === 0 ? 'ALL_LIVE_TESTS_PASS' : `${failures} FAILURES`);
  process.exit(failures === 0 ? 0 : 1);
}
main().catch(e => { console.error('HARNESS_ERROR', e); process.exit(2); });
