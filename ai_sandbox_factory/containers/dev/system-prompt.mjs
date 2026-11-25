import { readFileSync } from 'node:fs';

const selection = '/opt/pi-security/config/sandbox-system-prompt.json';
const snapshot = '/opt/pi-security/config/sandbox-state.json';

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

export default function systemPrompt(pi) {
  pi.on('before_agent_start', ({ systemPrompt }) => {
    const text = [read(selection)?.text, sandboxContext(read(snapshot))].filter(Boolean).join('\n\n');
    if (!text) return;
    const addendum = '\n\n' + text;
    if (!systemPrompt.includes(addendum)) return { systemPrompt: systemPrompt + addendum };
  });
}
