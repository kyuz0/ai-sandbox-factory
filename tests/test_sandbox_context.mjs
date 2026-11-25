import assert from 'node:assert/strict';
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import test from 'node:test';
import systemPrompt, {sandboxContext} from '../ai_sandbox_factory/containers/dev/system-prompt.mjs';

const restrictedState = {
  network: 'allowlist',
  values: {Network: 'Restricted', 'Additional allowed destinations': 'packages.example.test:443'},
  securityGuidance: 'Use the configured allowlist.',
};
const unrestrictedState = {
  network: 'unrestricted',
  values: {Network: 'Unrestricted'},
  securityGuidance: 'Use ordinary network connections.',
};

test('the registered per-turn hook rereads both files, including unchanged and removed add-ons', t => {
  const files = new Map();
  const reader = t.mock.method(fs, 'readFileSync', path => {
    const name = path.slice('/opt/pi-security/config/'.length);
    if (files.has(name)) return JSON.stringify(files.get(name));
    throw Object.assign(new Error('Fixture file missing: ' + path), {code: 'ENOENT'});
  });
  syncBuiltinESMExports();
  t.after(() => {
    reader.mock.restore();
    syncBuiltinESMExports();
  });

  let beforeTurn;
  systemPrompt({on(event, callback) {
    assert.equal(event, 'before_agent_start');
    beforeTurn = callback;
  }});
  const base = 'Base prompt with project instructions';
  const turn = (prompt = base) => beforeTurn({systemPrompt: prompt})?.systemPrompt ?? prompt;
  assert.equal(turn(), base);
  files.set('sandbox-system-prompt.json', {text: 'Persistent add-on'});
  files.set('sandbox-state.json', restrictedState);
  const restricted = turn();
  assert.ok(restricted.startsWith(base));
  assert.match(restricted, /^Network: Restricted$/m);
  assert.equal(turn(restricted), restricted);

  files.set('sandbox-state.json', unrestrictedState);
  const unrestricted = turn();
  assert.equal(unrestricted.split('Persistent add-on').length, 2);
  assert.match(unrestricted, /^Network: Unrestricted$/m);
  assert.doesNotMatch(unrestricted, /Network: Restricted|configured allowlist/);
  files.delete('sandbox-system-prompt.json');
  assert.doesNotMatch(turn(), /Persistent add-on/);
});

test('each snapshot describes the current mode once and retains public setting values', () => {
  const restricted = sandboxContext(restrictedState);
  assert.equal(restricted.match(/^Network: /gm).length, 1);
  assert.match(restricted, /^Network: Restricted$/m);
  assert.equal(restricted.split(restrictedState.securityGuidance).length, 2);
  assert.match(restricted, /Additional allowed destinations: packages\.example\.test:443/);

  const unrestricted = sandboxContext(unrestrictedState);
  assert.equal(unrestricted.match(/^Network: /gm).length, 1);
  assert.match(unrestricted, /^Network: Unrestricted$/m);
  assert.equal(unrestricted.split(unrestrictedState.securityGuidance).length, 2);
  assert.doesNotMatch(unrestricted, /configured allowlist|packages\.example\.test/);
});

test('an absent snapshot leaves the existing system prompt alone', () => {
  assert.equal(sandboxContext(undefined), '');
});

test('invalid snapshots report the expected settings and recovery action', () => {
  for (const [invalid, cause] of [
    [{network: 'unknown'}, 'network mode "unknown" must be allowlist or unrestricted'],
    [{values: []}, 'values must be an object containing the named settings'],
    [{securityGuidance: null}, 'securityGuidance must contain the applied security instructions'],
  ]) {
    assert.throws(() => sandboxContext({...restrictedState, ...invalid}), error =>
      error.message.includes('sandbox-state.json: ' + cause + '. Reopen the sandbox from Factory.'));
  }
});
