/* Optional real-browser smoke test: Node 22 + Chrome/Chromium. No npm packages.
   Generate dashboard first, then: node tests/dashboard_browser.cjs [path/to/dashboard.html]
   Served-mode integration (synthetic records, no keys): node tests/dashboard_browser.cjs --served
   Generative layout + markdown (fake model, no keys): node tests/dashboard_browser.cjs --generative
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
  const generative = process.argv[2] === '--generative';
  const served = process.argv[2] === '--served' || generative;
  let fixture, fixtureDir, url;
  const file = path.resolve(served ? 'runtime/dashboard.html' : process.argv[2] || 'runtime/dashboard.html');
  if (generative) fs.mkdirSync(path.dirname(file), {recursive:true});
  if (served) {
    fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-studio-'));
    fixture = spawn(process.env.PYTHON || 'python', ['-m', 'tests.studio_browser_fixture', fixtureDir, ...(generative ? ['--generative'] : [])], {stdio: ['pipe', 'pipe', 'inherit']});
    url = await new Promise((resolve, reject) => {
      let out = '';
      const timer = setTimeout(() => {fixture.kill(); reject(new Error('Fixture startup timeout'));}, 10000);
      fixture.stdout.on('data', chunk => {
        out += chunk;
        if (out.includes('\n')) { clearTimeout(timer); resolve(out.trim()); }
      });
      fixture.on('error', reject);
      fixture.on('exit', code => {clearTimeout(timer); reject(new Error('Fixture exited ' + code));});
    });
  } else {
    assert(fs.existsSync(file), 'Generate the dashboard before running this test');
    url = pathToFileURL(file).href;
  }
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
    async function settled() {
      for (let i=0;i<100;i++) {
        if (await evaluate("document.getElementById('chat-form').getAttribute('aria-busy') !== 'true'")) return;
        await pause(50);
      }
      throw new Error('Chat request did not settle');
    }
    async function question(text) {
      if (served) await pause(160); // Respect the real server's 10 requests/s limit.
      await evaluate(`document.getElementById('chat-input').value=${JSON.stringify(text)};document.getElementById('chat-form').requestSubmit()`);
      await settled();
      return evaluate("document.querySelector('#chat-log .from-agent:last-child').textContent");
    }
    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url});
    for(let i=0;i<100;i++) {
      if(await evaluate("!!document.querySelector('#chat-input') && !document.querySelector('#chat-input').disabled")) break;
      await pause(100);
    }
    assert(await evaluate("!document.querySelector('#chat-input').disabled"),'CSP blocked the bundled script / chat failed to initialize');
    assert(await evaluate("[...document.querySelectorAll('img')].every(i => i.complete && i.naturalWidth > 0)"),'Avatar failed to load');
    if (generative) {
      assert.equal(await evaluate("document.getElementById('chat-agent').value"), 'nova');
      assert.match(await evaluate("document.querySelector('#chat-log .message-source').textContent"), /read-only tools/);
      assert(await evaluate("!document.querySelector('[data-chat-agent=nova]').disabled"));
    }
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
    // Sidebar anchors reset a conflicting role filter and expose technical details.
    await evaluate("document.getElementById('agent-moon').click();document.querySelector('.sidebar a[href=\"#research-lab\"]').click()");
    assert(await evaluate("document.getElementById('agent-all').checked && getComputedStyle(document.getElementById('research-lab')).display !== 'none'"));
    assert.equal(await evaluate("document.querySelector('.sidebar [aria-current]').getAttribute('href')"), '#research-lab');
    await evaluate("document.querySelector('.sidebar a[href=\"#cycles\"]').click()");
    assert(await evaluate("document.querySelector('#cycles details').open"));
    assert.equal(await evaluate('document.activeElement.id'), 'cycles');
    await evaluate("document.querySelector('.sidebar a[href=\"#overview\"]').click()");
    assert(await evaluate("document.querySelector('.hero').getBoundingClientRect().top < document.querySelector('.agent-summary').getBoundingClientRect().top"));
    await evaluate("document.getElementById('agent-all').click();document.querySelector('[data-chat-agent=moon]').click()");
    assert.equal(await evaluate('document.activeElement.id'),'chat-input');
    assert.match(await question('Explain my risk limits'),/\$100/);
    if (generative) {
      assert(await evaluate("!!document.querySelector('#chat-log .message-heading') && !!document.querySelector('#chat-log strong') && !!document.querySelector('#chat-log li')"));
      assert.equal(await evaluate("document.querySelector('#chat-log a').getAttribute('rel')"), 'noopener noreferrer');
      assert(await evaluate("!document.querySelector('#chat-log img') && !globalThis.injected"));
    }
    assert.match(await question('Buy QQQ now'),/can't place/);
    assert.match(await question('Explain QQQ results'),/NOT option profits/);
    // Keyboard submit, independent threads, clearing, no unsafe HTML rendering.
    await evaluate("document.getElementById('chat-input').value='Give me a briefing'");
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await settled();
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
    for(const width of [1600,1440,1200,1024,768,390]) {
      await send('Emulation.setDeviceMetricsOverride',{width,height:1100,deviceScaleFactor:1,mobile:width<600});
      assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), `horizontal overflow ${width}`);
      await evaluate("document.getElementById('chat-expand').click()");
      assert(await evaluate("document.getElementById('chat-expand').getAttribute('aria-pressed') === 'true'"));
      assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), `expanded chat overflow ${width}`);
      await evaluate("document.getElementById('chat-expand').click();document.querySelector('.chat-jump').click()");
      assert.equal(await evaluate('document.activeElement.id'), 'chat-input');
      assert(await evaluate("document.getElementById('chat-input').getBoundingClientRect().bottom <= window.innerHeight"), `composer unreachable ${width}`);
      await evaluate('document.activeElement.blur();window.scrollTo(0,0)');
      await pause(250);
      const image = await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(served && !generative ? fixtureDir : path.dirname(file),`crew-${generative ? 'generative-' : ''}${width}.png`),Buffer.from(image.data,'base64'));
    }
    if (served) {
      assert.match(await evaluate("document.querySelector('#chat-snapshot').textContent"), /Live local records.*unknown/);
      fs.writeFileSync(path.join(fixtureDir, 'notifications.jsonl'), JSON.stringify({title: 'New record without reload'}) + '\n');
      assert.match(await question('notifications'), /New record without reload/);
      await question('chart test');
      assert(await evaluate("document.querySelector('#chat-log img').src.startsWith('data:image/svg+xml;base64,') && globalThis.injected !== true"));
      // A response arriving after persona-switch must stay in its original thread.
      await evaluate("document.getElementById('agent-houston').click();document.getElementById('chat-input').value='slow briefing';document.getElementById('chat-form').requestSubmit();document.getElementById('agent-star').click()");
      await settled();
      assert(!(await evaluate("document.getElementById('chat-log').textContent")).includes('slow briefing'));
      await evaluate("document.getElementById('agent-houston').click()");
      assert((await evaluate("document.getElementById('chat-log').textContent")).includes('Saved workspace'));
      // Clear discards in-flight replies too.
      await evaluate("document.getElementById('chat-input').value='slow briefing';document.getElementById('chat-form').requestSubmit();document.getElementById('chat-clear').click()");
      await settled();
      assert.equal(await evaluate("document.querySelectorAll('#chat-log .chat-message').length"), 1);
      const before = network.length;
      await question('api_key=DO-NOT-TRANSMIT');
      assert.equal(network.length, before, 'Credential guard must run before fetch');
      await send('Network.emulateNetworkConditions', {offline:true, latency:0, downloadThroughput:0, uploadThroughput:0});
      assert.match(await question('positions'), /Current state is unknown/);
      assert.match(await evaluate("document.querySelector('#chat-snapshot').textContent"), /Disconnected/);
      await send('Network.emulateNetworkConditions', {offline:false, latency:0, downloadThroughput:-1, uploadThroughput:-1});
    }
    // Progressive enhancement: disabling JS must leave the snapshot/filter usable and chat inert.
    await send('Emulation.setScriptExecutionDisabled',{value:true});
    await send('Page.reload');
    await pause(350);
    assert(await evaluate("document.querySelector('#chat-input').disabled && document.querySelector('.hero') !== null"));
    assert.equal(errors.length,0,errors.join('\n'));
    if (served) {
      assert(network.some(u => u === url + '/api/ask'), 'Served chat must use the real HTTP route');
      assert(network.every(u => new URL(u).origin === url), 'No external requests');
    } else assert.deepEqual(network,[], 'Static dashboard should make no network requests');
    console.log('PASS: avatars, all filters, chat replies and sources, read-only refusal, XSS handling, per-agent memory, redaction, clear/bounded history, responsive layout, no external network, no-JS fallback.');
  } finally {
    if(send && ws.readyState === WebSocket.OPEN) {
      try {await Promise.race([send('Browser.close'),pause(1500)]);} catch {}
    }
    if(ws) ws.close();
    browser.kill();
    await pause(500);
    try {fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:150});} catch {}
    if (fixture) {
      fixture.stdin.end();
      await pause(800);
      fixture.kill();
      fs.rmSync(fixtureDir,{recursive:true,force:true,maxRetries:3,retryDelay:150});
    }
  }
})().catch(e => {console.error(e);process.exitCode=1;});
