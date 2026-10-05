#!/usr/bin/env node
// Phase 4 demo-app verification: PlantDoc end-to-end in headless Chromium.
// Drives Chrome (remote-debugging-port 9222):
//   1. loads shell.html?app=plantdoc.js&mock=/mock.json -> Home paints
//   2. types into the notes TextInput
//   3. simulates a photo pick (DOM.setFileInputFiles) -> onCapture fires
//      -> typed diagnose() model call (mock) -> Result screen paints
//   4. Result shows "Diagnosis: Nitrogen deficiency" + 3 treatment items
// Usage: node cdp_plantdoc_test.js [base-url]
// Exit 0 = all pass; nonzero = failure (prints diagnostics).
'use strict';

// Node <22 has no global WebSocket (CI runs Node 20); fall back to the
// `ws` package installed alongside these tests (npm install ws).
const WebSocket = globalThis.WebSocket ?? require('ws');
const path = require('path');

const BASE = process.argv[2] || 'http://localhost:8471';
const CDP = 'http://localhost:9222';
const LEAF = path.resolve(__dirname, 'leaf.png');

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`PASS ${name}`); }
  else { failures++; console.log(`FAIL ${name}${detail ? ' :: ' + detail : ''}`); }
}

async function cdp() {
  const targets = await (await fetch(`${CDP}/json/list`)).json();
  const page = targets.find(t => t.type === 'page');
  if (!page) throw new Error('no page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl, { maxPayload: 256 * 1024 * 1024 });
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  function send(method, params) {
    return new Promise((resolve) => {
      const i = ++id;
      pending.set(i, resolve);
      ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    });
  }
  async function evalJs(fn, awaitPromise) {
    const r = await send('Runtime.evaluate', {
      expression: `(${fn})()`,
      awaitPromise: !!awaitPromise,
      returnByValue: true,
    });
    if (r.result && r.result.exceptionDetails)
      throw new Error('evaluate threw: ' + JSON.stringify(r.result.exceptionDetails).slice(0, 500));
    const v = r.result && r.result.result;
    return v ? v.value : undefined;
  }
  return { ws, send, evalJs, close() { ws.close(); } };
}

(async () => {
  const c = await cdp();
  try {
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('DOM.enable');

    // ---------- 1. boot ----------
    await c.send('Page.navigate', { url: `${BASE}/shell.html?app=plantdoc.js&mock=/mock.json` });
    const ready = await c.evalJs(`function() {
      return new Promise((res) => {
        const t = setInterval(() => {
          if (window.misaReady) { clearInterval(t); res('ready'); }
          if (window.misaBootError) { clearInterval(t); res('booterror:' + window.misaBootError); }
        }, 100);
        setTimeout(() => { clearInterval(t); res('timeout'); }, 25000);
      });
    }`, true);
    check('PlantDoc boots (misaReady)', ready === 'ready', String(ready).slice(0, 200));

    const heading = await c.evalJs(`function() {
      const h = [...document.querySelectorAll('h2')].map(x => x.textContent);
      return JSON.stringify(h);
    }`);
    check('Home shows PlantDoc heading', (heading || '').includes('PlantDoc'), heading);

    // ---------- 2. type notes ----------
    await c.evalJs(`function() {
      const inp = document.querySelector('input[type=text]');
      if (!inp) throw new Error('notes input not found');
      inp.value = 'yellow spots on leaves';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    }`);

    // ---------- 3. simulate photo pick -> onCapture -> diagnose -> Result ----------
    const doc = await c.send('DOM.getDocument', { depth: 0 });
    const q = await c.send('DOM.querySelector', {
      nodeId: doc.result.root.nodeId, selector: 'input[type=file]',
    });
    const hasInput = q.result && q.result.nodeId !== 0;
    check('CameraButton file input present', !!hasInput);
    if (hasInput) {
      await c.send('DOM.setFileInputFiles', { files: [LEAF], nodeId: q.result.nodeId });
    }

    const result = await c.evalJs(`function() {
      return new Promise((res) => {
        const t = setInterval(() => {
          const hs = [...document.querySelectorAll('h2')].map(x => x.textContent);
          const hit = hs.find(x => x.startsWith('Diagnosis:'));
          if (hit) { clearInterval(t); res(hit); }
        }, 200);
        setTimeout(() => { clearInterval(t); res('timeout'); }, 25000);
      });
    }`, true);
    check('diagnose() runs in-page, Result screen paints',
          result === 'Diagnosis: Nitrogen deficiency', String(result).slice(0, 200));

    // ---------- 4. treatment list ----------
    const items = await c.evalJs(`function() {
      return JSON.stringify([...document.querySelectorAll('li')].map(x => x.textContent));
    }`);
    let n = 0;
    try { n = JSON.parse(items || '[]').length; } catch (e) { /* keep 0 */ }
    check('treatment list has 3 items', n === 3, items);

    const noBootErr = await c.evalJs(`function() { return String(window.misaBootError || ''); }`);
    check('no boot errors', !noBootErr, noBootErr.slice(0, 200));
  } catch (e) {
    failures++;
    console.log('FAIL harness error :: ' + String((e && e.message) || e).slice(0, 300));
  } finally {
    c.close();
  }
  console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILURES`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.log('FAIL harness error :: ' + String((e && e.message) || e).slice(0, 300));
  process.exit(2);
});
