import { realpathSync, statSync } from 'node:fs';
import { basename, resolve } from 'node:path';

export function sourceAccess(context = {}, environment = process.env) {
  const allowedRoots = JSON.parse(environment.SANDBOX_KNOWLEDGE_ROOTS ?? '[]');
  if (!Array.isArray(allowedRoots) || !allowedRoots.length ||
      allowedRoots.some(root => typeof root !== 'string' || !root.trim())) {
    throw new Error('No workspace or mounted folders are available for indexing.');
  }
  return { ...context, filesystemAccess: { allowedRoots, allowNetwork: false } };
}

export function localSource(value, cwd = process.cwd()) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('Choose a file or folder to index.');
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(value.trim())) {
    throw new Error('Local Knowledge indexes files in the workspace or mounted folders, not URLs.');
  }
  const source = realpathSync(resolve(cwd, value.trim()));
  if (/^(?:\.env(?:\..*)?|\.npmrc|\.netrc|id_[^.]+)$|\.(?:key|pem|env)(?:\..*)?$/i.test(basename(source))) {
    throw new Error('Choose a source file or folder without credentials.');
  }
  const info = statSync(source);
  if (!info.isFile() && !info.isDirectory()) throw new Error('Choose a regular file or folder.');
  return source;
}

export function indexingTool(definition) {
  const { approval: _approval, promptGuidelines: _guidelines, ...tool } = definition;
  if (tool.name === 'knowledge_add') {
    tool.description = 'Index a requested workspace file or mounted document folder for search. Sources stay unchanged; the index is saved privately. Uses the configured embedding service.';
    tool.promptGuidelines = [
      'When the user asks to index documents, call knowledge_add with the requested file or folder. Do not send the user to a slash command.',
      'Use one call for the requested folder. If its knowledge base already exists, use knowledge_update to refresh it.',
    ];
    tool.parameters = { ...tool.parameters, properties: {
      source: { type: 'string', description: 'File or folder in the workspace or a mounted folder' },
      name: definition.parameters.properties.name,
    }, required: ['source'], additionalProperties: false };
  } else {
    tool.description = 'Refresh an existing document index from its original source using the configured embedding service. Source files stay unchanged.';
  }
  tool.execute = (id, params, signal, update, context = {}) => {
    const access = sourceAccess(context);
    let request = { target: params.target };
    if (tool.name === 'knowledge_add') {
      const source = localSource(params.source, context.cwd);
      request = { source, name: params.name?.trim() || basename(source) };
    }
    return definition.execute(id, request, signal, update, access);
  };
  return tool;
}
