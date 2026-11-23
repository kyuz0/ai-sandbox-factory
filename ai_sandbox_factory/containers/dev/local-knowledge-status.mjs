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
      ? 'Semantic search is on. When the user asks to index a file or folder, use knowledge_add. Use knowledge_update to refresh an existing index. The configured embedding service is trusted for processing these documents.'
      : canSearch ? 'Semantic search is on for previously indexed sources.'
      : 'Semantic search is unavailable in this session.';
    return { systemPrompt: systemPrompt + '\n\n' +
      'The Local Knowledge skill is active. Keyword search works on accessible files. ' + semanticHelp };
  });
}
