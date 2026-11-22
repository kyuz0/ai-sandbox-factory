// Inspect the same installed Pi SDK and extensions used by FrostPi and the CLI.
import assert from 'node:assert/strict';
import { createAgentSession } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js';

const { session } = await createAgentSession();
try {
  const tools = session.getAllTools().filter(tool => tool.name.startsWith('slack_'));
  assert.equal(tools.length, process.argv.includes('--disabled') ? 0 : 8);
  console.log(`PASS installed Pi: ${tools.length} Slack tools registered`);
} finally {
  session.dispose();
}
