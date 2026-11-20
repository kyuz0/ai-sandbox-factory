import { readFileSync } from 'node:fs';

const selection = '/opt/pi-security/config/sandbox-system-prompt.json';

export default function systemPrompt(pi) {
  pi.on('before_agent_start', ({ systemPrompt }) => {
    let saved;
    try {
      saved = JSON.parse(readFileSync(selection, 'utf8'));
    } catch (error) {
      if (error.code === 'ENOENT') return;
      throw error;
    }
    if (!saved?.text) return;
    const addendum = '\n\n' + saved.text;
    if (!systemPrompt.includes(addendum)) return { systemPrompt: systemPrompt + addendum };
  });
}
