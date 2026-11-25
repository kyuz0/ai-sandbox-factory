"""Render synthetic profile editing, validation and reviewed application states."""
import argparse
import asyncio
import copy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Collapsible, Input, Select
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.profile_edit_ui import EditProfile, ReviewProfile
from ai_sandbox_factory.chat_history_ui import DeleteSandbox
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, CopySandboxHelp, CopyWebAccess, CreateFromSandbox, MoreActions, SettingsForm, copy_draft
from test_profile_edit_ui import record, review
from test_tui import FakeService
from tui_geometry import check_checkbox_alignment, check_dropdown_layout, check_input_layout


async def capture(output,size,only=None):
    with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
            patch.dict(os.environ,SANDBOX_REGISTRY_DIR=str(Path(tmp)/'library')):
        dashboard=record()
        dashboard.update(profile='Default Developer Sandbox',workspace='/home/example/project',engine='podman',proxy='running')
        app=Cockpit(Path(tmp),FakeService([dashboard]))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            async def shot(name):
                await pilot.pause()
                await pilot.wait_for_scheduled_animations()
                if only and name not in only:
                    return
                if name in ('profile-sharing-copy-error','profile-sharing-copy-long-error'):
                    async def settled():
                        while any(button.has_class('-active') for button in app.screen.query(Button)):
                            await pilot.pause(.01)
                    await asyncio.wait_for(settled(),5)
                check_input_layout(app.screen)
                check_checkbox_alignment(app.screen)
                check_dropdown_layout(app.screen)
                for button in app.screen.query(Button):
                    if button.display and button.region.height:
                        assert button.region.height == 1,(name,button.id,button.region)
                        if button.id in ('copy-cancel','copy-continue','copy-web-cancel','copy-web-apply'):
                            assert 0 <= button.region.y < button.region.bottom <= size[1],(name,button.id,button.region)
                if name in ('profile-sharing-copy-independent','profile-sharing-copy-shared'):
                    fields=app.screen.query_one('#copy-sandbox-fields')
                    last=app.screen.query_one('#copy-credentials')
                    assert fields.content_region.bottom-last.region.bottom <= 1,(name,fields.content_region,last.region)
                if name=='profile-sharing-copy-preparing':
                    button=app.screen.query_one('#copy-continue',Button)
                    assert 'Preparing…' in button.render_line(0).text,(button.region,button.render_line(0).text)
                if name=='profile-sharing-copy-error':
                    fields=app.screen.query_one('#copy-sandbox-fields')
                    error=app.screen.query_one('#copy-error')
                    assert fields.content_region.contains_region(error.region),(name,fields.content_region,error.region)
                if name=='profile-web-copy-keys':
                    assert not app.screen.query_one('#copy-web-keys',Checkbox).disabled
                (output/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            for editable in (False,True):
                row=record(editable)
                await app.push_screen(MoreActions(row))
                await shot('profile-edit-actions-'+('attached' if editable else 'initial'))
                await app.pop_screen()
            page=EditProfile(record(),destination='/home/example/sandbox-profiles/example')
            await app.push_screen(page)
            await shot('profile-edit-folder')
            page.query_one('#profile-edit-folder',Input).value=''
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-folder-error')
            page.query_one('#profile-edit-folder',Input).value='/home/example/sandbox-profiles/example'
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-confirm-running')
            await pilot.press('escape')
            await app.pop_screen()
            page=EditProfile(record(running=False),destination='/home/example/sandbox-profiles/example')
            await app.push_screen(page)
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-confirm-stopped')
            await pilot.press('escape')
            await app.pop_screen()
            for error in ('',"Cannot review the profile for example (exit 1): profile.json: line 4, column 8: expected a comma. Fix the JSON, then choose Review changes again."):
                await app.push_screen(EditProfile(record(True),error=error))
                await shot('profile-edit-attached-error' if error else 'profile-edit-attached')
                await app.pop_screen()
            for changed in (False,True):
                await app.push_screen(ReviewProfile(record(True),review(changed)))
                await shot('profile-edit-review' if changed else 'profile-edit-no-changes')
                if changed:
                    app.screen.query_one('#profile-edit-diff').scroll_visible(top=True,animate=False)
                    await shot('profile-edit-review-diff')
                    app.screen.query_one('#profile-review-apply',Button).press()
                    await shot('profile-edit-apply-confirm')
                    await pilot.press('escape')
                await app.pop_screen()
            information=record(True)
            changed = {**review(), 'added_services':['analytics'], 'replaced_services':['database'], 'removed_services':['reporting'],
                       'changes':['Changed: profile.json','Changed: services/database/Dockerfile','Removed: services/reporting/Dockerfile'],
                       'retained_volumes':['pi-sandbox-factory-example-profile-data-0123456789abcdef',
                                           'pi-sandbox-factory-example-profile-data-fedcba9876543210']}
            await app.push_screen(ReviewProfile(record(True),changed))
            await shot('profile-services-change-review')
            app.screen.query_one('#profile-edit-diff').scroll_visible(top=True,animate=False)
            await shot('profile-services-change-diff')
            app.screen.query_one('#profile-review-apply',Button).press()
            await shot('profile-services-change-confirm')
            await pilot.press('escape')
            await app.pop_screen()
            for replace in (False,True):
                variant = 'replace-only' if replace else 'package-only'
                separate = {**review(), 'added_services':[], 'removed_services':[],
                            'replaced_services':['database'] if replace else [],
                            'retained_volumes':['pi-sandbox-factory-example-profile-data-0123456789abcdef'] if replace else []}
                await app.push_screen(ReviewProfile(record(True),separate))
                await shot('profile-services-'+variant+'-review')
                app.screen.query_one('#profile-review-apply',Button).press()
                await shot('profile-services-'+variant+'-confirm')
                await pilot.press('escape')
                await app.pop_screen()
            volume = 'pi-sandbox-factory-example-profile-data-0123456789abcdef'
            await app.push_screen(DeleteSandbox('example',has_config=True,services=['database'],retained_volumes=[volume]))
            await shot('profile-services-retained-delete')
            app.screen.query_one(Collapsible).collapsed = False
            await shot('profile-services-retained-delete-commands')
            await pilot.press('escape')
            await app.push_screen(DeleteSandbox('example',has_config=True,retained_volumes=[volume]))
            await shot('profile-services-retained-only-delete')
            await pilot.press('escape')
            information['config']['profile']=sandbox.load_profile('developer-postgres')
            with patch('ai_sandbox_factory.profile_runtime.applied',return_value={'path':'/synthetic/applied'}), \
                    patch('ai_sandbox_factory.profile_services.status',return_value=[{'name':'database','running':True}]):
                await app.push_screen(AppliedProfile(information,Path(tmp)))
                await app.workers.wait_for_complete()
                await shot('profile-edit-information')
                await app.pop_screen()
            shared=record(True)
            shared['config']['options']={'web_access':{'providers':{'exa':{'has_key':True}}}}
            shared['shared_profiles']=['source','another-sandbox']
            plain=copy.deepcopy(shared)
            plain['name']='plain'
            plain['config'].pop('editable_profile')
            no_keys=copy.deepcopy(shared)
            no_keys['name']='without-keys'
            no_keys['config']['options']={}
            source=Path(tmp)/shared['name']
            source.mkdir()
            (source/'allowlist.txt').write_text('')
            chooser=CreateFromSandbox([shared,plain,no_keys])
            await app.push_screen(chooser)
            await shot('profile-sharing-copy-independent')
            chooser.query_one('#copy-information',Button).press()
            await shot('profile-sharing-copy-help')
            app.screen.query_one('#copy-help-body').scroll_end(animate=False)
            await shot('profile-sharing-copy-help-bottom')
            await pilot.press('escape')
            database=copy.deepcopy(shared)
            database['config']['profile']=sandbox.load_profile('developer-postgres')
            await app.push_screen(CopySandboxHelp(database))
            app.screen.query_one('#copy-help-body').scroll_end(animate=False)
            await shot('profile-sharing-copy-database-help')
            await pilot.press('escape')
            chooser.query_one('#copy-source',Select).expanded=True
            await shot('profile-sharing-copy-sources')
            chooser.query_one('#copy-source',Select).expanded=False
            chooser.query_one('#copy-source',Select).focus()
            chooser.query_one('#copy-share-profile',Checkbox).value=True
            chooser.query_one('#copy-share-profile',Checkbox).focus()
            await shot('profile-sharing-copy-shared')
            chooser.query_one('#copy-keys',Checkbox).value=True
            for selector in ('#copy-source','#copy-share-profile','#copy-keys','#copy-information','#copy-continue'):
                chooser.query_one(selector).disabled=True
            chooser.query_one('#copy-continue',Button).label='Preparing…'
            await shot('profile-sharing-copy-preparing')
            for selector in ('#copy-source','#copy-share-profile','#copy-keys','#copy-information','#copy-continue'):
                chooser.query_one(selector).disabled=False
            chooser.query_one('#copy-continue',Button).label='Continue'
            def fail_keys(name):
                raise OSError('fixture I/O failure')
            app.service.search_keys=fail_keys
            chooser.query_one('#copy-continue',Button).press()
            await app.workers.wait_for_complete()
            await pilot.wait_for_scheduled_animations()
            assert 'Cannot prepare a copy of example while copying Web Search API keys: OSError:' in str(chooser.query_one('#copy-error').render())
            await shot('profile-sharing-copy-error')
            def fail_long_keys(name):
                raise OSError('fixture diagnostic.\n'+'\n'.join('Diagnostic line '+str(i) for i in range(60)))
            app.service.search_keys=fail_long_keys
            chooser.query_one('#copy-continue',Button).press()
            await app.workers.wait_for_complete()
            await pilot.wait_for_scheduled_animations()
            await shot('profile-sharing-copy-long-error')
            chooser.query_one('#copy-error').update('')
            chooser.query_one('#copy-source',Select).value='plain'
            chooser.query_one('#copy-source',Select).focus()
            await shot('profile-sharing-copy-unavailable')
            chooser.query_one('#copy-information',Button).press()
            await shot('profile-sharing-copy-plain-help')
            await pilot.press('escape')
            chooser.query_one('#copy-source',Select).value='without-keys'
            chooser.query_one('#copy-source',Select).focus()
            await shot('profile-sharing-copy-no-keys')
            stopped=copy.deepcopy(shared)
            stopped.update(name='stopped',dev='stopped')
            chooser.records['example']['dev']='stopped'
            chooser.query_one('#copy-source',Select).value='example'
            chooser.query_one('#copy-source',Select).focus()
            await shot('profile-sharing-copy-stopped')
            await app.pop_screen()
            native=copy.deepcopy(shared)
            native['config']['primary_agent']='codex'
            await app.push_screen(CopySandboxHelp(native))
            await shot('profile-sharing-copy-native-help')
            await pilot.press('escape')
            shared['dev']='running'
            web=CopyWebAccess([shared,no_keys,stopped])
            await app.push_screen(web)
            await shot('profile-web-copy-keys')
            web.query_one('#copy-web-source',Select).expanded=True
            await shot('profile-web-copy-sources')
            web.query_one('#copy-web-source',Select).expanded=False
            web.query_one('#copy-web-source',Select).focus()
            web.query_one('#copy-web-keys',Checkbox).value=True
            web.query_one('#copy-web-error').update('Cannot copy Web Search API keys from example (exit 1): fixture I/O failure. Try copying again, or leave Include Web Search API keys unchecked.')
            await shot('profile-web-copy-error')
            web.query_one('#copy-web-error').update('')
            web.query_one('#copy-web-source',Select).value='without-keys'
            web.query_one('#copy-web-source',Select).focus()
            await shot('profile-web-copy-no-keys')
            web.query_one('#copy-web-source',Select).value='stopped'
            web.query_one('#copy-web-source',Select).focus()
            await shot('profile-web-copy-stopped')
            await app.pop_screen()
            for reuse in (False,True):
                draft=copy_draft(shared,Path(tmp))
                draft.update(name='new-project',share_editable_profile=reuse)
                form=SettingsForm(draft)
                await app.push_screen(form)
                form.query_one('.form').scroll_home(animate=False)
                await shot('profile-sharing-review-shared' if reuse else 'profile-sharing-review-independent')
                await app.pop_screen()
            await app.push_screen(EditProfile(shared))
            await shot('profile-sharing-edit')
            await app.pop_screen()
            prepared=review()
            prepared['shared_with']=shared['shared_profiles']
            await app.push_screen(ReviewProfile(shared,prepared))
            await shot('profile-sharing-apply-review')
            app.screen.query_one('#profile-review-apply',Button).press()
            await shot('profile-sharing-apply-confirm')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--mono',action='store_true')
    parser.add_argument('--only',nargs='+',help='Capture only these named states')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.mono:
        os.environ['NO_COLOR']='1'
    else:
        os.environ.pop('NO_COLOR',None)
    os.environ['TEXTUAL_COLOR_SYSTEM']='truecolor'
    for size in ((80,24),(120,40),(220,50)):
        asyncio.run(capture(args.output,size,args.only))


if __name__=='__main__':
    main()
