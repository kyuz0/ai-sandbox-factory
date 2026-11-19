import { loadSessionNamingConfig, sessionNamingAgentDir } from './config.ts';
import { registerSessionNaming } from './extensions/index.ts';

export default function sessionNaming(pi) {
  registerSessionNaming(pi, {
    loadConfig: () => loadSessionNamingConfig(sessionNamingAgentDir()),
    // Naming falls back to the prompt. Keep raw provider errors out of editor logs.
    warn: () => {},
  });
  // FrostPi uses Pi's title notification to update a chat that is already open.
  pi.on('session_info_changed', ({ name }, ctx) => {
    if (name) ctx.ui.setTitle(name);
  });
}
