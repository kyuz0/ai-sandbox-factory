import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createAgentSession} from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/sdk.js';
const enabled = JSON.parse(fs.readFileSync('/opt/sandbox/options.json')).question_forms !== false;
const {session, extensionsResult} = await createAgentSession({cwd:'/workspace',agentDir:'/home/node/.pi/agent'});
try {
  assert.deepEqual(extensionsResult.errors, []);
  const tools = session.getAllTools();
  const names = tools.map(t => t.name);
  assert.equal(names.includes('ask_user_question'), enabled);
  assert(names.includes('bash_escalate'), 'Reviewed escalation missing');
  if (enabled) {
    const tool = tools.find(t => t.name === 'ask_user_question');
    for (const guidance of [tool.description, tool.promptGuidelines.join('\n')]) {
      assert(guidance.includes('all question-form fields plain text'), 'Plain-text form guidance missing');
      assert(guidance.includes('normal chat message before calling ask_user_question'), 'Chat-first link guidance missing');
    }
    assert.equal(tool.parameters.properties.waitForAnswer.type, 'boolean', 'Non-blocking question option missing');
  }
  console.log('PASS installed Pi: question forms ' + enabled + ', reviewed escalation retained');
} finally { session.dispose(); }
