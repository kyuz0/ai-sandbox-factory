import { readFileSync } from 'node:fs';
import http from 'node:http';
import { Type } from 'typebox';

const labels = new Map([
  ['accepted', 'The provider accepted the connection check.'],
  ['rejected', 'The provider refused authentication or access. Check the saved credential in Factory.'],
  ['not_configured', 'No usable stored credential is configured for this connection.'],
  ['unavailable', 'The provider connection could not be checked.'],
]);

function checkOnCompanion(provider, id, signal) {
  const deadline = AbortSignal.timeout(25000);
  return new Promise(resolve => {
    const request = http.get({
      socketPath: '/run/inference/proxy.sock',
      path: '/check/' + provider + '/' + id,
      signal: signal ? AbortSignal.any([deadline, signal]) : deadline,
    }, response => {
      if (response.statusCode !== 200) {
        response.destroy();
        resolve('unavailable');
        return;
      }
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 256) {
          resolve('unavailable');
          response.destroy();
        } else {
          chunks.push(chunk);
        }
      });
      response.on('end', () => {
        try {
          const { status } = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          resolve(labels.has(status) ? status : 'unavailable');
        } catch { resolve('unavailable'); }
      });
      response.on('error', () => resolve('unavailable'));
    });
    request.on('error', () => resolve('unavailable'));
  });
}

const result = (text, isError = false) => ({ content: [{ type: 'text', text }], isError });

function configuredChecks() {
  try {
    return JSON.parse(readFileSync('/opt/pi-security/config/provider-connection-checks.json', 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return {};
    throw error;
  }
}

export function registerProviderCheck(pi, approve) {
  if (!Object.keys(configuredChecks()).length) return;
  pi.registerTool({
    name: 'test_provider_connection',
    label: 'Test provider connection',
    description: 'Ask to test one managed provider connection using the saved credential. Returns only the connection result. Sends no chat, file contents or model generation request.',
    parameters: Type.Object({
      provider: Type.String({ description: 'ID of a managed provider configured in this sandbox.' }),
      justification: Type.String({ description: 'Why this connection needs to be checked.' }),
    }, { additionalProperties: false }),
    async execute(id, { provider, justification }, signal, _onUpdate, ctx) {
      const checks = configuredChecks();
      const check = Object.hasOwn(checks, provider) ? checks[provider] : undefined;
      if (!check || !/^[a-z][a-z0-9-]{0,254}$/.test(provider) || !/^[a-f0-9]{64}$/.test(check.id)) {
        return result('No connection check is available for this provider.', true);
      }
      if (!justification.trim()) return result('Explain why this provider connection needs to be checked.', true);
      const scope = 'Factory uses the saved credential to check this connection. Pi receives only the result.';
      const { reason } = await approve({
        ctx, signal, toolCallId: id, title: 'Test this provider connection?',
        details: ['Provider\n' + provider, 'Destination\n' + check.target, 'Why Pi needs this\n' + justification, scope],
        requestFacts: {
          surface: 'provider-connection',
          operation: 'check-stored-credential',
          toolName: 'test_provider_connection',
          destination: check.target,
          toolInputPreview: JSON.stringify({ provider, connectionCheck: check.id, scope, justification }),
          matchedPolicy: { decision: 'ask', rule: 'factory-provider-connection-check' },
        },
      });
      if (reason || signal?.aborted) return result(reason || 'The request was cancelled.', true);
      const status = await checkOnCompanion(provider, check.id, signal);
      if (signal?.aborted) return result('The connection check was cancelled.', true);
      return result(labels.get(status), status !== 'accepted');
    },
  });
}
