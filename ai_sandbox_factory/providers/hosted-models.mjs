// Executed in the sandbox. Requests arrive on stdin; replies never contain credentials.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';
import {locked} from '/opt/sandbox/provider-lock.mjs';
const root = process.env.FACTORY_PI_RUNTIME || '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent';
const {AuthStorage, readStoredCredential} = await import(pathToFileURL(root + '/dist/core/auth-storage.js'));
const {ModelRuntime} = await import(pathToFileURL(root + '/dist/core/model-runtime.js'));
const {SettingsManager} = await import(pathToFileURL(root + '/dist/core/settings-manager.js'));
const {stripJsonComments} = await import(pathToFileURL(root + '/dist/utils/json.js'));
const {configureHttpDispatcher} = await import(pathToFileURL(root + '/dist/core/http-dispatcher.js'));
configureHttpDispatcher();
const dir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), '.pi/agent');
const authPath = path.join(dir, 'auth.json');
const modelsPath = path.join(dir, 'models.json');
const ids = ['openrouter', 'together', 'azure', 'amazon-bedrock'];
const read = (file, fallback = {}) => fs.existsSync(file) ? JSON.parse(stripJsonComments(fs.readFileSync(file, 'utf8'))) : fallback;
const secretFields = new Set(['key', 'secret', 'apiKey', 'access_key', 'secret_key', 'session_token',
  'accessKeyId', 'secretAccessKey', 'sessionToken', 'accessToken', 'refreshToken']);
function credentialVariants(request) {
  const variants = new Set();
  function visit(object) {
    for (const [field, value] of Object.entries(object)) {
      if (value && typeof value === 'object') visit(value);
      if (value && typeof value === 'string' && secretFields.has(field)) {
        variants.add(value);
        variants.add(JSON.stringify(value).slice(1, -1));
        variants.add(encodeURIComponent(value.toWellFormed()));
      }
    }
  }
  if (request && typeof request === 'object') visit(request);
  return [...variants].sort((a, b) => b.length - a.length);
}
function diagnostic(error, variants) {
  const chain = [];
  const seen = new Set();
  for (let item = error; item != null && !seen.has(item); item = item.cause) {
    seen.add(item);
    // V8 may include a fragment of credentials or configuration in JSON parsing errors.
    const reason = item instanceof SyntaxError ? 'Invalid JSON' : String(item.message ?? item);
    chain.push([item.code, reason].filter(Boolean).join(': '));
  }
  let detail = chain.join(' — ') || 'No error details were returned.';
  for (const value of variants) detail = detail.split(value).join('[redacted]');
  return detail.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ');
}
function write(file, value) {
  fs.mkdirSync(path.dirname(file), {recursive: true, mode: 0o700});
  const temp = file + '.' + process.pid + '.tmp';
  try { fs.writeFileSync(temp, typeof value === 'string' ? value : JSON.stringify(value, null, 2) + '\n', {mode: 0o600}); fs.renameSync(temp, file); }
  finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
}
const runtime = () => ModelRuntime.create({authPath, modelsPath, modelsStorePath: path.join(dir, 'models-store.json'), allowModelNetwork: false});
function status(id) {
  const c = readStoredCredential(id, authPath);
  return {provider: id, configured: !!c, has_key: !!c?.key};
}
function safeModel(model) {
  return {id: model.id, name: model.name, api: model.api, contextWindow: model.contextWindow,
    maxTokens: model.maxTokens, reasoning: model.reasoning, input: model.input};
}
function localProviders(ports) {
  const endpoints = new Set(ports.map(port => new URL('http://local-model.internal:' + port).origin));
  const file = modelsPath + '.local-lab-state.json';
  if (!fs.existsSync(file)) return [];
  function catalogueFile(filename) {
    try { return read(filename); }
    catch (error) {
      const reason = error instanceof SyntaxError ? 'invalid JSON' : (error.code || error.name);
      throw Error('Cannot read the local model catalogue: ' + filename + ' (' + reason + '). Run local model discovery again.');
    }
  }
  const manifest = catalogueFile(file);
  const providers = catalogueFile(modelsPath)?.providers || {};
  if (manifest?.version !== 1 || !manifest.entries || typeof manifest.entries !== 'object' || Array.isArray(manifest.entries)) {
    throw Error('Cannot read the local model catalogue: ' + file + ' needs version 1 ownership entries. Run local model discovery again.');
  }
  return Object.entries(manifest.entries).filter(([id, entry]) =>
    endpoints.has(entry?.endpoint) && Object.hasOwn(providers, id) &&
    isDeepStrictEqual(providers[id], entry.generated)).map(([id]) => id);
}
async function main(r, variants) {
  if (r.action === 'status') return {providers: ids.map(status)};
  if (r.action === 'approval-catalogue') {
    const rt = await runtime();
    const available = await rt.getAvailable();
    return {models: available.map(({provider, id, name}) => ({provider, id, name}))};
  }
  const plugin = r.plugin === true && ['catalogue', 'select'].includes(r.action) &&
    typeof r.provider === 'string' && /^[a-z][a-z0-9-]{0,254}$/.test(r.provider);
  const local = Array.isArray(r.localPorts) && ['local-catalogue', 'select'].includes(r.action);
  if (local) {
    const providers = localProviders(r.localPorts);
    if (r.action === 'local-catalogue') {
      const rt = await runtime();
      return {models: providers.flatMap(provider => rt.getModels(provider).map(model => ({provider, ...safeModel(model)})))};
    }
    if (!providers.includes(r.provider)) {
      throw Error('Choose a local provider returned by models list --local. ' + JSON.stringify(r.provider) + ' is not in the current catalogue.');
    }
  }
  if (!plugin && !local && !ids.includes(r.provider)) throw Error('Unsupported provider.');
  const id = r.provider;
  if (r.action === 'configure') {
    if (!r.endpoint?.startsWith('http://127.0.0.1:19431/r/'+id)) throw Error('Invalid inference endpoint.');
    const old = readStoredCredential(id, authPath);
    const env = {};
    if (id === 'azure') {
      env.AZURE_OPENAI_BASE_URL = r.endpoint;
      if (old?.env?.AZURE_OPENAI_DEPLOYMENT_NAME_MAP) env.AZURE_OPENAI_DEPLOYMENT_NAME_MAP = old.env.AZURE_OPENAI_DEPLOYMENT_NAME_MAP;
    }
    if (id === 'amazon-bedrock') Object.assign(env,{AWS_REGION:r.region,AWS_BEDROCK_SKIP_AUTH:'1',AWS_BEDROCK_FORCE_HTTP1:'1'});
    await AuthStorage.create(authPath).modify(id,async () => ({type:'api_key',key:'factory-inference',env}));
    const config = read(modelsPath, {providers:{}});
    config.providers ||= {};
    const provider = config.providers[id] ||= {};
    provider.baseUrl = r.endpoint;
    provider.apiKey = 'factory-inference';
    for (const model of provider.models || []) model.baseUrl = r.endpoint;
    write(modelsPath,config);
    return status(id);
  }
  if (r.action === 'remove') {
    await AuthStorage.create(authPath).delete(id);
    const config=read(modelsPath,{providers:{}});
    if(config.providers?.[id]) delete config.providers[id].apiKey;
    write(modelsPath,config);
    return status(id);
  }
  const rt = await runtime();
  if (r.action === 'catalogue') {
    let warning = '';
    if (r.refresh && !plugin) {
      try {
        const result = await rt.refresh({providers: [id], allowNetwork: true, force: true, signal: AbortSignal.timeout(20000)});
        if (result.aborted || result.errors?.size) {
          const causes = [...(result.errors || [])].map(([provider, error]) => provider + ': ' + diagnostic(error, variants));
          if (result.aborted) causes.push('The refresh was interrupted.');
          warning = 'Showing saved models after a catalogue refresh failure. ' + causes.join(' — ');
        }
      } catch (error) { warning = 'Showing saved models after a catalogue refresh failure. ' + diagnostic(error, variants); }
    }
    return {models: rt.getModels(id).map(safeModel), warning};
  }
  const model = rt.getModel(id, r.model);
  if (!model && local) {
    throw Error('Choose a model from models list --local. Provider ' + JSON.stringify(id) +
      ' has no model ' + JSON.stringify(r.model) + '.');
  }
  if (!model) throw Error('Choose a model from the catalogue.');
  if (r.action === 'select') {
    if (!plugin && !local && id === 'azure' && r.deployment) {
      if (/[,=\r\n]/.test(r.deployment)) throw Error('Invalid Azure deployment name.');
      await AuthStorage.create(authPath).modify(id, async current => {
        if (!current) throw Error('Configure the provider first.');
        const env = {...current.env};
        const map = new Map((env.AZURE_OPENAI_DEPLOYMENT_NAME_MAP || '').split(',').filter(Boolean).map(s => s.split('=')));
        map.set(model.id, r.deployment);
        env.AZURE_OPENAI_DEPLOYMENT_NAME_MAP = [...map].map(([k,v]) => `${k}=${v}`).join(',');
        return {...current, env};
      });
    }
    let chosen = model.id;
    if (!plugin && !local && id === 'amazon-bedrock' && r.profile_id && r.profile_id !== model.id) {
      chosen = r.profile_id;
      const config = read(modelsPath, {providers: {}});
      config.providers ||= {};
      const provider = config.providers[id] ||= {};
      provider.models ||= [];
      // Copy the selected model's actual metadata, including API compatibility.
      const {provider: ignored, ...definition} = model;
      provider.models = provider.models.filter(m => m.id !== chosen);
      provider.models.push({...definition, id: chosen, name: model.name + ' · ' + chosen});
      write(modelsPath, config);
    }
    const settings = SettingsManager.create('/workspace', dir);
    settings.setDefaultModelAndProvider(id, chosen);
    await settings.flush();
    const errors = settings.drainErrors();
    if (errors.length) {
      throw Error('Could not save the default model. ' + errors.map(item =>
        (item.path || item.scope) + ': ' + diagnostic(item.error, variants)).join(' — '));
    }
    return {provider: id, model: chosen};
  }
  if (r.action === 'test') {
    const testModel = id === 'amazon-bedrock' && r.profile_id ? {...model, id:r.profile_id} : model;
    const env = id === 'azure' && r.deployment ? {AZURE_OPENAI_DEPLOYMENT_NAME_MAP: `${model.id}=${r.deployment}`} : undefined;
    const reply = await rt.completeSimple(testModel, {messages: [{role:'user', content:'Reply with OK.', timestamp:Date.now()}]}, {env, maxTokens: 64, signal: AbortSignal.timeout(45000)});
    if (reply.stopReason === 'error' || reply.stopReason === 'aborted') {
      const reason = reply.errorMessage || 'Check model access, endpoint and network settings.';
      throw Error('The model request failed: ' + reason);
    }
    return {ok: true};
  }
  throw Error('Unknown operation.');
}
let input = '';
let variants = [];
let operation = 'Read model request';
try {
  for await (const chunk of process.stdin) {
    input += chunk;
  }
  const request = JSON.parse(input);
  variants = credentialVariants(request);
  operation = 'Model operation ' + String(request.action);
  console.log(JSON.stringify(await locked(dir,()=>main(request, variants))));
} catch (error) {
  const failure = new Error(operation, {cause: error});
  console.log(JSON.stringify({error: diagnostic(failure, variants)})); process.exitCode = 1;
}
