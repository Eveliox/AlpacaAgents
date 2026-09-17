/* Optional real-browser smoke test: Node 22 + Chrome/Chromium. No npm packages.
   Generate dashboard first, then: node tests/dashboard_browser.cjs [path/to/dashboard.html]
   Set CHROME_PATH if Chrome isn't in a usual location. Uses an isolated temp profile.
*/
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const pause = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const chrome = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium'].find(p => p && fs.existsSync(p));
  assert(chrome, 'Chrome not found; set CHROME_PATH');
  const file = path.resolve(process.argv[2] || 'runtime/dashboard.html');
  assert(fs.existsSync(file), 'Generate the dashboard before running this test');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-ui-'));
  const browser = spawn(chrome, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {stdio:'ignore'});
  let ws, send;
  try {
    let port;
    for(let i=0;i<100;i++) {
      try { port = fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]; if(port) break; } catch {}
      await pause(100);
    }
    assert(port,'Browser did not start');
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
    await new Promise(r => ws.addEventListener('open', r, {once:true}));
    let id=0; const pending = new Map();
    const errors=[], network=[];
    ws.addEventListener('message', ({data}) => {
      const m=JSON.parse(data);
      if(m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
      if(m.method === 'Network.requestWillBeSent' && /^https?:/.test(m.params.request.url)) network.push(m.params.request.url);
      if(m.id && pending.has(m.id)) {
        const [resolve,reject]=pending.get(m.id); pending.delete(m.id);
        m.error ? reject(m.error) : resolve(m.result);
      }
    });
    send = (method, params={}) => new Promise((resolve,reject) => {
      const n=++id; pending.set(n,[resolve,reject]); ws.send(JSON.stringify({id:n,method,params}));
    });
    async function evaluate(expression) {
      const r = await send('Runtime.evaluate',{expression,returnByValue:true});
      if(r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    }
    async function question(text) {
      await evaluate(`document.getElementById('chat-input').value=${JSON.stringify(text)};document.getElementById('chat-form').requestSubmit()`);
      return evaluate("document.querySelector('#chat-log .from-agent:last-child').textContent");
    }
    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:pathToFileURL(file).href});
    for(let i=0;i<100;i++) {
      if(await evaluate("!!document.querySelector('#chat-input') && !document.querySelector('#chat-input').disabled")) break;
      await pause(100);
    }
    assert(await evaluate("!document.querySelector('#chat-input').disabled"),'CSP blocked the bundled script / chat failed to initialize');
    assert(await evaluate("[...document.querySelectorAll('img')].every(i => i.complete && i.naturalWidth > 0)"),'Avatar failed to load');
    for (const agent of ['all','houston','star','moon','astra']) {
      await evaluate(`document.getElementById('agent-${agent}').click()`);
      const visible = await evaluate("[...document.querySelectorAll('[data-agent]')].filter(e => getComputedStyle(e).display !== 'none').map(e => e.dataset.agent)");
      assert(visible.length > 0);
      if(agent !== 'all') {
        assert(visible.every(a => a === agent));
        assert.equal(await evaluate("document.querySelector('#chat-agent').value"),agent);
      } else assert.equal(new Set(visible).size,4);
      assert(await evaluate("getComputedStyle(document.querySelector('.hero')).display !== 'none'"));
    }
    await evaluate("document.getElementById('agent-all').click();document.querySelector('[data-chat-agent=moon]').click()");
    assert.equal(await evaluate('document.activeElement.id'),'chat-input');
    assert.match(await question('Explain my risk limits'),/\$100/);
    assert.match(await question('Buy QQQ now'),/can't place/);
    assert.match(await question('Explain QQQ results'),/NOT option profits/);
    // Keyboard submit, independent threads, clearing, no unsafe HTML rendering.
    await evaluate("document.getElementById('chat-input').value='Give me a briefing'");
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    assert.match(await evaluate("document.querySelector('#chat-log .from-agent:last-child').textContent"),/Saved workspace/);
    const historyCount = await evaluate("document.querySelectorAll('#chat-log .chat-message').length");
    await evaluate("document.getElementById('agent-star').click()");
    assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"),1);
    await evaluate("document.getElementById('agent-moon').click()");
    assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"),historyCount);
    await question('<img src=x onerror=globalThis.injected=true>');
    assert(await evaluate("document.querySelector('#chat-log img') === null && globalThis.injected !== true"));
    await question('ABC12345678901234567890123456789');
    assert(!(await evaluate("document.querySelector('#chat-log').textContent")).includes('ABC12345678901234567890123456789'));
    await evaluate("document.getElementById('chat-clear').click()");
    assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"),1);
    await evaluate("document.getElementById('chat-input').value='';document.getElementById('chat-form').dispatchEvent(new Event('submit',{cancelable:true}))");
    assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"),1);
    for(let i=0;i<23;i++) await question('Explain my risk limits');
    assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"),41);
    await evaluate("document.getElementById('chat-clear').click();document.getElementById('agent-all').click()");
    for(const width of [1600,1200,390]) {
      await send('Emulation.setDeviceMetricsOverride',{width,height:1100,deviceScaleFactor:1,mobile:width<600});
      assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), `horizontal overflow ${width}`);
      await evaluate('document.activeElement.blur();window.scrollTo(0,0)');
      await pause(250);
      const image = await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(path.dirname(file),`crew-${width}.png`),Buffer.from(image.data,'base64'));
    }
    // Progressive enhancement: disabling JS must leave the snapshot/filter usable and chat inert.
    await send('Emulation.setScriptExecutionDisabled',{value:true});
    await send('Page.reload');
    await pause(350);
    assert(await evaluate("document.querySelector('#chat-input').disabled && document.querySelector('.hero') !== null"));
    assert.equal(errors.length,0,errors.join('\n'));
    assert.deepEqual(network,[], 'Dashboard should make no external requests');
    console.log('PASS: avatars, all filters, chat replies and sources, read-only refusal, XSS handling, per-agent memory, redaction, clear/bounded history, responsive layout, no external network, no-JS fallback.');
  } finally {
    if(send && ws.readyState === WebSocket.OPEN) {
      try {await Promise.race([send('Browser.close'),pause(1500)]);} catch {}
    }
    if(ws) ws.close();
    browser.kill();
    await pause(500);
    try {fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:150});} catch {}
  }
})().catch(e => {console.error(e);process.exitCode=1;});
