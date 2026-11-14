// Run only inside a disposable Factory container; synthetic inference, no accounts.
import assert from 'node:assert/strict';
import http from 'node:http';
import {spawn,execFileSync} from 'node:child_process';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
const agent='/home/node/.pi/agent';
await mkdir('/var/tmp/pi',{recursive:true,mode:0o700});
await mkdir('/pi-config',{recursive:true});
await mkdir('/workspace',{recursive:true});
execFileSync('python3',['/usr/local/lib/sandbox-bootstrap.py']);
const gateway=http.createServer((req,res)=>{res.writeHead(req.url==='http://fixture.test/'?200:403);res.end('PREAPPROVED_OK');});
await new Promise(r=>gateway.listen(3128,'127.0.0.1',r));
process.env.HTTP_PROXY=process.env.HTTPS_PROXY='http://127.0.0.1:3128';
process.env.NO_PROXY='127.0.0.1,localhost';
const appSource=`from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(b'''<title>Local app</title><button onclick="window.open(\'/popup\')">Popup</button>''')\n def do_POST(self):\n  body=self.rfile.read(int(self.headers.get('Content-Length',0))); self.send_response(200); self.end_headers(); self.wfile.write(body)\nHTTPServer(('127.0.0.1',8765),H).serve_forever()\n`;
const cases=[
 {tool:'write',args:{path:'/tmp/native-local-test.txt',content:'NATIVE_TEMP_OK'},contains:'Successfully'},
 {tool:'bash',args:{command:'printf SHELL_TEMP_OK > /tmp/local-test.txt; cat /tmp/local-test.txt'},contains:'SHELL_TEMP_OK'},
 {tool:'bash',args:{command:'curl -fsS http://fixture.test/'},contains:'PREAPPROVED_OK'},
 {tool:'write',args:{path:'/workspace/app.py',content:appSource},contains:'Successfully'},
 {tool:'bash_escalate',args:{command:'nohup python3 /workspace/app.py >/tmp/local-server.log 2>&1 </dev/null & server=$!; for attempt in {1..100}; do curl --noproxy "*" -fsS http://127.0.0.1:8765 >/dev/null 2>&1 && exit 0; kill -0 "$server" || exit 1; sleep 0.1; done; exit 1' ,justification:'Start the user-requested local application for browser testing inside this container.'}},
 {tool:'browser',args:{code:"const p=await page; await p.goto('http://localhost:8765'); const value=await p.evaluate(async()=>await (await fetch('/api',{method:'POST',body:'API_OK'})).text()); const popup=p.waitForEvent('popup'); await p.getByRole('button',{name:'Popup'}).click(); const child=await popup; await child.waitForLoadState(); return {title:await child.title(),api:value};"},contains:'API_OK'},
 {tool:'bash',args:{command:'cat /tmp/local-test.txt'},contains:'SHELL_TEMP_OK'},
];
if(process.argv.includes('--goals')) {
 cases.unshift({tool:'create_goal',args:{objective:'Test the local application',auto_continue:false,skip_auditor:true,verification_contract:'The browser returns API_OK.'}});
 cases.push({tool:'get_goal',args:{}},
  {tool:'update_goal',args:{token_budget:null,status:'complete'}});
}
let reviews=0,requests=0;
const inference=http.createServer((req,res)=>{let data='';req.on('data',c=>data+=c);req.on('end',()=>{
 try {
 const body=JSON.parse(data),messages=body.messages;
 const reviewing=JSON.stringify(messages[0]).includes('approval') && !body.tools?.some(t=>t.function?.name==='browser');
 let delta,finish='stop';
 if(reviewing){reviews++;delta={content:JSON.stringify({outcome:'allow',risk_level:'low',user_authorization:'high',rationale:'Authorized local development server.'})};}
 else {
 requests++;
 assert(JSON.stringify(messages).includes('http://localhost:PORT'),'Missing generated localhost guidance');
 const count=messages.filter(m=>m.role==='tool').length;
 if(count<cases.length){const c=cases[count];delta={role:'assistant',tool_calls:[{index:0,id:'local-'+count,type:'function',function:{name:c.tool,arguments:JSON.stringify(c.args)}}]};finish='tool_calls';}
 else delta={content:'LOCAL_WORKFLOW_DONE'};
 }
 res.writeHead(200,{'content-type':'text/event-stream'});
 for(const choice of [{delta,finish_reason:null},{delta:{},finish_reason:finish}])res.write('data: '+JSON.stringify({id:'local',object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,...choice}]})+'\n\n');
 res.end('data: [DONE]\n\n');
 }catch(e){res.writeHead(500);res.end(String(e));console.error(e);}
 });});
await new Promise(r=>inference.listen(0,'127.0.0.1',r));
await writeFile(agent+'/models.json',JSON.stringify({providers:{fixture:{baseUrl:'http://127.0.0.1:'+inference.address().port+'/v1',api:'openai-completions',apiKey:'fixture-only',models:[{id:'fixture',name:'fixture',reasoning:false,input:['text'],contextWindow:131072,maxTokens:8192,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}}]}}}));
const client=process.argv[2]||'pi';
let child;
try {
 child=spawn(client==='pi'?'pi':'node',client==='pi'?['--mode','rpc','--provider','fixture','--model','fixture','--no-session']:['/fixture/session_worker_bridge.cjs','fixture','fixture'],{cwd:'/workspace',env:process.env,stdio:['pipe','pipe','pipe']});
 let errors='',buffer='';const events=[];
 child.stderr.on('data',c=>errors+=c);
 await new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>reject(Error('Workflow exceeded 180s: '+errors+' '+JSON.stringify(events.slice(-3)))),180000);
 child.on('exit',code=>{clearTimeout(timer);reject(Error('Client exited '+code+': '+errors));});
 child.stdout.on('data',c=>{buffer+=c;let n;while((n=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,n);buffer=buffer.slice(n+1);let e;try{e=JSON.parse(line);}catch{continue;}events.push(e);if(e.type==='agent_settled'){clearTimeout(timer);resolve();}}});
 child.stdin.write(JSON.stringify({type:'prompt',id:'local',message:'Build and test the synthetic local application, including a popup and API request. Start its server through bash_escalate. Use container /tmp for scratch.'+(process.argv.includes('--goals')?' Create a goal without a token budget; complete it after verification.':'')})+'\n');
 });
 const results=events.filter(e=>e.type==='tool_execution_end');
 assert.equal(results.length,cases.length,JSON.stringify(events)+' '+errors);
 for(const [i,c] of cases.entries()){assert(!results[i].isError,JSON.stringify(results[i],(key,value)=>key==='data'?'[image omitted]':value));assert(!results[i].result?.isError,JSON.stringify(results[i],(key,value)=>key==='data'?'[image omitted]':value));if(c.contains)assert(JSON.stringify(results[i],(key,value)=>key==='data'?'[image omitted]':value).includes(c.contains),JSON.stringify(results[i],(key,value)=>key==='data'?'[image omitted]':value));}
 assert(!events.some(e=>e.type==='extension_ui_request'&&['select','confirm','input'].includes(e.method)),'Permission prompt');
 assert.equal(reviews,1,'Only server start needs review; temp files and browser localhost do not');
 if(process.argv.includes('--goals')) {
  assert.equal(results[0].result.details.goal.status,'active');
  assert.equal(results[0].result.details.goal.tokenBudget,undefined);
  assert.equal(results.at(-1).result.details.goal.status,'complete');
  console.log('PASS native goal create/read/update: no review, unlimited creation, explicit budget removal and completion');
 }
 assert.equal(requests,cases.length+1);
 console.log('PASS '+client+': generated guidance, native /tmp write, ordinary Bash shared /tmp, reviewed server start, browser localhost API + popup, preapproved network request, no network reviews or permission dialogs');
} finally {child?.kill();inference.closeAllConnections();inference.close();gateway.closeAllConnections();gateway.close();}
