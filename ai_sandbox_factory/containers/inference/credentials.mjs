import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

export function localGatewayOptions(url, requestPath) {
  return { socketPath: '/run/egress/proxy.sock', path: url.origin + requestPath };
}

export function readCredentials(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return {}; throw error; }
}

export async function checkCredential(route, key, fetchImpl = fetch, requestSignal) {
  const auth = route?.auth;
  if (!['bearer', 'header'].includes(auth?.type) || typeof key !== 'string' || !/^[!-~]{1,16384}$/.test(key))
    throw Error('Unsupported credential check.');
  const operation = route.credentialCheck ??
    route.operations.find(op => op.method === 'GET' && op.path.endsWith('/models'));
  const endpoint = operation?.upstreamPath ?? operation?.path;
  if (!endpoint || !/^\/[A-Za-z0-9_./-]+$/.test(endpoint) ||
      endpoint.includes('//') || endpoint.includes('..'))
    throw Error('This provider has no credential check.');
  const url = new URL(route.origin);
  url.pathname = endpoint;
  for (const [name, values] of Object.entries(operation.query ?? {})) {
    url.searchParams.set(name, values[0]);
  }
  const headers = auth.type === 'bearer' ? { authorization: 'Bearer ' + key } : { [auth.header]: key };
  const timeout = AbortSignal.timeout(20000);
  const signal = requestSignal ? AbortSignal.any([timeout, requestSignal]) : timeout;
  const verdict = status => status === 401 || status === 403 ? 'rejected'
    : status >= 200 && status < 300 ? 'accepted' : 'unavailable';
  if (route.local) {
    return await new Promise((resolve, reject) => {
      const request = http.get({ ...localGatewayOptions(url, url.pathname + url.search),
        headers: { ...headers, host: url.host }, signal }, reply => {
        reply.destroy();
        resolve(verdict(reply.statusCode));
      });
      request.on('error', reject);
    });
  }
  const reply = await fetchImpl(url, { headers, redirect: 'manual', signal });
  try {
    return verdict(reply.status);
  } finally { await reply.body?.cancel().catch(() => {}); }
}

function configuredRoute(request) {
  const routes = JSON.parse(fs.readFileSync('/config/routes.json', 'utf8'));
  const route = Object.hasOwn(routes, request.id) ? routes[request.id] : undefined;
  if (!route || new URL(route.origin).origin !== request.origin || route.auth.type !== request.auth ||
      (route.auth.header ?? null) !== request.authHeader || (route.basePath ?? '') !== request.basePath)
    throw Error('The inference route changed.');
  return route;
}

export function updateCredentials(file, request) {
  if (!/^[a-z][a-z0-9-]{0,254}$/.test(request.id)) throw Error('Invalid connection name.');
  const values = readCredentials(file);
  if (request.remove) delete values[request.id];
  else {
    const origin = new URL(request.origin);
    if (!['https:', 'http:'].includes(origin.protocol) || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw Error('Invalid origin.');
    const current = values[request.id];
    const supplied = request.credentials || {};
    const fields = request.auth === 'sigv4' ? ['accessKeyId','secretAccessKey','sessionToken'] : ['key'];
    const retaining = fields.every(field => !supplied[field]);
    const entry = retaining && current?.origin === origin.origin ? {...current} : {origin:origin.origin};
    for (const field of fields) {
      const value = request.credentials?.[field];
      if (value !== undefined && !retaining) {
        if (typeof value !== 'string' || value.length > 16384 || /[\r\n\0]/.test(value)) throw Error('Invalid credential.');
        entry[field] = value;
      }
    }
    if (request.auth === 'sigv4') {
      delete entry.key;
      if (!entry.accessKeyId || !entry.secretAccessKey) throw Error('Enter complete credentials.');
    } else {
      delete entry.accessKeyId; delete entry.secretAccessKey; delete entry.sessionToken;
      if (!entry.key) throw Error('Enter complete credentials.');
    }
    if (Object.hasOwn(request, 'assignment')) {
      entry.scope = {basePath: request.basePath, auth: request.auth, header: request.authHeader};
      if (request.assignment) {
        const { ref, label } = request.assignment;
        if (!/^[0-9a-f]{32}$/.test(ref) || typeof label !== 'string' || !label)
          throw Error('Invalid saved credential assignment.');
        entry.assignment = { ref, label };
      } else {
        delete entry.assignment;
      }
    }
    values[request.id] = entry;
  }
  fs.mkdirSync(path.dirname(file), {recursive:true, mode:0o700});
  const temp = file + '.' + process.pid + '.tmp';
  try {
    const fd = fs.openSync(temp, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(values)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(temp, file);
    const directory = fs.openSync(path.dirname(file), 'r');
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
  } finally { fs.rmSync(temp, {force:true}); }
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  try {
    let input = '';
    for await (const part of process.stdin) {
      input += part;
      if (input.length > 65536) throw Error('Request too large.');
    }
    const request = JSON.parse(input);
    if (request.status) {
      const credentials = readCredentials(process.argv[2]);
      const assignments = Object.fromEntries(Object.entries(credentials)
        .filter(([, entry]) => entry.assignment).map(([id, entry]) => [id, entry.assignment]));
      console.log(JSON.stringify({configured:Object.keys(credentials), assignments}));
    } else if (request.check) {
      const status = await checkCredential(configuredRoute(request), request.credentials?.key).catch(() => 'unavailable');
      console.log(JSON.stringify({status}));
    } else {
      if (Object.hasOwn(request, 'assignment')) configuredRoute(request);
      updateCredentials(process.argv[2], request);
      console.log('{"ok":true}');
    }
  } catch { console.error('Could not update inference credentials.'); process.exitCode = 1; }
}
