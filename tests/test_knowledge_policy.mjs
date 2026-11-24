import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,writeFileSync,mkdirSync,symlinkSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {indexingTool,localSource,sourceAccess} from '../ai_sandbox_factory/containers/dev/local-knowledge/index-policy.mjs';

function fixture(t){
 const root=mkdtempSync(join(tmpdir(),'knowledge-policy-'));t.after(()=>rmSync(root,{recursive:true,force:true}));
 const docs=join(root,'docs');mkdirSync(docs);writeFileSync(join(docs,'source.md'),'SYNTHETIC_DOCUMENT');
 return {root,docs};
}

test('indexing resolves requested local files and folders without changing them',t=>{
 const {root,docs}=fixture(t);
 assert.equal(localSource('docs',root),docs);
 const manuals=join(root,'id_manuals');mkdirSync(manuals);
 assert.equal(localSource('id_manuals',root),manuals);
 assert.equal(localSource('source.md',docs),join(docs,'source.md'));
 assert.throws(()=>localSource('missing.md',docs),/ENOENT/);
 for(const source of ['', 'https://example.invalid/document', 'file:///etc/passwd'])assert.throws(()=>localSource(source,root));
});

test('direct credential sources and links to them remain blocked',t=>{
 const {root,docs}=fixture(t);
 for(const name of ['.env','.env.production','.npmrc','.netrc','id_ed25519','private.key','private.pem']){
  writeFileSync(join(docs,name),'SYNTHETIC_SECRET');assert.throws(()=>localSource(name,docs),/credentials/);
 }
 symlinkSync(join(docs,'.env'),join(root,'innocent.md'));
 assert.throws(()=>localSource('innocent.md',root),/credentials/);
});

test('indexing always uses host roots and refuses URL sources, overriding caller access',()=>{
 const context={cwd:'/workspace',filesystemAccess:{allowedRoots:['/'],allowNetwork:true}};
 const result=sourceAccess(context,{SANDBOX_KNOWLEDGE_ROOTS:'["/workspace","/mnt/docs"]'});
 assert.equal(result.cwd,'/workspace');
 assert.deepEqual(result.filesystemAccess,{allowedRoots:['/workspace','/mnt/docs'],allowNetwork:false});
 for(const roots of ['[]','{}','[3]','[""]'])assert.throws(()=>sourceAccess({}, {SANDBOX_KNOWLEDGE_ROOTS:roots}));
});

for(const name of ['knowledge_add','knowledge_update'])test(`${name} passes scope, progress and cancellation to the real tool`,async t=>{
 const {docs}=fixture(t);const previous=process.env.SANDBOX_KNOWLEDGE_ROOTS;
 process.env.SANDBOX_KNOWLEDGE_ROOTS=JSON.stringify([docs]);
 t.after(()=>{if(previous===undefined)delete process.env.SANDBOX_KNOWLEDGE_ROOTS;else process.env.SANDBOX_KNOWLEDGE_ROOTS=previous;});
 const signal=new AbortController().signal,progress=()=>{};let observed;
 const expected={content:[{type:'text',text:'INDEXED'}]};
 const definition={name,approval:'write',parameters:{type:'object',properties:{source:{type:'string'},name:{type:'string'},target:{type:'string'}}},execute:async(...args)=>{observed=args;return expected;}};
 const tool=indexingTool(definition);
 const params=name==='knowledge_add'?{source:docs,include_suggested_text:true,include_paths:['.env']}:{target:'docs'};
 assert.equal(await tool.execute('test',params,signal,progress,{cwd:docs}),expected);
 assert.deepEqual(observed.slice(0,4),['test',name==='knowledge_add'?{source:docs,name:'docs'}:{target:'docs'},signal,progress]);
 assert.deepEqual(observed[4].filesystemAccess,{allowedRoots:[docs],allowNetwork:false});
 if(name==='knowledge_add'){
  assert.deepEqual(tool.parameters.required,['source']);
  assert.deepEqual(Object.keys(tool.parameters.properties),['source','name']);
 }
});

test('index errors retain their cause instead of reporting success',async t=>{
 const {docs}=fixture(t);const previous=process.env.SANDBOX_KNOWLEDGE_ROOTS;
 process.env.SANDBOX_KNOWLEDGE_ROOTS=JSON.stringify([docs]);
 t.after(()=>{if(previous===undefined)delete process.env.SANDBOX_KNOWLEDGE_ROOTS;else process.env.SANDBOX_KNOWLEDGE_ROOTS=previous;});
 const error=new Error('Embedding upstream 503: synthetic failure');
 const tool=indexingTool({name:'knowledge_update',parameters:{},execute:async()=>{throw error;}});
 await assert.rejects(tool.execute('id',{target:'docs'}),value=>value===error);
});
