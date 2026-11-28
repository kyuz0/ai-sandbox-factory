"""Host controls: real HTTP boundaries and shared CLI previews, no live sandbox."""
import asyncio
import json
import os
import fcntl
import subprocess
import socket
import time
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import browser_editor, browser_shell, editor_settings, operations, settings_plan
from ai_sandbox_factory.sandbox_options import write_json


class ShellCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='factory-shell-test-')
        self.addCleanup(self.temporary.cleanup)
        self.locks = Path(self.temporary.name)/'locks'
        self.locks.mkdir()
        lock_patch = patch.object(browser_shell.operation_locks,'private_runtime_dir',return_value=self.locks)
        lock_patch.start(); self.addCleanup(lock_patch.stop)
        def private_command(state,*args):
            code = ('import sys; from pathlib import Path; from ai_sandbox_factory import operation_locks; '
                    'root=Path(sys.argv.pop(1)); operation_locks.private_runtime_dir=lambda:root; '
                    'from ai_sandbox_factory.cli import main; main()')
            return [sys.executable,'-c',code,str(self.locks),'--state-dir',str(Path(state).parent),*args]
        command_patch = patch.object(browser_shell,'command',side_effect=private_command)
        command_patch.start(); self.addCleanup(command_patch.stop)
        self.state = Path(self.temporary.name) / 'example'
        self.state.mkdir()
        self.config = {'name': 'example', 'engine': 'podman', 'network': 'allowlist', 'options': {}, 'model_providers': []}
        write_json(self.state / 'config.json', self.config)
        (self.state / 'allowlist.txt').write_text('example.test:443\n')
        write_json(self.state / 'editor-address.json', {'address': '127.0.0.1', 'port': 12345})
        self.shell = browser_shell.Shell(self.state)
        self.server = await asyncio.start_server(self.shell.handle, '127.0.0.1', 0)
        self.host = f'127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        self.shell.url = 'http://' + self.host
        self.headers = {'Host': self.host, 'Origin': self.shell.url, 'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + self.shell.bearer}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await asyncio.gather(*list(self.shell.tasks))

    async def request(self, path='/state', body=None, *, headers=None, method='POST', raw=None):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        data = raw if raw is not None else json.dumps(body or {}).encode()
        fields = self.headers if headers is None else headers
        writer.write((f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{key}: {value}\r\n' for key,value in fields.items()) +
                      f'Content-Length: {len(data)}\r\n\r\n').encode() + data)
        await writer.drain()
        reply = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, content = reply.split(b'\r\n\r\n', 1)
        return int(head.split(b' ')[1]), head.decode(), content

    async def test_authenticated_state_and_static_response_headers(self):
        code, head, body = await self.request()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['network'], 'allowlist')
        self.assertNotIn(self.shell.bearer, body.decode())
        for route in ('/', '/panel.js', '/panel.css', '/brand.css', '/missing'):
            code, head, body = await self.request(route, method='GET', headers={'Host': self.host})
            self.assertEqual(code, 404 if route == '/missing' else 200)
            for expected in ("frame-ancestors 'none'", 'X-Frame-Options: DENY', 'Cache-Control: no-store',
                             'Cross-Origin-Opener-Policy: same-origin', 'Referrer-Policy: no-referrer'):
                self.assertIn(expected, head)
            self.assertNotIn(self.shell.bearer, body.decode())

    async def test_wrong_host_origin_cookie_and_bearer_cannot_control(self):
        for changes in ({'Host': 'evil.example'}, {'Origin': 'http://evil.example'}, {'Origin': ''},
                        {'Authorization': ''}, {'Authorization': 'Bearer wrong'},
                        {'Authorization': '', 'Cookie': 'code-server-session=synthetic'},
                        {'Content-Type': 'text/plain'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(changes=changes):
                code, head, body = await self.request(headers={**self.headers, **changes})
                self.assertIn(code, (400, 403))
                self.assertIn('error', json.loads(body))
                self.assertNotIn('Access-Control-Allow-Origin', head)
        self.assertEqual((await self.request(method='OPTIONS'))[0], 404)

    async def test_pairing_is_one_use_and_new_code_revokes_previous(self):
        old = self.shell.pair().split('pair=')[1]
        code = self.shell.pair().split('pair=')[1]
        headers = {key:value for key,value in self.headers.items() if key != 'Authorization'}
        self.assertEqual((await self.request('/pair', {'code':old}, headers=headers))[0], 403)
        status, _, body = await self.request('/pair', {'code':code}, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['token'], self.shell.bearer)
        self.assertEqual((await self.request('/pair', {'code':code}, headers=headers))[0], 403)

    async def test_real_cli_preview_and_revision_reject_changed_config(self):
        code, _, body = await self.request('/preview', {'section':'network','values':{'network':'unrestricted'}})
        self.assertEqual(code, 200, body)
        plan = json.loads(body)
        self.assertNotIn('changes',plan)
        self.assertEqual(self.shell.plan['changes'], {'network':'unrestricted','allowlist':''})
        self.assertIn('Clears the additional allowed destinations.', plan['notes'])
        write_json(self.state / 'config.json', {**self.config, 'memory':'4g'})
        with patch.object(browser_shell, 'start_job') as start:
            code, _, body = await self.request('/apply', {'plan':plan['id']})
            self.assertEqual(code, 400)
            self.assertIn('changed after review', json.loads(body)['error'])
            start.assert_not_called()

    async def test_noop_and_out_of_scope_fields_never_start_job(self):
        code, _, body = await self.request('/preview', {'section':'network','values':{'network':'allowlist'}})
        plan = json.loads(body)
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan['notes'], ['No network mode settings changed.'])
        with patch.object(browser_shell, 'start_job') as start:
            self.assertEqual((await self.request('/apply', {'plan':plan['id']}))[0], 200)
            self.assertEqual((await self.request('/apply', {'plan':plan['id'], 'network':'unrestricted'}))[0], 400)
            start.assert_not_called()
        for data in ({'network':'typo'}, {'network':[]}, {'name':'other','network':'unrestricted'}, {'command':'rm'}):
            self.assertEqual((await self.request('/preview', data))[0], 400)

    async def test_overlapping_preview_does_not_publish_old_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def preview(body):
            if body['values']['network'] == 'allowlist':
                started.set()
                await release.wait()
            return {'id':body['values']['network'],'notes':[],'title':'Network mode','runtime_effect':'replace'}
        with patch.object(self.shell, 'preview', side_effect=preview):
            old = asyncio.create_task(self.request('/preview', {'section':'network','values':{'network':'allowlist'}}))
            await started.wait()
            self.assertEqual((await self.request('/preview', {'section':'network','values':{'network':'unrestricted'}}))[0], 200)
            release.set()
            self.assertEqual((await old)[0], 400)
            self.assertEqual(self.shell.plan['id'], 'unrestricted')

    async def test_authentication_precedes_body_and_long_rules_have_no_artificial_cap(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        writer.write(f'POST /preview HTTP/1.1\r\nHost: {self.host}\r\nOrigin: {self.shell.url}\r\nContent-Type: application/json\r\nContent-Length: 999999\r\n\r\n'.encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(),2)
        self.assertIn(b'403',response)
        writer.close(); await writer.wait_closed()
        rules = '\n'.join(f'host-{i}.example.test:443' for i in range(1000))
        code, _, body = await self.request('/preview',{'section':'destinations','values':{'allowlist':rules}})
        self.assertEqual(code,200,body)
        self.assertEqual(self.shell.plan['changes']['allowlist'],rules)

    async def test_package_cli_preview_and_failure_leave_a_new_review_possible(self):
        code, _, body = await self.request('/preview',{'section':'packages','values':{'packages':'jq, ripgrep'}})
        self.assertEqual(code,200,body)
        plan=json.loads(body)
        self.assertEqual(plan['packages'],['jq','ripgrep'])
        with patch.object(browser_shell,'start_job',side_effect=OSError('disk full')):
            code, _, body = await self.request('/apply',{'plan':plan['id']})
        self.assertEqual(code,400)
        self.assertIn('disk full',json.loads(body)['error'])
        code, _, body = await self.request('/preview',{'section':'packages','values':{'packages':'jq'}})
        self.assertEqual(code,200,body)


class JobReceipt(unittest.TestCase):
    def test_package_failure_keeps_cause_without_nested_cli_error_prefixes(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)
            directory=browser_shell.directory(state)
            write_json(directory/'job.json',{'id':'synthetic','status':'running','title':'Install system packages'})
            write_json(directory/'request.json',{'operation':'install-system-packages','packages':['missing-package']})
            (directory/'job.log').write_text('Error: Update command exited with status 1: Error: APT could not install missing-package (exit 100).\n')
            with (directory/'job.lock').open('a+') as lock, \
                 patch.object(browser_shell.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
                browser_shell.run_job(state,'revision',os.dup(lock.fileno()))
            job=browser_shell.job_status(state)
            self.assertEqual(job['error'],'Install system packages failed (exit 1). APT could not install missing-package (exit 100).')
            self.assertIn('Update command exited',job['output'])

    def test_worker_death_does_not_report_completion_while_cli_child_runs(self):
        with tempfile.TemporaryDirectory(prefix='factory-job-receipt-') as folder:
            state=Path(folder)
            directory=browser_shell.directory(state)
            write_json(directory/'job.json',{'id':'synthetic','status':'running'})
            write_json(directory/'request.json',{'operation':'update'})
            child_code = ('import pathlib,time,sys; root=pathlib.Path(sys.argv[1]); '
                '(root/"started").write_text("ready"); '
                '\nwhile not (root/"release").exists(): time.sleep(.02)\n'
                '(root/"finished").write_text("done")')
            runner_code = ('import sys; from pathlib import Path; from ai_sandbox_factory import browser_shell; '
                'state=Path(sys.argv[1]); child=sys.argv[3]; '
                'browser_shell.command=lambda state,*args:[sys.executable,"-c",child,str(state)]; '
                'browser_shell.run_job(state,"revision",int(sys.argv[2]))')
            def milestone(name):
                deadline=time.monotonic()+10
                while not (state/name).exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue((state/name).exists(),name)
            with (directory/'job.lock').open('a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                runner=subprocess.Popen([sys.executable,'-c',runner_code,str(state),str(lock.fileno()),child_code],pass_fds=(lock.fileno(),))
            try:
                milestone('started')
                runner.terminate(); runner.wait(timeout=10)
                self.assertEqual(browser_shell.job_status(state)['status'],'running')
            finally:
                (state/'release').touch()
                if runner.poll() is None: runner.terminate(); runner.wait(timeout=10)
            milestone('finished')
            deadline=time.monotonic()+10
            while browser_shell.locked(directory/'job.lock') and time.monotonic()<deadline: time.sleep(.02)
            with patch.object(browser_shell.operation_locks,'in_use',return_value=False):
                status=browser_shell.job_status(state)
            self.assertEqual(status['status'],'interrupted')
            self.assertIn('stopped without reporting a result',status['error'])


class Framing(unittest.TestCase):
    def test_only_toolbar_iframe_entrypoints_are_added(self):
        for path in ('/', '/login?to=%2F', '/other'):
            for dest in ('iframe', 'document'):
                head = f'GET {path} HTTP/1.1\r\nHost: localhost:123\r\nSec-Fetch-Site: same-site\r\nSec-Fetch-Mode: navigate\r\nSec-Fetch-Dest: {dest}\r\n\r\n'.encode()
                if dest == 'iframe' and path != '/other':
                    browser_editor.validate_head(head, 'localhost:123', toolbar_navigation=True)
                else:
                    with self.assertRaises(ValueError):
                        browser_editor.validate_head(head, 'localhost:123', toolbar_navigation=True)

    def test_csp_keeps_other_directives_and_http_delimiters(self):
        result = browser_editor.frame_ancestors(['HTTP/1.1 200 OK', 'X-Frame-Options: SAMEORIGIN',
            "Content-Security-Policy: script-src 'self'; frame-ancestors *", '', ''], ['http://localhost:123'])
        self.assertEqual(result, ['HTTP/1.1 200 OK', "Content-Security-Policy: script-src 'self'; frame-ancestors 'self' http://localhost:123", '', ''])

    def test_editor_save_keeps_toolbar_address(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            write_json(state/'editor-address.json', {'toolbar_origin':'http://127.0.0.2:123'})
            editor_settings.remember_connection(state, {'address':''}, '127.0.0.2', 456)
            self.assertEqual(browser_shell.origin(state), 'http://127.0.0.2:123')


class OpenOwnership(unittest.TestCase):
    def test_foreground_waits_through_replacement_without_stopping_either_server(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_shell.operation_locks,'sandbox'), \
             patch.object(browser_editor,'runtime',return_value=None), \
             patch.object(browser_shell,'start',return_value=('http://example/#pair=synthetic',True)), \
             patch.object(browser_editor,'open_browser_editor'), \
             patch.object(browser_shell,'locked',side_effect=[True,True,False]), \
             patch.object(browser_editor,'active',side_effect=[False,True]), \
             patch.object(browser_shell.operation_locks,'in_use',return_value=True), \
             patch.object(browser_shell.time,'sleep'), \
             patch.object(browser_shell,'stop') as shell_stop, \
             patch.object(browser_editor,'stop') as editor_stop:
            browser_shell.open_editor({},Path(folder),open_tab=False)
            shell_stop.assert_not_called();editor_stop.assert_not_called()

    def test_foreground_interrupt_stops_under_lifecycle_lock(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_shell.operation_locks,'sandbox') as lock, \
             patch.object(browser_editor,'runtime',return_value=None), \
             patch.object(browser_shell,'start',return_value=('http://example/#pair=synthetic',True)), \
             patch.object(browser_editor,'open_browser_editor'), \
             patch.object(browser_shell,'locked',return_value=True), \
             patch.object(browser_editor,'active',return_value=True), \
             patch.object(browser_shell.operation_locks,'in_use',return_value=False), \
             patch.object(browser_shell.time,'sleep',side_effect=KeyboardInterrupt), \
             patch.object(browser_shell,'stop') as shell_stop, \
             patch.object(browser_editor,'stop') as editor_stop:
            browser_shell.open_editor({},Path(folder),open_tab=False)
            self.assertEqual(lock.call_count,2)
            shell_stop.assert_called_once();editor_stop.assert_called_once()

    def test_failed_repeat_open_keeps_existing_toolbar(self):
        with patch.object(browser_shell.operation_locks, 'sandbox'), \
             patch.object(browser_editor, 'runtime', return_value={'url':'http://example'}), \
             patch.object(browser_shell, 'start', return_value=('http://example/#pair=synthetic', False)), \
             patch.object(browser_shell, 'stop') as stop, \
             patch.object(browser_editor, 'open_browser_editor', side_effect=ValueError('already starting')):
            with self.assertRaisesRegex(ValueError, 'already starting'):
                browser_shell.open_editor({}, Path('/tmp/example'))
            stop.assert_not_called()

    def test_foreground_repeat_open_does_not_own_existing_editor(self):
        with patch.object(browser_shell.operation_locks, 'sandbox') as lock, \
             patch.object(browser_editor, 'runtime', return_value={'url':'http://example'}), \
             patch.object(browser_shell, 'start', return_value=('http://example/#pair=synthetic', False)), \
             patch.object(browser_shell, 'stop') as stop, \
             patch.object(browser_editor, 'open_browser_editor'), \
             patch.object(browser_editor, 'active') as active:
            browser_shell.open_editor({}, Path('/tmp/example'), open_tab=False)
            lock.assert_called_once()
            active.assert_not_called()
            stop.assert_not_called()

    def test_failed_first_open_cleans_only_new_toolbar(self):
        with patch.object(browser_shell.operation_locks, 'sandbox'), \
             patch.object(browser_editor, 'runtime', return_value=None), \
             patch.object(browser_shell, 'start', return_value=('http://example/#pair=synthetic', True)), \
             patch.object(browser_shell, 'stop') as stop, \
             patch.object(browser_editor, 'open_browser_editor', side_effect=ValueError('start failed')):
            with self.assertRaisesRegex(ValueError, 'start failed'):
                browser_shell.open_editor({}, Path('/tmp/example'))
            stop.assert_called_once()


class ServerLifecycle(unittest.TestCase):
    def test_https_shutdown_without_newer_server_abort_api(self):
        original=browser_shell.host_platform.python_command
        def command(*args):
            argv=original(*args)
            argv[3]=('import asyncio; '
                     'hasattr(asyncio.Server,"abort_clients") and delattr(asyncio.Server,"abort_clients"); '
                     +argv[3])
            return argv
        with patch.object(browser_shell.host_platform,'python_command',side_effect=command):
            self.test_https_shutdown_with_unfinished_handshake_and_idle_client()

    def test_https_shutdown_with_unfinished_handshake_and_idle_client(self):
        import ssl
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory(prefix='factory-toolbar-tls-') as folder:
            state=Path(folder)
            cert,key=state/'cert.pem',state/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                            '-subj','/CN=Disposable toolbar test','-keyout',str(key),'-out',str(cert)],
                           check=True,capture_output=True)
            editor_settings.save(state,{'address':'127.0.0.1','password_hash':editor_settings.password_hash('synthetic-tls'),
                                        'certificate':str(cert),'private_key':str(key)})
            clients=[]
            try:
                url,_=browser_shell.start(state)
                parsed=urlsplit(url)
                clients.append(socket.create_connection((parsed.hostname,parsed.port)))
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname=False
                context.verify_mode=ssl.CERT_NONE
                tls=context.wrap_socket(socket.create_connection((parsed.hostname,parsed.port)),server_hostname='localhost')
                clients.append(tls)
                tls.sendall(b'GET / HTTP/1.1\r\n')
                browser_shell.stop(state)
                self.assertFalse(browser_shell.locked(state/'browser-controls'/'server.lock'))
            finally:
                for client in clients: client.close()
                browser_shell.stop(state)

    def test_idle_http_and_ipc_connections_do_not_delay_shutdown(self):
        with tempfile.TemporaryDirectory(prefix='factory-toolbar-idle-') as folder:
            state=Path(folder)
            write_json(state/'editor-address.json',{'automatic_address':'127.0.0.1'})
            idle_ipc=socket.socket(socket.AF_UNIX)
            idle_http=None
            try:
                url,_=browser_shell.start(state)
                from urllib.parse import urlsplit
                parsed=urlsplit(url)
                idle_http=socket.create_connection((parsed.hostname,parsed.port))
                idle_http.sendall(b'GET / HTTP/1.1\r\n')
                idle_ipc.connect(str(browser_shell.host_platform.control_path(state/'browser-controls')))
                browser_shell.stop(state)
                self.assertFalse(browser_shell.locked(state/'browser-controls'/'server.lock'))
            finally:
                idle_ipc.close()
                if idle_http: idle_http.close()
                browser_shell.stop(state)

    def test_unreachable_control_reports_a_verified_process_recovery_step(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)
            path=browser_shell.directory(state)
            (path/'server.pid').write_text('12345')
            with (path/'server.lock').open('a+') as held, \
                 patch.object(browser_shell,'control',side_effect=ConnectionRefusedError('synthetic refused')):
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                for action in (browser_shell.start,browser_shell.stop):
                    with self.assertRaises(ValueError) as raised: action(state)
                    self.assertIn('synthetic refused',str(raised.exception))
                    self.assertIn('ps -p 12345',str(raised.exception))
                    self.assertIn('kill -TERM 12345',str(raised.exception))

    def test_port_collision_retains_actual_address_and_error(self):
        with tempfile.TemporaryDirectory(prefix='factory-toolbar-collision-') as folder, socket.socket() as listener:
            state=Path(folder)
            listener.bind(('127.0.0.1',0));listener.listen()
            port=listener.getsockname()[1]
            write_json(state/'editor-address.json',{'automatic_address':'127.0.0.1','toolbar_origin':f'http://127.0.0.1:{port}'})
            with self.assertRaises(ValueError) as raised: browser_shell.start(state)
            self.assertIn('address already in use',str(raised.exception).lower())
            self.assertIn(str(port),str(raised.exception))
            self.assertIn('Open again',str(raised.exception))
            self.assertFalse(browser_shell.locked(browser_shell.directory(state)/'server.lock'))

    def test_custom_address_does_not_replace_the_saved_automatic_address(self):
        with tempfile.TemporaryDirectory(prefix='factory-toolbar-address-') as folder:
            state=Path(folder)
            write_json(state/'editor-address.json',{'automatic_address':'127.0.0.2','address':'127.0.0.2','port':0})
            editor_settings.save(state,{'address':'127.0.0.1','password_hash':editor_settings.password_hash('synthetic-address-test')})
            try:
                url,started=browser_shell.start(state)
                self.assertTrue(started)
                self.assertIn('127.0.0.1',url)
                saved=json.loads((state/'editor-address.json').read_text())
                self.assertEqual(saved['automatic_address'],'127.0.0.2')
                browser_shell.stop(state)
                editor_settings.save(state,{})
                self.assertEqual(editor_settings.connection(state,editor_settings.load(state))[0],'127.0.0.2')
            finally:
                browser_shell.stop(state)
