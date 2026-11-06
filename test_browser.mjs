import {mkdtemp,mkdir,writeFile,readFile,rm,realpath} from 'node:fs/promises';
import {homedir} from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';

const modulePath=process.env.PLAYWRIGHT_MODULE;
if(!modulePath)throw Error('Set PLAYWRIGHT_MODULE to an installed playwright index.mjs');
const {chromium}=await import(pathToFileURL(modulePath));
const project=path.dirname(fileURLToPath(import.meta.url));
// manager.validate_root only accepts roots under $HOME (non-hidden) or /Volumes, so never use os.tmpdir().
// Fixtures go next to this script when it lives under $HOME; otherwise (e.g. a copy in /private/tmp) under ~/Desktop.
const home=await realpath(homedir());
const fixtureBase=(project===home||project.startsWith(home+path.sep))&&!path.relative(home,project).split(path.sep).some(p=>p.startsWith('.'))?project:path.join(home,'Desktop');
const directory=await realpath(await mkdtemp(path.join(fixtureBase,'test-fixture-')));
const source=path.join(directory,'sources');
await (await import('node:fs/promises')).mkdir(source);
const one=path.join(source,'20261005-0900-市场研究 #1.html');
await writeFile(one,'<title>市场需求研究</title><h1>用户任务</h1><p>用户访谈独有词</p>');
await writeFile(path.join(source,'malicious.html'),'<title>&lt;img src=x onerror=window.INJECTED=1&gt;</title><h1>脚本安全验证</h1><p>&lt;/script&gt;原文</p><script>window.EXECUTED=1</script>');
for(let i=0;i<63;i++)await writeFile(path.join(source,`record-${i}.html`),`<title>资料页面${i}</title><h1>可搜索内容 ${i}</h1>`);
// One theme subfolder (first-level directory under the root) drives the theme filter in both serve and portable modes.
await (await import('node:fs/promises')).mkdir(path.join(source,'01-主题测试'));
await writeFile(path.join(source,'01-主题测试','20261023-1200-主题内页面.html'),'<title>主题内页面</title><h1>主题筛选验证</h1>');
// Default: the fixture server records the exact /usr/bin/open argv instead of launching the desktop browser.
// HTML_MANAGER_NATIVE_OPEN=1 restores the real system open for a manual visible check and keeps the fixture directory.
const nativeOpen=process.env.HTML_MANAGER_NATIVE_OPEN==='1';
const openLog=path.join(directory,'open-requests.jsonl');
const recorder=nativeOpen?'':`import json, subprocess\n_run=subprocess.run\ndef _record(args,*a,**k):\n    if list(args[:1])==['/usr/bin/open']:\n        with open(${JSON.stringify(openLog)},'a',encoding='utf-8') as f: f.write(json.dumps(list(args),ensure_ascii=False)+'\\n')\n        return subprocess.CompletedProcess(args,0,b'',b'')\n    return _run(args,*a,**k)\nsubprocess.run=_record\n`;
const program=`${recorder}from pathlib import Path\nfrom manager import Catalog, create_server\nc=Catalog(Path(${JSON.stringify(directory)})/'state', [Path(${JSON.stringify(source)})]); c.refresh(asynchronous=False)\ns=create_server(c,0); print(s.server_port,flush=True); s.serve_forever()`;
const child=spawn('python3',['-u','-c',program],{cwd:project,stdio:['ignore','pipe','pipe']});
let browser;
try{
  const lines=createInterface({input:child.stdout});
  const port=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture server startup timed out')),15000);lines.once('line',line=>{clearTimeout(timer);resolve(Number(line))});child.once('exit',code=>reject(Error('Fixture exited '+code)))});
  // Playwright's own pinned Chromium revision drifts from the cache; use the headless-shell wrapper like test_editor_browser.mjs.
  browser=await chromium.launch({executablePath:process.env.CHROME_HEADLESS_SHELL||path.join(homedir(),'.local/bin/chrome-headless-shell')});
  const page=await browser.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${port}`);
  await page.waitForFunction(()=>document.querySelector('#count').textContent==='66');
  for(const width of [390,768,1440]){
    await page.setViewportSize({width,height:950});
    await page.waitForTimeout(80);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Overflow at ${width}`);
    assert.equal(await page.locator('.card').count(),60);
    console.log(`PASS viewport ${width}: no overflow, 60 visible records`);
  }
  await page.locator('#query').fill('用户访谈独有词');
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.match(await page.locator('.topic').innerText(),/市场需求研究/);
  await page.locator('.favorite').click();
  await page.locator('[data-scope="favorites"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#favCount').textContent==='1');
  await page.locator('[data-scope="favorites"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.equal(await page.locator('.favorite').getAttribute('aria-pressed'),'true');
  await page.locator('.open').click(); // Full HTTP/CSRF/path-check path; only the final /usr/bin/open is recorded unless HTML_MANAGER_NATIVE_OPEN=1.
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已请求系统打开'));
  if(!nativeOpen)assert.deepEqual((await readFile(openLog,'utf8')).trim().split('\n').map(line=>JSON.parse(line)),[['/usr/bin/open',one]]);
  await page.locator('[data-scope="recent"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  await page.locator('[data-scope="all"]').click();
  await page.locator('#query').fill('INJECTED');
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.equal(await page.evaluate(()=>window.INJECTED),undefined);
  assert.equal(await page.locator('.card img').count(),0);
  await page.locator('#query').fill('there-is-no-such-file');
  await page.waitForFunction(()=>!document.querySelector('#empty').hidden);
  await page.locator('#clear').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===60);
  await page.locator('#next').click();
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===6);
  await page.locator('#prev').click();
  assert.deepEqual(await page.locator('#theme option').allTextContents(),['全部主题','01-主题测试 · 1']);
  assert.equal(await page.locator('#theme optgroup').getAttribute('label'),'sources');
  await page.locator('#theme').selectOption({label:'01-主题测试 · 1'});
  assert.equal(await page.locator('#folder').inputValue(),source);
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.match(await page.locator('.card .meta .theme').innerText(),/01-主题测试/);
  await page.locator('#theme').selectOption('');
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===60);
  console.log('PASS theme filter: subfolder listed with count, filters to 1 card, clears back');
  await page.locator('#theme').selectOption({label:'01-主题测试 · 1'});
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  await rm(path.join(source,'01-主题测试'),{recursive:true,force:true});
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#theme').value===''&&document.querySelectorAll('.card').length===60);
  assert.deepEqual(await page.locator('#theme option').allTextContents(),['全部主题']);
  await mkdir(path.join(source,'01-主题测试'),{recursive:true});
  await writeFile(path.join(source,'01-主题测试','20261023-1200-主题内页面.html'),'<title>主题内页面</title><h1>主题筛选验证</h1>');
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelectorAll('#theme option').length===2);
  console.log('PASS theme vanished: select resets to all and list requeries instead of staying empty');
  await page.locator('#coverageButton').click();
  assert.equal(await page.locator('#coverageDialog').isVisible(),true);
  await page.keyboard.press('Escape');
  await writeFile(path.join(source,'new-report.html'),'<title>新增报告验证</title>');
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#count').textContent==='67');
  assert.equal(errors.length,0,errors.join('\n'));
  console.log(`PASS search/fulltext, persistent favorite, ${nativeOpen?'real native-open':'recorded /usr/bin/open argv'} request/history, XSS/empty state, pagination, modal, refresh, 0 page errors`);
  // Portable export exercises file navigation and escaping independently from HTTP.
  const exported=path.join(directory,'portable.html');
  const python=spawn('python3',['manager.py','export','--root',source,'--out',exported,'--state-dir',path.join(directory,'state')],{cwd:project,stdio:'ignore'});
  await new Promise((resolve,reject)=>{python.once('exit',code=>code===0?resolve():reject(Error('Export failed '+code)))});
  await page.goto(pathToFileURL(exported).href);
  await page.waitForFunction(()=>document.querySelector('#count').textContent==='67');
  const exportedPage=await readFile(exported,'utf8');
  const raw=exportedPage.match(/<script id="snapshot" type="application\/json">([\s\S]*?)<\/script>/)[1];
  assert.equal(raw.includes('</script>'),false);
  assert.equal(JSON.parse(raw).records.some(r=>r.text.includes('</script>')),true);
  await page.locator('#query').fill('用户访谈独有词');
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.equal(await page.locator('.card').count(),1);
  const popupPromise=page.waitForEvent('popup');await page.locator('.open').click();const popup=await popupPromise;
  await popup.waitForLoadState();assert.equal(await popup.title(),'市场需求研究');await popup.close();
  await page.locator('#query').fill('');
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===60);
  await page.locator('#theme').selectOption({label:'01-主题测试 · 1'});
  await page.waitForFunction(()=>document.querySelectorAll('.card').length===1);
  assert.match(await page.locator('.topic').innerText(),/主题内页面/);
  await page.locator('#theme').selectOption('');
  console.log('PASS portable file: full-text search, script-boundary escaping, actual original file navigation, theme filter');
  if(nativeOpen){
    console.log('NATIVE_FIXTURE_URI='+pathToFileURL(one).href);
    console.log('FIXTURE_DIRECTORY='+directory);
  }
}finally{
  if(browser)await browser.close();
  child.kill('SIGTERM');
  // Native mode keeps the harmless fixture for a visible check; the printed FIXTURE_DIRECTORY is removed by the operator afterwards.
  if(!nativeOpen){await new Promise(resolve=>child.exitCode!==null||child.signalCode!==null?resolve():child.once('exit',resolve));await rm(directory,{recursive:true,force:true});}
}
