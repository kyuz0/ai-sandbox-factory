import assert from 'node:assert/strict';
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import test from 'node:test';
import systemPrompt, {sandboxContext, sandboxUpdate} from '../ai_sandbox_factory/containers/dev/system-prompt.mjs';

const restrictedState = {
  network: 'allowlist',
  values: {Network: 'Restricted', Browser: 'On', 'Additional allowed destinations': 'packages.example.test:443'},
  securityGuidance: 'Use the configured allowlist. Full guidance: /opt/sandbox/context.md.',
};
const unrestrictedState = {
  network: 'unrestricted',
  values: {Network: 'Unrestricted', Browser: 'On'},
  securityGuidance: 'Use ordinary network connections. Full guidance: /opt/sandbox/context.md.',
};

function harness(t, files, entries = []) {
  const reader = t.mock.method(fs, 'readFileSync', path => {
    const name = path.slice('/opt/pi-security/config/'.length);
    if (files.has(name)) return JSON.stringify(files.get(name));
    throw Object.assign(new Error('Fixture file missing: ' + path), {code: 'ENOENT'});
  });
  syncBuiltinESMExports();
  t.after(() => { reader.mock.restore(); syncBuiltinESMExports(); });
  let beforeTurn;
  systemPrompt({on(event, callback) {
    assert.equal(event, 'before_agent_start'); beforeTurn = callback;
  }, appendEntry(customType, data) { entries.push({type: 'custom', customType, data}); }});
  return {
    entries,
    turn: (branch = entries) => beforeTurn({systemPrompt: 'Base prompt with project instructions'},
      {sessionManager: {getBranch: () => branch}})?.systemPrompt ?? 'Base prompt with project instructions',
  };
}

test('startup keeps full guidance; later turns carry only differences from the conversation’s initial settings', t => {
  const files = new Map([['sandbox-state.json', restrictedState]]);
  const run = harness(t, files);
  const first = run.turn();
  assert.match(first, /Use the configured allowlist/);
  assert.doesNotMatch(first, /Sandbox update:/);
  assert.equal(run.turn(), first);
  files.set('sandbox-state.json', unrestrictedState);
  const changed = run.turn();
  assert.ok(changed.startsWith(first));
  const update = changed.split('Sandbox update:')[1];
  assert.match(update, /- Network: Unrestricted/);
  assert.match(update, /- Additional allowed destinations: no longer configured/);
  assert.doesNotMatch(update, /Browser:|Use ordinary network|configured allowlist/);
  assert.match(update, /\/opt\/sandbox\/context\.md/);
  assert.match(update, /Factory updated \/opt\/sandbox\/context\.md with the full current configuration/);
  assert.match(update, /Re-read it instead of relying on earlier tool output\./);
  assert.equal(run.turn(), changed, 'An unchanged turn does not accumulate another notice');
  files.set('sandbox-state.json', restrictedState);
  assert.equal(run.turn(), first, 'Reversion restores the initial context without a stale update');
  assert.equal(run.entries.length, 1);
});

test('resumed, forked and switched conversations use their branch’s initial context', t => {
  const files = new Map([['sandbox-state.json', unrestrictedState]]);
  const entries = [{type: 'custom', customType: 'factory-sandbox-context', data: restrictedState}];
  const run = harness(t, files, entries);
  assert.match(run.turn(), /- Network: Unrestricted/);
  assert.doesNotMatch(run.turn([]), /Sandbox update:/);
  assert.match(run.turn(entries.slice(0, 1)), /- Network: Unrestricted/);
});

test('cancelled or failed turns, process reloads and repeated reversions cannot lose a current correction', t => {
  const files = new Map([['sandbox-state.json', restrictedState]]);
  const run = harness(t, files);
  run.turn();
  files.set('sandbox-state.json', unrestrictedState);
  const failed = run.turn();
  // No completion callback is needed: every request is derived from its branch.
  const resumed = harness(t, files, run.entries);
  assert.equal(resumed.turn(), failed);
  files.set('sandbox-state.json', restrictedState);
  assert.doesNotMatch(resumed.turn(), /Sandbox update:/);
  files.set('sandbox-state.json', unrestrictedState);
  assert.equal(resumed.turn(), failed);
});

test('the custom prompt add-on still updates and can be removed without changing initial guidance', t => {
  const files = new Map([['sandbox-state.json', restrictedState], ['sandbox-system-prompt.json', {text: 'First add-on'}]]);
  const run = harness(t, files);
  assert.match(run.turn(), /First add-on/);
  files.set('sandbox-system-prompt.json', {text: 'Second add-on'});
  assert.doesNotMatch(run.turn(), /First add-on/);
  assert.match(run.turn(), /Second add-on/);
  files.delete('sandbox-system-prompt.json');
  assert.doesNotMatch(run.turn(), /add-on/);
});

test('one changed setting is concise and long values keep every label within the prompt budget', () => {
  const one = sandboxUpdate({...restrictedState, values: {...restrictedState.values, Browser: 'Off'}}, restrictedState);
  assert.match(one, /- Browser: Off/);
  assert.doesNotMatch(one, /Network:|Additional allowed destinations:/);
  assert.ok(one.length < 350);
  const values = Object.fromEntries(Array.from({length: 36}, (_, i) => [`Configured feature ${i}`, 'long value '.repeat(200)]));
  const many = sandboxUpdate({...restrictedState, values}, {...restrictedState, values: {}});
  assert.ok(Buffer.byteLength(many, 'utf8') <= 1500);
  for (const name of Object.keys(values)) assert.ok(many.includes(name + ':'));
  const unicode = sandboxUpdate({...restrictedState, values:Object.fromEntries(Object.keys(values).map(name => [name,'漢字🌍'.repeat(10)]))},
    {...restrictedState, values:{}});
  assert.ok(Buffer.byteLength(unicode, 'utf8') <= 1500);
  const guidance = sandboxUpdate({...restrictedState, guidanceRevision:'new'}, {...restrictedState, guidanceRevision:'old'});
  assert.match(guidance, /- Sandbox guidance: updated/);
  assert.doesNotMatch(guidance, /- Network:/);
});

test('startup context is complete and invalid snapshots retain actionable errors', () => {
  assert.match(sandboxContext(restrictedState), /Additional allowed destinations: packages\.example\.test:443/);
  assert.equal(sandboxContext(undefined), '');
  for (const [invalid, cause] of [
    [{network: 'unknown'}, 'network mode "unknown" must be allowlist or unrestricted'],
    [{values: []}, 'values must be an object containing the named settings'],
    [{securityGuidance: null}, 'securityGuidance must contain the applied security instructions'],
  ]) {
    assert.throws(() => sandboxContext({...restrictedState, ...invalid}), error =>
      error.message.includes('sandbox-state.json: ' + cause + '. Reopen the sandbox from Factory.'));
  }
});
