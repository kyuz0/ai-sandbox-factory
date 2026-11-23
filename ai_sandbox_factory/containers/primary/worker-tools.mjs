// Standard-input MCP server. The workspace has no model account credentials.
import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { access, mkdtemp, open, readFile, rm } from 'node:fs/promises';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';

const shell = {
  name: 'workspace_shell',
  description: 'Run a shell command inside the workspace container. Use this tool to inspect, edit and test files.',
  inputSchema: {type: 'object', properties: {command: {type: 'string'}}, required: ['command'], additionalProperties: false}
};
const browser = {
  name: 'browser',
  description: 'Run Playwright code in the workspace browser and return its result and live viewer link. The page variable is the active Chromium tab.',
  inputSchema: {type: 'object', properties: {code: {type: 'string'}}, required: ['code'], additionalProperties: false}
};
const browserPath = '/opt/betterwright/browser.mjs';
const hasBrowser = await access(browserPath, constants.R_OK).then(() => true, () => false);
const tools = [ ...(process.env.FACTORY_PRIMARY_AGENT !== 'codex' ? [shell] : []), ...(hasBrowser ? [browser] : []) ];
let browserSession;
const running = new Set();
const requests = new Map();

function stopCommand(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  try { process.kill(-child.pid, 'SIGKILL'); } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
}

async function workspaceShell(command, signal) {
  if (typeof command !== 'string') throw new Error('command must be text');
  const cwd = JSON.parse(await readFile('/run/primary-runtime/cwd.json', 'utf-8'));
  const directory = await mkdtemp(path.join(tmpdir(), 'factory-agent-'));
  const log = path.join(directory, 'output');
  const file = await open(log, 'w+', 0o600);
  let keepLog = false;
  let child;
  const cancel = () => stopCommand(child);
  try {
    signal.throwIfAborted();
    child = spawn('/bin/bash', ['-c', command], {cwd, detached: true, stdio: ['ignore', file.fd, file.fd]});
    running.add(child);
    signal.addEventListener('abort', cancel, {once: true});
    const [exit, exitSignal] = await once(child, 'close');
    // The model receives a bounded preview and can request more through the
    // workspace tools. Direct file output avoids buffering large command logs.
    const preview = Buffer.alloc(32768);
    const {bytesRead} = await file.read(preview, 0, preview.length, 0);
    keepLog = (await file.stat()).size > bytesRead;
    return `${preview.subarray(0, bytesRead).toString()}\n${exitSignal ? `Signal ${exitSignal}` : `Exit ${exit}`}` +
      (keepLog ? `\nFull output is in ${log}` : '');
  } finally {
    signal.removeEventListener('abort', cancel);
    running.delete(child);
    await file.close();
    if (!keepLog) await rm(directory, {recursive: true, force: true});
  }
}

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
  for (const child of running) stopCommand(child);
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
        const text = name === shell.name ? await workspaceShell(args.command, request.signal) : await callBrowser(args.code);
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
