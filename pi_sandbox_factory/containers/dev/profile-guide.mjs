import { readFileSync } from 'node:fs';
import { knowledgeCapabilities } from './local-knowledge-status.mjs';

function readObject(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return {};
  }
}

export default function profileGuide(pi) {
  const guide = readObject('/opt/pi-security/config/profile-guide.json');
  if (!guide.text) return;

  const options = readObject('/opt/sandbox/options.json');
  const webOff = guide.id === 'deep-research' && !options.web_access?.enabled;
  const webHelp = 'Web Search is off. Turn it on in Factory under Network and Web Search to research the web.';
  const firstStep = guide.text.split('\n\n', 1)[0].replace(/\s+/g, ' ');
  const widget = 'factory-profile-guide';
  let showing = false;

  function hideHint(ctx) {
    if (!showing) return;
    showing = false;
    ctx.ui.setWidget(widget, undefined);
  }

  pi.on('session_start', (event, ctx) => {
    if (!ctx.hasUI || event.reason !== 'startup') return;
    if (ctx.sessionManager.getEntries().some(entry => entry.type === 'message' && entry.message.role === 'user')) return;
    ctx.ui.setWidget(widget, [guide.title, webOff ? 'Web Search is off.' : firstStep, 'Type /profile-guide for first steps.']);
    showing = true;
  });
  pi.on('before_agent_start', (_event, ctx) => hideHint(ctx));

  pi.registerCommand('profile-guide', {
    description: `Get started with ${guide.title}`,
    handler: (_args, ctx) => {
      hideHint(ctx);
      const parts = [guide.title, guide.text];
      if (webOff) parts.push(webHelp);
      const { canIndex, canSearch } = knowledgeCapabilities(pi);
      if (canIndex && canSearch && (guide.id === 'local-knowledge' || guide.id === 'deep-research')) {
        parts.push('Semantic search is on. Ask Pi to index a file or folder, then ask questions about its contents.');
      }
      ctx.ui.notify(parts.join('\n\n'), 'info');
    },
  });
}
