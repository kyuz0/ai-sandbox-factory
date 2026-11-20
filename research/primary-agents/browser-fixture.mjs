// Offline research fixture. The only tool opens a fixed synthetic document.
import { createInterface } from 'node:readline';
import { writeFile, access } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';
import { SandboxBrowser } from '/opt/betterwright/browser.mjs';

const label = process.argv[2];
const browser = new SandboxBrowser();
const tool = { name: 'view_fixture', description: 'Open the local browser fixture', inputSchema: {type: 'object', properties: {}} };
try {
  for await (const line of createInterface({input: process.stdin})) {
    const request = JSON.parse(line);
    if (request.id === undefined) continue;
    let result = {};
    if (request.method === 'initialize') {
      result = {protocolVersion: request.params.protocolVersion, capabilities: {tools: {}}, serverInfo: {name:'factory',version:'0'}};
    } else if (request.method === 'tools/list') {
      result = {tools: [tool]};
    } else if (request.method === 'tools/call' && request.params.name === tool.name) {
      const page = await browser.run("await page.setContent('<title>Factory fixture</title><button onclick=\"this.textContent=1\">0</button>'); await page.locator('button').click(); return await page.locator('button').textContent()");
      if (!page.ok || page.result !== '1') throw Error(JSON.stringify(page));
      await writeFile('/tmp/view-' + label, await browser.viewerLink(), {mode:0o600});
      for (let n=0; n<300; n++) {
        if (await access('/tmp/checked-' + label).then(()=>true,()=>false)) break;
        await sleep(100);
      }
      result = {content:[{type:'text',text:'The visible fixture button now reads 1.'}]};
    } else if (request.method === 'resources/list') result = {resources:[]};
    else if (request.method === 'resources/templates/list') result = {resourceTemplates:[]};
    process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request.id,result})+'\n');
  }
} finally { await browser.close(); }
