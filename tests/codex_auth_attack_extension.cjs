// Disposable hostile-extension fixture. Receives no private credentials or hashes.
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

exports.activate = async () => {
  const report = {checks: [], errors: []};
  try {
    const authPath = path.join(os.homedir(), '.codex/auth.json');
    const checkAuth = (data, label) => {
      const tokens = JSON.parse(data).tokens;
      for (const name of ['access_token', 'refresh_token', 'id_token']) {
        const value = tokens[name];
        if (typeof value !== 'string' || !/(?:^factory-codex-|\.factory-)[a-f0-9]{32}$/.test(value)) {
          throw new Error(label + ': ' + name + ' is not a Factory placeholder');
        }
      }
      report.checks.push(label);
    };
    const original = await fs.readFile(authPath);
    checkAuth(original, 'Node filesystem reads contain placeholders only');
    checkAuth(Buffer.from(await vscode.workspace.fs.readFile(vscode.Uri.file(authPath))),
      'VS Code filesystem reads contain placeholders only');
    await vscode.workspace.fs.writeFile(vscode.Uri.file(authPath), Buffer.concat([original, Buffer.from('\n')]));
    report.checks.push('VS Code wrote the workspace auth file; host test verifies private auth is unchanged');
    for (const name of [
      '.codex/factory-placeholders.json', '.codex-proxy/mitmproxy-ca.pem',
      '/run/primary-input/settings.json', '/run/podman/podman.sock',
    ]) {
      const target = name.startsWith('/') ? name : path.join(os.homedir(), name);
      try {
        await fs.readFile(target);
        throw new Error('Private path was readable: ' + name);
      } catch (error) {
        if (!['ENOENT', 'EACCES'].includes(error.code)) throw error;
      }
    }
    report.checks.push('Private credential storage, CA key and container-control socket are inaccessible');
  } catch (error) {
    report.errors.push(String(error));
  }
  await fs.writeFile('/qa/credential-attack-result.json', JSON.stringify(report, null, 2));
};
