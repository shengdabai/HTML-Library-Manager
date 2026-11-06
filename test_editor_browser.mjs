import {mkdtemp,writeFile,readFile,readdir,mkdir,rm,realpath,chmod} from 'node:fs/promises';
import {homedir} from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';

// Usage: PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node test_editor_browser.mjs
// Uses chrome-headless-shell only (never a visible Chrome); CHROME_HEADLESS_SHELL overrides the path.
const modulePath=process.env.PLAYWRIGHT_MODULE;
if(!modulePath)throw Error('Set PLAYWRIGHT_MODULE to an installed playwright index.mjs');
const {chromium}=await import(pathToFileURL(modulePath));
const project=path.dirname(fileURLToPath(import.meta.url));
const home=await realpath(homedir());
const directory=await realpath(await mkdtemp(path.join(project.startsWith(home+path.sep)?project:path.join(home,'Desktop'),'test-fixture-editor-')));
const source=path.join(directory,'sources');
await mkdir(path.join(source,'img'),{recursive:true});
const PNG=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==','base64');
await writeFile(path.join(source,'img/hero.png'),PNG);
const pagePath=path.join(source,'页面.html');
const original='<!DOCTYPE html>\n<!-- 顶部注释 -->\n<html lang="zh-CN"><head><meta charset="utf-8"><title>编辑测试页</title><style>h1{color:#333}</style></head>'+
  '<body><h1>错别子标题</h1><p id="intro">第一段内容需要加粗</p><p class="second">第二段原文</p><img src="img/hero.png" alt="主图" width="160" height="90">'+
  '<a href="https://example.com">外链</a><script>document.body.dataset.ran="yes"</script></body></html>\n';
await writeFile(pagePath,original);
const fake=path.join(directory,'fake-codex');
await writeFile(fake,`#!/usr/bin/env python3
import sys, time, pathlib
a = sys.argv[1:]
work = pathlib.Path(a[a.index('-C') + 1]); last = pathlib.Path(a[a.index('-o') + 1])
prompt = sys.stdin.read()
print('fake codex working', flush=True)
time.sleep(1.2)
page = next(p for p in work.iterdir() if p.suffix == '.html')
page.write_text(page.read_text().replace('第二段原文', '第二段由 Codex 改写'))
(work / (page.stem + '_assets') / 'codex-test.png').write_bytes(bytes.fromhex('89504e470d0a1a0a'))
last.write_text('1. 改写了第二段' + ('（含标注）' if '更生动' in prompt else ''))
`);
await chmod(fake,0o755);
const program=`from pathlib import Path\nfrom manager import Catalog, create_server\nfrom editor import Editor\nc=Catalog(Path(${JSON.stringify(directory)})/'state', [Path(${JSON.stringify(source)})]); c.refresh(asynchronous=False)\ns=create_server(c,0,Editor(c,codex=${JSON.stringify(fake)})); print(s.server_port,flush=True); s.serve_forever()`;
const child=spawn('python3',['-u','-c',program],{cwd:project,stdio:['ignore','pipe','inherit']});
const read=()=>readFile(pagePath,'utf8');
let browser,failed=false;
try{
  const lines=createInterface({input:child.stdout});
  const port=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture server startup timed out')),15000);lines.once('line',line=>{clearTimeout(timer);resolve(Number(line))});child.once('exit',code=>reject(Error('Fixture exited '+code)))});
  browser=await chromium.launch({executablePath:process.env.CHROME_HEADLESS_SHELL||path.join(homedir(),'.local/bin/chrome-headless-shell')});
  const context=await browser.newContext({viewport:{width:1440,height:950}});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  page.on('dialog',dialog=>dialog.accept());
  await page.goto(`http://127.0.0.1:${port}/`);
  const edit=page.locator('a.edit').first();
  await edit.waitFor();
  const href=await edit.getAttribute('href');
  assert.match(href,/^\/edit\?id=[0-9a-f]{24}$/);
  console.log('PASS library card links to editor');

  await page.goto(`http://127.0.0.1:${port}${href}`);
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');
  const frame=page.frameLocator('#frame');
  const inner=page.frames().find(f=>f.url().includes('/p/'));
  assert.equal(await inner.evaluate(()=>document.body.dataset.ran),undefined,'page script must not run');
  assert.match(await page.locator('#notice').innerText(),/带脚本/);
  console.log('PASS preview loads without running page scripts');

  // 1. Fix a typo directly in the page.
  await frame.locator('h1').click();
  await inner.evaluate(()=>{const t=document.querySelector('h1').firstChild;const r=document.createRange();r.setStart(t,1);r.setEnd(t,3);const s=getSelection();s.removeAllRanges();s.addRange(r)});
  await page.keyboard.type('别字');
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='有未保存的修改');
  await page.locator('#save').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');
  let saved=await read();
  assert.match(saved,/<h1>错别字标题<\/h1>/);
  assert.ok(saved.startsWith('<!DOCTYPE html>\n<!-- 顶部注释 -->\n<html'),'doctype and top comment kept');
  assert.match(saved,/<script>document\.body\.dataset\.ran="yes"<\/script>/);
  assert.doesNotMatch(saved,/data-hm-|contenteditable/);
  console.log('PASS typo fixed in place and saved without editor markup');

  // 2. Bold a word with the format toolbar, then undo/redo.
  await frame.locator('#intro').click();
  await inner.evaluate(()=>{const t=document.querySelector('#intro').firstChild;const r=document.createRange();r.setStart(t,5);r.setEnd(t,7);const s=getSelection();s.removeAllRanges();s.addRange(r)});
  await page.locator('[data-cmd="bold"]').click();
  await page.waitForTimeout(500);
  assert.match(await inner.evaluate(()=>document.querySelector('#intro').innerHTML),/<(b|strong|span style="font-weight: bold;")>需要<\/\1?/);
  await page.locator('#undo').click();
  assert.equal(await inner.evaluate(()=>document.querySelector('#intro').innerHTML),'第一段内容需要加粗');
  await page.locator('#redo').click();
  assert.match(await inner.evaluate(()=>document.querySelector('#intro').innerHTML),/需要/);
  assert.notEqual(await inner.evaluate(()=>document.querySelector('#intro').innerHTML),'第一段内容需要加粗');
  console.log('PASS bold formatting with undo/redo');

  // 3. Select mode: style a heading and replace the image.
  await page.locator('#modeSelect').click();
  await frame.locator('h1').click();
  await page.waitForFunction(()=>document.querySelector('#elementName').textContent.startsWith('一级标题'));
  await page.locator('[data-prop="font-size"]').first().fill('40px');
  await page.locator('[data-prop="font-size"]').first().press('Enter');
  await page.locator('[data-prop="font-size"]').first().blur();
  assert.equal(await inner.evaluate(()=>document.querySelector('h1').style.fontSize),'40px');
  await frame.locator('img').click();
  await page.locator('#imageBox').waitFor();
  await page.locator('#imageFile').setInputFiles({name:'新图.png',mimeType:'image/png',buffer:PNG});
  await page.waitForFunction(()=>/图片已放进/.test(document.querySelector('#notice').textContent));
  const newSrc=await inner.evaluate(()=>document.querySelector('img').getAttribute('src'));
  assert.match(decodeURIComponent(newSrc),/^页面_assets\/新图-[0-9a-f]{6}\.png$/);
  assert.equal(await inner.evaluate(()=>document.querySelector('img').naturalWidth),1,'replacement image renders');
  await page.keyboard.press('Meta+s');
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');
  saved=await read();
  assert.match(saved,/<h1 style="font-size: 40px;">错别字标题<\/h1>/);
  assert.ok(saved.includes(newSrc));
  assert.equal((await readdir(path.join(source,'页面_assets'))).length,1);
  console.log('PASS element style + image replacement saved');

  // 4. Annotate the second paragraph and hand it to Codex.
  await frame.locator('p.second').click();
  await page.locator('#annotateElement').click();
  await page.locator('#noteList textarea').first().fill('写得更生动');
  await page.waitForTimeout(800);
  const notes=JSON.parse(await readFile(path.join(directory,'state/editor/annotations',href.split('=')[1]+'.json'),'utf8'));
  assert.equal(notes.length,1);
  assert.equal(notes[0].instruction,'写得更生动');
  assert.match(notes[0].selector,/p:nth-of-type\(2\)/);
  console.log('PASS annotation stored outside the page');
  await page.locator('[data-tab="codex"]').click();
  assert.equal(await page.locator('#pickedCount').innerText(),'1');
  await page.locator('#codexAsk').fill('整体保持原样');
  await page.locator('#codexRun').click();
  await page.waitForFunction(()=>document.querySelector('#jobStatus').textContent==='运行中'||document.querySelector('#jobStatus').textContent==='已写回');
  assert.equal(await page.locator('#save').isDisabled(),true,'editing locked while Codex runs');
  await page.waitForFunction(()=>document.querySelector('#jobStatus').textContent==='已写回',null,{timeout:20000});
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');
  assert.match(await read(),/第二段由 Codex 改写/);
  assert.match(await page.locator('#jobSummary').innerText(),/改写了第二段（含标注）/);
  const fresh=page.frames().find(f=>f.url().includes('/p/'));
  await page.waitForFunction(()=>document.querySelector('#frame').contentDocument.querySelector('p.second')?.textContent==='第二段由 Codex 改写');
  assert.equal(await fresh.evaluate(()=>document.body.dataset.ran),undefined);
  assert.ok((await readdir(path.join(source,'页面_assets'))).includes('codex-test.png'));
  console.log('PASS Codex job applied, page reloaded, asset copied');
  await page.locator('[data-tab="notes"]').click();
  assert.match(await page.locator('#noteList').innerText(),/已完成/);
  await page.locator('[data-tab="codex"]').click();
  await page.locator('#jobUndo').click();
  await page.waitForFunction(()=>document.querySelector('#jobStatus').textContent==='已撤销');
  await page.waitForFunction(()=>document.querySelector('#frame').contentDocument.querySelector('p.second')?.textContent==='第二段原文');
  assert.match(await read(),/第二段原文/);
  console.log('PASS Codex change undone');

  // 5. Versions list and restore to the very first backup.
  await page.locator('[data-tab="versions"]').click();
  const versions=page.locator('#versionList .version');
  assert.ok(await versions.count()>=4,'versions recorded');
  await versions.last().locator('button').click();
  await page.waitForFunction(()=>/已恢复/.test(document.querySelector('#notice').textContent));
  assert.equal(await read(),original);
  console.log('PASS version restore returns the original bytes');

  // 6. External change while editing produces a conflict, not an overwrite.
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');
  await page.locator('#modeText').click();
  await writeFile(pagePath,original.replace('第二段原文','外部修改'));
  await inner.evaluate(()=>0).catch(()=>0);
  const current=page.frames().find(f=>f.url().includes('/p/'));
  await frame.locator('#intro').click();
  await current.evaluate(()=>{const t=document.querySelector('#intro').firstChild;const r=document.createRange();r.setStart(t,0);r.setEnd(t,0);const s=getSelection();s.removeAllRanges();s.addRange(r)});
  await page.keyboard.type('新');
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='有未保存的修改');
  await page.locator('#save').click();
  await page.waitForFunction(()=>/编辑期间被修改/.test(document.querySelector('#notice').textContent));
  assert.match(await read(),/外部修改/);
  assert.equal(await page.locator('#notice button').count(),2);
  console.log('PASS concurrent external edit reported as conflict');

  // 7. Narrow screens: editor stays usable without horizontal overflow.
  await page.locator('#notice button').last().click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='已保存');

  // 6b. Preview reloads while an image upload is in flight: the stale node must not be touched.
  await page.locator('#modeSelect').click();
  await page.frames().find(f=>f.url().includes('/p/')).locator('img').click();
  await page.locator('#imageBox').waitFor();
  const beforeSrc=await page.evaluate(()=>document.querySelector('#frame').contentDocument.querySelector('img').getAttribute('src'));
  let release;const gate=new Promise(resolve=>{release=resolve});
  await page.route('**/api/editor/upload',async route=>{await gate;await route.continue()});
  await page.locator('#imageFile').setInputFiles({name:'迟到.png',mimeType:'image/png',buffer:PNG});
  await page.waitForFunction(()=>/正在上传图片/.test(document.querySelector('#notice').textContent));
  const oldDoc=await page.evaluateHandle(()=>document.querySelector('#frame').contentDocument);
  await page.evaluate(()=>load());
  await page.waitForFunction(old=>document.querySelector('#frame').contentDocument!==old&&document.querySelector('#status').textContent==='已保存',oldDoc);
  release();
  await page.waitForFunction(()=>/没有替换/.test(document.querySelector('#notice').textContent));
  await page.unroute('**/api/editor/upload');
  assert.equal(await page.evaluate(()=>document.querySelector('#frame').contentDocument.querySelector('img').getAttribute('src')),beforeSrc);
  assert.equal(await page.locator('#status').innerText(),'已保存');
  console.log('PASS late upload after preview reload leaves the page untouched');
  for(const width of [390,768]){
    await page.setViewportSize({width,height:900});
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`overflow at ${width}`);
    console.log(`PASS editor viewport ${width}: no horizontal overflow`);
  }
  assert.deepEqual(errors,[]);
  console.log('PASS no page errors');
}catch(error){failed=true;throw error}
finally{
  if(browser)await browser.close();
  child.kill();
  if(!failed||process.env.KEEP_FIXTURE!=='1')await rm(directory,{recursive:true,force:true});
}
