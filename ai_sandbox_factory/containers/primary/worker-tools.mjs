// Browser-only MCP server; native agents keep their own shell and file tools.
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';
import { createInterface } from 'node:readline';

const browser = {
  name: 'browser',
  description: 'Run Playwright code in the workspace browser and return its result and live viewer link. The page variable is the active Chromium tab.',
  inputSchema: {type: 'object', properties: {code: {type: 'string'}}, required: ['code'], additionalProperties: false}
};
const browserPath = '/opt/betterwright/browser.mjs';
const hasBrowser = await access(browserPath, constants.R_OK).then(() => true, () => false);
const tools = hasBrowser ? [browser] : [];
let browserSession;
const requests = new Map();

async function callBrowser(code) {
  if (typeof code !== 'string') throw new Error('code must be text');
  if (!browserSession) {
    const { SandboxBrowser } = await import(browserPath);
    browserSession = new SandboxBrowser();
  }
  const result = await browserSession.run(code);
  const text = JSON.stringify(result);
  if (!result.ok) return text;
  return text + '\nLive browser: ' + await browserSession.viewerLink();
}

const input = createInterface({input: process.stdin});
input.on('close', () => {
  for (const request of requests.values()) request.abort();
});
async function respond(message) {
    const request = new AbortController();
    requests.set(message.id, request);
    try {
    const reply = {jsonrpc: '2.0', id: message.id};
    const method = message.method;
    if (method === 'initialize') {
      reply.result = {protocolVersion: message.params.protocolVersion,
        capabilities: {tools: {}}, serverInfo: {name: 'factory-workspace', version: '1'}};
    } else if (method === 'tools/list') {
      reply.result = {tools};
    } else if (method === 'tools/call') {
      const {name, arguments: args = {}} = message.params;
      try {
        if (!tools.some(tool => tool.name === name)) throw new Error('Unknown workspace tool');
        const text = await callBrowser(args.code);
        reply.result = {content: [{type: 'text', text}]};
      } catch (error) {
        reply.result = {isError: true, content: [{type: 'text', text: error.message}]};
      }
    } else if (method === 'resources/list' || method === 'resources/templates/list' || method === 'prompts/list') {
      const property = {'resources/list': 'resources', 'resources/templates/list': 'resourceTemplates', 'prompts/list': 'prompts'}[method];
      reply.result = {[property]: []};
    } else if (method === 'ping') {
      reply.result = {};
    } else {
      reply.error = {code: -32601, message: 'Unknown method'};
    }
    process.stdout.write(JSON.stringify(reply) + '\n');
    } finally {
      requests.delete(message.id);
    }
}
const replies = new Set();
try {
  for await (const line of input) {
    let message;
    try { message = JSON.parse(line); } catch { continue; }
    if (message.method === 'notifications/cancelled') {
      requests.get(message.params?.requestId)?.abort();
      continue;
    }
    if (!Object.hasOwn(message, 'id') || requests.has(message.id)) continue;
    const pending = respond(message).catch(error => {
      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id,
        error: {code: -32602, message: error.message}}) + '\n');
    }).finally(() => replies.delete(pending));
    replies.add(pending);
  }
  await Promise.all(replies);
} finally {
  await browserSession?.close();
}
