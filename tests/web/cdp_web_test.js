#!/usr/bin/env node
// Phase 4 M3/M4 headless-Chromium verification via CDP.
// Drives /opt/meta-chromium (remote-debugging-port 9222):
//   1. loads shell.html?mock=/mock.json -> initial screen paints
//   2. DOM matches the text-driver tree node-for-node (mechanical)
//   3. Button click -> handler -> navigate() -> second screen paints
//   4. TextInput typing -> handler reads .text -> value correct
//   5. mock model call returns validated data; budget breach -> E500
//   6. http.get works (separate fetch.js app)
// Usage: node cdp_web_test.js [base-url]
// Exit 0 = all pass; nonzero = failure (prints diagnostics).
'use strict';

// Node <22 has no global WebSocket (CI runs Node 20); fall back to the
// `ws` package installed alongside these tests (npm install ws).
const WebSocket = globalThis.WebSocket ?? require('ws');

const BASE = process.argv[2] || 'http://localhost:8471';
const CDP = 'http://localhost:9222';
// Repo root (for the host `misa` binary used as the reference tree).
// Overridden in CI: MISA_ROOT=$GITHUB_WORKSPACE.
const MISA_ROOT = process.env.MISA_ROOT || '/home/hatch/workspace/misa';

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
  const events = [];
  const consoleMsgs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    else if (m.method === 'Runtime.consoleAPICalled') {
      const args = (m.params.args || []).map(a =>
        a.type === 'string' ? a.value : (a.description || JSON.stringify(a.value)));
      consoleMsgs.push({ type: m.params.type, text: args.join(' ') });
    }
    else events.push(m);
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
  return { ws, send, evalJs, events, consoleMsgs,
    clearConsole() { consoleMsgs.length = 0; },
    close() { ws.close(); } };
}

// ---- DOM tree walker (runs in the page) ----
const TREE_FN = `function() {
  function norm(el) {
    if (!el || el.nodeType !== 1) return null;
    const tag = el.tagName;
    let kind = tag, props = {};
    if (el.classList.contains('misa-screen')) kind = 'Screen';
    else if (tag === 'DIV' && el.style.display === 'flex')
      kind = el.style.flexDirection === 'row' ? 'Row' : 'Column';
    else if (tag === 'DIV' && el.style.flex === '1 1 auto') kind = 'Spacer';
    else if (tag === 'P') kind = 'Text';
    else if (tag === 'H2') kind = 'Heading';
    else if (tag === 'DIV' && el.classList.contains('misa-banner')) {
      kind = 'Banner';
      for (const c of el.classList)
        if (c.startsWith('misa-banner-') && c !== 'misa-banner')
          props.style = c.slice('misa-banner-'.length);
    }
    else if (tag === 'BUTTON') { kind = 'Button'; props.label = el.textContent; }
    else if (tag === 'INPUT') { kind = 'TextInput'; props.hint = el.placeholder; }
    else if (tag === 'UL') kind = 'List';
    else if (tag === 'LI') { kind = 'Item'; props.text = el.textContent; }
    else if (tag === 'IMG') kind = 'Image';
    else if (tag === 'SPAN' && el.classList.contains('misa-cambtn')) kind = 'CameraButton';
    if (kind === 'Text' || kind === 'Heading' || (kind === 'Banner'))
      props.text = el.childNodes[0] && el.childNodes[0].nodeType === 3
        ? el.childNodes[0].textContent : el.textContent;
    const kids = [];
    for (const c of el.children) {
      if (c.tagName === 'INPUT' && c.type === 'file') continue; // camera internals
      if (c.tagName === 'BUTTON' && el.classList.contains('misa-cambtn')) continue;
      const n = norm(c);
      if (n) kids.push(n);
    }
    return { kind, props, kids };
  }
  const root = document.getElementById('misa-root');
  const out = [];
  for (const c of root.children) { const n = norm(c); if (n) out.push(n); }
  return JSON.stringify(out);
}`;

// ---- text-driver tree parser (host binary output) ----
function parseTextTree(text) {
  const lines = text.split('\n').filter(l => l.trim() && !l.startsWith('---'));
  const root = { kids: [] };
  const stack = [{ node: root, indent: -1 }];
  for (const line of lines) {
    const indent = line.match(/^ */)[0].length / 2;
    const body = line.trim();
    const m = body.match(/^(\w+)(?:\((.*)\))?$/);
    if (!m) continue;
    const kind = m[1], props = {};
    if (m[2]) {
      // split top-level ", " (values may contain ", " inside [...] or quotes)
      const parts = [];
      let depth = 0, cur = '';
      for (const ch of m[2]) {
        if (ch === '[') depth++;
        if (ch === ']') depth--;
        if (ch === ',' && depth === 0) { parts.push(cur); cur = ''; }
        else cur += ch;
      }
      parts.push(cur);
      for (const p of parts) {
        const kv = p.match(/^\s*(\w+)=(.*)$/s);
        if (kv) props[kv[1].trim()] = kv[2].trim();
      }
    }
    const node = { kind, props, kids: [] };
    while (stack[stack.length - 1].indent >= indent) stack.pop();
    stack[stack.length - 1].node.kids.push(node);
    stack.push({ node, indent });
  }
  return root.kids;
}

function compareTrees(t, d, path) {
  // t: text-driver node, d: DOM node
  if (t.kind !== d.kind)
    return [`${path}: kind ${t.kind} != ${d.kind}`];
  const errs = [];
  const tp = t.props, dp = d.props;
  const textOf = (n) =>
    n.kind === 'Button' ? n.props.label :
    n.kind === 'Item' ? n.props.text :
    n.kind === 'TextInput' ? '' : (n.props.text || '');
  if (t.kind === 'Button') {
    if ((tp.label || '') !== (dp.label || ''))
      errs.push(`${path}: label ${(tp.label)} != ${(dp.label)}`);
  } else if (t.kind === 'Banner') {
    if ((tp.text || '') !== (dp.text || '')) errs.push(`${path}: banner text mismatch`);
    if ((tp.style || '') !== (dp.style || '')) errs.push(`${path}: banner style mismatch`);
  } else if (t.kind === 'TextInput') {
    if ((tp.hint || '') !== (dp.hint || '')) errs.push(`${path}: hint mismatch`);
  } else if (t.kind === 'Text' || t.kind === 'Heading') {
    if ((tp.text || '') !== (textOf(d) || '')) errs.push(`${path}: text '${tp.text}' != '${textOf(d)}'`);
  } else if (t.kind === 'List') {
    const items = (tp.items || '').replace(/^\[/, '').replace(/\]$/, '')
      .split(',').map(s => s.trim()).filter(Boolean);
    const ditems = d.kids.filter(k => k.kind === 'Item').map(k => k.props.text);
    if (JSON.stringify(items) !== JSON.stringify(ditems))
      errs.push(`${path}: list items ${JSON.stringify(items)} != ${JSON.stringify(ditems)}`);
    return errs; // items compared as leaves
  }
  if (t.kids.length !== d.kids.length)
    errs.push(`${path}: ${t.kids.length} kids != ${d.kids.length}`);
  const n = Math.min(t.kids.length, d.kids.length);
  for (let i = 0; i < n; i++)
    errs.push(...compareTrees(t.kids[i], d.kids[i], `${path}/${t.kind}[${i}]`));
  return errs;
}

async function main() {
  const { execSync } = await import('child_process');
  const c = await cdp();
  try {
    await c.send('Page.enable');
    await c.send('Runtime.enable');

    // ---------- 1. initial screen ----------
    await c.send('Page.navigate', { url: `${BASE}/shell.html?mock=/mock.json` });
    const ready = await c.evalJs(`function() {
      return new Promise((res) => {
        const t = setInterval(() => {
          if (window.misaReady) { clearInterval(t); res('ready'); }
          if (window.misaBootError) { clearInterval(t); res('booterror:' + window.misaBootError); }
        }, 100);
        setTimeout(() => { clearInterval(t); res('timeout'); }, 25000);
      });
    }`, true);
    check('page boots (misaReady)', ready === 'ready', String(ready).slice(0, 200));

    // ---------- 2. DOM vs text-driver tree ----------
    const domJson = await c.evalJs(TREE_FN);
    const domTree = JSON.parse(domJson || '[]');
    const hostOut = execSync(
      `"${MISA_ROOT}/impl/bin/misa" run "${MISA_ROOT}/examples/webslice.misa" --mock "${MISA_ROOT}/examples/webslice.mock.json"`,
      { encoding: 'utf8' });
    const textTree = parseTextTree(hostOut);
    // text tree root: [Column]; DOM root: [Screen > Column]
    const domScreen = domTree.length === 1 && domTree[0].kind === 'Screen' ? domTree[0].kids : domTree;
    let cmpErrs = [];
    if (textTree.length !== domScreen.length)
      cmpErrs.push(`root kids: ${textTree.length} != ${domScreen.length}`);
    else
      for (let i = 0; i < textTree.length; i++)
        cmpErrs.push(...compareTrees(textTree[i], domScreen[i], 'root'));
    check('DOM matches text-driver tree node-for-node', cmpErrs.length === 0,
          cmpErrs.slice(0, 5).join(' | '));

    // ---------- 3. click Next -> Second screen ----------
    await c.evalJs(`function() {
      const b = [...document.querySelectorAll('button')]
        .find(x => x.textContent === 'Next');
      if (!b) throw new Error('Next button not found');
      b.click();
      return true;
    }`);
    await new Promise(r => setTimeout(r, 800));
    const h2after = await c.evalJs(`function() {
      return document.getElementById('misa-root').textContent;
    }`);
    check('click Next -> navigate -> Second paints',
          /second screen/.test(h2after), h2after.slice(0, 120));

    // back home
    await c.evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Back').click();
    }`);
    await new Promise(r => setTimeout(r, 800));

    // ---------- 4. TextInput typing -> handler reads .text ----------
    await c.evalJs(`function() {
      const inp = document.querySelector('input[type="text"]');
      inp.value = 'Ada';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Greet').click();
    }`);
    await new Promise(r => setTimeout(r, 800));
    const greetText = await c.evalJs(`function() {
      return document.getElementById('misa-root').textContent;
    }`);
    check('TextInput .text readback ("Ada")',
          /Hello Ada!/.test(greetText), greetText.slice(0, 120));
    await c.evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Back').click();
    }`);
    await new Promise(r => setTimeout(r, 800));

    // ---------- 5a. mock model call -> validated data ----------
    await c.evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Ask model').click();
    }`);
    await new Promise(r => setTimeout(r, 2500));
    const modelText = await c.evalJs(`function() {
      return document.getElementById('misa-root').textContent;
    }`);
    check('mock model call returns validated data',
          /model said: four/.test(modelText), modelText.slice(0, 160));
    await c.evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Back').click();
    }`);
    await new Promise(r => setTimeout(r, 800));

    // ---------- 5b. forced budget breach -> catchable E500 ----------
    await c.evalJs(`function() {
      [...document.querySelectorAll('button')].find(x => x.textContent === 'Blow budget').click();
    }`);
    await new Promise(r => setTimeout(r, 2500));
    const budgetText = await c.evalJs(`function() {
      return document.getElementById('misa-root').textContent;
    }`);
    check('budget breach raises catchable E500 in-page',
          /caught E500/.test(budgetText), budgetText.slice(0, 160));

    // ---------- 6. http.get (separate app) ----------
    c.clearConsole();
    await c.send('Page.navigate', { url: `${BASE}/shell.html?app=fetch.js` });
    const ready2 = await c.evalJs(`function() {
      return new Promise((res) => {
        const t = setInterval(() => {
          if (window.misaReady) { clearInterval(t); res('ready'); }
          if (window.misaBootError) { clearInterval(t); res('booterror:' + window.misaBootError); }
        }, 100);
        setTimeout(() => { clearInterval(t); res('timeout'); }, 25000);
      });
    }`, true);
    await new Promise(r => setTimeout(r, 1000));
    const logs = c.consoleMsgs.filter(m => m.type === 'log').map(m => m.text).join('\n');
    const mm = logs.match(/(\d{3,})/);
    check('http.get works in-page (fetched byte length)',
          String(ready2) === 'ready' && !!mm && parseInt(mm[1], 10) > 1000,
          `ready=${ready2} logs=${logs.slice(0, 200)}`);

    // ---------- 7. no boot errors / stray error banners ----------
    const bootErr = await c.evalJs(`function() { return window.misaBootError; }`);
    check('no boot errors', !bootErr, String(bootErr).slice(0, 200));
  } finally {
    c.close();
  }
  console.log(failures === 0 ? 'ALL_WEB_TESTS_PASS' : `${failures} FAILURES`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error('HARNESS_ERROR', e); process.exit(2); });
