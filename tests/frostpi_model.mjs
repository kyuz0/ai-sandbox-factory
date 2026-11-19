// Synthetic inference for the disposable FrostPi editor regression.
import http from 'node:http';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

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

http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => body += chunk);
  request.on('end', async () => {
    const input = JSON.parse(body);
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
    } else if (tools.length && userText === 'Factory live token meter fixture') {
      await streamReasoningAndAnswer(response);
      return;
    } else if (tools.length && userText === 'Factory burst token meter fixture') {
      await streamOutputBurst(response);
      return;
    } else if (context.includes('RUN_FACTORY_FOLDER_ACCESS')) {
      const cases = JSON.parse(readFileSync('/tmp/folder-access-cases.json', 'utf8'));
      const step = messages.filter(message => message.role === 'tool').length;
      if (step < cases.length) {
        const test = cases[step];
        if (!tools.includes(test.tool)) throw new Error('Folder fixture tool missing: ' + test.tool);
        delta = choice(test.tool, test.args);
      } else {
        delta = { content: 'FACTORY FOLDER CHECK FINISHED' };
      }
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
      const delivered = ['"Pick a fruit"="Pear"', '"Pick another fruit"="Dragon fruit"',
        '"Pick several fruits"="Apple, Pear"'].every(segment => answer.includes(segment));
      const outcome = answer.includes('User declined to answer questions')
        ? 'FACTORY QUESTION DISMISSED' : 'UNEXPECTED QUESTION RESULT: ' + answer;
      delta = { content: delivered ? 'FACTORY QUESTION ANSWERS RECEIVED' : outcome };
    } else if (userText === 'Factory link fixture') {
      delta = { content: '[Open link fixture](http://127.0.0.2:19382/fixture)' };
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
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    writeChunk(response, { role: 'assistant', ...delta });
    writeChunk(response, {}, delta.tool_calls ? 'tool_calls' : 'stop');
    response.end('data: [DONE]\n\n');
  });
}).listen(19381, '127.0.0.1', () => console.log('READY'));
