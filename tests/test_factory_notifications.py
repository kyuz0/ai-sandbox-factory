import email.message
import importlib.util
import io
import json
from pathlib import Path
import re
import unittest
import urllib.error

import yaml

CHECKOUT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('notify_factory_failure', CHECKOUT / 'scripts/notify_factory_failure.py')
notifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifier)

REPO = 'kyuz0/ai-sandbox-factory'
REPO_API_URL = f'https://api.github.com/repos/{REPO}'
ISSUE = 7
BOT = {'login': 'github-actions[bot]', 'type': 'Bot'}
HUMAN = {'login': 'someone-else', 'type': 'User'}
TOKEN = 'ghs_synthetic0123456789'
SHA = 'a' * 40


def make_run(run_id=1001, attempt=1, *, conclusion='failure', path='.github/workflows/images.yml',
             name='Factory image candidates', status='completed', created='2026-11-20T03:27:00Z', **extra):
    return {'id': run_id, 'run_attempt': attempt, 'name': name, 'path': path, 'status': status,
            'conclusion': conclusion, 'head_sha': SHA, 'created_at': created, **extra}


class FakeGitHub:
    """Synthetic REST API holding run attempts, one configured issue and its comments."""

    def __init__(self, runs=(), *, issue=None):
        self.runs = {(run['id'], run['run_attempt']): run for run in runs}
        self.issue = issue or {'number': ISSUE, 'state': 'open', 'repository_url': REPO_API_URL}
        self.comments = []
        self.calls = []
        self.failures = []
        self.clock = '2026-11-20T04:00:00Z'

    def add_comment(self, body, user=BOT, updated='2026-11-20T04:00:00Z'):
        self.comments.append({'id': len(self.comments) + 1, 'body': body, 'user': user, 'updated_at': updated})

    def fail(self, method, pattern, error, *, after=False):
        """Raise error for the next matching request; with after=True the request still takes effect."""
        self.failures.append((method, re.compile(pattern), error, after))

    def posts(self):
        return [call for call in self.calls if call[0] != 'GET']

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, dict(params or {}), body))
        failure = next(((i, f) for i, f in enumerate(self.failures) if f[0] == method and f[1].fullmatch(path)), None)
        if failure:
            del self.failures[failure[0]]
            if not failure[1][3]:
                raise failure[1][2]
        result = self._handle(method, path, params or {}, body)
        if failure:
            raise failure[1][2]
        return result

    def _handle(self, method, path, params, body):
        rest = path.removeprefix(f'/repos/{REPO}')
        assert rest != path, path
        if match := re.fullmatch(r'/actions/runs/(\d+)/attempts/(\d+)', rest):
            key = (int(match[1]), int(match[2]))
            if key not in self.runs:
                raise notifier.GitHubAPIError(method, path, 404, 'Not Found')
            return json.loads(json.dumps(self.runs[key]))
        if rest == f'/issues/{ISSUE}' and method == 'GET':
            return dict(self.issue)
        if rest == f'/issues/{ISSUE}/comments' and method == 'GET':
            items = [c for c in self.comments if c['updated_at'] >= params['since']]
            size, page = params['per_page'], params['page']
            return [dict(c) for c in items[(page - 1) * size:page * size]]
        if rest == f'/issues/{ISSUE}/comments' and method == 'POST':
            self.add_comment(body['body'], updated=self.clock)
            return self.comments[-1]
        if rest.startswith('/issues/'):
            raise notifier.GitHubAPIError(method, path, 404, 'Not Found')
        raise AssertionError(f'unexpected request {method} {path}')


def notify(api, run_id=1001, attempt=1):
    return notifier.notify(api, run_id=run_id, attempt=attempt, issue_number=ISSUE)


def run_main(api, **overrides):
    environ = {'GITHUB_TOKEN': TOKEN, 'GITHUB_REPOSITORY': REPO, 'RUN_ID': '1001', 'RUN_ATTEMPT': '1',
               'FACTORY_FAILURE_ISSUE': str(ISSUE), **overrides}
    out = io.StringIO()
    status = notifier.main({k: v for k, v in environ.items() if v is not None},
                           client_factory=lambda token: api, stdout=out)
    return status, out.getvalue()


class FailurePostingTests(unittest.TestCase):
    def test_failure_posts_one_short_comment_on_the_configured_issue(self):
        api = FakeGitHub([make_run()])
        self.assertIn(f'Reported Factory image candidates run 1001 attempt 1 on issue #{ISSUE}', notify(api))
        self.assertEqual([(c[0], c[1]) for c in api.posts()], [('POST', f'/repos/{REPO}/issues/{ISSUE}/comments')])
        body = api.comments[0]['body']
        self.assertEqual(body.splitlines()[0], '<!-- factory-failure-notice run=1001 attempt=1 -->')
        self.assertEqual(re.findall(r'@[\w-]+', body), ['@kyuz0'])
        self.assertIn('- Workflow: Factory image candidates', body)
        self.assertIn('- Conclusion: `failure`', body)
        self.assertIn(f'- Run: https://github.com/{REPO}/actions/runs/1001/attempts/1', body)
        self.assertIn('- Attempt: 1', body)
        self.assertIn(f'- Commit: `{SHA}`', body)

    def test_every_failure_conclusion_is_reported_and_others_are_not(self):
        for conclusion in notifier.FAILURE_CONCLUSIONS:
            with self.subTest(conclusion=conclusion):
                api = FakeGitHub([make_run(conclusion=conclusion)])
                notify(api)
                self.assertIn(f'- Conclusion: `{conclusion}`', api.comments[0]['body'])
        for conclusion in ('success', 'cancelled', 'skipped', 'neutral', 'action_required', 'stale'):
            with self.subTest(conclusion=conclusion):
                api = FakeGitHub([make_run(conclusion=conclusion)])
                self.assertIn('Not reported', notify(api))
                self.assertEqual(api.posts(), [])

    def test_startup_failure_listed_under_its_path_uses_the_known_workflow_name(self):
        api = FakeGitHub([make_run(conclusion='startup_failure', path='.github/workflows/checks.yml',
                                   name='.github/workflows/checks.yml')])
        notify(api)
        self.assertIn('- Workflow: Checks and release', api.comments[0]['body'])

    def test_each_watched_workflow_is_reported(self):
        for path, name in notifier.WATCHED_WORKFLOWS.items():
            with self.subTest(path=path):
                api = FakeGitHub([make_run(path=path + '@refs/heads/main', name=name)])
                notify(api)
                self.assertIn(f'- Workflow: {name}', api.comments[0]['body'])

    def test_distinct_runs_and_attempts_each_post_once(self):
        api = FakeGitHub([make_run(), make_run(attempt=2), make_run(run_id=1002)])
        for run_id, attempt in ((1001, 1), (1001, 2), (1002, 1)):
            self.assertIn('Reported', notify(api, run_id, attempt))
        self.assertEqual(len(api.comments), 3)
        markers = [c['body'].splitlines()[0] for c in api.comments]
        self.assertEqual(markers, [notifier.marker(1001, 1), notifier.marker(1001, 2), notifier.marker(1002, 1)])

    def test_repeated_delivery_of_the_same_attempt_posts_nothing_more(self):
        api = FakeGitHub([make_run()])
        notify(api)
        self.assertIn('Already reported', notify(api))
        self.assertEqual(len(api.comments), 1)

    def test_attempt_number_prefixes_do_not_count_as_reported(self):
        api = FakeGitHub([make_run(attempt=1), make_run(attempt=10)])
        notify(api, attempt=10)
        self.assertIn('Reported', notify(api, attempt=1))

    def test_marker_written_by_another_user_does_not_suppress_the_notice(self):
        api = FakeGitHub([make_run()])
        api.add_comment(notifier.marker(1001, 1), user=HUMAN)
        self.assertIn('Reported', notify(api))

    def test_comments_since_the_run_are_read_across_pages(self):
        api = FakeGitHub([make_run()])
        api.add_comment(notifier.marker(1001, 1), updated='2026-11-19T00:00:00Z')
        for index in range(250):
            api.add_comment(f'note {index}')
        api.add_comment(notifier.marker(1001, 1))
        self.assertIn('Already reported', notify(api))
        reads = [c[2] for c in api.calls if c[0] == 'GET' and c[1].endswith('/comments')]
        self.assertEqual([r['page'] for r in reads], [1, 2, 3])
        self.assertTrue(all(r['since'] == '2026-11-20T03:27:00Z' and r['per_page'] == 100 for r in reads))
        self.assertEqual(api.posts(), [])

    def test_exactly_full_last_page_requests_one_more_page(self):
        api = FakeGitHub([make_run()])
        for index in range(100):
            api.add_comment(f'note {index}')
        notify(api)
        reads = [c[2]['page'] for c in api.calls if c[0] == 'GET' and c[1].endswith('/comments')]
        self.assertEqual(reads, [1, 2])

    def test_fork_pull_request_run_is_reported_without_fork_controlled_text(self):
        run = make_run(name='Checks and release', path='.github/workflows/checks.yml', event='pull_request',
                       head_branch='fix-@victim`](http://x)', display_title='@victim ping',
                       head_repository={'full_name': 'forker/ai-sandbox-factory'})
        api = FakeGitHub([run])
        notify(api)
        body = api.comments[0]['body']
        self.assertEqual(re.findall(r'@[\w-]+', body), ['@kyuz0'])
        self.assertNotIn('victim', body)
        self.assertNotIn('http://x', body)

    def test_unwatched_workflow_file_is_not_reported_even_with_a_watched_name(self):
        api = FakeGitHub([make_run(path='.github/workflows/copy.yml', name='Checks and release')])
        self.assertIn("'.github/workflows/copy.yml', which is not a watched Factory workflow", notify(api))
        self.assertEqual(len(api.calls), 1)


class ConfiguredIssueTests(unittest.TestCase):
    def refused(self, api, expected):
        with self.assertRaisesRegex(notifier.NotificationError, expected):
            notify(api)
        self.assertEqual(api.posts(), [])

    def test_closed_issue_fails_without_posting(self):
        api = FakeGitHub([make_run()], issue={'number': ISSUE, 'state': 'closed', 'repository_url': REPO_API_URL})
        self.refused(api, f'FACTORY_FAILURE_ISSUE is {ISSUE}, which is closed. Reopen issue #{ISSUE}')

    def test_pull_request_number_fails_without_posting(self):
        api = FakeGitHub([make_run()], issue={'number': ISSUE, 'state': 'open', 'repository_url': REPO_API_URL,
                                              'pull_request': {}})
        self.refused(api, 'which is a pull request')

    def test_issue_transferred_to_another_repository_fails_without_posting(self):
        api = FakeGitHub([make_run()], issue={'number': 3, 'state': 'open',
                                              'repository_url': 'https://api.github.com/repos/kyuz0/elsewhere'})
        self.refused(api, "returned issue #3 from 'https://api.github.com/repos/kyuz0/elsewhere'")

    def test_missing_issue_names_the_variable_and_status(self):
        api = FakeGitHub([make_run()])
        with self.assertRaisesRegex(notifier.NotificationError, r'FACTORY_FAILURE_ISSUE is 8, but .* HTTP 404'):
            notifier.notify(api, run_id=1001, attempt=1, issue_number=8)

    def test_unset_or_invalid_variable_fails_before_any_request(self):
        for value, expected in ((None, 'FACTORY_FAILURE_ISSUE is not set. Create an issue'),
                                ('', 'FACTORY_FAILURE_ISSUE is not set'),
                                ('0', "FACTORY_FAILURE_ISSUE must be a positive whole number; got '0'"),
                                ('#7', "got '#7'")):
            with self.subTest(value=value):
                api = FakeGitHub([make_run()])
                status, out = run_main(api, FACTORY_FAILURE_ISSUE=value)
                self.assertEqual(status, 1)
                self.assertIn('The failure notice for run 1001 attempt 1 was not posted.', out)
                self.assertIn(expected, out)
                self.assertEqual(api.calls, [])


class MetadataTests(unittest.TestCase):
    def check(self, change, expected):
        run = make_run()
        change(run)
        api = FakeGitHub([run])
        with self.assertRaisesRegex(notifier.NotificationError, expected):
            notify(api)
        self.assertEqual(api.posts(), [])

    def test_unusable_run_fields_are_named_with_their_value(self):
        self.check(lambda run: run.pop('head_sha'), 'workflow run without head_sha')
        self.check(lambda run: run.update(head_sha='not-a-sha'), "commit 'not-a-sha'; expected a hexadecimal Git SHA")
        self.check(lambda run: run.update(path=None), 'workflow path None')
        self.check(lambda run: run.update(created_at=5), 'creation time 5')

    def test_api_returning_another_attempt_is_refused(self):
        api = FakeGitHub([make_run(attempt=3)])
        api.runs[(1001, 2)] = api.runs.pop((1001, 3))
        with self.assertRaisesRegex(notifier.NotificationError, 'described run 1001 attempt 3 instead of run 1001 attempt 2'):
            notify(api, attempt=2)

    def test_incomplete_run_is_refused_with_its_status(self):
        api = FakeGitHub([make_run(status='in_progress', conclusion=None)])
        with self.assertRaisesRegex(notifier.NotificationError, "status 'in_progress'. Rerun this notifier after"):
            notify(api)

    def test_main_rejects_malformed_run_input_on_one_line(self):
        api = FakeGitHub([make_run()])
        status, out = run_main(api, RUN_ID='12\nabc')
        self.assertEqual(status, 1)
        self.assertEqual(out.count('\n'), 1)
        self.assertIn(r"RUN_ID must be a positive whole number; got '12\nabc'", out)
        self.assertEqual(api.calls, [])

    def test_main_refuses_another_repository(self):
        api = FakeGitHub([make_run()])
        status, out = run_main(api, GITHUB_REPOSITORY='forker/ai-sandbox-factory')
        self.assertEqual(status, 1)
        self.assertIn("GITHUB_REPOSITORY is 'forker/ai-sandbox-factory'", out)
        self.assertEqual(api.calls, [])

    def test_main_reports_the_requested_attempt(self):
        api = FakeGitHub([make_run(), make_run(attempt=2)])
        status, out = run_main(api, RUN_ATTEMPT='2')
        self.assertEqual(status, 0)
        self.assertIn('Reported Factory image candidates run 1001 attempt 2', out)
        self.assertEqual(api.calls[0][1], f'/repos/{REPO}/actions/runs/1001/attempts/2')


class APIFailureTests(unittest.TestCase):
    def test_read_failure_propagates_without_posting(self):
        api = FakeGitHub([make_run()])
        api.fail('GET', f'/repos/{REPO}/issues/{ISSUE}/comments',
                 notifier.GitHubAPIError('GET', '/comments', 503, 'Service Unavailable', 'ABCD:1234'))
        with self.assertRaisesRegex(notifier.GitHubAPIError, r'HTTP 503: Service Unavailable \(request ID ABCD:1234\)'):
            notify(api)
        self.assertEqual(api.posts(), [])

    def test_failed_post_is_retried_once(self):
        api = FakeGitHub([make_run()])
        api.fail('POST', f'/repos/{REPO}/issues/{ISSUE}/comments',
                 notifier.GitHubTransportError('POST', '/comments', 'ConnectionResetError: reset'))
        with self.assertRaisesRegex(notifier.GitHubTransportError, 'ConnectionResetError'):
            notify(api)
        self.assertEqual(api.comments, [])
        self.assertIn('Reported', notify(api))
        self.assertIn('Already reported', notify(api))
        self.assertEqual(len(api.comments), 1)

    def test_retry_after_a_lost_post_response_finds_the_marker(self):
        api = FakeGitHub([make_run()])
        api.fail('POST', f'/repos/{REPO}/issues/{ISSUE}/comments',
                 notifier.GitHubTransportError('POST', '/comments', 'TimeoutError: timed out'), after=True)
        with self.assertRaises(notifier.GitHubTransportError):
            notify(api)
        self.assertIn('Already reported', notify(api))
        self.assertEqual(len(api.comments), 1)

    def test_main_prints_api_error_with_status_request_id_and_redacted_token(self):
        api = FakeGitHub([make_run()])
        api.fail('GET', f'/repos/{REPO}/actions/runs/1001/attempts/1',
                 notifier.GitHubAPIError('GET', '/runs', 401, f'Bad credentials {TOKEN}\n::warning::injected', 'REQ:9'))
        status, out = run_main(api)
        self.assertEqual(status, 1)
        self.assertEqual(out.count('\n'), 1)
        self.assertIn('%0A::warning::injected', out)
        self.assertIn('HTTP 401: Bad credentials ***', out)
        self.assertIn('request ID REQ:9', out)
        self.assertNotIn(TOKEN, out)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClientTests(unittest.TestCase):
    def test_request_uses_the_github_api_token_and_json(self):
        seen = []
        def urlopen(request, timeout):
            seen.append((request, timeout))
            return Response(b'{"ok": true}')
        client = notifier.GitHubClient(TOKEN, urlopen=urlopen)
        self.assertEqual(client.request('POST', '/repos/x/y/issues/1/comments', {'page': 2}, {'body': 't'}), {'ok': True})
        request, timeout = seen[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/x/y/issues/1/comments?page=2')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(json.loads(request.data), {'body': 't'})
        self.assertEqual(timeout, notifier.REQUEST_TIMEOUT_SECONDS)

    def test_http_error_keeps_status_message_and_request_id_without_token(self):
        headers = email.message.Message()
        headers['X-GitHub-Request-Id'] = 'F00D:42'
        body = json.dumps({'message': 'Resource not accessible by integration ' + TOKEN,
                           'errors': [{'resource': 'IssueComment', 'code': 'custom'}],
                           'documentation_url': 'https://docs.github.com/rest'}).encode()
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', headers, io.BytesIO(body))
        client = notifier.GitHubClient(TOKEN, urlopen=urlopen)
        with self.assertRaises(notifier.GitHubAPIError) as caught:
            client.request('POST', '/repos/x/y/issues/1/comments')
        message = str(caught.exception)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn('POST /repos/x/y/issues/1/comments returned HTTP 403: Resource not accessible by integration ***', message)
        self.assertIn('"code": "custom"', message)
        self.assertIn('request ID F00D:42', message)
        self.assertNotIn(TOKEN, message)

    def test_transport_error_keeps_the_underlying_cause(self):
        def urlopen(request, timeout):
            raise urllib.error.URLError(ConnectionResetError(104, 'Connection reset by peer'))
        client = notifier.GitHubClient(TOKEN, urlopen=urlopen)
        with self.assertRaisesRegex(notifier.GitHubTransportError, 'GET /x did not respond: URLError: .*Connection reset by peer'):
            client.request('GET', '/x')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (CHECKOUT / '.github/workflows/notify.yml').read_text()
        self.workflow = yaml.safe_load(self.text)
        self.triggers = self.workflow.get('on', self.workflow.get(True))
        self.job = self.workflow['jobs']['notify']

    def test_watches_exactly_the_factory_workflows(self):
        self.assertEqual(self.triggers['workflow_run']['workflows'], list(notifier.WATCHED_WORKFLOWS.values()))
        self.assertEqual(self.triggers['workflow_run']['types'], ['completed'])
        for path, name in notifier.WATCHED_WORKFLOWS.items():
            file = CHECKOUT / path
            if path != '.github/workflows/maintenance.yml' or file.exists():
                with self.subTest(path=path):
                    self.assertEqual(yaml.safe_load(file.read_text())['name'], name)

    def test_runs_for_every_failure_conclusion_including_fork_pull_requests(self):
        condition = self.job['if']
        self.assertIn(f"github.repository == '{REPO}'", condition)
        for conclusion in notifier.FAILURE_CONCLUSIONS:
            self.assertIn(f'"{conclusion}"', condition)
        for excluded in ('pull_request', 'head_repository', 'head_branch', 'success'):
            self.assertNotIn(excluded, condition)

    def test_permissions_and_per_attempt_concurrency(self):
        self.assertEqual(self.workflow['permissions'], {})
        self.assertEqual(self.job['permissions'], {'actions': 'read', 'contents': 'read', 'issues': 'write'})
        concurrency = self.workflow['concurrency']
        self.assertFalse(concurrency['cancel-in-progress'])
        self.assertIn('github.event.workflow_run.id || inputs.run_id', concurrency['group'])
        self.assertIn('github.event.workflow_run.run_attempt || inputs.run_attempt', concurrency['group'])
        self.assertNotIn('workflow_id', concurrency['group'])
        self.assertNotIn('strategy', self.job)

    def test_dispatch_and_step_use_the_same_run_and_configured_issue(self):
        inputs = self.triggers['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'run_id', 'run_attempt'})
        self.assertTrue(all(item['required'] for item in inputs.values()))
        env = self.job['steps'][1]['env']
        self.assertEqual(env['FACTORY_FAILURE_ISSUE'], '${{ vars.FACTORY_FAILURE_ISSUE }}')
        self.assertEqual(env['RUN_ID'], '${{ github.event.workflow_run.id || inputs.run_id }}')
        self.assertEqual(env['RUN_ATTEMPT'], '${{ github.event.workflow_run.run_attempt || inputs.run_attempt }}')

    def test_runs_only_default_branch_notifier_code(self):
        self.assertNotIn('head_sha', self.text)
        self.assertNotIn('download-artifact', self.text)
        self.assertEqual([step.get('uses') for step in self.job['steps']], ['actions/checkout@v4', None])
        checkout = self.job['steps'][0]
        self.assertEqual(checkout['with']['ref'], '${{ github.event.repository.default_branch }}')
        self.assertFalse(checkout['with']['persist-credentials'])
        self.assertNotIn('${{', self.job['steps'][1]['run'])


if __name__ == '__main__':
    unittest.main()
