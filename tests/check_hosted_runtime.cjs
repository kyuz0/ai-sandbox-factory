const assert = require('node:assert/strict');
const fs = require('node:fs');
const root='/usr/local/lib/node_modules/@earendil-works/pi-coding-agent';
const agent='/home/node/.pi/agent';
const read = p => JSON.parse(fs.readFileSync(p));
async function check() {
 const expected=read(agent+'/settings.json');
 const credentials=read(agent+'/auth.json');
 assert.equal(credentials.azure.env.AZURE_OPENAI_DEPLOYMENT_NAME_MAP,'gpt-4o=team-gpt');
 assert.equal(fs.statSync(agent+'/auth.json').mode & 0o777,0o600);
 assert(!fs.existsSync('/home/node/.aws/credentials'));
 assert(!fs.existsSync('/private/credentials.json'));
 assert(!JSON.stringify(credentials).includes('fixture-secret'));
 const core=await import(root+'/dist/core/model-runtime.js');
 const rt=await core.ModelRuntime.create({authPath:agent+'/auth.json',modelsPath:agent+'/models.json',modelsStorePath:agent+'/models-store.json',allowModelNetwork:false});
 for (const id of ['openrouter','together','azure','amazon-bedrock']) {
  const auth=await rt.getAuth(id);
  assert(auth,id+' native credentials resolve');
  assert.equal(auth.auth.apiKey,'factory-inference');
  if(id==='amazon-bedrock')assert.equal(auth.env.AWS_BEDROCK_SKIP_AUTH,'1');
 }
 const model=rt.getModel(expected.defaultProvider,expected.defaultModel);
 assert(model);assert(model.contextWindow>1000);assert(model.api);
 for(const id of ['openrouter','together','azure','amazon-bedrock']) {
  const selected=id==='azure'?rt.getModel(id,'gpt-4o'):id==='amazon-bedrock'?model:rt.getModels(id).find(m=>id==='openrouter'?m.api==='anthropic-messages':m.api==='openai-completions');
  assert(selected,id+' fixture model');
  const reply=await rt.completeSimple(selected,{messages:[{role:'user',content:'test',timestamp:Date.now()}]},{maxTokens:16,signal:AbortSignal.timeout(15000)});
  assert.notEqual(reply.stopReason,'error',JSON.stringify(reply));
  assert.equal(reply.content.filter(c=>c.type==='text').map(c=>c.text).join(''),'OK',id);
  console.log('PASS Pi '+id+': native streamed reply through isolated credentials');
 }
}
check().catch(e=>{console.error(e);process.exitCode=1});
