// Test-only loopback surface for a disposable editor. The secret arrives on stdin.
import http from 'node:http';
import {readFileSync} from 'node:fs';

const [port, socketPath] = process.argv.slice(2);
const ticket = readFileSync(0, 'utf8');
let authority;
let origin;
const cookie = 'editor_test=' + ticket;

function authorized(request) {
  return request.headers.host === authority &&
    (!request.headers.origin || request.headers.origin === origin) &&
    (request.headers.cookie || '').split(';').some(value => value.trim() === cookie);
}

function upstream(request) {
  const headers = {...request.headers};
  delete headers.cookie;
  return http.request({socketPath, path: request.url, method: request.method, headers});
}

const server = http.createServer((request, response) => {
  if (!authorized(request)) {
    response.writeHead(403).end();
    return;
  }
  const target = upstream(request);
  target.on('error', () => response.writeHead(502).end());
  target.on('response', reply => {
    response.writeHead(reply.statusCode, reply.headers);
    reply.pipe(response);
  });
  request.pipe(target);
});

server.on('upgrade', (request, client, head) => {
  if (!authorized(request)) return client.destroy();
  const target = upstream(request);
  target.on('error', () => client.destroy());
  target.on('response', () => client.destroy());
  target.on('upgrade', (reply, remote, rest) => {
    const headers = Object.entries(reply.headers).map(([key, value]) => key + ': ' + value);
    client.write(['HTTP/1.1 101 Switching Protocols', ...headers, '', ''].join('\r\n'));
    if (head.length) remote.write(head);
    if (rest.length) client.write(rest);
    client.on('error', () => remote.destroy());
    remote.on('error', () => client.destroy());
    remote.pipe(client).pipe(remote);
  });
  target.end();
});
server.listen(Number(port), '127.0.0.1', () => {
  authority = '127.0.0.1:' + server.address().port;
  origin = 'http://' + authority;
  console.log(server.address().port);
});
