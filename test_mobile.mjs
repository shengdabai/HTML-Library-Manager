// Offline regression acceptance. No private catalog, Chrome.app, or network required.
import {mkdtemp,mkdir,writeFile,realpath} from 'node:fs/promises';
import {rmSync,existsSync} from 'node:fs';import {homedir} from 'node:os';import path from 'node:path';import {spawn} from 'node:child_process';import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
if(!process.env.PLAYWRIGHT_MODULE)throw Error('Set PLAYWRIGHT_MODULE');
const {chromium,webkit}=await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE));
// Chromium uses the headless-shell wrapper (same as test_editor_browser.mjs), so Playwright's pinned Chromium revision need not be cached.
const chromiumOptions={executablePath:process.env.CHROME_HEADLESS_SHELL||path.join(homedir(),'.local/bin/chrome-headless-shell')};
// WebKit has no wrapper: it needs the exact revision this Playwright expects. A missing binary is reported as NOT RUN, never as PASS;
// REQUIRE_WEBKIT=1 turns it into a failure.
let webkitPath='';try{webkitPath=webkit.executablePath()}catch{}
const webkitInstalled=Boolean(webkitPath)&&existsSync(webkitPath);
if(!webkitInstalled&&process.env.REQUIRE_WEBKIT==='1')throw Error(`WEBKIT NOT INSTALLED - NOT RUN (REQUIRE_WEBKIT=1): ${webkitPath||'no executable path'}`);
const project=path.dirname(fileURLToPath(import.meta.url));
// manager.validate_root only accepts roots under $HOME (non-hidden) or /Volumes, so never use os.tmpdir().
const home=await realpath(homedir());const fixtureBase=(project===home||project.startsWith(home+path.sep))&&!path.relative(home,project).split(path.sep).some(p=>p.startsWith('.'))?project:path.join(home,'Desktop');
const dir=await mkdtemp(path.join(fixtureBase,'test-mobile-'));// The exit handler removes the fixture on failure and on the default (stubbed-trash) success path.
let cleaned=false;process.on('exit',()=>{if(!cleaned)rmSync(dir,{recursive:true,force:true})});const source=path.join(dir,'source');await mkdir(source);
await writeFile(path.join(source,'one.html'),'<title>ＦＴＳ 手机资料</title><p>正文独有词 __FALLBACK__ __SNAPSHOT__ &lt;/script&gt;</p>');
await writeFile(path.join(source,'two.html'),'<title>普通资料</title>');
const output=path.join(dir,'catalog.html');const child=spawn('python3',['manager.py','export','--root',source,'--state-dir',path.join(dir,'state'),'--out',output],{cwd:project,stdio:'ignore'});
await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',c=>c===0?resolve():reject(Error('Export failed '+c)))});
const results=[];
for(const type of [chromium,webkit]){
 if(type===webkit&&!webkitInstalled){console.log(`!!! WEBKIT NOT INSTALLED - NOT RUN: ${webkitPath||'no executable path'} (npx playwright install webkit, then rerun; REQUIRE_WEBKIT=1 makes this a failure)`);results.push('webkit NOT RUN (unverified)');continue}
 const b=await type.launch(type===chromium?chromiumOptions:{});try{
 const p=await b.newPage({viewport:{width:390,height:844},userAgent:'iPhone'});const errors=[];p.on('pageerror',e=>errors.push(e.message));await p.goto(pathToFileURL(output).href);await p.waitForFunction(()=>document.querySelector('#count').textContent==='2');
 await p.locator('#query').fill('fts');await p.waitForFunction(()=>current.total===1);await p.locator('#query').fill('正文独有词');await p.waitForFunction(()=>current.total===1);await p.locator('#searchMode').selectOption('meta');await p.waitForFunction(()=>current.total===0);await p.locator('#searchMode').selectOption('full');await p.waitForFunction(()=>current.total===1);await p.locator('#query').fill('__FALLBACK__');await p.waitForFunction(()=>current.total===1);
 await p.locator('#resetQuery').click();await p.waitForFunction(()=>current.total===2);await p.locator('#query').dispatchEvent('compositionstart');await p.locator('#query').fill('fts');await p.waitForTimeout(150);assert.equal(await p.evaluate(()=>current.total),2);await p.locator('#query').dispatchEvent('compositionend');await p.waitForFunction(()=>current.total===1);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
 const blocked=await b.newPage({viewport:{width:390,height:844},userAgent:'iPhone'});await blocked.addInitScript(()=>{const W=Worker;window.Worker=class extends W{postMessage(m){if(!m.init){this.dispatchEvent(new MessageEvent('message',{data:{id:m.id,error:'fixture failure'}}));return}super.postMessage(m)}};Object.defineProperty(window,'localStorage',{get(){throw Error('blocked')}})});await blocked.goto(pathToFileURL(output).href);await blocked.waitForFunction(()=>document.querySelector('#count').textContent==='2');assert.match(await blocked.evaluate(()=>engineBackend),/分片/);await blocked.locator('.favorite').first().click();await blocked.waitForFunction(()=>document.querySelector('#favCount').textContent==='1');assert.match(await blocked.locator('#notice').innerText(),/仅本次有效/);await blocked.locator('.open').first().click();assert.equal(await blocked.locator('.mobile-help').isVisible(),true);
 const nojs=await b.newContext({javaScriptEnabled:false});const q=await nojs.newPage();await q.goto(pathToFileURL(output).href);assert.equal(await q.locator('#fallback').isVisible(),true);assert.match(await q.locator('.fallback-text').innerText(),/one.html[\s\S]*two.html|two.html[\s\S]*one.html/);await nojs.close();
 console.log(`PASS ${type.name()}: portable markers, fullwidth/meta/body search, IME, mobile layout, worker fault fallback, blocked storage, visible open hint, no-JS catalog`);results.push(`${type.name()} PASS`);
 }finally{await b.close()}
}
// Fixture cleanup goes through macOS FileManager.trashItem, which ignores $HOME and would drop test fixtures into the
// real ~/.Trash. By default the trash call is stubbed: its command and arguments are asserted, then the fixture
// (our own mkdtemp dir) is removed locally by the exit handler. Set TEST_MOBILE_REAL_TRASH=1 to run the real call.
const trashSwift='import Foundation; try FileManager.default.trashItem(at:URL(fileURLWithPath:CommandLine.arguments[1]),resultingItemURL:nil)';
const trashCall={command:'/usr/bin/swift',args:['-e',trashSwift,dir]};
assert.equal(path.dirname(trashCall.args[2]),fixtureBase);assert.match(path.basename(trashCall.args[2]),/^test-mobile-/);assert.equal(trashCall.args[2],dir);assert.match(trashCall.args[1],/trashItem\(at:URL\(fileURLWithPath:CommandLine\.arguments\[1\]\)/);
if(process.env.TEST_MOBILE_REAL_TRASH==='1'){
 const trash=spawn(trashCall.command,trashCall.args,{stdio:'inherit'});await new Promise((resolve,reject)=>{trash.once('error',reject);trash.once('exit',c=>c===0?resolve():reject(Error('Fixture cleanup failed '+c)))});cleaned=true;console.log('Fixture moved to Trash');
}else console.log('Fixture trash call stubbed (arguments verified); fixture removed locally, real Trash untouched');
console.log(`SUMMARY: ${results.join('; ')}${webkitInstalled?'':' -- WEBKIT UNVERIFIED'}`);
