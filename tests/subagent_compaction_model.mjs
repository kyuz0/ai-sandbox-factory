// A loopback-only OpenAI-compatible fixture. Only the usage counters are large.
import http from 'node:http';

const active = new Map();
const text = m => typeof m.content === 'string' ? m.content : (m.content ?? []).map(p => p.text).join('\n');
const recall = names => names.filter(name => name.startsWith('history_')).sort();
const entry = /\[([a-f\d]{8})\]/i;
const log = event => console.log(JSON.stringify(event));
const tool = (id, name, args) => ({ tool_calls: [{ index: 0, id, type: 'function',
  function: { name, arguments: JSON.stringify(args) } }] });
const matches = (value, regex) => { if (!regex.test(value)) throw Error('Unexpected fixture result: ' + value.slice(0, 400)); };
const acceptance = { level: 'none', reason: 'The fixture checks the saved session and exact tool results.' };

function reply(request, bytes) {
  const messages = request.messages;
  const names = (request.tools ?? []).map(t => t.function.name);
  const body = messages.map(text).join('\n');
  const parent = [...messages].reverse().find(m => m.role === 'user' && /^Factory compaction (pi|frostpi)-(foreground|background|specialist)\b/m.test(text(m)));
  const child = body.match(/CHILD_WORK_(pi|frostpi)-(foreground|background|specialist)\b/);
  const checkpoint = body.match(/FIXTURE_CHECKPOINT_(pi|frostpi)-(foreground|background|specialist)\b/);
  const last = messages.at(-1);
  const isParent = parent && (parent === last || messages.slice(messages.indexOf(parent) + 1)
    .some(m => m.role === 'assistant' && m.tool_calls?.some(t => t.function.name === 'subagent')));
  const high = 1_000_000;
  let tokens = 80;
  let delta;
  if (!names.length && /Generate a concise title/.test(body)) return [{ content: 'Compaction fixture' }, tokens];
  if (!names.length && (child || checkpoint)) {
    const [, client, worker] = child ?? checkpoint;
    const id = client + '-' + worker;
    if (!active.has(id)) throw Error('Unexpected compaction before child launch: ' + id);
    matches(body, /summar|turn prefix/i);
    log({id, event:'summary', bytes, containsPrivate:body.includes('COBALT_VALUE_8123')});
    return [{ content: 'FIXTURE_CHECKPOINT_' + id + ' Continue the read task.' }, tokens];
  }
  if (isParent) {
    const id = text(parent).match(/Factory compaction ([\w-]+)/)[1];
    const worker = id.split('-')[1];
    const first = !active.has(id);
    if (first) {
      active.set(id, { stage: 0 });
      log({id, event:'parent', tools:recall(names), bytes});
      delta = tool('launch-' + id, 'subagent', {agent:worker === 'specialist' ? 'fixture-reader' : 'assistant',
        task:'CHILD_WORK_' + id + ' Inspect the cobalt calibration archive.', async:worker === 'background', options:{acceptance}});
    } else {
      if (worker !== 'background') matches(text(last), /CHILD FINISHED/);
      log({id, event:'parent-result', bytes, leakedSecret:body.includes('COBALT_VALUE_8123')});
      delta = {content:'PARENT FINISHED ' + id};
    }
  } else if (child || checkpoint) {
    const [, client, worker] = child ?? checkpoint;
    const id = client + '-' + worker;
    const state = active.get(id);
    if (!state) throw Error('Unknown child ' + id);
    const stage = state.stage++;
    const available = recall(names);
    const perform = (name, args) => {
      if (!names.includes(name)) throw Error('Tool unavailable: ' + name);
      return tool('child-' + id + '-' + stage, name, args);
    };
    log({id,event:'child',stage,tools:available,bytes, checkpoint:!!checkpoint, original:!!child,
      leakedParent:body.includes('PARENT_PRIVATE_7294'), rawSecret:body.includes('COBALT_VALUE_8123')});
    if (stage === 0) {
      delta = perform('read', {path:'/workspace/compact-seed.txt'});
      tokens = 950_000;
    } else if (stage === 1) {
      matches(text(last), /COBALT_VALUE_8123/);
      if (checkpoint) throw Error('Compacted before crossing the configured reserve');
      delta = perform('read', {path:'/workspace/compact-tail.txt'});
      tokens = high;
    } else if (stage === 2) {
      if (!checkpoint || child || body.includes('COBALT_VALUE_8123')) throw Error('Uncompacted prefix survived');
      matches(text(last), /TAIL_CONTENT/);
      delta = available.includes('history_grep') ? perform('history_grep', {pattern:'compact-seed[.]txt'}) : {content:'CHILD FINISHED ' + id};
    } else if (stage === 3) {
      const result = text(last);
      matches(result, /compact-seed[.]txt/);
      log({id,event:'grep-found'});
      delta = perform('history_expand', {id:result.match(entry)[1], before:0, after:1});
    } else if (stage === 4) {
      matches(text(last), /COBALT_VALUE_8123/);
      log({id,event:'expand-found'});
      delta = perform('history_grep', {pattern:'PARENT_PRIVATE_7294'});
    } else if (stage === 5) {
      matches(text(last), /No matches in compacted history.*totalEntries=0/);
      log({id,event:'grep-parent', result:text(last).slice(0,200)});
      delta = available.includes('history_recall') ? perform('history_recall', {concepts:[['cobalt calibration archive']]}) : {content:'CHILD FINISHED ' + id};
    } else if (stage === 6) {
      matches(text(last), new RegExp('CHILD_WORK_' + id + ' Inspect the cobalt calibration archive', 'i'));
      log({id,event:'lexical-found'});
      delta = {content:'CHILD FINISHED ' + id};
    } else throw Error('Unexpected child request ' + id + ' ' + stage);
    if (delta.content) log({id,event:'child-finished'});
  } else {
    // Detached completions and title generation can arrive after the foreground chat has settled.
    if (!/subagent|title/i.test(body)) throw Error('Unknown request');
    delta = {content:'Acknowledged.'};
  }
  return [delta, tokens];
}

http.createServer((request, response) => {
  let raw = '';
  request.on('data', chunk => { raw += chunk; });
  request.on('end', () => {
    try {
      const input = JSON.parse(raw);
      const [delta, tokens] = reply(input, Buffer.byteLength(raw));
      const usage = {prompt_tokens:tokens-1, completion_tokens:1, total_tokens:tokens};
      const packet = (delta, finish, usage) => 'data: ' + JSON.stringify({id:'fixture', model:'fixture',
        created:1, object:'chat.completion.chunk', choices:[{index:0, delta, finish_reason:finish}], usage}) + '\n\n';
      response.writeHead(200, {'content-type':'text/event-stream'});
      response.end(packet({role:'assistant', ...delta}, null) + packet({}, delta.tool_calls ? 'tool_calls' : 'stop', usage) + 'data: [DONE]\n\n');
    } catch (error) {
      log({event:'error', detail:String(error), stack:error.stack});
      response.writeHead(400, {'content-type':'application/json'});
      response.end(JSON.stringify({error:{message:String(error)}}));
    }
  });
}).listen(19381, '127.0.0.1');
