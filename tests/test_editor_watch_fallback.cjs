const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pollWatch, install} = require('../pi_sandbox_factory/containers/editor/watch-fallback.cjs');
const delay = ms => new Promise(r => setTimeout(r, ms));
async function until(predicate) {
  for(let n=0;n<100;n++){if(predicate())return;await delay(20);}
  assert.ok(predicate(),'Expected watcher event');
}
test('directory additions, modifications, rename and deletion; close stops events', async t => {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'factory-watch-'));
 t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const events=[];const w=pollWatch(dir,{},(kind,name)=>events.push([kind,name]),20);t.after(()=>w.close());
 fs.writeFileSync(path.join(dir,'one'),'a');await until(()=>events.some(e=>e[0]==='rename'&&e[1]==='one'));
 events.length=0;fs.writeFileSync(path.join(dir,'one'),'longer');await until(()=>events.some(e=>e[0]==='change'&&e[1]==='one'));
 events.length=0;fs.renameSync(path.join(dir,'one'),path.join(dir,'two'));await until(()=>events.some(e=>e[1]==='two')&&events.some(e=>e[1]==='one'));
 events.length=0;fs.unlinkSync(path.join(dir,'two'));await until(()=>events.some(e=>e[0]==='rename'&&e[1]==='two'));
 w.close();events.length=0;fs.writeFileSync(path.join(dir,'late'),'late');await delay(80);assert.equal(events.length,0);
});
test('file watches support buffers, abort and ref/unref', async t => {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'factory-watch-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const file=path.join(dir,'file');fs.writeFileSync(file,'a');const controller=new AbortController();let seen;
 const w=pollWatch(new URL('file://'+file),{encoding:'buffer',signal:controller.signal,persistent:false},(_,name)=>seen=name,20);t.after(()=>w.close());
 assert.equal(w.ref(),w);assert.equal(w.unref(),w);fs.writeFileSync(file,'changed');await until(()=>seen);assert.ok(Buffer.isBuffer(seen));assert.equal(seen.toString(),'file');
 controller.abort();seen=null;fs.writeFileSync(file,'later');await delay(80);assert.equal(seen,null);
});
test('fallback is restricted to resource errors and nonrecursive watches', t => {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'factory-watch-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 let code='EACCES';install(()=>{throw Object.assign(new Error('fixture'),{code});});
 assert.throws(()=>fs.watch(dir),{code:'EACCES'});
 for(code of ['EMFILE','ENFILE','ENOSPC']) {const w=fs.watch(dir);w.close();assert.throws(()=>fs.watch(dir,{recursive:true}),{code});}
});
