import { constants, closeSync, fstatSync, openSync, opendirSync, readFileSync, readSync } from 'node:fs';
import { Type } from 'typebox';
import { managedChild } from './packages/pi-auto-review/src/managed-child.ts';

const agentRoot = '/home/node/.pi/agent';
const instructionFiles = new Set(['AGENTS.md', 'APPEND_SYSTEM.md']);
const resourceFolders = new Set(['skills', 'prompts', 'themes', 'extensions']);
const readableResources = new Set(['skills', 'prompts']);
const outputBytes = 50 * 1024;
// Linux O_PATH opens a handle for inspection without reading a device or file.
const openPath = 0o10000000;

function safeName(name) {
  return /^[a-z0-9_][a-z0-9._ -]*$/i.test(name) &&
    !/(?:^|[._ -])(?:auth|credentials?|keys?|passwords?|secrets?|tokens?)(?:$|[._ -])/i.test(name) &&
    !/\.(?:pem|key|env)(?:$|\.)/i.test(name);
}

function allowedPath(parts, operation) {
  if (!parts.every(safeName)) return false;
  if (!parts.length) return operation === 'list';
  if (operation === 'list') {
    return resourceFolders.has(parts[0]) && (parts.length === 1 || readableResources.has(parts[0]));
  }
  return parts.length === 1 ? instructionFiles.has(parts[0])
    : readableResources.has(parts[0]) && /\.(?:md|txt)$/i.test(parts.at(-1));
}

function fdPath(fd) {
  return '/proc/self/fd/' + fd;
}

function mountId(fd) {
  const value = readFileSync('/proc/self/fdinfo/' + fd, 'utf8').match(/^mnt_id:\s*(\d+)$/m)?.[1];
  if (!value) throw new Error('Could not verify the file mount.');
  return value;
}

// Walk using open directory handles. O_NOFOLLOW rejects links on every component.
// Comparing mount IDs also rejects nested bind mounts on the same filesystem.
function inspectPath(parts, operation, callback) {
  const directoryFlags = openPath | constants.O_DIRECTORY | constants.O_NOFOLLOW;
  let fd = openSync('/', directoryFlags);
  try {
    for (const component of agentRoot.slice(1).split('/')) {
      const next = openSync(fdPath(fd) + '/' + component, directoryFlags);
      closeSync(fd);
      fd = next;
    }
    const mount = mountId(fd);
    for (const [index, part] of parts.entries()) {
      const file = operation === 'read' && index === parts.length - 1;
      const next = openSync(fdPath(fd) + '/' + part, file
        ? openPath | constants.O_NOFOLLOW
        : directoryFlags);
      closeSync(fd);
      fd = next;
      if (mountId(fd) !== mount) throw new Error('Nested mounts are outside the approved folder.');
    }
    const info = fstatSync(fd, { bigint: true });
    if (operation === 'read' && (!info.isFile() || info.nlink !== 1n)) {
      throw new Error('Choose a regular file without other links.');
    }
    const identity = [mount, info.dev, info.ino, info.ctimeNs].join(':');
    return callback(fd, identity);
  } finally {
    closeSync(fd);
  }
}

function readText(fd) {
  const bytes = Buffer.alloc(outputBytes + 1);
  const readable = openSync(fdPath(fd), constants.O_RDONLY | constants.O_NONBLOCK);
  let length = 0;
  try {
    while (length < bytes.length) {
      const count = readSync(readable, bytes, length, bytes.length - length, null);
      if (!count) break;
      length += count;
    }
  } finally {
    closeSync(readable);
  }
  if (length > outputBytes) throw new Error('This file is too large to read in one request.');
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, length));
  if (text.includes('\0')) throw new Error('Choose a plain text file.');
  return text;
}

function listNames(fd, parts) {
  const directory = opendirSync(fdPath(fd));
  const names = [];
  let size = 0;
  try {
    for (let entry; (entry = directory.readSync());) {
      if (!entry.isFile() && !entry.isDirectory()) continue;
      if (!parts.length && !resourceFolders.has(entry.name) && !instructionFiles.has(entry.name)) continue;
      if (!safeName(entry.name)) continue;
      size += Buffer.byteLength(entry.name) + 1;
      if (size > outputBytes) throw new Error('This folder has too many entries for one request.');
      names.push(entry.name + (entry.isDirectory() ? '/' : ''));
    }
  } finally {
    directory.closeSync();
  }
  return names.sort().join('\n') || 'No eligible entries.';
}

const result = (text, isError = false) => ({ content: [{ type: 'text', text }], isError });

export function registerReviewedFileAccess(pi, approve) {
  pi.registerTool({
    name: 'request_file_access',
    label: 'Request file access',
    description: 'Ask to read one Pi instruction or skill/prompt text file, or list one Pi resource folder. Only safe resources under ~/.pi/agent are eligible. Use read and ls for ordinary workspace access. State how the request serves the user. Never use this tool for credentials, configuration, or session files.',
    parameters: Type.Object({
      operation: Type.Union([Type.Literal('read'), Type.Literal('list')]),
      // This tool owns its path checks and approval. Native file tools retain their existing rules.
      resource: Type.String({ description: 'Exact path under /home/node/.pi/agent or ~/.pi/agent.' }),
      justification: Type.String({ description: 'Why this exact file or listing is needed.' }),
    }, { additionalProperties: false }),
    async execute(id, { operation, resource, justification }, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      if (managedChild(sessionId)) return result('Ask the parent session to request this file access.', true);
      const expanded = resource.startsWith('~/') ? '/home/node/' + resource.slice(2) : resource;
      const absolute = operation === 'list' ? expanded.replace(/\/$/, '') : expanded;
      const parts = absolute === agentRoot ? [] : absolute.startsWith(agentRoot + '/')
        ? absolute.slice(agentRoot.length + 1).split('/') : null;
      if (!parts || !['read', 'list'].includes(operation) || !allowedPath(parts, operation)) {
        return result('Blocked by sandbox policy. This path is outside the reviewable Pi resources.', true);
      }
      if (!justification.trim()) return result('Explain why this file or folder is needed.', true);

      let identity;
      try {
        identity = inspectPath(parts, operation, (_fd, value) => value);
      } catch {
        return result('Blocked by sandbox policy. Choose an existing resource of the requested type without links.', true);
      }
      const action = operation === 'read' ? 'Read this file' : 'List this folder';
      const scope = operation === 'read'
        ? 'The file text will be visible to Pi and its model.' : 'Pi will receive only the names of eligible immediate entries.';
      const { reason } = await approve({
        ctx, signal, toolCallId: id, title: action + '?',
        details: ['Path\n' + absolute, 'Why Pi needs access\n' + justification, scope],
        requestFacts: {
          surface: 'filesystem-read',
          operation,
          toolName: 'request_file_access',
          path: absolute,
          resolvedPath: absolute,
          toolInputPreview: JSON.stringify({ action, scope, justification }),
          matchedPolicy: { decision: 'ask', rule: 'factory-reviewed-pi-resource' },
        },
      });
      if (reason || signal?.aborted) return result(reason || 'The request was cancelled.', true);
      try {
        return inspectPath(parts, operation, (fd, current) => current !== identity
          ? result('The file or folder changed during approval. Access was blocked.', true)
          : result(operation === 'read' ? readText(fd) : listNames(fd, parts)));
      } catch {
        return result('The resource changed or could not be read as requested. Access was blocked.', true);
      }
    },
  });
}
