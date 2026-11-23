// Synthetic inference for the disposable FrostPi editor regression.
import http from 'node:http';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { subagentReply } from './frostpi_subagent_model.mjs';

const suffix = 'FINAL_APPROVAL_ARGUMENT';
const command = "# Write a marker to the test workspace.\n" +
  "printf '%s' '" + "command detail ".repeat(65) + suffix + "' > /workspace/approval-marker.txt";
const choice = (name, args) => ({
  tool_calls: [{ index: 0, id: 'factory-' + name + '-' + Date.now(), type: 'function',
    function: { name, arguments: JSON.stringify(args) } }],
});
const writeChunk = (response, delta, finishReason = null, usage) => {
  response.write('data: ' + JSON.stringify({ id: 'factory', model: 'fixture',
    object: 'chat.completion.chunk', created: 1, choices: [{ index: 0, delta, finish_reason: finishReason }], usage }) + '\n\n');
};

function sendReply(response, delta) {
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  writeChunk(response, { role: 'assistant', ...delta });
  writeChunk(response, {}, delta.tool_calls ? 'tool_calls' : 'stop');
  response.end('data: [DONE]\n\n');
}

async function streamReasoningAndAnswer(response) {
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  writeChunk(response, { role: 'assistant' });
  for (const field of ['reasoning_content', 'content']) {
    const text = field === 'reasoning_content' ? 'Planning how to stream this fixture. ' : 'FACTORY STREAMING ANSWER. ';
    for (let index = 0; index < 12; index++) {
      writeChunk(response, { [field]: text });
      await new Promise(resolve => setTimeout(resolve, 350));
    }
  }
  writeChunk(response, {}, 'stop');
  response.end('data: [DONE]\n\n');
}

async function streamOutputBurst(response) {
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  await new Promise(resolve => setTimeout(resolve, 2_400));
  writeChunk(response, { role: 'assistant' });
  writeChunk(response, { reasoning_content: 'abc '.repeat(200) });
  await new Promise(resolve => setTimeout(resolve, 30));
  writeChunk(response, { content: 'def '.repeat(220) });
  await new Promise(resolve => setTimeout(resolve, 850));
  writeChunk(response, {}, 'stop', { prompt_tokens: 40, completion_tokens: 360, total_tokens: 400 });
  response.end('data: [DONE]\n\n');
}

function researchReply(messages, tools) {
  const prefix = 'factory-research-';
  const checked = messages.filter(message => message.role === 'tool' && message.tool_call_id?.startsWith(prefix));
  const system = JSON.stringify(messages.filter(message => message.role === 'system'));
  const output = '/workspace/research/factory-fixture/';
  const web = 'Example research source (https://example.org/research-fixture) gives MAPLE_WEB_EVIDENCE_185.';
  const local = 'Local source (/mnt/reference/fixture.md) gives CEDAR_LOCAL_EVIDENCE_746.';
  const steps = [
    ['read', { path: '/opt/research-skills/deep-research/SKILL.md' }, /Write `report\.md`/],
    ['subagent', { agent: 'assistant', name: 'Maple', async: false,
      task: 'Find FACTORY_RESEARCH_WEB_EVIDENCE with web_search and return the source URL.' },
      /https:\/\/example\.org\/research-fixture.*MAPLE_WEB_EVIDENCE_185/],
    ['read', { path: '/mnt/reference/fixture.md' }, /CEDAR_LOCAL_EVIDENCE_746/],
    ['write', { path: output + 'sources.md', content: '# Sources\n\n- ' + web + '\n- ' + local + '\n' }, /wrote/i],
    ['write', { path: output + 'report.md', content: '# Research report\n\n' + web + '\n\n' + local + '\n' }, /wrote/i],
  ];
  if (!system.includes('deep-research/SKILL.md')) {
    return { content: 'FACTORY RESEARCH FAILED skill absent from system prompt' };
  }
  for (const [index, result] of checked.entries()) {
    if (!steps[index][2].test(String(result.content))) {
      return { content: 'FACTORY RESEARCH FAILED ' + steps[index][0] + ' returned ' + String(result.content).slice(0, 400) };
    }
  }
  if (checked.length === steps.length) return { content: 'FACTORY RESEARCH REPORT SAVED' };
  const [tool, args] = steps[checked.length];
  if (!tools.includes(tool)) return { content: 'FACTORY RESEARCH FAILED missing ' + tool };
  const delta = choice(tool, args);
  delta.tool_calls[0].id = prefix + checked.length;
  return delta;
}

function knowledgeReply(messages, tools) {
  if (!tools.length) return; // Session title generation has no agent tools.
  const textOf = message => typeof message.content === 'string' ? message.content
    : message.content?.map(part => part.text ?? '').join('') ?? '';
  const user = messages.findLast(message => message.role === 'user');
  if (!user) return;
  const text = textOf(user);
  if (!/(?:Index the files|Refresh the index).*collection (frostpi|pi)-docs/.test(text)) return;
  const [, client] = text.match(/collection (frostpi|pi)-docs/);
  const name = client + '-docs';
  const refresh = text.startsWith('Refresh');
  const prefix = 'factory-knowledge-workflow-' + name + (refresh ? '-refresh-' : '-index-');
  const results = messages.filter(message => message.role === 'tool' && message.tool_call_id?.startsWith(prefix));
  const steps = refresh ? [
    ['knowledge_update', {target: name}, /(?:Updated|Refreshed)/i],
    ['knowledge_search', {query: 'ORCHID_REFRESH_' + client.toUpperCase() + '_842', kb_id: name, mode: 'fast'},
      new RegExp('ORCHID_REFRESH_' + client.toUpperCase() + '_842')],
  ] : [
    ['knowledge_add', {source: '/mnt/reference', name}, /2 files/],
    ['knowledge_search', {query: 'DOCX_SOURCE_842', kb_id: name, mode: 'semantic'}, /DOCX_SOURCE_842/],
    ['knowledge_search', {query: 'PDF_SOURCE_842', kb_id: name, mode: 'semantic'}, /PDF_SOURCE_842/],
    ['bash', {command: 'cd /tmp && pdfinfo /mnt/reference/maintenance.pdf >/tmp/factory-pdf-info.txt && pdftotext -layout /mnt/reference/maintenance.pdf factory-doc-verify.txt && grep PDF_SOURCE_842 factory-doc-verify.txt | head -3'}, /PDF_SOURCE_842/],
    ['bash', {command: 'pdftotext -layout /mnt/reference/maintenance.pdf /mnt/reference/denied.txt'}, /Denied by policy|Read-only file system|Permission denied|Couldn.t open text file/i],
  ];
  for (const [index, result] of results.entries()) {
    if (!steps[index]?.[2].test(String(result.content)))
      return {content: 'FACTORY KNOWLEDGE WORKFLOW FAILED ' + steps[index]?.[0] + ': ' + String(result.content).slice(0, 800)};
  }
  if (tools.some(name => ['knowledge_configure', 'knowledge_export'].includes(name)))
    return {content: 'FACTORY KNOWLEDGE WORKFLOW FAILED unexpected administrative tools'};
  if (results.length === steps.length)
    return {content: 'FACTORY KNOWLEDGE ' + client.toUpperCase() + (refresh ? ' REFRESH' : ' INDEX') + ' VERIFIED'};
  const [tool, args] = steps[results.length];
  if (!tools.includes(tool)) return {content: 'FACTORY KNOWLEDGE WORKFLOW FAILED missing ' + tool};
  const delta = choice(tool, args);
  delta.tool_calls[0].id = prefix + results.length;
  return delta;
}

http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => body += chunk);
  request.on('end', async () => {
    const input = JSON.parse(body);
    if (request.url === '/v1/embeddings') {
      const texts = input.input;
      if (request.method !== 'POST' || request.headers.authorization !== 'Bearer not-needed' ||
          input.model !== 'fixture-embedding' || !Array.isArray(texts) || !texts.length) {
        response.writeHead(400).end('Invalid fixture embedding request');
        return;
      }
      // Deterministic vectors exercise the upstream API protocol and persisted index.
      for (const text of texts) console.log('EMBEDDING', text.includes('ORCHID_WINDOW_842') ? 'passage' : 'query');
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({
        data: texts.map((_, index) => ({ index, embedding: [1, 0, 0] })),
      }));
      return;
    }
    const messages = input.messages;
    const last = messages.at(-1);
    const userText = last.role === 'user'
      ? (typeof last.content === 'string' ? last.content : last.content?.map(part => part.text ?? '').join('') ?? '')
      : '';
    const context = JSON.stringify(messages);
    const tools = input.tools?.map(tool => tool.function.name) ?? [];
    const messageImages = message => Array.isArray(message?.content)
      ? message.content.filter(part => part.type === 'image_url').map(part => part.image_url.url) : [];
    // A provider that cannot embed images in tool messages receives a follow-up user image.
    const toolResult = last.role === 'tool' ? last : messageImages(last).length ? messages.at(-2) : undefined;
    const delegatedReply = knowledgeReply(messages, tools) ?? subagentReply({ messages, tools, choice });
    if (delegatedReply) {
      sendReply(response, delegatedReply);
      return;
    }
    let delta;
    if (!tools.length && context.includes('Generate a concise title for a coding-agent chat session')) {
      console.log('TITLE REQUEST');
      await new Promise(resolve => setTimeout(resolve, 1200));
      if (userText.includes('Investigate the failing checkout request')) {
        response.writeHead(400, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: { message: 'Fixture cannot generate a title' } }));
        return;
      }
      delta = { content: userText.includes('Read the Factory image fixture.') ? 'Inspect tool images' : 'Fix sign-in errors' };
    } else if (userText === 'Factory profile prompt fixture') {
      const { text } = JSON.parse(readFileSync('/opt/pi-security/config/profile-system-prompt.json', 'utf8'));
      const system = messages.filter(message => message.role === 'system')
        .flatMap(message => typeof message.content === 'string' ? [message.content] : message.content.map(part => part.text))
        .join('\n');
      const markers = [text, 'FACTORY USER CONTEXT MARKER', 'FACTORY PROJECT CONTEXT MARKER', 'FACTORY PROJECT APPEND MARKER'];
      const count = markers.map(marker => marker ? system.split(marker).length - 1 : 0);
      delta = { content: count.every(value => value === 1)
        ? 'FACTORY PROFILE PROMPT VERIFIED' : 'FACTORY PROFILE PROMPT COUNTS ' + count.join(',') };
    } else if (userText.startsWith('Factory sandbox system prompt fixture ')) {
      const label = userText.split(' ').at(-1);
      const system = messages[0].role === 'system' ? JSON.stringify(messages[0].content) : '';
      const markers = [...system.matchAll(/FACTORY_SANDBOX_PROMPT_(FIRST|SECOND)/g)].map(match => match[1]);
      const inHistory = /FACTORY_SANDBOX_PROMPT_(FIRST|SECOND)/.test(JSON.stringify(messages.slice(1)));
      const expected = ['FIRST', 'SECOND'].includes(label) ? label : '';
      const expectedAlias = {CONNECTOR_ONLY: 'first', FIRST: 'first', SECOND: 'second'}[label] || '';
      const aliases = [...system.matchAll(/fixture-connector-(first|second)/g)].map(match => match[1]);
      const connectorMatches = aliases.join() === expectedAlias && !system.includes('FIXTURE_PRIVATE_MARKER');
      delta = { content: !inHistory && connectorMatches && markers.join() === expected && system.includes('You are')
        ? 'FACTORY SANDBOX SYSTEM PROMPT ' + label + ' VERIFIED'
        : 'FACTORY SANDBOX PROMPT FAILED ' + label + ' markers ' + markers.join(',') + ' aliases ' + aliases.join(',') };
    } else if (tools.length && userText.includes('Find FACTORY_RESEARCH_WEB_EVIDENCE')) {
      delta = tools.includes('web_search')
        ? choice('web_search', { query: 'Factory research source', numResults: 1 })
        : { content: 'FACTORY RESEARCH FAILED worker has no web search' };
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-web_search-') &&
               context.includes('Find FACTORY_RESEARCH_WEB_EVIDENCE')) {
      const result = String(last.content);
      delta = { content: result.includes('MAPLE_WEB_EVIDENCE_185') && result.includes('https://example.org/research-fixture')
        ? 'Example research source (https://example.org/research-fixture) gives MAPLE_WEB_EVIDENCE_185.'
        : 'FACTORY RESEARCH FAILED web evidence missing' };
    } else if (tools.length && context.includes('Factory Deep Web Research fixture')) {
      delta = researchReply(messages, tools);
    } else if (userText === 'Factory keyword fixture' && tools.length) {
      delta = tools.includes('knowledge_search') ? { content: 'FACTORY UNEXPECTED SEMANTIC SEARCH' }
        : choice('bash', { command: 'rg -a -n ORCHID_WINDOW /mnt/reference' });
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-bash-') &&
               context.includes('Factory keyword fixture')) {
      delta = { content: String(last.content).includes('ORCHID_WINDOW_842')
        ? 'FACTORY LOCAL KNOWLEDGE VERIFIED' : 'FACTORY KEYWORD RESULT MISSING' };
    } else if (userText === 'Factory knowledge fixture' && tools.length) {
      if (!tools.includes('knowledge_search') || tools.some(name =>
        ['knowledge_configure', 'knowledge_export'].includes(name))) {
        delta = { content: 'FACTORY INVALID KNOWLEDGE TOOL SCOPE' };
      } else {
        delta = choice('knowledge_search', { query: 'When is the field unavailable?', mode: 'semantic' });
      }
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-knowledge_search-')) {
      delta = { content: String(last.content).includes('ORCHID_WINDOW_842')
        ? 'FACTORY LOCAL KNOWLEDGE VERIFIED' : 'FACTORY KNOWLEDGE RESULT MISSING' };
    } else if (tools.length && userText === 'Schedule the fixture check in 45 seconds.') {
      if (!tools.includes('schedule_prompt')) throw new Error('The schedule tool is unavailable');
      delta = choice('schedule_prompt', { action: 'add', type: 'once', schedule: '+45s',
        name: 'Fixture status check', prompt: 'Run the scheduled fixture check now.' });
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-schedule_prompt-')) {
      delta = { content: 'The fixture check is scheduled.' };
    } else if (tools.length && userText === 'Run the scheduled fixture check now.') {
      delta = { content: 'The scheduled fixture check completed.' };
    } else if (tools.length && userText === 'Factory live token meter fixture') {
      await streamReasoningAndAnswer(response);
      return;
    } else if (tools.length && userText === 'Factory burst token meter fixture') {
      await streamOutputBurst(response);
      return;
    } else if (context.includes('RUN_FACTORY_FOLDER_ACCESS')) {
      if (!JSON.stringify(messages.filter(message => message.role === 'system')).includes('FACTORY FRESH PROJECT CONTEXT MARKER')) {
        throw new Error('Fresh project instructions were skipped');
      }
      const cases = JSON.parse(readFileSync('/tmp/folder-access-cases.json', 'utf8'));
      const step = messages.filter(message => message.role === 'tool').length;
      if (step < cases.length) {
        const test = cases[step];
        if (!tools.includes(test.tool)) throw new Error('Folder fixture tool missing: ' + test.tool);
        delta = choice(test.tool, test.args);
      } else {
        delta = { content: 'FACTORY FOLDER CHECK FINISHED' };
      }
    } else if (!tools.length && context.includes('test_provider_connection') &&
               /Review one proposed coding-agent action|You are a fail-closed permission reviewer/.test(context)) {
      const { request: action } = JSON.parse(typeof last.content === 'string' ? last.content : last.content[0].text);
      if (action?.toolName !== 'test_provider_connection') throw new Error('Unexpected provider review request');
      const deny = action.toolInputPreview.includes('PROVIDER_FIXTURE_DENY');
      console.log('PROVIDER CHECK REVIEW', deny ? 'deny' : 'allow', action.destination);
      delta = { content: JSON.stringify({ outcome: deny ? 'deny' : 'allow', risk_level: 'medium',
        user_authorization: 'high', rationale: 'Fixture decision for this provider check.' }) };
    } else if (userText === 'Factory provider check capability') {
      delta = { content: tools.includes('test_provider_connection') ? 'PROVIDER CHECK AVAILABLE' : 'PROVIDER CHECK HIDDEN' };
    } else if (userText.startsWith('Factory protected provider file ')) {
      delta = choice('read', { path: '/home/node/.pi/agent/' + userText.split(' ').at(-1) });
    } else if (last.role === 'tool' && context.includes('Factory protected provider file ') &&
               last.tool_call_id?.startsWith('factory-read-')) {
      console.log('PROVIDER FILE RESULT', JSON.stringify(last.content));
      delta = { content: 'PROVIDER FILE CHECK COMPLETE' };
    } else if (userText.startsWith('Factory provider check request ')) {
      if (!tools.includes('test_provider_connection')) throw new Error('Provider check tool is missing');
      delta = choice('test_provider_connection', { provider: 'sample', justification: userText.endsWith('deny')
        ? 'PROVIDER_FIXTURE_DENY' : 'Check the saved provider connection.' });
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-test_provider_connection-')) {
      console.log('PROVIDER CHECK RESULT', JSON.stringify(last.content));
      delta = { content: 'PROVIDER CHECK COMPLETE' };
    } else if (!tools.length && context.includes('request_file_access') &&
               /Review one proposed coding-agent action|You are a fail-closed permission reviewer/.test(context)) {
      const action = JSON.parse(typeof last.content === 'string' ? last.content : last.content[0].text).request;
      if (action?.toolName !== 'request_file_access') throw new Error('Unexpected file review request');
      console.log('FILE ACCESS REVIEW', action.operation, action.path);
      delta = { content: JSON.stringify({
        outcome: action.path.endsWith('restricted.md') ? 'deny' : 'allow',
        risk_level: 'medium', user_authorization: 'high', rationale: 'Fixture decision for this exact resource.',
      }) };
    } else if (userText.startsWith('Factory file access ')) {
      const [operation, path] = userText.slice('Factory file access '.length).split(' ');
      if (!tools.includes('request_file_access')) throw new Error('File access tool is missing');
      delta = choice('request_file_access', { operation, resource: path, justification: 'Check the requested Pi resource.' });
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-request_file_access-')) {
      console.log('FILE ACCESS RESULT', JSON.stringify(last.content));
      delta = { content: 'FILE ACCESS CHECK COMPLETE' };
    } else if (!tools.length && context.includes(suffix)) {
      delta = { content: JSON.stringify({
        outcome: 'deny', risk_level: 'medium', user_authorization: 'high',
        rationale: 'The fixture declined this command. REVIEWER_REASON_AT_THE_END',
      }) };
    } else if (userText === 'Read the Factory image fixture.') {
      if (!tools.includes('read')) throw new Error('Pi read tool missing');
      delta = choice('read', { path: '/workspace/image-fixture.png' });
    } else if (userText === 'Capture the Factory browser fixture.') {
      if (!tools.includes('browser')) throw new Error('Betterwright browser tool missing');
      delta = choice('browser', {
        code: "await page.setContent('<title>Vision screenshot fixture</title><main style=\"padding:32px;background:#147d78;color:white;font:32px sans-serif\">Factory screenshot fixture</main>'); return await screenshot({kind:'proof', name:'factory-vision'})",
      });
    } else if (toolResult?.role === 'tool' &&
        /^factory-(read|browser)-/.test(toolResult.tool_call_id)) {
      const related = toolResult === last ? messageImages(toolResult) : messageImages(last);
      const digest = url => createHash('sha256').update(Buffer.from(url.split(',')[1], 'base64')).digest('hex');
      if (related.length) console.log('TOOL IMAGE', toolResult.tool_call_id, related.map(digest).join(' '));
      delta = { content: related.length ? 'FACTORY TOOL IMAGE REACHED MODEL' : 'FACTORY TOOL IMAGE MISSING' };
    } else if (last.role === 'user' && JSON.stringify(last.content).includes('Factory approval fixture')) {
      if (!tools.includes('bash_escalate')) throw new Error('Factory escalation tool missing');
      delta = choice('bash_escalate', { command, justification: 'Check the full approval request in this disposable test.' });
    } else if (last.role === 'user' && JSON.stringify(last.content).includes('Factory tools fixture')) {
      const required = ['bash_escalate', 'ask_user_question'];
      if (!required.every(name => tools.includes(name))) throw new Error('Managed tools missing: ' + tools);
      delta = { content: 'FACTORY TOOLS READY' };
    } else if (userText.startsWith('Factory terminal blocking question fixture')) {
      delta = choice('ask_user_question', { waitForAnswer: true, questions: [{
        header: 'Blocking', question: userText.endsWith(' again') ? 'Choose another fruit now?' : 'Choose a fruit now?',
        options: [{ label: 'Apple (Recommended)', description: 'First choice' },
          { label: 'Pear', description: 'Second choice' }],
      }] });
      delta.tool_calls[0].id = 'factory-terminal-question-' + Date.now();
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-terminal-question-')) {
      const answer = String(last.content);
      delta = { content: answer.includes('User declined to answer questions')
        ? 'FACTORY BLOCKING QUESTION DISMISSED'
        : answer.includes('"Choose a fruit now?"="Pear"')
          ? 'FACTORY BLOCKING ANSWER RECEIVED' : 'FACTORY UNEXPECTED BLOCKING ANSWER' };
    } else if (userText.startsWith('Factory deferred question fixture')) {
      delta = choice('ask_user_question', { waitForAnswer: false, questions: [{
        header: 'Preference', question: 'Which fruit should I use later?',
        options: [{ label: 'Apple (Recommended)', description: 'First choice' },
          { label: 'Pear', description: 'Second choice' }],
      }] });
    } else if (userText.startsWith('Answer to question ')) {
      delta = { content: userText.includes('User declined')
        ? 'FACTORY DEFERRED QUESTION DISMISSED'
        : userText.includes('"Which fruit should I use later?"="Pear"')
          ? 'FACTORY DEFERRED ANSWER RECEIVED' : 'FACTORY UNEXPECTED DEFERRED ANSWER' };
    } else if (last.role === 'user' && JSON.stringify(last.content).includes('Factory question fixture')) {
      if (!tools.includes('ask_user_question')) throw new Error('Managed question tool missing');
      const options = [
        { label: 'Apple', description: 'First choice' },
        { label: 'Pear', description: 'Second choice' },
      ];
      delta = choice('ask_user_question', { questions: [
        { header: 'Choose one', question: 'Pick a fruit',
          options: [{ ...options[0], label: 'Apple (Recommended)' }, options[1]] },
        { header: 'Custom answer', question: 'Pick another fruit', options },
        { header: 'Choose several', question: 'Pick several fruits', options, multiSelect: true },
      ] });
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-ask_user_question-')) {
      const answer = String(last.content);
      if (answer.includes('is pending. Continue only work')) {
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        writeChunk(response, { role: 'assistant', content: 'FACTORY WORK CONTINUED' });
        await new Promise(resolve => setTimeout(resolve, 3500));
        writeChunk(response, {}, 'stop');
        response.end('data: [DONE]\n\n');
        return;
      }
      const delivered = ['"Pick a fruit"="Pear"', '"Pick another fruit"="Dragon fruit"',
        '"Pick several fruits"="Apple, Pear"'].every(segment => answer.includes(segment));
      const outcome = answer.includes('User declined to answer questions')
        ? 'FACTORY QUESTION DISMISSED' : 'UNEXPECTED QUESTION RESULT: ' + answer;
      delta = { content: delivered ? 'FACTORY QUESTION ANSWERS RECEIVED' : outcome };
    } else if (userText === 'Factory link fixture') {
      delta = { content: '![Image fixture](http://127.0.0.2:19382/image.png)\n\n[Open link fixture](http://127.0.0.2:19382/fixture)' };
    } else if (userText.startsWith('Factory chat ')) {
      const label = userText.slice('Factory chat '.length);
      if (label === 'steering initial') await new Promise(resolve => setTimeout(resolve, 2500));
      delta = { content: 'CHAT REPLY ' + label };
    } else if (last.role === 'tool' && last.tool_call_id?.startsWith('factory-goal_update-')) {
      delta = { content: 'FACTORY GOAL COMPLETE' };
    } else if (userText.includes('Revised Factory objective')) {
      const token = userText.match(/Completion token: (\S+)\. Supply/)?.[1];
      if (!token || !tools.includes('goal_update')) throw new Error('Resumed goal instructions missing');
      delta = choice('goal_update', { token, status: 'complete', reason: 'Verified the revised synthetic objective.' });
    } else if (context.includes('Factory goal fixture')) {
      // Leave the goal running until the user exercises the pause command.
      if (!tools.includes('goal_update')) throw new Error('Goal tool missing from active session');
      await new Promise(resolve => setTimeout(resolve, 1500));
      delta = { content: 'FACTORY GOAL TURN' };
    } else {
      delta = { content: 'FACTORY FIXTURE READY' };
    }
    sendReply(response, delta);
  });
}).listen(19381, '127.0.0.1', () => console.log('READY'));
