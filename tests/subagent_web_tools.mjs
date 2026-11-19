// Loopback search and browser fixtures for foreground, nested and parallel assistants.
import http from 'node:http';

const parallelWorkers = new Set(['READ_ALPHA', 'READ_BETA']);
const workers = new Set([...parallelWorkers, 'NESTED', 'LEAF']);
const waiting = new Map();

// A response can finish only while both workers have an outstanding request.
function whenBothArrive(operation, worker, respond) {
  if (!parallelWorkers.has(worker)) {
    respond();
    return;
  }
  if (!waiting.has(operation)) waiting.set(operation, new Map());
  const pending = waiting.get(operation);
  if (pending.has(worker)) throw new Error('Duplicate ' + operation + ' request from ' + worker);
  pending.set(worker, respond);
  if (pending.size === parallelWorkers.size) {
    console.log('WEB WORKERS OVERLAP', operation);
    waiting.delete(operation);
    for (const finish of pending.values()) finish();
  }
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost:19382');
  const worker = url.searchParams.get('q') ?? url.searchParams.get('worker');
  if (!workers.has(worker)) {
    res.writeHead(404).end();
    return;
  }
  const send = (type, body) => res.writeHead(200, { 'content-type': type }).end(body);
  if (url.pathname === '/search') {
    whenBothArrive('search', worker, () => send('application/json', JSON.stringify({ results: [{
      title: 'SEARCH_RESULT_' + worker, url: 'https://example.org/' + worker, content: 'Fixture result',
    }] })));
  } else if (url.pathname === '/page') {
    send('text/html', '<!doctype html><title>PAGE_' + worker + '</title><h1>' + worker + '</h1>');
  } else if (url.pathname === '/ready') {
    whenBothArrive('browser', worker, () => send('text/plain', 'ready'));
  } else {
    res.writeHead(404).end();
  }
}).listen(19382, '127.0.0.1');

export function webWorkerSteps(role) {
  return [
    ['web_search', { query: role, numResults: 1 }, new RegExp('SEARCH_RESULT_' + role)],
    ['web_search', { query: role, provider: 'exa' }, /disabled provider/i],
    ['browser', { code: `const p = await page;
      await p.goto('http://127.0.0.1:19382/page?worker=${role}');
      await p.evaluate(() => localStorage.setItem('web-worker-marker', '${role}'));
      return await p.title();` }, new RegExp('PAGE_' + role)],
    ['browser', { code: `const p = await page;
      const marker = await p.evaluate(async () => {
        const reply = await fetch('/ready?worker=${role}');
        if (!reply.ok) throw new Error('Fixture request failed');
        return [document.title, localStorage.getItem('web-worker-marker')].join('/');
      });
      const tabs = await pages;
      return marker + '/TABS=' + tabs.length;` }, new RegExp('PAGE_' + role + '/' + role + '/TABS=1\\b')],
  ];
}
