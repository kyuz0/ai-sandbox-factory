export function knowledgeCapabilities(pi) {
  const commands = pi.getCommands();
  return {
    skill: commands.some(command => command.source === 'skill' && command.name === 'skill:local-knowledge'),
    canIndex: commands.some(command => command.name === 'knowledge-index'),
    canSearch: pi.getActiveTools().includes('knowledge_search'),
  };
}

export default function localKnowledgeStatus(pi) {
  pi.on('before_agent_start', ({ systemPrompt }) => {
    const { skill, canIndex, canSearch } = knowledgeCapabilities(pi);
    if (!skill) return;
    const semanticHelp = canSearch && canIndex
      ? 'Semantic search is on. The user can run /knowledge-index to choose a file or folder.'
      : canSearch ? 'Semantic search is on for previously indexed sources.'
      : 'Semantic search is unavailable in this session.';
    return { systemPrompt: systemPrompt + '\n\n' +
      'The Local Knowledge skill is active. Keyword search works on accessible files. ' + semanticHelp };
  });
}
