import { readFileSync } from 'node:fs';

const selection = '/opt/pi-security/config/sandbox-system-prompt.json';
const snapshot = '/opt/pi-security/config/sandbox-state.json';
const reference = '/opt/sandbox/context.md';
const entryType = 'factory-sandbox-context';

function read(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch (error) {
    if (error.code !== 'ENOENT') throw new Error(`Cannot read Factory context from ${path}: ${error.message}. Reopen the sandbox from Factory.`, { cause: error });
  }
}

export function sandboxContext(state) {
  if (state === undefined) return '';
  const invalid = reason => new Error(`Cannot read Factory settings from ${snapshot}: ${reason}. Reopen the sandbox from Factory.`);
  if (!['allowlist', 'unrestricted'].includes(state?.network))
    throw invalid(`network mode ${JSON.stringify(state?.network) ?? 'missing'} must be allowlist or unrestricted`);
  if (!state.values || typeof state.values !== 'object' || Array.isArray(state.values))
    throw invalid('values must be an object containing the named settings');
  if (typeof state.securityGuidance !== 'string' || !state.securityGuidance.trim())
    throw invalid('securityGuidance must contain the applied security instructions');
  return ['Current Factory sandbox settings. These replace older Factory settings in this conversation; project instructions still apply.',
    ...Object.entries(state.values).map(([name, value]) => `${name}: ${value}`),
    state.securityGuidance.trimEnd(),
    'Use only tools actually available in this session.'].join('\n');
}

export function sandboxUpdate(state, initial) {
  const old = initial.values;
  const fields = Object.entries(state.values).filter(([name, value]) => old[name] !== value)
    .map(([name, value]) => [name, String(value).replace(/\s+/g, ' ').trim()]);
  for (const name of Object.keys(old)) {
    if (!(name in state.values)) fields.push([name, 'no longer configured']);
  }
  if (!fields.length) {
    if (state.securityGuidance === initial.securityGuidance && state.guidanceRevision === initial.guidanceRevision) return '';
    fields.push(['Sandbox guidance', 'updated']);
  }
  for (const field of fields) {
    if (Buffer.byteLength(field[1], 'utf8') > 120) field[1] = 'updated';
  }
  const render = () => [
    'Sandbox update: current Factory settings.',
    'These replace older Factory settings in this conversation; project instructions still apply.',
    ...fields.map(([name, value]) => `- ${name}: ${value}`),
    `Full current configuration, guidance and examples: ${reference}.`,
  ].join('\n');
  for (const field of [...fields].sort((a, b) => Buffer.byteLength(b[1], 'utf8') - Buffer.byteLength(a[1], 'utf8'))) {
    if (Buffer.byteLength(render(), 'utf8') <= 1500) break;
    if (Buffer.byteLength(field[1], 'utf8') > 'updated'.length) field[1] = 'updated';
  }
  return render();
}

export default function systemPrompt(pi) {
  pi.on('before_agent_start', ({ systemPrompt }, ctx) => {
    const state = read(snapshot);
    // Validate current data even when a persisted initial context is available.
    sandboxContext(state);
    let initial = ctx.sessionManager.getBranch().find(entry =>
      entry.type === 'custom' && entry.customType === entryType)?.data;
    if (state && !initial) {
      initial = state;
      // Session metadata is branch-local and is not sent to the model. Keeping
      // the initial context makes reload, cancellation and A -> B -> A safe.
      pi.appendEntry(entryType, initial);
    }
    const text = [read(selection)?.text, sandboxContext(initial),
      state && initial ? sandboxUpdate(state, initial) : ''].filter(Boolean).join('\n\n');
    if (!text) return;
    const addendum = '\n\n' + text;
    if (!systemPrompt.includes(addendum)) return { systemPrompt: systemPrompt + addendum };
  });
}
