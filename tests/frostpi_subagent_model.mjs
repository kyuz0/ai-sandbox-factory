// Synthetic model replies for delegated work in the disposable Factory sandbox.
const seed = '/workspace/subagents-seed.txt';
const native = '/workspace/subagents-inline.txt';
const protectedFile = '/workspace/subagents-protected.env';
const boundary = '/home/node/.pi/agent/subagents-boundary-fixture.txt';
const acceptance = { level: 'none', reason: 'The fixture independently checks the tool results and workspace.' };
const parentPrompt = /^Factory subagents (disabled|inline|context|background|parallel|specialist|nested|cancel|manual|off)$/m;
const childPrompt = /^(?:Task:\s*)?FACTORY_CHILD_(INLINE|CONTEXT|ALPHA|BETA|READ_ALPHA|READ_BETA|SPECIALIST|NESTED|LEAF|CANCEL|MANUAL|OFF)\b/m;
const denied = /deni|declin|reject|block|not approved|cancel|refus/i;
const asyncId = /Async: assistant \[([^\]]+)\]/;

function content(message) {
  return typeof message?.content === 'string' ? message.content
    : (message?.content ?? []).map(part => part.text ?? '').join('\n');
}

function reviewReply(messages) {
  const system = messages.find(message => message.role === 'system' || message.role === 'developer');
  if (!/Review one proposed coding-agent action|You are a fail-closed permission reviewer/.test(content(system))) return;
  // Reviewer conversations can contain earlier requests. Route only the current action.
  let action;
  try {
    action = JSON.parse(content(messages.at(-1)));
  } catch {
    return;
  }
  const tag = action.request?.command?.match(/\bFACTORY_REVIEW_(ALLOW|DENY|CONTEXT|ALPHA|BETA|CANCEL|MANUAL)\b/)?.[1];
  if (!tag) return;
  const outcome = tag === 'ALLOW' ? 'allow' : ['DENY', 'CONTEXT'].includes(tag) ? 'deny' : 'defer';
  if (tag === 'CONTEXT') {
    const evidence = action.evidence?.parentOriginalMessages?.messages ?? [];
    const includes = (marker) => evidence.some(entry => entry.role === 'user' && entry.content?.includes(marker));
    console.log('SUBAGENT REVIEW CONTEXT SOURCE root=' + includes('Factory subagents context')
      + ' delegated=' + includes('FACTORY_CHILD_CONTEXT'));
  }
  console.log('SUBAGENT REVIEW', tag, outcome);
  return { content: JSON.stringify({ outcome, risk_level: 'medium', user_authorization: 'high',
    rationale: 'Synthetic decision for the ' + tag + ' workspace marker.' }) };
}

function delegate(task, background = false, agent = 'assistant') {
  return ['subagent', { agent, task: 'FACTORY_CHILD_' + task, async: background, options: { acceptance } }];
}

function escalation(tag) {
  const marker = 'FACTORY_REVIEW_' + tag;
  return ['bash_escalate', {
    command: 'printf %s ' + marker + ' > /workspace/subagents-' + tag.toLowerCase() + '.txt && printf %s ' + marker,
    justification: 'Exercise the decision for the ' + tag + ' synthetic workspace file.',
  }];
}

function scriptForChild(role) {
  switch (role) {
    case 'INLINE':
      return [
        ['read', { path: seed }, /FACTORY_SEED/],
        ['write', { path: native, content: 'FACTORY_NATIVE_WRITE' }],
        ['edit', { path: native, edits: [{ oldText: 'FACTORY_NATIVE_WRITE', newText: 'FACTORY_NATIVE_EDIT' }] }],
        ['bash', { command: 'cat ' + native }, /^FACTORY_NATIVE_EDIT\s*$/m],
        ['read', { path: protectedFile }, denied],
        ['write', { path: protectedFile, content: 'FACTORY_PROTECTED_MUTATION' }, denied],
        [...escalation('ALLOW'), /^FACTORY_REVIEW_ALLOW\s*$/m],
        [...escalation('DENY'), denied],
      ];
    case 'READ_ALPHA':
    case 'READ_BETA':
      return [['read', { path: seed }, /FACTORY_SEED/],
        ['fixture_echo', { text: 'FACTORY_AMBIENT_OK' }, /^FACTORY_AMBIENT_OK\s*$/m]];
    case 'ALPHA':
      return [
        ['fixture_echo', { text: 'FACTORY_AMBIENT_OK' }, /^FACTORY_AMBIENT_OK\s*$/m],
        [...escalation(role), /^FACTORY_REVIEW_ALPHA\s*$/m],
      ];
    case 'BETA':
    case 'CANCEL':
    case 'CONTEXT':
      return [[...escalation(role), denied]];
    case 'SPECIALIST':
    case 'LEAF':
      return [['read', { path: seed }, /FACTORY_SEED/]];
    case 'NESTED':
      return [[...delegate('LEAF'), /FACTORY CHILD LEAF OK/]];
    case 'MANUAL':
      return [[...escalation(role), /^FACTORY_REVIEW_MANUAL\s*$/m]];
    case 'OFF':
      return [['bash', { command: 'test ! -r ' + boundary +
        ' && printf FACTORY_OFF > /workspace/subagents-off.txt && cat /workspace/subagents-off.txt' }, /^FACTORY_OFF\s*$/m]];
  }
}

function scriptForParent(role) {
  switch (role) {
    case 'DISABLED':
      return [];
    case 'BACKGROUND':
      return [[...delegate('ALPHA', true), asyncId], [...delegate('BETA', true), asyncId]];
    case 'PARALLEL':
      return [[...delegate('READ_ALPHA', true), asyncId], [...delegate('READ_BETA', true), asyncId]];
    case 'CANCEL':
      return [[...delegate('CANCEL', true), asyncId]];
    case 'SPECIALIST':
      return [[...delegate(role, false, 'fixture-reader'), /FACTORY CHILD SPECIALIST OK/]];
    case 'INLINE':
    case 'CONTEXT':
    case 'NESTED':
    case 'MANUAL':
    case 'OFF':
      return [[...delegate(role), new RegExp('FACTORY CHILD ' + role + ' OK')]];
  }
}

export function subagentReply({ messages, tools, choice }) {
  if (!tools.length) return reviewReply(messages);
  let role, child, start;
  for (let index = messages.length - 1; index >= 0; index--) {
    if (messages[index].role !== 'user') continue;
    const prompt = content(messages[index]);
    const task = prompt.match(childPrompt);
    const parent = prompt.match(parentPrompt);
    if (task || parent) {
      child = Boolean(task);
      role = (task ?? parent)[1].toUpperCase();
      start = index;
      break;
    }
    // A later user prompt in a reused chat belongs to a different request.
    if (index === messages.length - 1) return;
  }
  if (!role) return;

  const actor = child ? 'CHILD' : 'PARENT';
  const prefix = 'factory-sub-' + actor.toLowerCase() + '-' + role.toLowerCase() + '-';
  const steps = child ? scriptForChild(role) : scriptForParent(role);
  const replies = messages.slice(start + 1).filter(message => message.role === 'tool'
    && message.tool_call_id?.startsWith(prefix));
  function failure(message) {
    console.log('SUBAGENT', actor, role, 'FAIL', message);
    return { content: 'FACTORY SUBAGENT FIXTURE FAILED ' + actor + ' ' + role + ' ' + message };
  }
  if (!child && role === 'DISABLED' && tools.some(name => ['subagent', 'subagents_enable', 'bg_wait'].includes(name))) {
    return failure('unexpected delegation tools');
  }
  if (child && role === 'SPECIALIST' && tools.some(name => ['write', 'edit', 'bash', 'bash_escalate'].includes(name))) {
    return failure('unexpected specialist tools');
  }
  if (child && role === 'OFF' && tools.includes('bash_escalate')) return failure('unexpected escalation tool');
  if (!replies.length) {
    const visible = ['read', 'write', 'edit', 'bash', 'bash_escalate', 'subagent', 'ask_user_question', 'schedule_prompt', 'fixture_echo']
      .filter(name => tools.includes(name));
    console.log('SUBAGENT', actor, role, 'START', visible.join(','));
  }
  for (const [index, reply] of replies.entries()) {
    const step = steps[index];
    if (!step || reply.tool_call_id !== prefix + index) return failure('unexpected tool result ' + index);
    if (step[2] && !step[2].test(content(reply))) {
      if (child && ['OFF', 'INLINE'].includes(role) && step[0] === 'bash') {
        console.log('SUBAGENT FIXTURE BASH RESULT', JSON.stringify(content(reply).slice(0, 350)));
      }
      return failure('unexpected result for ' + step[0] + ' step ' + index);
    }
  }
  if (replies.length < steps.length) {
    const [tool, args] = steps[replies.length];
    if (!tools.includes(tool)) return failure('missing tool ' + tool);
    console.log('SUBAGENT', actor, role, 'CALL', replies.length, tool);
    const delta = choice(tool, args);
    delta.tool_calls[0].id = prefix + replies.length;
    return delta;
  }
  if (!child && ['BACKGROUND', 'PARALLEL'].includes(role)) {
    const ids = replies.map(reply => content(reply).match(asyncId)[1]);
    if (ids[0] === ids[1]) return failure('duplicate background run ID');
  }
  const status = !child && ['BACKGROUND', 'PARALLEL', 'CANCEL'].includes(role) ? 'STARTED' : 'OK';
  console.log('SUBAGENT', actor, role, status);
  return { content: 'FACTORY ' + (child ? 'CHILD' : 'SUBAGENTS') + ' ' + role + ' ' + status };
}
