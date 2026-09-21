/* Passive Agent Desk smoke test. Real loopback server + synthetic journal only.
   node tests/agent_desk_browser.cjs [--static] ; Node 22 + Chrome, no npm deps. */
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
const pause = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const offline = process.argv.includes('--static');
  const chrome = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe', '/usr/bin/google-chrome', '/usr/bin/chromium'].find(p => p && fs.existsSync(p));
  assert(chrome, 'Chrome not found');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-desk-'));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-browser-'));
  const fixture = spawn(process.env.PYTHON || 'python', ['-m', 'tests.studio_browser_fixture', root, '--agent-desk'], {stdio:['pipe','pipe','inherit']});
  let browser, ws, send;
  try {
    const origin = await new Promise((resolve, reject) => {
      let out = '';
      const timer = setTimeout(() => reject(new Error('Fixture timeout')), 10000);
      fixture.stdout.on('data', chunk => { out += chunk; if (out.includes('\n')) { clearTimeout(timer); resolve(out.trim()); } });
      fixture.on('error', reject);
      fixture.on('exit', () => { clearTimeout(timer); reject(new Error('Fixture exited')); });
    });
    const url = offline ? pathToFileURL(path.join(root, 'dashboard.html')).href : origin;
    browser = spawn(chrome, ['--headless', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {stdio:'ignore'});
    let port;
    for (let n=0;n<100;n++) { try { port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]; if(port) break; } catch {} await pause(100); }
    assert(port);
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
    await new Promise(r => ws.addEventListener('open',r,{once:true}));
    let sequence = 0;
    const pending = new Map(), requests = [], errors = [];
    ws.addEventListener('message', ({data}) => {
      const m=JSON.parse(data);
      if(m.method === 'Network.requestWillBeSent' && /^https?:/.test(m.params.request.url)) requests.push(m.params.request);
      if(m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
      if(m.id && pending.has(m.id)) { const [resolve,reject]=pending.get(m.id); pending.delete(m.id); m.error ? reject(m.error) : resolve(m.result); }
    });
    send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,[resolve,reject]);ws.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    async function settle() {
      if(offline)return;
      for(let n=0;n<100;n++){if(await evaluate("!document.getElementById('desk-refresh').disabled"))return;await pause(50);}
      throw new Error('Desk read timeout');
    }
    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url});
    for(let n=0;n<100;n++){if(await evaluate("!!document.getElementById('desk-cycle') && !document.getElementById('desk-cycle').disabled"))break;await pause(100);}
    assert.equal(await evaluate("document.querySelectorAll('[data-desk-agent]').length"),6);
    await evaluate("document.getElementById('agent-moon').click();document.querySelector('.sidebar a[href=\"#agent-desk\"]').click()");
    await pause(120); await settle();
    assert(await evaluate("document.getElementById('desk-panel').open"));
    assert.match(await evaluate("document.getElementById('desk-cycle-title').textContent"),/bbbbbbbb/);
    assert.equal(await evaluate("document.querySelector('[data-desk-agent=spotter]').dataset.status"),'skipped');
    assert.equal(await evaluate("document.querySelectorAll('[data-status=not_implemented]').length"),2);
    await evaluate(`document.getElementById('desk-cycle').value='${'a'.repeat(32)}';document.getElementById('desk-cycle').dispatchEvent(new Event('change'))`);
    await evaluate("document.querySelector('[data-desk-agent=risk]').click()");
    assert.equal(await evaluate('document.activeElement.id'),'desk-inspector');
    assert.match(await evaluate("document.getElementById('desk-inspector').textContent"),/Moon \/ Risk budget/);
    assert.match(await evaluate("document.getElementById('desk-inspector').textContent"),/risk pass/);
    await evaluate("document.querySelector('.desk-proposal summary').click()");
    assert.match(await evaluate("document.querySelector('.desk-proposal').textContent"),/91\.00/);
    assert.match(await evaluate("document.querySelector('.desk-proposal').textContent"),/IWM261016C00205000/);
    assert(!(await evaluate("document.getElementById('agent-desk').textContent")).includes('PRIVATEKEY'));
    assert(await evaluate("!document.querySelector('#agent-desk img') && !globalThis.injected"));
    assert.match(await evaluate("document.getElementById('agent-desk').textContent"),/not human approval/);
    await evaluate("document.querySelector('.desk-activity-item summary').click()");
    assert(await evaluate("document.querySelector('.desk-activity-item').open"));
    assert.match(await evaluate("document.querySelector('.desk-activity-item').textContent"),/stages.reconcile/);
    assert.equal(await evaluate("document.querySelectorAll('#agent-desk form').length"),0);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.desk-flow strong')].map(n=>n.textContent)"),['Reconcile','Existing exits','Entry-mode gate','Scan & candidate gates','Reserve → optional claim / submit']);
    for(const width of [1600,1200,768,390]) {
      await send('Emulation.setDeviceMetricsOverride',{width,height:1100,deviceScaleFactor:1,mobile:width<600});
      assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'),`overflow ${width}`);
      await evaluate("document.activeElement.blur();document.getElementById('agent-desk').scrollIntoView({block:'start'})");
      await pause(100);
      const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.mkdirSync('runtime',{recursive:true});
      fs.writeFileSync(`runtime/desk-${offline ? 'static-' : ''}${width}.png`,Buffer.from(image.data,'base64'));
    }
    if(!offline) {
      const lines=fs.readFileSync(path.join(root,'cycles.jsonl'),'utf8').trim().split('\n');
      const newest=JSON.parse(lines[1]); newest.cycle_id='c'.repeat(32);
      fs.appendFileSync(path.join(root,'cycles.jsonl'),JSON.stringify(newest)+'\n');
      await evaluate("document.getElementById('desk-refresh').click()"); await settle();
      assert.equal(await evaluate("document.getElementById('desk-cycle').value"),'a'.repeat(32),'Polling must not switch a pinned historical selection');
      await evaluate("document.getElementById('desk-follow').click()");
      assert.equal(await evaluate("document.getElementById('desk-cycle').value"),'c'.repeat(32));
      // A pinned record leaving the bounded window is retained and labeled, not replaced by unrelated evidence.
      await evaluate(`document.getElementById('desk-cycle').value='${'a'.repeat(32)}';document.getElementById('desk-cycle').dispatchEvent(new Event('change'))`);
      fs.writeFileSync(path.join(root,'cycles.jsonl'),JSON.stringify(newest)+'\n');
      await evaluate("document.getElementById('desk-refresh').click()"); await settle();
      assert.equal(await evaluate("document.getElementById('desk-cycle').value"),'a'.repeat(32));
      assert.match(await evaluate("document.getElementById('desk-freshness').textContent"),/outside the current history window/);
      await send('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
      await evaluate("document.getElementById('desk-refresh').click()"); await settle();
      assert.match(await evaluate("document.getElementById('desk-connection').textContent"),/current state unknown/);
      await send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
      await evaluate("document.getElementById('desk-follow').click();document.getElementById('desk-refresh').click()"); await settle();
      assert.match(await evaluate("document.getElementById('desk-connection').textContent"),/Connected/);
    }
    await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    assert(await evaluate("getComputedStyle(document.querySelector('.desk-connectors')).animationName === 'none'"));
    await send('Emulation.setScriptExecutionDisabled',{value:true});
    await send('Page.reload'); await pause(300);
    await evaluate("document.getElementById('desk-panel').open=true;document.querySelector('.desk-evidence').open=true");
    assert.match(await evaluate("document.getElementById('desk-evidence').textContent"),/MARKET_CLOSED/);
    assert(await evaluate("document.getElementById('desk-cycle').disabled"));
    assert.equal(await evaluate("document.getElementById('desk-cycle').value"), (offline ? 'b' : 'c').repeat(32));
    assert.deepEqual(errors,[]);
    if(offline)assert.deepEqual(requests,[]);
    else {
      assert(requests.some(r=>r.url.includes('/api/agent-desk/state')));
      assert(requests.every(r=>new URL(r.url).origin===origin));
      assert(requests.every(r=>r.method==='GET'),'Desk must only issue GET requests');
    }
    console.log(`PASS Agent Desk ${offline?'offline':'served'}: six views, cycle selection, native details, risk/proposal provenance, XSS, polling/pinning/disconnection, responsive and no-JS, no action requests.`);
  } finally {
    if(send && ws && ws.readyState===WebSocket.OPEN){try{await Promise.race([send('Browser.close'),pause(1000)]);}catch{}}
    if(ws)ws.close(); if(browser)browser.kill(); fixture.stdin.end(); await pause(600); fixture.kill();
    for(const p of [root,profile]){try{fs.rmSync(p,{recursive:true,force:true,maxRetries:4,retryDelay:150});}catch{}}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
