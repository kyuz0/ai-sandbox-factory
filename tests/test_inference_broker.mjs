import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {compileRoutes,createBroker} from '../pi_sandbox_factory/containers/inference/broker.mjs';
import {checkCredential,readCredentials,updateCredentials} from '../pi_sandbox_factory/containers/inference/credentials.mjs';

const listen = server => new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
const close = server => {server.closeAllConnections();return new Promise(resolve=>server.close(resolve));};
const secret = 'fixture-secret-that-must-stay-private';
const operation = {method:'POST',path:'/v1/chat/completions'};

test('HTTP model routes are limited to a matching local gateway port', () => {
  const route = { local:true, origin:'http://local-model.internal:8000', auth:{type:'bearer'}, operations:[operation] };
  assert.equal(compileRoutes({'local-8000':route}).size, 1);
  assert.throws(() => compileRoutes({'local-8001':route}), /local model route/);
  assert.throws(() => compileRoutes({'local-8000':{...route,origin:'http://192.0.2.80:8000'}}), /local model route/);
  assert.throws(() => compileRoutes({'local-8000':{...route,local:false}}), /Invalid upstream origin/);
});
async function setup(t, handler, options={}) {
  const received=[];
  const source=http.createServer((req,res)=>{let body='';req.on('data',b=>body+=b);req.on('end',()=>{received.push({path:req.url,headers:req.headers,body});handler(req,res,body);});});
  await listen(source);
  const route={origin:'http://127.0.0.1:'+source.address().port,auth:{type:'bearer'},operations:[operation]};
  const compiled=compileRoutes({sample:route,...options.routes},{allowHttpFixture:true});
  const server=createBroker(compiled,()=>options.secret||{key:secret,origin:route.origin},options);
  await listen(server);
  t.after(async()=>{await close(server);await close(source);});
  const base='http://127.0.0.1:'+server.address().port;
  return {server,source,received,base,send:async(body='{}',headers={})=>fetch(base+'/r/sample/v1/chat/completions',{method:'POST',body,headers})};
}

test('credential storage replaces atomically, reports names only, and removes connections',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'factory-keys-'));const file=path.join(dir,'credentials.json');
 try{
  updateCredentials(file,{id:'sample',origin:'https://provider.example',credentials:{key:secret}});
  assert.equal(fs.statSync(file).mode&0o777,0o600);assert.equal(readCredentials(file).sample.key,secret);
  assert.throws(()=>updateCredentials(file,{id:'../escape',credentials:{key:'x'}}));
  assert.throws(()=>updateCredentials(file,{id:'sample',credentials:{key:'x\r\ny'}}));
  assert.equal(readCredentials(file).sample.key,secret);
  updateCredentials(file,{id:'sample',origin:'https://provider.example',auth:'sigv4',credentials:{accessKeyId:'fixture-id',secretAccessKey:'fixture-secret'}});
  assert.equal(readCredentials(file).sample.key,undefined);
  updateCredentials(file,{id:'sample',remove:true});assert.deepEqual(readCredentials(file),{});
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('candidate verification uses its fixed route without following a redirect', async t => {
  let redirected = 0;
  const target = http.createServer((_req, res) => { redirected++; res.end(); });
  await listen(target);
  const seen = [];
  const upstream = http.createServer((req, res) => {
    seen.push([req.method, req.url, req.headers.authorization]);
    if (req.headers.authorization === 'Bearer redirect') {
      res.writeHead(302, { location: 'http://127.0.0.1:' + target.address().port }).end();
    } else {
      res.writeHead(req.headers.authorization === 'Bearer accepted' ? 200 : 401).end();
    }
  });
  await listen(upstream);
  t.after(async () => { await close(upstream); await close(target); });
  const route = {origin:'http://127.0.0.1:' + upstream.address().port, auth:{type:'bearer'},
    operations:[{method:'GET',path:'/api/v1/models',query:{model_type:['ALL_TEXT']}}]};
  assert.equal(await checkCredential(route, 'accepted'), 'accepted');
  assert.equal(await checkCredential(route, 'bad-key'), 'rejected');
  assert.equal(await checkCredential(route, 'redirect'), 'unavailable');
  assert(seen.every(([method, url]) => method === 'GET' && url === '/api/v1/models?model_type=ALL_TEXT'));
  assert.equal(redirected, 0);
});

test('the next request uses the new assignment and a different path cannot reuse it', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'factory-keys-'));
  t.after(() => fs.rmSync(dir, {recursive:true, force:true}));
  const file = path.join(dir, 'credentials.json');
  const seen = [];
  const upstream = http.createServer((req, res) => {
    seen.push(req.headers.authorization);
    req.resume();
    res.end('ok');
  });
  await listen(upstream);
  const origin = 'http://127.0.0.1:' + upstream.address().port;
  const route = {origin, basePath:'/v1', auth:{type:'bearer'}, operations:[operation]};
  const routes = compileRoutes({sample:route, other:{...route, basePath:'/other'}}, {allowHttpFixture:true});
  const proxy = createBroker(routes, () => readCredentials(file).sample);
  await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  const request = {id:'sample', origin, basePath:'/v1', auth:'bearer', authHeader:null};
  const send = id => fetch('http://127.0.0.1:' + proxy.address().port + '/r/' + id + '/v1/chat/completions',
    {method:'POST', body:'{}'});
  const work = {ref:'a'.repeat(32),label:'Work'};
  const test = {ref:'b'.repeat(32),label:'Test'};
  updateCredentials(file, {...request, credentials:{key:'work-key'}, assignment:work});
  await (await send('sample')).text();
  updateCredentials(file, {...request, credentials:{key:'test-key'}, assignment:test});
  await (await send('sample')).text();
  assert.deepEqual(readCredentials(file).sample.assignment, test);
  assert.equal((await send('other')).status, 503);
  assert.deepEqual(seen, ['Bearer work-key', 'Bearer test-key']);
});

test('route configuration requires a fixed HTTPS origin and declared authentication',()=>{
 const valid={origin:'https://provider.example',auth:{type:'bearer'},operations:[operation]};
 assert.equal(compileRoutes({sample:valid}).size,1);
 for(const origin of ['http://provider.example','https://user:pass@provider.example','https://provider.example/path','https://provider.example?next=evil'])assert.throws(()=>compileRoutes({sample:{...valid,origin}}));
 assert.throws(()=>compileRoutes({sample:{...valid,auth:{type:'header',header:'host'}}}));
});

test('fixed destination, replaced auth, and exact streamed bytes',async t=>{
 const body=Buffer.from([0,1,255,13,10,0,200,65]);
 const f=await setup(t,(_req,res)=>{res.writeHead(200,{'content-type':'application/vnd.amazon.eventstream','x-private':secret});res.write(body.subarray(0,3));setTimeout(()=>res.end(body.subarray(3)),20);});
 const response=await f.send('{}',{authorization:'Bearer attacker','api-key':'attacker','x-api-key':'attacker',cookie:'attacker',host:'attacker.invalid','x-amz-security-token':'attacker','x-forwarded-host':'attacker.invalid'});
 assert.deepEqual(Buffer.from(await response.arrayBuffer()),body);assert.equal(response.headers.get('x-private'),null);
 const headers=f.received[0].headers;assert.equal(headers.authorization,'Bearer '+secret);assert.equal(headers['accept-encoding'],'identity');
 for(const key of ['api-key','x-api-key','cookie','x-amz-security-token','x-forwarded-host'])assert.equal(headers[key],undefined);
 assert(!headers.host.includes('attacker'));
});

test('unlisted routes, queries, methods, traversal and raw absolute URLs never reach upstream',async t=>{
 const f=await setup(t,(_req,res)=>res.end('unexpected'));
 for(const [method,url]of [['GET','/r/sample/v1/chat/completions'],['POST','/r/other/v1/chat/completions'],['POST','/admin'],['POST','/r/sample/v1/chat/completions?url=https://attacker.invalid']]){
  const r=await fetch(f.base+url,{method});assert(r.status>=400);await r.text();
 }
 async function raw(target,extra=''){
  return new Promise((resolve,reject)=>{let output='';const s=net.connect(f.server.address().port,'127.0.0.1',()=>s.end(`POST ${target} HTTP/1.1\r\nHost: local\r\nContent-Length: 0\r\nConnection: close\r\n${extra}\r\n`));s.on('data',b=>output+=b);s.on('end',()=>resolve(output));s.on('error',reject);});
 }
 for(const url of ['https://attacker.invalid/r/sample/v1/chat/completions','//attacker.invalid','/r/sample/v1/%2e%2e/keys','/r/sample/v1/%252e%252e/keys','/r/sample/v1/./chat/completions','/r/sample/v1/chat%00/completions'])assert.match(await raw(url),/HTTP\/1.1 40[04]/);
 assert.match(await raw('/r/sample/v1/chat/completions','Transfer-Encoding: chunked\r\n'),/HTTP\/1.1 400/);
 assert.equal(f.received.length,0);
});

test('redirects are not followed and provider error bodies do not expose keys',async t=>{
 const f=await setup(t,(_req,res,body)=>{res.writeHead(body==='redirect'?302:401,{location:'http://attacker.invalid','retry-after':'9'});res.end(secret);});
 for(const [body,status]of [['redirect',502],['auth',401]]){
  const r=await f.send(body);assert.equal(r.status,status);assert.equal(r.headers.get('location'),null);assert(!(await r.text()).includes(secret));
 }
});

test('disconnect cancels an upstream stream',async t=>{
 let cancelled;
 const stopped=new Promise(r=>cancelled=r);
 const f=await setup(t,(_req,res)=>{res.writeHead(200,{'content-type':'text/event-stream'});res.write('data: first\n\n');res.on('close',cancelled);});
 const controller=new AbortController();const r=await fetch(f.base+'/r/sample/v1/chat/completions',{method:'POST',body:'{}',signal:controller.signal});await r.body.getReader().read();controller.abort();
 await Promise.race([stopped,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error('Upstream not cancelled')),1000);timer.unref();})]);
});

test('body limits and upstream timeouts return bounded failures',async t=>{
 const f=await setup(t,()=>{},{maxBody:4,idleMs:40,totalMs:300});
 let r=await f.send('12345');assert.equal(r.status,413);await r.text();assert.equal(f.received.length,0);
 r=await f.send('{}');assert.equal(r.status,504);await r.text();
});

test('encoded Bedrock profile path is preserved and re-signed with companion credentials',async t=>{
 let seen;
 const source=http.createServer((req,res)=>{seen={url:req.url,headers:req.headers};req.resume();req.on('end',()=>res.end('ok'));});await listen(source);
 const origin='http://127.0.0.1:'+source.address().port;
 const route={origin,auth:{type:'sigv4',region:'eu-west-1'},operations:[{method:'POST',path:'/model/{model}/converse-stream'}]};
 const server=createBroker(compileRoutes({bedrock:route},{allowHttpFixture:true}),()=>({origin,accessKeyId:'COMPANION_ID',secretAccessKey:secret,sessionToken:'companion-session'}));await listen(server);
 t.after(async()=>{await close(server);await close(source);});
 const model=encodeURIComponent('arn:aws:bedrock:eu-west-1:123456789012:application-inference-profile/fixture');
 const r=await fetch('http://127.0.0.1:'+server.address().port+'/r/bedrock/model/'+model+'/converse-stream',{method:'POST',body:'{}',headers:{authorization:'dummy-client-signature','x-amz-security-token':'dummy-client-session'}});
 assert.equal(await r.text(),'ok');assert.equal(seen.url,'/model/'+model+'/converse-stream');
 assert.match(seen.headers.authorization,/AWS4-HMAC-SHA256 Credential=COMPANION_ID\/.+\/eu-west-1\/bedrock\/aws4_request/);
 assert(!seen.headers.authorization.includes(secret));assert.equal(seen.headers['x-amz-security-token'],'companion-session');
});


test('changing an upstream cannot reuse a credential saved for another origin',async t=>{
 const f=await setup(t,(_req,res)=>res.end('unexpected'),{secret:{origin:'https://old-provider.example',key:secret}});
 const r=await f.send();assert.equal(r.status,503);await r.text();assert.equal(f.received.length,0);
});

test('chunked body limits are enforced while reading the stream',async t=>{
 const f=await setup(t,(_req,res)=>res.end('unexpected'),{maxBody:4});
 const status=await new Promise((resolve,reject)=>{
  const req=http.request(f.base+'/r/sample/v1/chat/completions',{method:'POST'},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));});
  req.on('error',reject);req.write('123');req.end('456');
 });
 assert.equal(status,413);assert.equal(f.received.length,0);
});

test('an explicitly configured total deadline stops a stream even while chunks keep arriving',async t=>{
 let closed;
 const stopped=new Promise(r=>closed=r);
 const f=await setup(t,(_req,res)=>{
  res.writeHead(200,{'content-type':'text/event-stream'});
  const interval=setInterval(()=>res.write('data: live\n\n'),5);
  res.on('close',()=>{clearInterval(interval);closed();});
 },{totalMs:45,idleMs:200});
 const response=await f.send();await assert.rejects(response.text());await stopped;
});

test('timed-out uploads release their shared buffer allowance',async t=>{
 const f=await setup(t,(_req,res)=>res.end('ok'),{totalMs:40,maxBuffered:32});
 for(let attempt=0;attempt<3;attempt++){
  await new Promise((resolve,reject)=>{
   const req=http.request(f.base+'/r/sample/v1/chat/completions',{method:'POST'},res=>{
    res.resume();res.on('end',()=>{req.end('late bytes');resolve();});
   });
   req.on('error',error=>{if(error.code!=='EPIPE'&&error.code!=='ECONNRESET')reject(error);});
   req.write('partial upload');
  });
 }
 const response=await f.send('normal');assert.equal(response.status,200);assert.equal(await response.text(),'ok');
});

test('encoded traversal in a Bedrock model slot is rejected before reaching upstream',async t=>{
 let count=0;
 const source=http.createServer((req,res)=>{count++;req.resume();res.end('unexpected');});await listen(source);
 const origin='http://127.0.0.1:'+source.address().port;
 const server=createBroker(compileRoutes({bedrock:{origin,auth:{type:'bearer'},operations:[{method:'POST',path:'/model/{model}/converse-stream'}]}},{allowHttpFixture:true}),()=>({origin,key:secret}));await listen(server);
 t.after(async()=>{await close(server);await close(source);});
 for(const model of ['..%2Fx','%2e%2e%2Fx','a%2F..%2Fb','%252e%252e%252Fx']){
  const status=await new Promise((resolve,reject)=>{
   const req=http.request({hostname:'127.0.0.1',port:server.address().port,path:'/r/bedrock/model/'+model+'/converse-stream',method:'POST'},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));});req.on('error',reject);req.end('{}');
  });
  assert.equal(status,400);
 }
 assert.equal(count,0);
});

test('native context-limit errors survive with credentials redacted and AWS error type preserved',async t=>{
 const f=await setup(t,(_req,res)=>{
  res.writeHead(400,{'content-type':'application/json','x-amzn-errortype':'ValidationException'});
  res.end(JSON.stringify({error:{code:'context_length_exceeded',message:'prompt is too long '+secret}}));
 });
 const r=await f.send();assert.equal(r.status,400);assert.equal(r.headers.get('x-amzn-errortype'),'ValidationException');
 const value=await r.json();assert.equal(value.error.code,'context_length_exceeded');
 assert(value.error.message.includes('prompt is too long'));assert(!value.error.message.includes(secret));
});

test('oversized upstream errors are withheld',async t=>{
 const f=await setup(t,(_req,res)=>{res.writeHead(400);res.end(JSON.stringify({message:'x'.repeat(70000)}));});
 const r=await f.send();assert.equal(r.status,400);assert((await r.text()).length<1000);
});

test('eighty live inference streams release uploaded bodies and complete independently', async t => {
 const streams=[];
 const f=await setup(t,(_req,res,body)=>{
  res.writeHead(200,{'content-type':'text/event-stream'});
  res.write('data: ready\n\n');
  streams.push({res,id:JSON.parse(body).id});
 });
 const responses=await Promise.all(Array.from({length:80},async(_,id)=>{
  const response=await f.send(JSON.stringify({id,padding:'x'.repeat(1024*1024)}));
  assert.equal(response.status,200,`stream ${id}`);
  return response;
 }));
 assert.equal(streams.length,80);
 for(const {res,id} of streams)res.end(`data: ${id}\n\n`);
 for(let id=0;id<80;id++)assert.equal(await responses[id].text(),`data: ready\n\ndata: ${id}\n\n`);
});

test('default inference stream survives thirty minutes of continuing progress', async t => {
 t.mock.timers.enable({apis:['setTimeout']});
 let upstream;
 const f=await setup(t,(_req,res)=>{upstream=res;res.writeHead(200);res.write('start');});
 const completed=new Promise((resolve,reject)=>{
  const req=http.request(f.base+'/r/sample/v1/chat/completions',{method:'POST'},res=>{
   let body='';res.on('data',chunk=>body+=chunk);res.on('error',reject);res.on('end',()=>resolve(body));
  });
  req.on('error',reject);req.end('{}');
 });
 // Install a handler immediately so an unexpected disconnect is captured.
 completed.catch(()=>{});
 while(!upstream)await new Promise(setImmediate);
 for(let minute=0;minute<32;minute++){
  upstream.write('.');
  await new Promise(setImmediate);
  t.mock.timers.tick(60000);
  await new Promise(setImmediate);
 }
 upstream.end('end');
 assert.equal(await completed,'start'+'.'.repeat(32)+'end');
});

test('aggregate upload pressure queues and cancelled uploads release space',async t=>{
 const f=await setup(t,(_req,res)=>res.end('ok'),{maxBuffered:4});
 let first;
 const waiting=new Promise(resolve=>{
  first=http.request(f.base+'/r/sample/v1/chat/completions',{method:'POST'},res=>res.resume());
  first.on('error',()=>{});first.write('123',resolve);
 });
 await waiting;
 // The first body stays incomplete while the second competes for buffer space.
 await new Promise(resolve=>setTimeout(resolve,20));
 try{
  let settled=false;
  const pending=f.send('456').then(response=>{settled=true;return response;});
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(settled,false);
  first.destroy();
  const response=await pending;
  assert.equal(response.status,200);assert.equal(await response.text(),'ok');
 }finally{first.destroy();}
});

test('cancelling a waiting upload does not consume the next request budget',async t=>{
 const f=await setup(t,(_req,res)=>res.end('ok'),{maxBuffered:4});
 const open=()=>{
  const req=http.request(f.base+'/r/sample/v1/chat/completions',
   {method:'POST',headers:{'content-length':'4'}},res=>res.resume());
  req.on('error',()=>{});req.flushHeaders();return req;
 };
 const first=open();
 await new Promise(resolve=>setTimeout(resolve,20));
 const queued=open();
 await new Promise(resolve=>setTimeout(resolve,20));
 queued.destroy();
 await new Promise(resolve=>setTimeout(resolve,20));
 try{
  let settled=false;
  const pending=f.send('1234').then(res=>{settled=true;return res;});
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(settled,false);
  first.destroy();
  const response=await pending;
  assert.equal(response.status,200);assert.equal(await response.text(),'ok');
  assert.equal(f.received.length,1);
 }finally{first.destroy();queued.destroy();}
});
