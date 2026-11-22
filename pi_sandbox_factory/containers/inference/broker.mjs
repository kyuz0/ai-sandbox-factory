import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import {pipeline} from 'node:stream';
import {randomUUID} from 'node:crypto';
import {SignatureV4} from '@smithy/signature-v4';
import {Sha256} from '@aws-crypto/sha256-js';
import {checkCredential, localGatewayOptions, readCredentials} from './credentials.mjs';

const requestHeaders = ['content-type', 'accept', 'anthropic-version', 'anthropic-beta'];
const responseHeaders = ['content-type', 'content-encoding', 'retry-after'];
// Provider IDs also serve as directory names in the Linux container.
const idSyntax = '[a-z][a-z0-9-]{0,254}';
const idPattern = new RegExp(`^${idSyntax}$`);
const checkPath = new RegExp(`^/check/(${idSyntax})/([a-f0-9]{64})$`);
const routePath = new RegExp(`^/r/(${idSyntax})(/[^?]*)(?:\\?(.*))?$`);
const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Diagnostics must retain the failure, never the authentication used to send it.
const credentialFields = ['key','accessKeyId','secretAccessKey','sessionToken'];
function redact(text, values) {
  const variants = values.filter(value => typeof value === 'string' && value).flatMap(value =>
    [value, JSON.stringify(value).slice(1,-1), encodeURIComponent(value)]);
  for (const value of [...new Set(variants)].sort((a,b) => b.length-a.length))
    text = text.split(value).join('[redacted]');
  return text;
}
function redactDetails(value, secrets) {
  if (typeof value === 'string') return redact(value,secrets);
  if (Array.isArray(value)) return value.map(item=>redactDetails(item,secrets));
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.entries(value).map(([key,item])=>[key,redactDetails(item,secrets)]));
  return value;
}
function errorDetails(error, seen=new Set()) {
  if (!error || typeof error !== 'object') return {message:String(error)};
  if (seen.has(error)) return {message:'[circular error cause]'};
  seen.add(error);
  const detail = {};
  for (const key of ['name','message','code','errno','syscall','address','port','stack'])
    if (['string','number'].includes(typeof error[key])) detail[key] = error[key];
  if (error.cause !== undefined) detail.cause = errorDetails(error.cause,seen);
  if (Array.isArray(error.errors)) detail.errors = error.errors.map(item => errorDetails(item,seen));
  return detail;
}

export function compileRoutes(values, {allowHttpFixture=false}={}) {
  const routes = new Map();
  for (const [id, value] of Object.entries(values)) {
    if (!idPattern.test(id)) throw Error('Invalid route ID.');
    const origin = new URL(value.origin);
    const local = value.local === true;
    if (local && (origin.protocol !== 'http:' || origin.hostname !== 'local-model.internal' ||
        id !== 'local-' + (origin.port || '80') || value.auth.type !== 'bearer')) throw Error('Invalid local model route.');
    if ((origin.protocol !== 'https:' && !local && !(allowHttpFixture && origin.protocol === 'http:')) ||
        origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw Error('Invalid upstream origin.');
    if (!['bearer', 'header', 'sigv4'].includes(value.auth.type)) throw Error('Invalid authentication type.');
    if (value.auth.type === 'header' && !['api-key', 'x-api-key'].includes(value.auth.header)) throw Error('Invalid authentication header.');
    if (value.auth.type === 'sigv4' && !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(value.auth.region)) throw Error('Invalid AWS region.');
    if (value.credentialCheckId !== undefined && (local || value.auth.type === 'sigv4' ||
        !/^[a-f0-9]{64}$/.test(value.credentialCheckId) ||
        !/^\/[A-Za-z0-9_./-]+$/.test(value.credentialCheck?.path ?? '') ||
        value.credentialCheck.path.includes('..') || value.credentialCheck.path.includes('//') ||
        Object.keys(value.credentialCheck).some(key => !['path','method'].includes(key)) ||
        (value.credentialCheck.method !== undefined && value.credentialCheck.method !== 'GET')))
      throw Error('Invalid credential check.');
    const operations = value.operations.map(op => {
      if (!['GET','POST'].includes(op.method) || !op.path.startsWith('/') || op.path.includes('..')) throw Error('Invalid route operation.');
      if (op.upstreamPath && (!op.upstreamPath.startsWith('/') || /[?%\\{}]/.test(op.upstreamPath) || op.upstreamPath.includes('..') || op.upstreamPath.includes('//'))) throw Error('Invalid upstream path.');
      const pattern = op.path.split('{model}').map(escape).join('(?:[A-Za-z0-9._-]|%[a-fA-F0-9]{2})+');
      for (const [key, values] of Object.entries(op.query || {})) {
        if (!/^[a-z][a-z0-9_-]*$/.test(key) || !Array.isArray(values) || values.some(v => typeof v !== 'string')) throw Error('Invalid query rule.');
      }
      return {...op, pattern:new RegExp('^'+pattern+'$')};
    });
    routes.set(id, {...value, origin, operations});
  }
  return routes;
}

function matchesCredentialScope(route, secret) {
  return !secret.scope || (
    secret.scope.basePath === (route.basePath ?? '') && secret.scope.auth === route.auth.type &&
    secret.scope.header === (route.auth.header ?? null)
  );
}

export function createBroker(routes, credentials, {idleMs=0, totalMs=0, maxBody=32*1024*1024, maxBuffered=64*1024*1024, concurrency=Infinity,
  logError=record=>console.error(JSON.stringify(record))}={}) {
  let active = 0, reserved = 0;
  const uploads = [];
  function admitUploads() {
    while (uploads.length && reserved + uploads[0].bytes <= maxBuffered) {
      const next = uploads.shift();
      next.granted = true;
      reserved += next.bytes;
      next.resolve(true);
    }
  }
  const server = http.createServer({maxHeaderSize:16384}, async (req,res) => {
    const requestId = randomUUID();
    res.setHeader('x-factory-request-id',requestId);
    const secrets = [req.headers.authorization, req.headers.authorization?.replace(/^Bearer /i,'')];
    let routeId, reported = false;
    const report = (stage,status,error,extra={}) => {
      if (reported) return;
      reported = true;
      const record = redactDetails({timestamp:new Date().toISOString(),
        request_id:requestId,route:routeId,method:req.method,stage,status,
        ...(error === undefined ? {} : {error:errorDetails(error)}),...extra},secrets);
      logError(record);
      return record;
    };
    function fail(res, status, detail, error, stage='proxy') {
      const message = detail || ({401:'Provider authentication failed. Replace the credential in Factory.',
        403:'The provider refused access to this model or operation.',429:'The provider rate or usage limit was reached.',
        413:`Inference request exceeds this proxy's ${maxBody} byte upload limit.`,404:'Inference route is not available.',
        400:'Invalid inference proxy request.',503:'Inference connection is unavailable. Check its configuration in Factory.'})[status] || 'Inference proxy failure.';
      const diagnostic = report(stage,status,error,{message});
      if (res.destroyed || res.writableEnded) return;
      if (res.headersSent) { res.destroy(); return; }
      const cause = diagnostic?.error;
      const safeMessage = redact(cause ? `${message} ${cause.code ? '['+cause.code+'] ' : ''}${cause.message}` : message,secrets);
      res.writeHead(status, {'content-type':'application/json'});
      res.end(JSON.stringify({error:{type:'upstream_error',message:safeMessage,status,request_id:requestId,
        ...(cause ? {details:cause} : {})}}));
    }
    let upstream, deadline, admission, body, held = 0, counted = false, finished = false;
    const releaseBody = () => {
      body = undefined;
      if (!admission) return;
      if (admission.granted) reserved -= admission.bytes;
      else {
        const index = uploads.indexOf(admission);
        if (index !== -1) uploads.splice(index, 1);
        admission.resolve(false);
      }
      admission = undefined;
      admitUploads();
    };
    const cleanup = () => {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      if (upstream && !upstream.destroyed) upstream.destroy();
      if (counted) active--;
      releaseBody();
    };
    res.on('close', cleanup);
    try {
      if (active >= concurrency) {
        res.setHeader('retry-after','1');
        return fail(res,503,'Inference service is busy. Retry shortly.');
      }
      if (!req.url.startsWith('/') || req.url.startsWith('//') || /[\\\x00-\x20#]/.test(req.url) || req.headers.upgrade) return fail(res,400);
      const check = checkPath.exec(req.url);
      if (check) {
        if (req.method !== 'GET' || req.headers['transfer-encoding'] || Number(req.headers['content-length'] ?? 0) !== 0)
          return fail(res,400);
        const route = routes.get(check[1]);
        if (!route || route.credentialCheckId !== check[2]) return fail(res,404);
        const secret = credentials(check[1]) || {};
        routeId = check[1];
        for (const field of credentialFields) if (secret[field]) secrets.push(secret[field]);
        const controller = new AbortController();
        res.once('close', () => controller.abort());
        active++; counted = true;
        const status = secret.key && secret.origin === route.origin.origin && matchesCredentialScope(route, secret)
          ? await checkCredential(route, secret.key, fetch, controller.signal).catch(error => {
            report('credential_check',503,error);return 'unavailable';
          })
          : 'not_configured';
        if (finished) return;
        // Never forward provider headers, bodies, URLs, or exception messages.
        res.writeHead(200, {'content-type':'application/json', 'cache-control':'no-store'});
        return res.end(JSON.stringify({status}));
      }
      const match = routePath.exec(req.url);
      if (!match) return fail(res,404);
      const route = routes.get(match[1]);
      if (!route) return fail(res,404);
      routeId = match[1];
      const rawPath = match[2];
      let decoded;
      try { decoded = decodeURIComponent(rawPath); } catch { return fail(res,400); }
      if (decoded.includes('%') || decoded.includes('\\') || decoded.includes('//') ||
          decoded.split('/').some(s => s === '..' || s === '.') || /[\x00-\x20\x7f]/.test(decoded)) return fail(res,400);
      const op = route.operations.find(op => op.method === req.method && op.pattern.test(rawPath));
      if (!op) return fail(res,404);
      const upstreamPath = op.upstreamPath || rawPath;
      const query = new URLSearchParams(match[3] || '');
      for (const [key,value] of query) {
        if (!op.query?.[key]?.includes(value) || query.getAll(key).length !== 1) return fail(res,400);
      }
      const secret = credentials(match[1]) || {};
      for (const field of credentialFields) if (secret[field]) secrets.push(secret[field]);
      if ((!route.local || secret.key) && secret.origin !== route.origin.origin) return fail(res,503);
      if (!matchesCredentialScope(route, secret)) return fail(res,503);
      if (!route.local && (route.auth.type === 'sigv4' ? !(secret.accessKeyId && secret.secretAccessKey) : !secret.key)) return fail(res,503);
      if (Number(req.headers['content-length']) > maxBody) return fail(res,413);
      active++; counted = true;
      if (totalMs > 0) deadline = setTimeout(() => {
        fail(res,504,`Inference request exceeded its configured ${totalMs} ms total deadline.`,undefined,'total_timeout'); cleanup();
      },totalMs);
      // Reserve upload space before reading. Waiting sockets apply backpressure;
      // a burst must not turn valid requests into misleading size errors.
      const declared = Number(req.headers['content-length']);
      const bytes = Math.min(Number.isFinite(declared) ? declared : req.headers['transfer-encoding'] ? maxBody : 0, maxBuffered);
      const admitted = await new Promise(resolve => {
        admission = {bytes, resolve, granted:false};
        uploads.push(admission);
        admitUploads();
      });
      if (!admitted || finished) return;
      const chunks = [];
      for await (const chunk of req) {
        if (finished) return;
        if (held + chunk.length > maxBody) return fail(res,413);
        if (held + chunk.length > admission.bytes) {
          res.setHeader('retry-after','1');
          return fail(res,503,'Inference upload buffer is busy. Retry shortly.');
        }
        held += chunk.length; chunks.push(chunk);
      }
      if (finished) return;
      body = Buffer.concat(chunks);
      chunks.length = 0;
      let headers = {'accept-encoding':'identity', 'content-length':String(body.length)};
      for (const key of requestHeaders) if (req.headers[key]) headers[key] = req.headers[key];
      const url = route.origin;
      headers.host = url.host;
      if (route.auth.type === 'sigv4') {
        const signer = new SignatureV4({credentials:secret, region:route.auth.region, service:'bedrock', sha256:Sha256});
        const signed = await signer.sign({protocol:url.protocol,hostname:url.hostname,port:url.port ? Number(url.port) : undefined,
          method:req.method,path:upstreamPath,query:Object.fromEntries(query),headers,body});
        headers = signed.headers;
      } else if (route.auth.type === 'header') headers[route.auth.header] = secret.key;
      else if (secret.key) headers.authorization = 'Bearer '+secret.key;
      else if (route.local && req.headers.authorization && req.headers.authorization !== 'Bearer not-needed') {
        // A manually configured Pi key still works on its fixed local port.
        headers.authorization = req.headers.authorization;
      }
      if (finished) return;
      const upstreamRequestPath = upstreamPath+(match[3] ? '?'+match[3] : '');
      const destination = route.local ? localGatewayOptions(url, upstreamRequestPath)
        : {protocol:url.protocol,hostname:url.hostname,port:url.port,path:upstreamRequestPath};
      upstream = (url.protocol === 'https:' ? https : http).request({
        ...destination, method:req.method, headers, timeout:idleMs}, async response => {
        if (response.statusCode < 200 || response.statusCode >= 300) {
          if (response.headers['retry-after']) res.setHeader('retry-after',response.headers['retry-after']);
          const status=response.statusCode;
          // Preserve all upstream statuses and error text, including non-JSON errors.
          // A bounded, explicitly marked prefix prevents an unbounded error upload.
          const limit = 65536;
          const lookahead = Math.max(0,...secrets.filter(Boolean).map(value=>Buffer.byteLength(encodeURIComponent(value))));
          const chunks=[]; let size=0, truncated=false, readError;
          try {
            for await(const chunk of response){
              const remaining=limit+lookahead-size;
              chunks.push(chunk.subarray(0,Math.max(0,remaining))); size+=chunk.length;
              if(size>limit+lookahead){truncated=true;response.destroy();break;}
            }
          } catch(error) { readError=error; }
          const rawText=Buffer.concat(chunks).toString('utf8');
          let text=redact(rawText,secrets);
          truncated ||= size>limit;
          if(truncated) text=text.slice(0,limit);
          let safe;
          try {
            if(truncated||readError)throw Error();
            safe=JSON.stringify(redactDetails(JSON.parse(rawText),secrets));
          }
          catch {safe=JSON.stringify({error:{type:'upstream_error',message:text || response.statusMessage,
            status,request_id:requestId,truncated,...(readError?{details:redactDetails(errorDetails(readError),secrets)}:{})}});}
          report('upstream_http',status,readError,{body:JSON.parse(safe),truncated});
          const errorType=response.headers['x-amzn-errortype'];
          const headers={'content-type':'application/json'};
          if(typeof errorType==='string'&&/^[A-Za-z0-9_.:#-]{1,256}$/.test(errorType)&&redact(errorType,secrets)===errorType)headers['x-amzn-errortype']=errorType;
          if(!finished){res.writeHead(status,headers);res.end(safe);}
          return;
        }
        const safe = {};
        for (const key of responseHeaders) if (response.headers[key]) safe[key] = response.headers[key];
        res.writeHead(response.statusCode,safe); res.flushHeaders();
        response.once('error',error=>report('upstream_stream',response.statusCode,error));
        pipeline(response,res,error=>{if(error&&!reported)report(finished?'client_disconnect':'upstream_stream',response.statusCode,error);});
      });
      upstream.on('socket',socket=>socket.setKeepAlive(true,30000));
      upstream.on('timeout',()=>{ fail(res,504,`Upstream connection exceeded its configured ${idleMs} ms idle timeout.`,undefined,'idle_timeout'); cleanup(); });
      upstream.on('error',error=>{if(!finished)fail(res,502,'Upstream connection failed.',error,'upstream_connection');});
      upstream.end(body, releaseBody);
    } catch(error) { fail(res,502,'Inference proxy failed while processing the request.',error,'proxy'); }
  });
  server.headersTimeout = 15000;
  server.requestTimeout = 0;
  server.timeout = 0;
  server.on('connect', (_req,socket) => socket.end('HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n'));
  server.on('upgrade', (_req,socket) => socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'));
  return server;
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  try {
    const [routesFile,secretsFile,socket] = process.argv.slice(2);
    const routes = compileRoutes(JSON.parse(fs.readFileSync(routesFile,'utf8')));
    const server = createBroker(routes,id => readCredentials(secretsFile)[id]);
    fs.rmSync(socket,{force:true});
    server.listen(socket,()=>fs.chmodSync(socket,0o666));
    server.on('error',error=>{ console.error(JSON.stringify({timestamp:new Date().toISOString(),stage:'startup',error:errorDetails(error)})); process.exit(1); });
    for (const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>{server.close();server.closeAllConnections();});
  } catch(error) { console.error(JSON.stringify({timestamp:new Date().toISOString(),stage:'configuration',error:errorDetails(error)})); process.exitCode = 1; }
}
