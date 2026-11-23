// Receives no real tokens or token-derived data. Host verifies captured output.
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const vscode = require('vscode');
exports.activate = async () => {
  const report = {checks: [], errors: [], observed: []};
  try {
    let original;
    const check = (data, label) => {
      report.observed.push(data.toString());
      const parsed = JSON.parse(data);
      const fields = agent === 'claude' ? parsed.claudeAiOauth : parsed.token;
      const names = agent === 'claude' ? ['accessToken', 'refreshToken'] : ['access_token', 'refresh_token'];
      for (const name of names) {
        if (typeof fields[name] !== 'string' || !fields[name].startsWith(`factory-${agent}-`)) {
          throw new Error(label + ': ' + name + ' is not a placeholder');
        }
      }
      report.checks.push(label);
    };
    if (agent === 'claude') {
      const filename = path.join(os.homedir(), '.claude/.credentials.json');
      original = await fs.readFile(filename);
      check(original, 'Node filesystem');
      check(Buffer.from(await vscode.workspace.fs.readFile(vscode.Uri.file(filename))), 'VS Code filesystem');
      const changed = {...JSON.parse(original), 'factory-hostile-extension-marker': true};
      await vscode.workspace.fs.writeFile(vscode.Uri.file(filename), Buffer.from(JSON.stringify(changed)));
    } else {
      const opts = {env: {...process.env, DBUS_SESSION_BUS_ADDRESS: 'unix:path=/tmp/factory-agy-session/bus'}};
      const lookup = ['lookup', 'service', 'gemini', 'username', 'antigravity'];
      original = execFileSync('secret-tool', lookup, opts);
      check(original, 'Extension Secret Service access');
      check(execFileSync('sh', ['-c', 'secret-tool lookup service gemini username antigravity'], opts), 'Shell Secret Service access');
      const changed = {...JSON.parse(original), 'factory-hostile-extension-marker': true};
      const store = ['store', '--label=Antigravity CLI', 'service', 'gemini', 'username', 'antigravity'];
      execFileSync('secret-tool', store, {...opts, input: JSON.stringify(changed)});
    }
    report.checks.push('Workspace authentication is writable independently of private authentication');
    const privatePaths = agent === 'claude' ? ['.claude/factory-placeholders.json', '.claude-proxy/mitmproxy-ca.pem'] : ['.factory-agy/placeholders.json', '.agy-proxy/mitmproxy-ca.pem'];
    for (const name of [...privatePaths, '/run/primary-input/settings.json', '/run/podman/podman.sock']) {
      try {
        await fs.readFile(name.startsWith('/') ? name : path.join(os.homedir(), name));
        throw new Error('Private path readable: ' + name);
      } catch (error) {
        if (!['ENOENT', 'EACCES'].includes(error.code)) throw error;
      }
    }
    report.checks.push('Private credential metadata, CA key and container socket inaccessible');
  } catch (error) { report.errors.push(String(error)); }
  await fs.writeFile('/qa/credential-attack-result.json', JSON.stringify(report, null, 2));
};
