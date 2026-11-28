"""Browser form, asynchronous-result and agent-visibility checks without an engine."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from playwright.async_api import async_playwright, expect
from ai_sandbox_factory import browser_shell, settings_controls
from ai_sandbox_factory.sandbox_options import options, write_json


async def check(output):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='factory-toolbar-controls-') as folder:
        state=Path(folder)/'example'; state.mkdir()
        config={'name':'example','primary_agent':'pi','network':'allowlist','subagents':False,'options':options()}
        write_json(state/'config.json',config)
        (state/'allowlist.txt').write_text('example.test:443')
        shell=browser_shell.Shell(state)
        current={'name':'example','network':'allowlist','controls':settings_controls.describe(config,state),'editor':None,'job':None}
        async def snapshot(): return copy.deepcopy(current)
        shell.snapshot=snapshot
        server=await asyncio.start_server(shell.handle,'127.0.0.1',0)
        shell.url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            page=await browser.new_page(viewport={'width':1200,'height':800})
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            try:
                await page.goto(shell.pair())
                await page.locator('#handle').click()
                await page.locator('#settings').click()
                for agent in ('pi','codex','claude','agy'):
                    config['primary_agent']=agent
                    current['controls']=settings_controls.describe(config,state)
                    # Poll only delivers state; a deliberate reload also checks
                    # pairing survives within the same top-level browser tab.
                    await page.reload()
                    await page.locator('#handle').click()
                    await page.locator('#settings').click()
                    await page.locator('[data-section=approvals]').click()
                    key='approval' if agent=='pi' else 'native_approval'
                    await expect(page.locator('#'+key)).to_be_visible()
                    await expect(page.locator('#protect_credentials')).to_have_count(1 if agent=='pi' else 0)
                    await page.screenshot(path=str(output/(agent+'-approvals.png')))
                    if agent=='pi':
                        label=await page.locator('label[for=protect_credentials]').inner_text()
                        await page.locator('#protect_credentials').uncheck()
                        assert await page.locator('label[for=protect_credentials]').inner_text()==label
                        await page.locator('#help summary').click()
                        await expect(page.locator('#help-content')).to_contain_text('Blocks file tools')
                    await page.locator('#section-back').click()
                    await expect(page.locator('[data-section=features]')).to_have_count(1 if agent=='pi' else 0)
                    await expect(page.locator('[data-section=web-search]')).to_have_count(0 if agent=='pi' else 1)
                print('PASS current-agent controls, stable checkbox copy, contextual help and reload pairing',flush=True)

                config['primary_agent']='pi'
                current['controls']=settings_controls.describe(config,state)
                await page.reload()
                await page.locator('#handle').click()
                await page.locator('#settings').click()
                await page.locator('[data-section=approvals]').click()
                # Another interface changes an untouched choice after this form
                # opens. Only the user's edited field may be sent for review.
                config['options']['inner_sandbox']=False
                current['controls']=settings_controls.describe(config,state)
                received=[]
                async def capture(body):
                    received.append(body)
                    return {'id':'partial','runtime_effect':'replace','notes':[]}
                shell.preview=capture
                await page.locator('#approval').select_option('manual')
                await page.locator('#apply').click()
                await expect(page.locator('#review')).to_be_visible()
                assert received==[{'section':'approvals','values':{'approval':'manual'}}],received
                print('PASS an open form sends only edited fields, preserving changes made elsewhere',flush=True)
                from ai_sandbox_factory import network_groups
                config['options']['website_groups']=list(network_groups.catalog())[:2][::-1]
                current['controls']=settings_controls.describe(config,state)
                await page.reload()
                await page.locator('#handle').click()
                await page.locator('#settings').click()
                await page.locator('[data-section=destinations]').click()
                await expect(page.locator('#apply')).to_be_disabled()
                print('PASS saved category order does not create a spurious change',flush=True)
                await page.locator('#section-back').click()
                await page.locator('[data-section=network]').click()
                await page.locator('#network').select_option('unrestricted')
                entered,release=asyncio.Event(),asyncio.Event()
                async def delayed(body):
                    entered.set(); await release.wait()
                    raise ValueError('Synthetic upstream preview failed (exit 7): repository unavailable. Retry.')
                shell.preview=delayed
                await page.locator('#apply').click()
                await asyncio.wait_for(entered.wait(),10)
                await page.locator('#section-back').click()
                await page.locator('[data-section=resources]').click()
                await page.locator('#memory').fill('4g')
                release.set()
                await expect(page.locator('#memory')).to_have_value('4g')
                await expect(page.locator('#review')).to_be_hidden()
                await expect(page.locator('#status')).not_to_contain_text('repository unavailable')
                print('PASS a dismissed preview cannot overwrite another form or its feedback',flush=True)

                async def failed(body): raise ValueError('Synthetic upstream preview failed (exit 7): repository unavailable. Retry.')
                shell.preview=failed
                await page.locator('#apply').click()
                await expect(page.locator('#status')).to_contain_text('exit 7')
                await expect(page.locator('#memory')).to_have_value('4g')
                async def good(body):
                    return {'id':'review','revision':'synthetic','title':'Resources','notes':['Changes the memory limit.'],
                            'runtime_effect':'replace','summary':[],'changes':{'memory':'4g'}}
                shell.preview=good
                await page.locator('#apply').click()
                await expect(page.locator('#review')).to_be_visible()
                with patch('ai_sandbox_factory.settings_plan.check'), patch.object(browser_shell,'start_job') as start:
                    job={'id':'job','status':'running','title':'Resources','effect':'replace'}
                    start.return_value=job
                    current['job']=job
                    await page.locator('#apply').click()
                    await expect(page.locator('#status')).to_contain_text('applying')
                    await page.locator('#cancel').click()
                    await page.reload()
                    await page.locator('#handle').click()
                    await page.locator('#settings').click()
                    await expect(page.locator('#status')).to_contain_text('applying')
                    await page.locator('#cancel').click()
                    current['job']={**job,'status':'failed','error':'Candidate startup failed (exit 9). Previous sandbox restored.',
                                    'output':'Synthetic readiness check failed: connection refused.'}
                    await expect(page.locator('#status')).to_contain_text('Previous sandbox restored',timeout=10000)
                    await expect(page.locator('#drawer')).to_be_visible()
                    await page.locator('#job-output summary').click()
                    await expect(page.locator('#job-output pre')).to_contain_text('connection refused')
                    await page.screenshot(path=str(output/'rollback-error.png'))
                    await page.reload()
                    await page.locator('#handle').click()
                    await page.locator('#settings').click()
                    await expect(page.locator('#status')).to_have_text('')
                print('PASS preview failure/retry, accepted-job reload and actionable rollback diagnostics',flush=True)
                assert not errors,errors
            finally:
                await browser.close()
                server.close(); await server.wait_closed()
                await asyncio.gather(*list(shell.tasks))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(check(parser.parse_args().output))
