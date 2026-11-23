import { getKnowledgeToolDefinitions } from "./node_modules/pi-knowledge/dist/index.js";
import { indexingTool } from "./index-policy.mjs";

const searchTools = ["knowledge_search", "knowledge_symbol_search", "knowledge_status", "knowledge_show"];
const searchModes = ["fast", "hybrid", "semantic"];

export default function localKnowledge(pi) {
  if (!process.env.PI_KNOWLEDGE_EMBEDDING?.startsWith("openai:") ||
      !process.env.PI_KNOWLEDGE_EMBEDDING_BASE_URL || !process.env.PI_KNOWLEDGE_EMBEDDING_API_KEY) {
    throw new Error("Open Document search → Configure… in Factory and enter an embedding server and model.");
  }
  const definitions = new Map(getKnowledgeToolDefinitions().map(tool => [tool.name, tool]));
  for (const name of ["knowledge_add", "knowledge_update"]) {
    const tool = indexingTool(definitions.get(name));
    definitions.set(name, tool);
    pi.registerTool(tool);
  }
  for (const name of searchTools) {
    const { approval: _approval, promptGuidelines: _guidelines, ...tool } = definitions.get(name);
    if (name === "knowledge_search") {
      tool.parameters.properties.mode = { anyOf: searchModes.map(mode => ({ const: mode })) };
      const execute = tool.execute;
      tool.execute = (id, params, signal, update, ctx) =>
        execute(id, { ...params, mode: searchModes.includes(params.mode) ? params.mode : "hybrid" }, signal, update, ctx);
    }
    pi.registerTool(tool);
  }

  async function indexSource(tool, parameters, context) {
    try {
      const result = await definitions.get(tool).execute(tool, parameters(), undefined, undefined, context);
      context.ui.notify(result.content.map(item => item.text).join("\n"), result.isError ? "error" : "info");
    } catch (error) {
      context.ui.notify(error.message, "error");
    }
  }

  // Slash commands remain available alongside the agent's indexing tools.
  pi.registerCommand("knowledge-index", {
    description: "Index a file or folder from the workspace or a mounted folder",
    handler: (args, context) => indexSource("knowledge_add", () => {
      if (!args.trim()) throw new Error("Enter a file or folder after /knowledge-index.");
      return { source: args.trim() };
    }, context),
  });
  pi.registerCommand("knowledge-refresh", {
    description: "Refresh an indexed file or folder by its knowledge base name",
    handler: (args, context) => indexSource("knowledge_update", () => {
      if (!args.trim()) throw new Error("Enter a knowledge base name after /knowledge-refresh.");
      return { target: args.trim() };
    }, context),
  });
}
