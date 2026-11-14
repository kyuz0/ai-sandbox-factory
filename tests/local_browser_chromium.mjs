import assert from 'node:assert/strict';
import http from 'node:http';
import {WebSocketServer} from 'ws';
import {SandboxBrowser} from '/opt/betterwright/browser.mjs';
const listen=(s,p,h='127.0.0.1')=>new Promise((r,j)=>{s.once('error',j);s.listen(p,h,r);});
let protectedHits=0,upstreamHits=[];
const protectedServer=http.createServer((req,res)=>{protectedHits++;req.resume();res.end('private fixture');});
await listen(protectedServer,19431);
const app=http.createServer((req,res)=>{res.setHeader('content-type','text/html');res.end(req.url==='/popup'?'<title>Popup works</title>':`<title>Local dev works</title><button onclick="window.open('/popup')">Open popup</button><script>fetch('http://127.0.0.1:19431/r/fixture/v1/chat/completions',{method:'POST',mode:'no-cors',body:'{"model":"fixture","messages":[]}'}).catch(()=>{});window.ws=new WebSocket('ws://localhost:8765/ws');ws.onmessage=e=>document.body.dataset.ws=e.data;</script>`);});
const wss=new WebSocketServer({server:app});wss.on('connection',s=>s.send('live-reload-works'));
await listen(app,8765);
const upstream=http.createServer((req,res)=>{upstreamHits.push(req.url);res.writeHead(req.url.startsWith('http://allowed.example/')?200:403,{'content-type':'text/html'});res.end('<title>External fixture</title>');});
await listen(upstream,3128);
const ipv6=http.createServer((_req,res)=>res.end('<title>IPv6 app</title>'));
await listen(ipv6,8766,'::1');
const browser=new SandboxBrowser();
try {
 for(const host of ['localhost','127.0.0.1','app.localhost']) {
  const result=await browser.run(`const p=await page;await p.goto('http://${host}:8765/');await p.waitForFunction(()=>document.body.dataset.ws==='live-reload-works');return {title:await p.title(),ws:await p.locator('body').getAttribute('data-ws')};`);
  assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.result.title,'Local dev works');assert.equal(result.result.ws,'live-reload-works');
 }
 const popup=await browser.run("const p=await page;const popup=p.waitForEvent('popup');await p.getByRole('button',{name:'Open popup'}).click();const child=await popup;await child.waitForLoadState();return await child.title();");
 assert.equal(popup.ok,true,JSON.stringify(popup));assert.equal(popup.result,'Popup works');
 for(const host of ['localhost','[::1]']) {const r=await browser.run(`const p=await page;await p.goto('http://${host}:8766/');return await p.title();`);assert.equal(r.result,'IPv6 app',JSON.stringify(r));}
 const external=await browser.run("const p=await page;const r=await p.goto('http://allowed.example/');return r.status();");
 assert.equal(external.result,200,JSON.stringify(external));
 const denied=await browser.run("const p=await page;const r=await p.goto('http://denied.example/');return r.status();");
 assert.equal(denied.result,403,JSON.stringify(denied));
 assert.equal(protectedHits,0);assert(upstreamHits.some(u=>u.startsWith('http://allowed.example/')));
 console.log('PASS managed Chromium: localhost, 127.0.0.1, *.localhost, IPv6, WebSockets, popup, external proxy allow/deny, private-port block');
} finally {await browser.close();wss.close();for(const s of [app,upstream,protectedServer,ipv6]){s.closeAllConnections();await new Promise(r=>s.close(r));}}
