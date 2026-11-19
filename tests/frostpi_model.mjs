// Synthetic inference for the disposable FrostPi editor regression.
import http from 'node:http';

const suffix = 'FINAL_APPROVAL_ARGUMENT';
const command = "printf '%s' '" + "command detail ".repeat(65) + suffix + "' > /workspace/approval-marker.txt";
const choice = (name, args) => ({
  tool_calls: [{ index: 0, id: 'factory-' + name + '-' + Date.now(), type: 'function',
    function: { name, arguments: JSON.stringify(args) } }],
});
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
    let delta;
    if (!tools.length && context.includes(suffix)) {
      delta = { content: JSON.stringify({
        outcome: 'deny', risk_level: 'medium', user_authorization: 'high',
        rationale: 'The fixture declined this command. REVIEWER_REASON_AT_THE_END',
      }) };
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
        { header: 'Choose one', question: 'Pick a fruit', options },
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
    for (const data of [
      { delta: { role: 'assistant', ...delta }, finish_reason: null },
      { delta: {}, finish_reason: delta.tool_calls ? 'tool_calls' : 'stop' },
    ]) {
      response.write('data: ' + JSON.stringify({ id: 'factory', model: 'fixture',
        object: 'chat.completion.chunk', created: 1, choices: [{ index: 0, ...data }] }) + '\n\n');
    }
    response.end('data: [DONE]\n\n');
  });
}).listen(19381, '127.0.0.1', () => console.log('READY'));
