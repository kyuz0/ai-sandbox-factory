import { realpathSync, statSync } from "node:fs";
import { basename, resolve } from "node:path";
import { getKnowledgeToolDefinitions } from "./node_modules/pi-knowledge/dist/index.js";

const searchTools = ["knowledge_search", "knowledge_symbol_search", "knowledge_status", "knowledge_show"];
const searchModes = ["fast", "hybrid", "semantic"];

function sourceAccess() {
  const allowedRoots = JSON.parse(process.env.SANDBOX_KNOWLEDGE_ROOTS ?? "[]");
  if (!Array.isArray(allowedRoots) || !allowedRoots.length) {
    throw new Error("No workspace or mounted folders are available for indexing.");
  }
  return { filesystemAccess: { allowedRoots, allowNetwork: false } };
}

export default function localKnowledge(pi) {
  const definitions = new Map(getKnowledgeToolDefinitions().map(tool => [tool.name, tool]));
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
      const result = await definitions.get(tool).execute(tool, parameters(), undefined, undefined, sourceAccess());
      context.ui.notify(result.content.map(item => item.text).join("\n"), result.isError ? "error" : "info");
    } catch (error) {
      context.ui.notify(error.message, "error");
    }
  }

  // Indexing stays in user commands. Models can only search the corpus the user chose.
  pi.registerCommand("knowledge-index", {
    description: "Index a file or folder from the workspace or a mounted folder",
    handler: (args, context) => indexSource("knowledge_add", () => {
      if (!args.trim()) throw new Error("Enter a file or folder after /knowledge-index.");
      const source = realpathSync(resolve(context.cwd, args.trim()));
      if (/^(?:\.env(?:\..*)?|\.npmrc|\.netrc|id_[^.]+)$|\.(?:key|pem|env)(?:\..*)?$/i.test(basename(source))) {
        throw new Error("Choose a source file or folder without credentials.");
      }
      const info = statSync(source);
      if (!info.isFile() && !info.isDirectory()) throw new Error("Choose a regular file or folder.");
      return { source, name: basename(source) };
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
