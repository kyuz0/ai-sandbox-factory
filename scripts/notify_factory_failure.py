"""Comment on the Factory CI failure issue when a watched workflow run fails.

The repository Actions variable FACTORY_FAILURE_ISSUE holds the number of one
open issue in kyuz0/ai-sandbox-factory. For each failed run attempt, this script
posts one comment on that issue mentioning the repository owner. A hidden marker
in the comment lets a rerun of the notifier skip an attempt it already reported.

The script reads run metadata from the GitHub REST API only. It never fetches
the reported run's source, logs or artifacts, so runs from fork pull requests
are reported the same way as other runs.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = 'kyuz0/ai-sandbox-factory'
OWNER = REPOSITORY.split('/')[0]
API_URL = 'https://api.github.com'
SERVER_URL = 'https://github.com'
# Runs are matched by workflow file. A workflow that fails to start can be
# listed under its path instead of its name, and a fork can copy a name.
WATCHED_WORKFLOWS = {
    '.github/workflows/checks.yml': 'Checks and release',
    '.github/workflows/images.yml': 'Factory image candidates',
    '.github/workflows/maintenance.yml': 'Factory image maintenance',
}
# Conclusions that mean the pipeline failed. "cancelled" is a deliberate stop.
FAILURE_CONCLUSIONS = ('failure', 'timed_out', 'startup_failure')
BOT_LOGIN = 'github-actions[bot]'
# GitHub REST list endpoints return at most 100 items per page.
PER_PAGE = 100
# urllib waits indefinitely by default; a stalled connection would hold this
# attempt's concurrency group until the job's own timeout.
REQUEST_TIMEOUT_SECONDS = 60
COMMIT_SHA = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


class NotificationError(Exception):
    """The notice was not posted; the message gives the cause."""


class GitHubAPIError(NotificationError):
    def __init__(self, method, path, status, message, request_id=None):
        self.status = status
        detail = f'GitHub API {method} {path} returned HTTP {status}: {message}'
        if request_id:
            detail += f' (request ID {request_id})'
        super().__init__(detail)


class GitHubTransportError(NotificationError):
    def __init__(self, method, path, reason):
        super().__init__(f'GitHub API {method} {path} did not respond: {reason}')


def redact(text, secret):
    return text.replace(secret, '***') if secret else text


class GitHubClient:
    """Minimal REST client. Tests replace it with any object providing request()."""

    def __init__(self, token, urlopen=urllib.request.urlopen):
        self.token = token
        self.urlopen = urlopen

    def request(self, method, path, params=None, body=None):
        url = API_URL + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer ' + self.token,
            'User-Agent': 'ai-sandbox-factory-failure-notifier',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            request_id = error.headers.get('X-GitHub-Request-Id') if error.headers else None
            raise GitHubAPIError(method, path, error.code, redact(_error_message(error), self.token),
                                 request_id) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, 'reason', error)
            raise GitHubTransportError(method, path, redact(f'{type(error).__name__}: {reason}', self.token)) from None
        try:
            return json.loads(raw) if raw else None
        except ValueError as error:
            raise NotificationError(f'GitHub API {method} {path} returned a response that is not JSON: {error}') from None


def _error_message(error):
    try:
        raw = error.read().decode('utf-8', 'replace')
    except OSError as read_error:
        return f'{error.reason} (response body unreadable: {read_error})'
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.strip() or str(error.reason)
    if not isinstance(payload, dict):
        return raw.strip()
    message = str(payload.get('message') or error.reason)
    details = [item if isinstance(item, str) else json.dumps(item, sort_keys=True)
               for item in payload.get('errors') or []]
    if details:
        message += '; ' + '; '.join(details)
    if payload.get('documentation_url'):
        message += f' (see {payload["documentation_url"]})'
    return message


def fetch_run(client, run_id, attempt):
    path = f'/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}'
    run = client.request('GET', path)
    if not isinstance(run, dict):
        raise NotificationError(f'GitHub API GET {path} returned {type(run).__name__} instead of a workflow run.')
    for key in ('id', 'run_attempt', 'status', 'conclusion', 'path', 'head_sha', 'created_at'):
        if key not in run:
            raise NotificationError(f'GitHub API GET {path} returned a workflow run without {key}.')
    if (run['id'], run['run_attempt']) != (run_id, attempt):
        raise NotificationError(f'GitHub API GET {path} described run {run["id"]!r} attempt {run["run_attempt"]!r} '
                                f'instead of run {run_id} attempt {attempt}.')
    # These values are written into the public comment or sent back to the API.
    if not isinstance(run['path'], str):
        raise NotificationError(f'Run {run_id} has workflow path {run["path"]!r}; expected text.')
    if not isinstance(run['head_sha'], str) or not COMMIT_SHA.fullmatch(run['head_sha']):
        raise NotificationError(f'Run {run_id} has commit {run["head_sha"]!r}; expected a hexadecimal Git SHA.')
    if not isinstance(run['created_at'], str):
        raise NotificationError(f'Run {run_id} has creation time {run["created_at"]!r}; expected an ISO 8601 time.')
    return run


def check_issue(client, number):
    """Require the configured number to be an open issue in this repository."""
    path = f'/repos/{REPOSITORY}/issues/{number}'
    fix = (f'Set the Actions variable FACTORY_FAILURE_ISSUE to an open issue in {REPOSITORY}, '
           'then rerun this notifier.')
    try:
        issue = client.request('GET', path)
    except GitHubAPIError as error:
        if error.status in (404, 410):
            raise NotificationError(f'FACTORY_FAILURE_ISSUE is {number}, but {error}. {fix}') from None
        raise
    if not isinstance(issue, dict):
        raise NotificationError(f'GitHub API GET {path} returned {type(issue).__name__} instead of an issue.')
    # GitHub redirects requests for a transferred issue to its new repository.
    if issue.get('number') != number or issue.get('repository_url') != f'{API_URL}/repos/{REPOSITORY}':
        raise NotificationError(f'FACTORY_FAILURE_ISSUE is {number}, but GitHub returned issue '
                                f'#{issue.get("number")} from {issue.get("repository_url")!r}; '
                                f'it may have been transferred. {fix}')
    if 'pull_request' in issue:
        raise NotificationError(f'FACTORY_FAILURE_ISSUE is {number}, which is a pull request. {fix}')
    if issue.get('state') != 'open':
        raise NotificationError(f'FACTORY_FAILURE_ISSUE is {number}, which is {issue.get("state")}. '
                                f'Reopen issue #{number} or change the variable, then rerun this notifier.')


def paginate(client, path, params):
    page = 1
    while True:
        items = client.request('GET', path, {**params, 'per_page': PER_PAGE, 'page': page})
        if not isinstance(items, list):
            raise NotificationError(f'GitHub API GET {path} page {page} returned {type(items).__name__} instead of a list.')
        yield from items
        if len(items) < PER_PAGE:
            return
        page += 1


def marker(run_id, attempt):
    return f'<!-- factory-failure-notice run={run_id} attempt={attempt} -->'


def already_reported(client, issue_number, run):
    expected = marker(run['id'], run['run_attempt'])
    # `since` filters on each comment's last update, which is never earlier than
    # its creation, so every comment written after the run started is still read.
    comments = paginate(client, f'/repos/{REPOSITORY}/issues/{issue_number}/comments', {'since': run['created_at']})
    for comment in comments:
        user = comment.get('user') if isinstance(comment, dict) else None
        if isinstance(user, dict) and user.get('login') == BOT_LOGIN and expected in (comment.get('body') or ''):
            return True
    return False


def failure_comment(workflow, run):
    run_id, attempt = run['id'], run['run_attempt']
    # Only trusted names and validated values appear here; fork branch names
    # and run titles are left out so they cannot add mentions or markup.
    return '\n'.join([
        marker(run_id, attempt),
        f'@{OWNER} A Factory CI run failed.',
        '',
        f'- Workflow: {workflow}',
        f'- Conclusion: `{run["conclusion"]}`',
        f'- Run: {SERVER_URL}/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}',
        f'- Attempt: {attempt}',
        f'- Commit: `{run["head_sha"]}`',
    ])


def notify(client, *, run_id, attempt, issue_number):
    """Post the failure notice for one run attempt. Returns a line for the job log."""
    run = fetch_run(client, run_id, attempt)
    workflow_path = run['path'].split('@')[0]
    workflow = WATCHED_WORKFLOWS.get(workflow_path)
    if workflow is None:
        return f'Not reported: run {run_id} is from {workflow_path!r}, which is not a watched Factory workflow.'
    if run['status'] != 'completed':
        raise NotificationError(f'{workflow} run {run_id} attempt {attempt} has status {run["status"]!r}. '
                                'Rerun this notifier after the run completes.')
    if run['conclusion'] not in FAILURE_CONCLUSIONS:
        return f'Not reported: {workflow} run {run_id} attempt {attempt} concluded {run["conclusion"]!r}.'
    check_issue(client, issue_number)
    if already_reported(client, issue_number, run):
        return f'Already reported: {workflow} run {run_id} attempt {attempt} on issue #{issue_number}.'
    client.request('POST', f'/repos/{REPOSITORY}/issues/{issue_number}/comments',
                   body={'body': failure_comment(workflow, run)})
    return f'Reported {workflow} run {run_id} attempt {attempt} on issue #{issue_number}.'


def _positive_integer(environ, name, missing):
    value = environ.get(name, '')
    if not value:
        raise NotificationError(f'{name} is not set. {missing}')
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise NotificationError(f'{name} must be a positive whole number; got {value!r}.')
    return int(value)


def _workflow_command(text):
    # Escape values so a message cannot start a new workflow command line.
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def main(environ=os.environ, client_factory=GitHubClient, stdout=sys.stdout):
    token = environ.get('GITHUB_TOKEN', '')
    run_label = 'The failure notice'
    try:
        if not token:
            raise NotificationError('GITHUB_TOKEN is not set; pass the workflow token to this step.')
        if environ.get('GITHUB_REPOSITORY') != REPOSITORY:
            raise NotificationError(f'This notifier reports only {REPOSITORY} runs; GITHUB_REPOSITORY is '
                                    f'{environ.get("GITHUB_REPOSITORY")!r}.')
        run_id = _positive_integer(environ, 'RUN_ID', 'Pass the ID of the run to report.')
        attempt = _positive_integer(environ, 'RUN_ATTEMPT', 'Pass the attempt number of the run to report.')
        run_label = f'The failure notice for run {run_id} attempt {attempt}'
        issue_number = _positive_integer(environ, 'FACTORY_FAILURE_ISSUE', (
            f'Create an issue in {REPOSITORY}, set its number as the repository Actions variable '
            'FACTORY_FAILURE_ISSUE, then rerun this notifier.'))
        result = notify(client_factory(token), run_id=run_id, attempt=attempt, issue_number=issue_number)
    except NotificationError as error:
        message = f'{run_label} was not posted. {error}'
        print('::error title=Factory failure notification::' + _workflow_command(redact(message, token)), file=stdout)
        return 1
    print(_workflow_command(result), file=stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
