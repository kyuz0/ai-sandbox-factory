// Run inside a disposable Factory image. Uses synthetic metadata and replies, no real inference.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { fork, execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, writeFile, readFile, readdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { initializeModels } from '/opt/pi-local-model-manager/scripts/initialize-models.mjs';
const require = createRequire(import.meta.url);
const librarySkills = process.argv.includes('--library-skills');
const agentDir = '/tmp/radiance-fixture-agent';
const cwd = '/tmp/radiance-fixture-workspace';
await mkdir(agentDir, { recursive:true });
await mkdir(cwd, { recursive:true });
process.env.PI_CODING_AGENT_DIR = agentDir;
const id = 'Qwen3.8-Flash-Next IQ4R';
const advertised = { id, owned_by:'radiance', max_model_len:262144,
  capabilities:{ chat:true, image:true, reasoning:true, reasoning_efforts:['none','minimal','low','medium','xhigh'] } };
const requests = [];
const server = createServer((req,res) => {
  if(req.method === 'POST' && req.url === '/v1/chat/completions') {
    let input=''; req.on('data',chunk=>input+=chunk);
    req.on('end',()=>{
      requests.push(JSON.parse(input));
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      for(const choice of [{delta:{role:'assistant',content:'Synthetic reply.'},finish_reason:null},{delta:{},finish_reason:'stop'}]) {
        res.write('data: '+JSON.stringify({id:'fixture',object:'chat.completion.chunk',created:1,model:id,choices:[{index:0,...choice}]})+'\n\n');
      }
      res.end('data: [DONE]\n\n');
    });
    return;
  }
  const body = req.url === '/v1/models' ? {data:[advertised]} : req.url === '/server_info' ?
    {server:{model:id},engine:{arch:'qwen4exp',quant:'bf16',n_ctx_train:262144}} : req.url === '/props' ?
    {default_generation_settings:{n_ctx:262144},modalities:{vision:true},chat_template:'fixture',chat_template_caps:{supports_thinking:true}} : null;
  res.writeHead(body ? 200 : 404, {'Content-Type':'application/json'});
  res.end(JSON.stringify(body));
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
let worker;
try {
  await writeFile(agentDir+'/local-pi-models.json',JSON.stringify({host:'127.0.0.1',ports:String(server.address().port)}));
  await writeFile(agentDir+'/settings.json',JSON.stringify({extensions:['/opt/pi-local-model-manager/extensions/local-models.js'],skills:librarySkills ? ['/opt/extra-skills/library-first','/opt/extra-skills/library-second'] : []}));
  await initializeModels(agentDir);
  const catalogue = JSON.parse(await readFile(agentDir+'/models.json','utf8'));
  const provider = Object.keys(catalogue.providers)[0];
  const model = catalogue.providers[provider].models[0];
  assert.equal(model.name,id);
  assert.deepEqual(model.input,['text','image']);
  assert.deepEqual(model.thinkingLevelMap,{off:'none',minimal:null,low:'low',medium:'medium',high:null,xhigh:'xhigh',max:null});
  const cli = await promisify(execFile)('pi',['--list-models'], {cwd,env:process.env});
  assert(cli.stdout.includes(id),'Pi CLI did not load discovered model');
  console.log('PASS shipped Pi CLI: fresh Radiance catalogue, model identity, vision and thinking map');
  const extension = (await readdir('/opt/pendant')).find(x=>x.startsWith('cdervis.vscode-pi-'));
  const directory = '/opt/pendant/'+extension;
  const sdk = require(directory+'/dist/pi-sdk.cjs')['@earendil-works/pi-coding-agent']();
  const manager=sdk.SessionManager.create(cwd,agentDir+'/sessions');
  manager.appendMessage({role:'user',content:'Preserve this saved conversation.',timestamp:Date.now()});
  manager.appendMessage({role:'assistant',content:[{type:'text',text:'Saved fixture reply.'}],api:'openai-completions',provider,model:id,usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'stop',timestamp:Date.now()});
  const sessionFile=manager.getSessionFile();
  const cliFile=agentDir+'/sessions/cli-resume.jsonl';
  let cliInitial=true;
  function checkPrompt(expected,absent,start) {
    const payload=requests.slice(start).find(body=>body.messages?.some(m=>JSON.stringify(m.content).includes('Check the current skill catalogue.')));
    assert(payload,'No inference request captured');
    const systems=payload.messages.filter(m=>m.role==='system'||m.role==='developer');
    const latest=JSON.stringify(systems.at(-1));
    for(const name of expected) assert(latest.includes(name),'Current system prompt missing '+name);
    for(const name of absent) assert(!latest.includes(name),'Current system prompt still lists '+name);
    assert(JSON.stringify(payload.messages).includes('Preserve this saved conversation.'),'Original user history missing from outbound request');
    assert(!latest.includes('Use this synthetic fixture only.'),'Skill body was eagerly loaded into system prompt');
  }
  async function cliSkills(expected, absent=[]) {
  const rpc=spawn('pi',['--mode','rpc','--session',cliFile,'--provider',provider,'--model',id],{cwd,env:process.env});
  let buffer='';
  try {
    const commands=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('CLI skill inventory timed out')),60000);
      rpc.on('error',reject);
      rpc.stdout.on('data',chunk=>{
        buffer+=chunk;
        let newline;
        while((newline=buffer.indexOf('\n'))>=0){
          const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);
          try { const event=JSON.parse(line); if(event.id==='skills'){clearTimeout(timer);resolve(event);} } catch{}
        }
      });
      rpc.stdin.write(JSON.stringify({id:'skills',type:'get_commands'})+'\n');
    });
    for(const name of expected) assert(JSON.stringify(commands).includes('skill:'+name),JSON.stringify(commands));
    for(const name of absent) assert(!JSON.stringify(commands).includes('skill:'+name),JSON.stringify(commands));
    const start=requests.length;
    await new Promise((resolve,reject)=>{
      let lines='';const timer=setTimeout(()=>reject(new Error('CLI prompt timed out')),60000);
      rpc.stdout.on('data',chunk=>{lines+=chunk;let end;while((end=lines.indexOf('\n'))>=0){const line=lines.slice(0,end);lines=lines.slice(end+1);try{const event=JSON.parse(line);if(event.type==='agent_end'){clearTimeout(timer);resolve();}}catch{}}});
      rpc.stdin.write(JSON.stringify({id:'prompt',type:'prompt',message:(cliInitial?'Preserve this saved conversation.\n':'')+'Check the current skill catalogue.'})+'\n');
    });
    checkPrompt(expected,absent,start);
    cliInitial=false;
  } finally { rpc.kill(); await new Promise(resolve=>{if(rpc.exitCode!==null)resolve();else rpc.once('exit',resolve);}); }
  }
  if(librarySkills) {await cliSkills(['library-first','library-second']);console.log('PASS shipped Pi CLI: both library skills registered in a saved conversation');}


  const version = JSON.parse(await readFile(directory+'/package.json','utf8')).version;
  const sdkVersion = require(directory+'/dist/pi-sdk.cjs')['@earendil-works/pi-coding-agent']().VERSION;
  worker = fork(directory+'/dist/pi-sdk-worker.cjs',[],{cwd,env:process.env,silent:true,serialization:'advanced'});
  let errors = '';
  worker.stderr.on('data',chunk=>{errors=(errors+chunk).slice(-8000);});
  worker.stdout.resume();
  const pending = new Map();
  let sequence=0;
  worker.on('message',message=>{
    if(message.type==='sdk_bridge_response') {
      const entry=pending.get(message.requestId);
      if(entry){pending.delete(message.requestId);clearTimeout(entry.timer);message.error?entry.reject(new Error(JSON.stringify(message.error))):entry.resolve(message.result);}
    }
  });
  const ready = new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Pendant bootstrap timed out: '+errors)),60000);
    worker.on('message',message=>{
      if(message.type==='sdk_bridge_ready'){clearTimeout(timer);resolve();}
      if(message.type==='sdk_worker_failure'){clearTimeout(timer);reject(new Error(JSON.stringify(message)));}
    });
    worker.on('error',error=>{clearTimeout(timer);reject(error);});
  });
  worker.send({type:'sdk_worker_bootstrap',protocolVersion:3,hostVersion:version,workspaceId:'radiance-fixture',workspaceCwd:cwd,
    workspaceGeneration:1,runtimeOwnerGeneration:1,agentDirectory:agentDir,agentDirectorySource:'explicit',offline:false,extensionMode:'rpc'});
  worker.send({type:'sdk_bridge_hello',protocolVersion:3,hostKind:'vscode',hostVersion:version,workspaceId:'radiance-fixture',workspaceGeneration:1,
    piSdkPackage:'@earendil-works/pi-coding-agent',piSdkVersion:sdkVersion,capabilities:['sessions','models','settings','packages','resources','extension_ui']});
  await ready;
  function request(command,sessionKey) {
    return new Promise((resolve,reject)=>{
      const requestId='fixture-'+(++sequence);
      const timer=setTimeout(()=>{pending.delete(requestId);reject(new Error('Pendant request timed out: '+command.type+' '+errors));},60000);
      pending.set(requestId,{resolve,reject,timer});
      worker.send({type:'sdk_bridge_request',requestId,workspaceGeneration:1,...(sessionKey?{sessionKey,sessionGeneration:1}:{}),command});
    });
  }
  const models=await request({type:'models_list',cwd});
  assert(JSON.stringify(models).includes(id),'Pendant catalogue missing discovered model');
  async function pendantPrompt(key,expected,absent=[]) {
    await request({type:'session_set_model',provider,modelId:id,persist:false},key);
    const start=requests.length;
    await request({type:'session_prompt',text:'Check the current skill catalogue.',images:[],source:'interactive'},key);
    await request({type:'session_wait_for_idle'},key);
    checkPrompt(expected,absent,start);
  }
  const inventory=await request({type:'session_create',target:librarySkills?{kind:'file',path:sessionFile}:{kind:'new',cwd}},'fixture');
  if (librarySkills) {
  for(const name of ['library-first','library-second']) assert(inventory.commands.some(c=>c.name==='skill:'+name), 'Pendant missing '+name);
  console.log('PASS shipped Pendant worker: both library skills registered after library removal');
  }
  assert(inventory.commands.some(c=>c.name==='local-models'),'Local discovery extension did not load');
  await request({type:'session_set_model',provider,modelId:id,persist:false},'fixture');
  const state=await request({type:'session_get_state'},'fixture');
  assert.equal(state.model.id,id);
  if(librarySkills) await pendantPrompt('fixture',['library-first','library-second']);
  await request({type:'session_dispose',reason:'replacement'},'fixture');
  console.log('PASS shipped Pendant worker: fresh Radiance model discovery and selection');
  if(librarySkills) {
    await writeFile(agentDir+'/settings.json',JSON.stringify({extensions:['/opt/pi-local-model-manager/extensions/local-models.js'],skills:['/opt/extra-skills/library-second']}));
    await cliSkills(['library-second'],['library-first']);
    const resumed=await request({type:'session_create',target:{kind:'file',path:sessionFile}},'resumed');
    assert(resumed.commands.some(c=>c.name==='skill:library-second'));
    assert(!resumed.commands.some(c=>c.name==='skill:library-first'));
    const kept=await request({type:'session_get_state'},'resumed');
    assert(JSON.stringify(kept.messages).includes('Preserve this saved conversation.'), JSON.stringify(kept));
    await pendantPrompt('resumed',['library-second'],['library-first']);
    await request({type:'session_dispose',reason:'replacement'},'resumed');
    await writeFile(agentDir+'/settings.json',JSON.stringify({extensions:['/opt/pi-local-model-manager/extensions/local-models.js'],skills:['/opt/extra-skills/library-first','/opt/extra-skills/library-second']}));
    await cliSkills(['library-first','library-second']);
    const added=await request({type:'session_create',target:{kind:'file',path:sessionFile}},'added');
    for(const name of ['library-first','library-second'])assert(added.commands.some(c=>c.name==='skill:'+name));
    await pendantPrompt('added',['library-first','library-second']);
    await request({type:'session_dispose',reason:'replacement'},'added');
    console.log('PASS shipped Pi CLI and Pendant: saved conversation resumes with removed and added skills; outbound system prompts updated and messages preserved');
  }
} finally {
  if(worker){worker.kill();await new Promise(resolve=>{if(worker.exitCode!==null)resolve();else worker.once('exit',resolve);});}
  server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
}
