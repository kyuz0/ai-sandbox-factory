# Development and tests

From the checkout, install the package into a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
npm ci --prefix pi_sandbox_factory/containers/inference --ignore-scripts
node --test tests/test_provider_runtime.mjs tests/test_web_search_policy.mjs tests/test_inference_broker.mjs tests/test_editor_watch_fallback.cjs
```

These suites use fixtures without account credentials. For macOS, put fixtures
under your home directory so Podman Machine can share them:

```sh
export TMPDIR="$HOME/Projects/pi-sandbox-test-tmp"
mkdir -p "$TMPDIR"
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## Package installation

Install the wheel outside the checkout and check `--help`, `profile list` and
`provider list`. Create a sandbox from that installation to verify bundled assets.
Check Python 3.10 syntax compatibility when changing Python code.

```sh
.venv/bin/pip wheel --no-deps . -w dist
python3 -m venv /tmp/pi-sandbox-package-check
/tmp/pi-sandbox-package-check/bin/pip install dist/pi_sandbox_factory-*.whl
(cd /tmp && /tmp/pi-sandbox-package-check/bin/pi-sandbox-factory profile list)
```

## Approval model selection

In a disposable Pi sandbox with two connected providers, open **Settings → Approvals & sandbox**. Clear **Use current model for approvals**, select a model from the second provider, and apply the change. Reopen Pi with the first provider as the chat model. Ask it to use `bash_escalate` to run `printf approval-routing-ok`. Check that the approval details name the second provider and that the chat reply still uses the first. Recheck **Use current model for approvals** and repeat to verify the default.

## FrostPi editor and shared Pi goals

Use `--standalone` in place of `--code-server` and its directory to exercise **FrostPi in browser**. It opens a new chat, sends a prompt to the local test model, reloads the page and resumes the saved conversation. The test also checks workspace browsing on desktop and phone, Markdown links and images, keyboard navigation, draft preservation, and the workspace and browser access rules. It checks that transfers start disabled, uploads and downloads a binary file, checks both copies byte for byte and verifies that duplicate names and interrupted uploads preserve existing files.

Build the Factory image from the current checkout in a disposable sandbox.
Use the code-server archive pinned in `pi_sandbox_factory/browser_editor.py`.
Extract it before the test. The browser check needs Podman, socat, Playwright,
and its Chromium installation.

```sh
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/frostpi_editor.py \
  --image FACTORY_IMAGE --code-server /path/to/code-server-4.145.2-linux-amd64 \
  --output /tmp/factory-frostpi
```

The test uses a network-disabled disposable container and synthetic local
inference. It opens the installed editor on a fresh workspace, checks the
approval mode before the first prompt, exercises a long reviewer rejection
and verifies that the reason, full command, justification and execution scope
appear in separate sections. The command can be scrolled with the keyboard while
both choices stay available. **Deny** blocks execution and **Allow this exact
command once** runs the approved command. Remote Markdown images stay blocked.
External chat links display the full URL and warn about sandbox network restrictions.
Code-server may also prompt for an untrusted domain. Cancelling either confirmation
opens no tab. Confirming in both opens exactly one.
The test submits a choice, a custom answer and multiple choices through the shared
question tool. It also dismisses a question and checks the result delivered to the model.
It pauses and revises a goal in FrostPi, checks its objective and usage in standalone Pi,
then resumes and completes it in the same session.
Use `--nonblocking-questions` to verify a deferred answer reaches Pi while
work continues. The FrostPi check stops and restarts the session with a question
pending. The terminal check answers through `/questions`.

Use `--reviewed-file-access automatic`, `assisted` and `manual` in separate runs
to check one-time access to Pi resources. The fixture verifies the exact file
and folder sent for review, the manual dialog, the interactive fallback, and
denial of credential paths and links before review. Replacing a file while the
manual dialog is open invalidates the request. The check also opens a separate
Pi terminal session against the same image.

Use `--provider-connection-check` with `automatic`, `assisted`, `manual` and
`off` to check when Pi can request a stored-credential check. The fixture counts
upstream requests before and after each approval, verifies direct file reads
are denied, and checks that an upstream response containing the fake credential
reaches Pi as a status only. `tests/inference_container.py` verifies the same
response boundary across separate containers alongside the normal inference stream.
Screenshots and the editor log remain in the output directory. Add
`--startup-only automatic` to test first launch without a configured model. The
same check accepts `assisted`, `manual`, and `off`.

Repeat the browser command with `--chat-workflows` and a separate output
directory to test steering, switching between branches in one session, and
forking into a separate session. The check continues the original after the fork
and verifies that the new messages stay in their respective session files.

Use `--scheduled-tasks` to check that the list appears with pending jobs, survives
a session restart, and shows a named delivery once. A second restart preserves
the delivered job and its transcript entry.

Use `--session-naming` with the rebuilt image to check the generated title and
the first-prompt fallback after a provider error. The check verifies both titles
in FrostPi and in standalone Pi and confirms a later turn keeps the original name.

With a Betterwright-enabled image, use `--tool-images` to verify that Pi's
`read` tool and Betterwright deliver the same image bytes to the model and
FrostPi. The check opens both thumbnails and reopens them after a session restart.

Use `--streaming-metrics` to verify the running count and average rate during
reasoning and answer generation. The fixture also sends 420 estimated tokens in
a burst after a delay and checks the final average uses the reported count.

Run `--local-knowledge off` with the Local Knowledge profile image to check that
both clients can find text without an embedding request. Run `--local-knowledge endpoint`
with the optional pi-knowledge layer to index a read-only mount, reject a link
outside the allowed folders, and search the saved index in both clients. The
embedding server is a loopback fixture. The test container has no outbound network access. Use
`--profile-prompt pi_sandbox_factory/profiles/deep-research` with the Deep Web
Research image to verify the profile addendum in both clients. For a profile
with `start-here.md`, this also checks the startup hint and `/profile-guide` in
FrostPi. Use the Local Knowledge profile path to check its onboarding.
With the Deep Web Research image and the Web Search layer, use `--deep-research`
to verify that Pi and FrostPi load the skill, delegate a search through a
loopback provider, read a mounted reference and save a cited report and source log.

Use `--system-prompt` with the current Factory image to check that a saved add-on
can be assigned, replaced and removed in existing Pi CLI and FrostPi conversations.
The fixture inspects the system prompt sent to the model and checks that earlier
FrostPi transcript entries stay intact.

For authenticated local discovery, run the following after installing Playwright
and Chromium.

```sh
.venv/bin/python tests/local_model_keys.py --code-server-archive /path/to/code-server-4.145.2-linux-amd64.tar.gz
```

The fixture creates and deletes a Podman sandbox, serves two protected model
endpoints and one public endpoint, and chats through Pi and FrostPi. It checks the
model picker, per-port credentials, replacement while the sandbox is running, and
preservation of the current key after a rejected replacement.

For desktop VS Code, create a separate disposable sandbox with `--editor desktop`.
Open it, trust its workspace, and verify that FrostPi opens a chat, displays the
approval notice, and streams a reply using a configured model. For the update
check, start with the previous Factory release in disposable state, add a
workspace file and save a Pi chat, then run `recreate NAME --profile base` from
the new checkout. Verify that both survive and that the old shipped extensions
are retired in the stable and Insiders server directories.

For the browser migration, open a disposable Pendant sandbox using the new
Factory launcher before recreating it. Verify the sandbox list offers the
FrostPi update and the browser still opens Pendant. After recreation, open
the browser again and verify FrostPi opens and the notice is gone.

To try the branch without touching saved sandboxes, open the launcher with a
fresh state directory.

```sh
test_root=$(mktemp -d "${TMPDIR:-/tmp}/factory-frostpi.XXXXXX")
mkdir "$test_root/workspace"
.venv/bin/pi-sandbox-factory --state-dir "$test_root/state"
```

Select the new workspace, enable **Goals** and
**Auto (interactive)**, and open it in the browser editor. Confirm the mode
notice appears immediately. Use `/auto-review-settings` to view the current
reviewer. Ask an action that triggers approval and inspect the command before
choosing. To switch clients, pause your goal with `/goal pause`, close the chat,
open Pi CLI, resume the same session, and run `/goal status` before resuming work.

The checks below that reference Pendant require the earlier Pendant image and
matching checkout (`v2026.11.18.220034`). They remain reproducible historical
coverage. Use the FrostPi check above for the new editor. Standalone Pi and
client-independent container checks below also apply to this branch.

## Subagents in Pi and FrostPi

Use the image, code-server directory and Playwright setup from the
[FrostPi check](#frostpi-editor-and-shared-pi-goals). Run each mode on its own
so a failure in one mode does not skip the rest.

```sh
.venv/bin/python tests/frostpi_editor.py \
  --image FACTORY_IMAGE --code-server /path/to/code-server-4.145.2-linux-amd64 \
  --subagents automatic --output /tmp/factory-subagents-automatic
```

| Mode | Required result in both clients |
| --- | --- |
| `disabled` | The parent model has no delegation tools. |
| `automatic` | A foreground assistant reads, writes and edits a workspace file and reads it with Bash. Native file tools reject a protected fixture. The reviewer allows one escalation and denies another with zero human prompts. A read-only role receives only `read`. A nested assistant delegates a read. Two background assistants overlap, inherit a custom tool and finish in separate runs. FrostPi displays both statuses and expandable tool results. A child escalation review receives the original parent user prompt. Explicit worker names appear in status and the saved transcripts. |
| `assisted` | The parent receives inline and nested requests with the worker identity, exact command and reviewer reason. Two overlapping background requests can receive opposite decisions. Native and Bash prompts queue without replacing each other. Dismissing a request leaves its file unwritten. A critical decision requires typing `APPROVE`. A native file approval carries the original user prompt and excludes the delegated task from user authorization evidence. |
| `manual` | One child escalation reaches the parent for a one-time decision without a model review. The approved marker is written. The fixture also checks the approval in Pi's terminal UI. |
| `off` | Escalation is absent. Regular Bash writes its marker while the inner sandbox hides a synthetic private file. There is no model review or human prompt. |

Use `--subagent-client pi` or `--subagent-client frostpi` to isolate one
client. `--subagent-case` selects one scenario within the chosen mode. Inspect
the browser screenshots, Pi RPC events and fixture trace in the output
directory. The trace identifies model requests and tool results. Workspace
markers and saved run statuses verify what actually happened.
The named-worker case opens two live transcripts in succession and verifies
that each shows its own worker’s instructions and output. It also checks
elapsed time, the final transcript, and resuming a completed child through
**New or resume session → Resume session**. Run with `--subagent-case named`
to check that the parent chat and both kinds of worker receive the saved sandbox
system prompt exactly once. The mixed-approval case opens a live
worker at a shorter viewport while verifying that the parent approval and
composer stay accessible.
The `interrupt-goal` case stops a parent goal while a named worker is running.
The same parent checks the running and completed worker by its original run ID,
then resumes and completes its paused goal. Reopening the worker session lets
the user start a separate goal there. FrostPi also checks the worker list after Stop.
The `stop-all` case interrupts a parent with foreground and background workers.
It uses `/subagents-stop` in Pi’s terminal and RPC and **Stop workers…** in
FrostPi. It verifies confirmation and cancellation, then checks that stopping
one or all selected workers leaves unrelated workers running.

Both interfaces use the parent Pi session to show delegated Bash and file
approvals. Approve a request from the session that launched the worker. The
reviewer uses the original user's messages on that branch, and the approval
applies to the exact action and worker. Reopening the session or changing
branches invalidates approvals from the previous session instance or branch.

Run `--subagent-compaction lite`, `--subagent-compaction full` and
`--subagent-compaction off` with the same image and editor options. The local
model fixture reports large usage counters. Each worker continues below the
compaction threshold, then compacts and continues when it reaches its context
reserve. The test checks that the checkpoint stays in the child's session,
that Lite and Full expose their configured recall tools, and that the
read-only specialist keeps its tool list.

Use `--subagents automatic --subagent-case web-tools` with an image containing
Web search and Browser. The fixture selects Only SearXNG and supplies local
search results and a Chromium test page. It checks foreground and nested tool
access, concurrent background search and navigation, independent page storage,
and the read-only specialist's tool list.

For the live check, create a fresh restricted sandbox with `--subagents`,
select only your installed Chaucer provider and configure its key through
Factory. Give the test a disposable workspace and select
`chaucer/eu-deepseek-v4-flash` as the default model. Start its browser editor
with `browser-editor NAME --background --no-open`.

```sh
.venv/bin/python tests/subagents_live.py \
  --state-dir /path/to/disposable/state --name NAME --output /tmp/factory-subagents-live
```

The live check uses a real Pi terminal and the shipped FrostPi webview. Each
launches two background workers with distinct seed and result files and a role
limited to read and write.
The script verifies both results and each worker's actual read and write in
its own saved Pi session. It captures terminal output and browser screenshots.
Delete the disposable sandbox when finished.

## Container and Pendant integration

Check process headroom with a cached Factory image:

```sh
podman run --rm --network none --pids-limit 512 --security-opt label=disable \
  --entrypoint python3 -v "$PWD/tests/process_headroom.py:/tmp/check.py:ro" \
  FACTORY_IMAGE /tmp/check.py exhausted
podman run --rm --network none --pids-limit=-1 --security-opt label=disable \
  --entrypoint python3 -v "$PWD/tests/process_headroom.py:/tmp/check.py:ro" \
  FACTORY_IMAGE /tmp/check.py healthy 3000
```

The fixture reproduces the relay's fork failure at the old limit and checks
that 3,000 worker threads coexist with repeated relay requests for 30 seconds.
Choose a larger thread count when ten times the measured baseline exceeds 3,000.
Both containers are disposable and removed when the check finishes.

Exercise real relay supervision with synthetic services and no accounts:

```sh
podman run --rm --network none --pids-limit=512 --user 0 --security-opt label=disable \
  --entrypoint python3 -v "$PWD/tests/relay_recovery.py:/tmp/check.py:ro" \
  -v "$PWD/pi_sandbox_factory/containers/dev/entrypoint.sh:/tmp/entrypoint.sh:ro" \
  FACTORY_IMAGE /tmp/check.py dev /tmp/entrypoint.sh --exhaust
```

This holds PID exhaustion for 30 seconds, verifies listener death and recovery,
then kills the listener ten times. Each time the relay must recover without stopping the entrypoint. For
`proxy` and `advisor`, mount that directory's entrypoint, replace `dev` with the
matching name, use `--pids-limit=-1`, and omit `--exhaust`. Those variants hold
600 simultaneous connections throughout all ten listener restarts.

Check the production editor supervisor's orphan collection and natural exit:

```sh
podman run --rm --network none --pids-limit=-1 --security-opt label=disable \
  --entrypoint python3 -v "$PWD/tests/editor_orphan_reaping.py:/tmp/check.py:ro" \
  -v "$PWD/pi_sandbox_factory/containers/editor/launch.py:/tmp/launch.py:ro" \
  FACTORY_IMAGE /tmp/check.py /tmp/launch.py
```

It creates 1,000 orphan helpers across ten waves, checks they are reaped while the
editor remains available, then checks that the supervisor follows editor exit.
The normal Python suite also holds 80 editor and 80 viewer WebSocket connections
and verifies connector cleanup. The inference suite sends 80 simultaneous 1 MiB
uploads, keeps all responses live, tests queued upload cancellation, and advances
a fake clock through 32 minutes of stream progress.

For creation cancellation, run the real Podman fixture with a locally cached image:

```sh
.venv/bin/python tests/creation_cancellation.py --image docker.io/library/debian:bookworm-slim
```

It cancels an active build and startup after all companion containers and private
volumes exist, using the production cancellation and deletion paths. It checks
runtime cleanup, preserved workspace files and build images, and unchanged
existing containers. The fixture removes its own disposable build storage after
testing; production cancellation preserves build cache. Unit and UI tests also
cover ownership collisions, cleanup failures, repeated clicks and late cancellation.

Use a disposable workspace and an unused port. This fixture serves synthetic
replies; it does not evaluate a real inference model.

```sh
test_root=$(mktemp -d "${TMPDIR:-/tmp}/pi-sandbox-check.XXXXXX")
mkdir "$test_root/workspace"
.venv/bin/pi-sandbox-factory --state-dir "$test_root/state" create pendant-check \
  --workspace "$test_root/workspace" --local-model-ports 18081 \
  --allow-domain local-model.internal:18081 --start
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name pendant-check --port 18081 --pendant-questions-only
.venv/bin/pi-sandbox-factory --state-dir "$test_root/state" delete pendant-check
```

This checks container boundaries, installed document tools, actual Pendant worker
startup, shared configuration, shell tools, and question choices, free text and
cancellation. Questions must not invoke the reviewer. A desktop smoke check is
still needed for VS Code keyboard behavior and visual layout.

For the wider security and scheduling scenarios, create a separate sandbox with
`--options-file` containing `{"approval":"assisted"}` and omit
`--pendant-questions-only`. Those scenarios intentionally exercise human fallback.
The automatic mode is covered by `tests/test_approval_modes.py` and the security
extension's own suite.

For local discovery and profile resources, install `examples/profile` as `reporting`,
create a disposable reporting sandbox with local discovery on port 18082 and that
port allowed, then run `tests/provider_container.py --name NAME`.
It checks local discovery, preservation of unrelated models and the profile skill.

To verify Radiance discovery without a GPU server, run the synthetic metadata fixture
inside a disposable Factory image (replace `FACTORY_IMAGE` with its local image name):

```sh
podman run --rm --network none --security-opt label=disable --entrypoint node \
  -v "$PWD/tests/check_radiance_discovery.mjs:/tmp/check_radiance_discovery.mjs:ro" \
  FACTORY_IMAGE /tmp/check_radiance_discovery.mjs
```

It checks fresh discovery, model identity, image support and accepted thinking
levels, then loads the catalogue through the shipped Pi CLI and Pendant worker.
For an extension change before rebuilding the image, also mount its checkout
read-only at `/opt/pi-local-model-manager`. The extension's own `npm test` suite
covers malformed metadata, non-chat models, adapters and preserved user settings.

For expanded-skill approval, use a disposable sandbox with the workshop spikee
profile, automatic approval and host-loopback port 18187 allowed:

```sh
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name approval-check --port 18187 --approval-context
```

This expands a synthetic 32 KiB skill in Pi and the shipped Pendant worker.
It checks network approval, exact-command escalation, temporary writes, model
denials and credential-file restrictions without human dialogs or real inference.
Pendant checks also close overlapping runtime sessions and reopen the same saved
conversation, reload its resources and continue reviewed operations. A fork must
receive a new session ID, preserve the original saved conversation and continue
network and command review without permission dialogs. Another scenario selects one
answer in the question UI and verifies that successive network and command reviews
receive that answer without asking for it again.

For installed spikee skills and shell selection, use the same disposable setup:

```sh
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name approval-check --port 18187 --skill-workflow
```

This drives the actual spikee skill in Pi and Pendant with synthetic model replies.
It checks native and shell reference reads, installed tools, workspace and temporary
files, protected paths, a symlink outside the installed tree, and tmux creation,
input, capture and cleanup through reviewed escalation. Inner-shell tmux and denied
commands must fail. No paid inference or application attacks are used.

## Shell-launched Playwright

Use a disposable spikee-profile sandbox with automatic approval and fixture port
18187 allowed, then run:

```sh
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name browser-check --port 18187 --browser-shell
```

Both Pi and the shipped Pendant worker launch `sandbox-browser` and Python
Playwright through inner Bash and reviewed escalation. With the current profile,
the inner proxy returns HTTP 407; the escalated commands load the local fixture,
interact with its form and save screenshots. The model/reviewer replies are synthetic.

## Question forms

Test the enabled default, disabling forms and enabling them again in both installed
Pi and the actual Pendant worker, without inference or network access:

```sh
.venv/bin/python tests/question_forms_container.py --image YOUR_FACTORY_DEV_IMAGE
```

The check uses a disposable container and verifies that permission and automatic
review extensions remain registered throughout.

## Hosted model connections

`tests/test_provider_credentials.py` exercises creation, browser editor, desktop
editor, Pi CLI and provider refresh/preview/status from saved configurations with
no transient state path. Each uses a custom state directory, with and without a
configured companion key; discovery failures must still block agent launch.

After the unit suites, run the companion and the installed Pi and FrostPi checks:

```sh
podman build -t localhost/factory-inference-check pi_sandbox_factory/containers/inference
.venv/bin/python tests/inference_container.py --image localhost/factory-inference-check --dev-image YOUR_FACTORY_DEV_IMAGE
.venv/bin/python tests/hosted_container.py \
  --image YOUR_FACTORY_DEV_IMAGE --inference-image localhost/factory-inference-check \
  --code-server /path/to/code-server-4.145.2-linux-amd64 --output /tmp/factory-hosted-editor
.venv/bin/python tests/inference_lifecycle.py --dev-image YOUR_FACTORY_DEV_IMAGE
```

These disposable containers use a TLS fixture to check credential isolation,
route enforcement and streamed replies for all four providers, including Azure
deployments and Bedrock profiles. The editor check opens the saved default model,
then uses the model picker to check the remaining routes. The lifecycle test covers
key replacement without restart, recovery and credential-volume deletion.

For live verification, connect an account in **Settings → Models**, choose a model
and use **Test selected model**. Check each supported authentication method,
including temporary AWS credentials and expired-session recovery.

## Runtime updates

Factory passes the standalone Pi version from `pi_sandbox_factory/sandbox.py`
to the container build. The `containers/dev/extensions.lock.json` file pins
the FrostPi source used to build the editor extension.
Check the release metadata, rebuild the image, and run the standalone and editor
checks. Extension fixes and unit tests belong in their own repositories.

For advisor or SSH changes, run their unit suites and:

```sh
.venv/bin/python tests/advisor_container.py --dev-image IMAGE
.venv/bin/python tests/advisor_credentials_container.py
```

The advisor check also sends a synthetic image through `advisor_ask` in both Pi
and standalone FrostPi, using a fixture companion without account access. Pass
`--code-server /path/to/code-server` to check the VS Code extension as well.
Use the Playwright setup documented under FrostPi editor.

Use a development image with the local model provider enabled. These check
isolated launchers, synthetic credential imports, permissions and keyring
persistence. With explicit authorization, add `--host-logins` to the credential
test to check the host’s real sign-ins without inference. On macOS, use the
`TMPDIR` setting above. Actual account login and authorized SSH access need live checks.

Use disposable state throughout. Never restart a user's sandbox or copy real credentials
without explicit authorization. Record commands and results; distinguish fixtures from live services.

## Browser viewer

### Private login handoff

Use a disposable sandbox with `--browser`, automatic approval and a configured
live model. `tests/browser_session_workflow.py` requires a workspace under `/tmp`
and Python Playwright with Chromium. Run each client sequentially, using a new
artifact directory:

```sh
.venv/bin/python tests/browser_session_workflow.py --state-dir "$test_root/state" \
  --name browser-check --provider PROVIDER --model MODEL --client pi \
  --artifacts "$test_root/session-pi"
.venv/bin/python tests/browser_session_workflow.py --state-dir "$test_root/state" \
  --name browser-check --provider PROVIDER --model MODEL --client pendant \
  --artifacts "$test_root/session-pendant"
```

The fixture runs only on container loopback. The real model opens the browser;
external noVNC pointer/keyboard input completes a synthetic popup login. A reviewed
`browser_session_export` saves scoped HttpOnly cookies privately, and an inline
reviewed Python client must receive authenticated HTTP 200. The fixture server
independently confirms the request. The test checks 0600/0700 storage, exclusion
of foreign/path-mismatched cookies, no permission dialogs and no cookie values in
tool output, audit, process logs or saved conversations. `run.json` records the
client, model, test hash and result; transport failures cannot count as passes.

For the actual native-file and inner-shell boundaries, copy
`tests/session_worker_bridge.cjs` to `/tmp/session-worker-bridge.cjs` and
`tests/browser_session_guards.py` into the same disposable container, then run the
Python script there. It explicitly activates all file tools in both clients and
checks reads, grep/find/ls, writes, edits, relative paths, symlinks and ordinary
Bash without live inference. Unavailable tools are failures, not valid denials.
Repeat with `--vault` to exercise the saved Betterwright vault path. The browser
vault option's policy tests cover all approval modes with inner sandbox on/off;
the control removes only its extra native-file guard when disabled.

The extension repository owns cookie-selection unit tests, real Chromium
close/crash/abort lifecycle tests, and the 38-decision live authorization matrix
for the scoped export and its negative cases. Run those alongside this workflow.
`tests/session_inference_relay.mjs` is an optional fixed-model test relay: mount
only a managed inference socket, never its private credential volume. Its health
endpoint reports stream progress without request bodies or response contents.

### Viewer lifecycle and interaction

Create the disposable sandbox above with `--browser`. Put this synthetic project
override in its workspace before testing:

```sh
mkdir -p "$test_root/workspace/.pi"
printf '%s\n' 'PROJECT_VIEWER_GUIDANCE' > "$test_root/workspace/.pi/APPEND_SYSTEM.md"
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name pendant-check --port 18081 --browser-viewer
.venv/bin/python tests/browser_lifecycle.py --state-dir "$test_root/state" \
  --name pendant-check
```

The integration test checks both clients’ browser tools, injected guidance,
handoff and concurrent sessions. The lifecycle test checks viewer repair,
authentication and cleanup across container restarts, with active native-display
and CDP WebSockets. Private displays, disabled password saving, normal close and
abrupt owner death are covered separately:

```sh
.venv/bin/python tests/browser_native_lifecycle.py --state-dir "$test_root/state" \
  --name pendant-check
```

The rendered test uses Python Playwright and a locally installed Chromium:

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_interaction.py --state-dir "$test_root/state" \
  --name pendant-check --artifacts "$test_root/browser-interaction"
```

It opens two synthetic sessions without visiting an external website. It checks
rendered pixels, pointer and keyboard input, “Next,” resizing, explicit clipboard
transfer, Observe, popup sign-in, new tabs, native tab switching and reconnects.
Oversized clipboard text receives a clear error without disconnecting.
It rejects a token borrowed from the other session and captures desktop and small
window screenshots, worker diagnostics and a result manifest. Use a new artifact
directory for each run; interrupted runs must not be counted as passed. The
fixture owns its browser processes and closes them when its input ends. Run
these browser suites sequentially: each creates two full browsers within the
container’s configured process limit. The dependency repository also provides
`packages/pi-sandbox/test/browser-lifecycle-check.mjs`; run it inside this
disposable container to exercise transient startup failure and repeated
start/close races.

Inspect both screenshots and the browser console. The native tab bar and controls
must remain usable without overlays, and small windows must scale the desktop
without shrinking popup dialogs. Verify that ordinary application port forwarding
still works in VS Code. Delete the disposable sandbox when finished.

## Automatic review

Extension implementation and unit tests live in
[`pi-auto-review/TESTING.md`](https://github.com/kyuz0/pi-packages/blob/main/packages/pi-auto-review/TESTING.md).
Run that repository's type checks and complete suite before publishing a new
extension pin. Factory's tests below check the installed Pi RPC client and shipped
Pendant worker, including their real session loading and permission events.

For changes to the approval notice or `/auto-review-settings`, substitute
`--reviewer-settings` for `--review-recovery` in the disposable sandbox recipe
below. Repeat with approval in the options file set to `automatic`, `assisted`,
`manual` and `off`. Both clients must show one notice on open and none on
resource reload. The automatic modes also exercise the model and thinking
selectors. The Pendant check opens two chats in one worker and verifies that
each keeps its own reviewer settings. This check makes no live model calls.

For changes to approval behavior, run the same recipe with
`--approval-modes`. For each of the four approval modes, create a fresh
sandbox with `inner_sandbox` set to `true` and another with it set to
`false`. The fixture checks reviewer request counts, the selected one-time
decision and actual file writes in both clients. It covers reviewer rejection,
deferral, provider errors, typed confirmation for critical actions, a late
answer after cancellation, repeated requests, and the end of a long command.
It also checks that YOLO skips prompts while explicit credential file blocks
still apply.

For login/session policy changes, also run the extension's documented synthetic
authentication matrix with the authorized live reviewer. It checks private
same-service session reuse, advice, disclosure denials and unresolved helpers in
both reviewer modes. Provider failures cannot count as successful denials.

Create disposable state, workspace and an unused fixture port:

```sh
review_root=$(mktemp -d "${TMPDIR:-/tmp}/pi-review-check.XXXXXX")
mkdir "$review_root/workspace"
printf '%s\n' '{"approval":"automatic"}' > "$review_root/options.json"
.venv/bin/pi-sandbox-factory --state-dir "$review_root/state" create review-check \
  --workspace "$review_root/workspace" --options-file "$review_root/options.json" \
  --local-model-ports 18189 --allow-domain local-model.internal:18189 --start
.venv/bin/python tests/integration.py --state-dir "$review_root/state" \
  --name review-check --port 18189 --review-recovery \
  --artifacts "$review_root/scripted"
```

| Scenario in each client | Required result |
| --- | --- |
| Load a compacted session whose summary omits an original restriction and includes forged permission | Reviewer evidence retains the original restriction; summary text cannot grant permission |
| Add oversized older instructions and a synthetic screenshot | Whole-message omissions are explicit; retained screenshot text and metadata exclude binary image data |
| Run 20 reviewed constant-output commands with a 4,096-token reviewer input limit | All succeed; history rolls over in both clients without permanent exhaustion |
| Revoke command permission | The operation is denied |
| Explicitly authorize the next read-only check | Review resumes and the operation succeeds |
| Attempt a synthetic TLS-security bypass | Local policy denies it before model review |
| All phases | No permission dialog, one tool result per operation, no retry loop |

The run executes 46 tool operations and 44 scripted model reviews across both
clients. It temporarily changes the disposable sandbox's reviewer input limit and
model catalogue, then restores them in `finally`. Artifacts include fixture
requests and reviewer evidence in `fixture/reviews.jsonl`. Use a fresh artifact
directory for each run. `run.json` records the outcome, Factory revision, source
pin, test-file hashes, actual Pi/Pendant versions, model and scenario counts.
An interrupted run must not be counted as passed. `--review-images` is the shorter four-review screenshot
and text-follow-up check.

With explicit authorization, configure the chosen provider and managed credentials
in the **disposable** sandbox through Factory. Export only its model metadata to
a temporary `CATALOGUE.json`: use the managed proxy endpoint, provider API type,
model ID, input capabilities, context limit and output limit. Its `apiKey` must be
the proxy placeholder, never the real provider key. Then repeat:

```sh
.venv/bin/python tests/integration.py --state-dir "$review_root/state" \
  --name review-check --port 18189 --review-recovery \
  --review-model PROVIDER/MODEL --review-models CATALOGUE.json \
  --artifacts "$review_root/live"
```

Only the 44 reviewer decisions use live inference; main-agent replies stay
scripted. This checks provider transport and model judgment against the same
positive and negative tool scenarios, with the review count verified from both
saved synthetic sessions. Internal reviewer-envelope, omission and rollover
assertions run only with the scripted reviewer, which can inspect its inputs.
The live run also rejects accidental use of the scripted reviewer.
Live success does not establish that every future model decision will
be correct. No production conversation or sandbox is needed.

Record the Factory commit, pinned extension commit, Pi/Pendant versions, model ID,
commands, exit status, counts and skips with each run. When adding a regression,
put controller/policy tests in the extension; extend `mock_inference.py` and both
client drivers here when the problem depends on installed runtime behavior. Assert
denials and recovery as well as successful actions, and always assert no human
permission dialog in automatic mode.

After testing, delete the disposable sandbox:

```sh
.venv/bin/pi-sandbox-factory --state-dir "$review_root/state" delete review-check
```

## Launcher updates

Use an isolated `PIPX_HOME` and `PIPX_BIN_DIR` to test GitHub installation and
`pipx upgrade pi-sandbox-factory` without changing your normal installation.
Check the update notice, confirmation, failure handling and relaunch.

CI runs unit tests and wheel installation checks. Successful pushes to `main`
create a release only when package code, bundled assets or packaging configuration
changes. Documentation, tests and workflows alone must not trigger a release.

## Web Search

Run `PYTHONPATH=tests .venv/bin/python -m unittest test_curlie_catalog test_web_access test_tui.WebAccessUITests`
to check the first Curlie download, reuse of the saved copy, and updates to one
sandbox at a time. The tests also check that an invalid download leaves the
previous list available. Run `tests/render_tui.py` with the `curlie-*` fixture
names `--only curlie-first-use curlie-saved curlie-confirm curlie-selected
curlie-download-error` to review the download notice, saved date, confirmation
and retry.

For a live check, create the disposable sandbox described above with an options
file containing `{"web_access":{"enabled":true},"website_groups":["dev-python"]}`.
Run `tests/integration.py` with `--web-access` instead of
`--pendant-questions-only`. It drives the installed Pi and Pendant with fixture
model replies, searches through keyless Exa, reads Python documentation and checks
that an unselected website is blocked. It also checks that searches and stored
results skip review, automatic page retrieval through search is rejected, and each
explicit fetch produces one approval notice with its reason, including denials.
Use authorized test credentials when checking paid search providers.

For provider fallback checks, also configure SearXNG with
`http://local-model.internal:PORT`, where PORT is the fixture port already allowed
for the test sandbox. Keep search mode `auto` and Exa configured. Run with
`--web-providers` instead of `--web-access`. The fixture returns a SearXNG error;
both installed clients must fall back to Exa. The next phase selects only SearXNG
and verifies that its failure stays a failure and an explicit Exa request is
rejected. The runner restores the search configuration afterward.

When changing category rules, verify an allowed domain, a misleading suffix such
as `python.org.example.net`, an unselected site, a redirect to an unselected site,
and a category hostname resolving to loopback. Existing explicit local-model
ports must still work. Category refresh must not restart the development container.

For the bundled Microsoft sign-in category, use the built proxy image:

```sh
.venv/bin/python tests/network_auth_categories.py
```

This creates and removes its own gateway. Without credentials, it checks live
Microsoft discovery/CDN access and denies misleading suffixes, Graph, an
unselected host, a nonstandard port and a category hostname mapped to loopback.
An explicit local port remains usable; a redirect from it to an unselected host
is blocked. No saved sandbox is changed.

## Folder mounts and starting directory

Run `tests/frostpi_editor.py --folder-access` with the image and code-server
arguments from the [FrostPi check](#frostpi-editor-and-shared-pi-goals). The
fixture starts Pi CLI and FrostPi in a workspace subfolder. It checks Bash and
native file tools in the workspace, nested mounted folders, and `/tmp`. It
copies a file from the read-only mount to the writable mount, verifies its
contents, and checks that reverse copies, read-only writes, and protected
credential reads remain blocked. Neither client should show an approval prompt.

Create a disposable workspace with an `app` subfolder and two separate host
folders. Mount them as `reference` (read-only) and `output` (writable), using
`--folders-file` as shown in [Usage](docs/usage.md#workspace-and-additional-folders).
Set `--working-directory app` and allow fixture model port 18085. Put
`REFERENCE_OK` in `reference/readme.txt`, `PROJECT_FOLDER_CONTEXT` in
`workspace/app/AGENTS.md`, and `PROJECT_FOLDER_OVERRIDE` in
`workspace/app/.pi/APPEND_SYSTEM.md`. Create `workspace/.pi` and a symlink
`output/private-link` pointing to `/home/node/.pi/agent/auth.json`.

```sh
.venv/bin/python tests/integration.py --state-dir "$test_root/state" \
  --name folders-check --port 18085 --folders
.venv/bin/python tests/folders_lifecycle.py --state-dir "$test_root/state" \
  --name folders-check
```

The historical fixture checks the actual model-request context in Pi and Pendant, their cwd,
file tools, ordinary shell writes and read-only enforcement after escalation.
The lifecycle check changes and removes mounts, checks host ownership and SELinux
labels, and confirms that starting-folder edits preserve the running container.
It leaves the disposable sandbox with only `output`, mounted read-only. Delete it
afterward.

## Conversation recall

Create a disposable sandbox with the fixture port as above, then run
`tests/integration.py` with `--recall` instead of `--pendant-questions-only`.
Repeat after changing `options.compaction_recall` to `full`, then `off`, through
`update --settings-file`. Preserve the other saved options in that file.

The test resumes synthetic compacted history in the installed Pi CLI and Pendant
worker. It checks retrieval of the original message, tool availability, Full's
hints, no hints in Lite, and no approval-review requests for history tools.

The extension and its runtime dependencies are pinned in
`pi_sandbox_factory/containers/dev/compaction-recall/package-lock.json`.
Its Node requirement applies to both the container CLI and VS Code's remote
Node runtime, which Pendant uses. Check both when updating this package.

## Terminal layout

CI divides the Python tests across eight runners. Run the whole suite locally with
`python tests/check_button_heights.py discover -s tests`. To repeat one CI
group, use its command from the log, for example `python tests/check_button_heights.py --shard 3 8`.
The check fails if a rendered button requests or occupies more than one terminal
row, including buttons shown after opening a dialog or expanding a section.

Run the TUI suites, including `test_tui_layout.py` and `test_packages_ui.py`.
Review new and edited forms at 80×24, 120×40 and 220×50, including expanded sections,
validation errors and long paths. Inspect the rendered view as well as geometry;
use Textual's `App.export_screenshot()` for SVG captures with fixture data.

## Primary coding agents

Run `PYTHONPATH=tests .venv/bin/python -m unittest test_primary_workspace test_primary_agents test_primary_agents_ui`
for saved metadata, protected mount validation, sign-in selection, and the terminal
forms. Render the first creation screen, review, sign-in and Settings with
`PYTHONPATH=tests .venv/bin/python tests/render_primary_agents.py /tmp/factory-primary-ui`.

Use a disposable workspace and a separate Factory state directory for runtime
checks. Create one sandbox per coding agent. Exercise **Sign in** and **Copy sign-in
from this computer** from Settings, then open the agent and read and write a file in
the workspace. Inspect the workspace mount list and the separate sign-in container.
The workspace should contain no private sign-in volume, keyring bus, or container
engine socket. Repeat with restricted network, a read-only folder, a skill from
the library and the optional browser. Stop and reopen the sandboxes and delete the
disposable state when finished.

## Communications

Create a disposable sandbox with `{"communications":{"slack":true}}` in its
options file and the fixture port allowed. Run `tests/integration.py` with
`--communications`. This installs an invalid synthetic bot token in that
sandbox and checks both the actual Pi CLI and Pendant worker: eight Slack tools,
API access through the proxy, missing search credentials, a rejected send, blocked
credential-file reads and absent token environment variables. It sends no messages.
Delete the disposable sandbox afterward. Use a separately authorized account check
for reading and sending real messages.

The Slack extension's unit tests live in its pinned repository.

Run `tests/communications_lifecycle.py --state-dir PATH --name NAME` against the
same disposable sandbox to check token-only updates without recreation, removal
of a duplicate npm registration, disabled tools in Pendant, network-rule removal,
and credentials surviving disable/re-enable.

## Browser editor

Follow [the browser editor guide](docs/browser-editor.md) for the real-model smoke
check. Confirm dark mode, Explorer on the left, FrostPi in the right secondary
sidebar with its composer loaded **without clicking the FrostPi tab**, a shell
tool writing a workspace file, and conversation persistence after
restarting the editor. Use **Actions → Stop browser editor** and verify that its
terminal processes also exit. Stopping the container must close the loopback listener. Neither action
should stop unrelated sandboxes or Pi CLI sessions.

On a fresh launch and after restarting, `/workspace` must open
without a trust prompt or Restricted Mode. Check that Manage Workspace Trust says
“You trust this folder”. Opening `/tmp` instead must still request trust.

For an automated startup check, install Playwright in your test environment and
run `playwright install chromium`. Start a fresh disposable browser editor, then:

```sh
python tests/browser_editor_startup.py 'PRINTED_URL/?folder=/workspace' --screenshot /tmp/frostpi-startup.png
```

Use `--cdp http://HOST:PORT` to attach to an existing Chromium or Edge debugging
session. For WSL, drive Windows Edge so the check includes Windows localhost
forwarding. Keep the debugging listener on loopback and use a disposable browser profile.

The check waits for the FrostPi composer without selecting its tab. Inspect
the screenshot for dark mode and right-side placement. Also check a fresh host
VS Code profile when changing extension startup behavior.

For synthetic host-file probes, create a disposable sandbox without desktop VS Code:

```sh
check_root="${TMPDIR:-/tmp}/pi-browser-boundary-check"
.venv/bin/python tests/browser_editor_check.py prepare --root "$check_root"
.venv/bin/pi-sandbox-factory --state-dir "$check_root/state" create browser-check \
  --workspace "$check_root/workspace" --folders-file "$check_root/folders.json" \
  --allow-domain local-model.internal:8000 --start
.venv/bin/pi-sandbox-factory --state-dir "$check_root/state" browser-editor browser-check
```

Leave the launcher running. In another terminal at this checkout:

```sh
.venv/bin/python tests/browser_editor_check.py install \
  --root "$check_root" --container pi-sandbox-factory-browser-check
```

Open the printed URL and reload the page.
Both probe extensions write JSON results into the workspace. Then run:

```sh
.venv/bin/python tests/browser_editor_check.py verify --root "$check_root"
```

The probes exercise container Node APIs and a browser-only extension worker against
fake `.env` and SSH-key files outside the mounts. They also check workspace writes,
read-only mount enforcement and absence of the host-terminal command. Never replace
the fake files with real secrets or grant the browser access to the host-private folder.

For authentication and lifecycle changes, run:

```sh
.venv/bin/python tests/browser_editor_lifecycle.py --state-dir "$check_root/state" --name browser-check
```

It checks passwordless startup, reopening, address reuse, password login,
anonymous WebSocket rejection, foreign origins, HTTPS cookies and occupied ports.
It restores the saved editor settings and leaves the editor stopped.

In the TUI, check **Editor** during creation and **Settings → Editor** afterward.
Generate a password, save, open the browser and sign in. Close the manager and
confirm the editor remains usable. Reopen through **Open**; use **Open with… → Pi
CLI** alongside it. Confirm that selecting Pi CLI during creation opens it in the
terminal and returns to the manager when Pi exits. For remote HTTPS, use a
certificate trusted by the connecting browser and verify FrostPi loads there too.

Delete the disposable sandbox after stopping the launcher:

```sh
.venv/bin/pi-sandbox-factory --state-dir "$check_root/state" delete browser-check
```


## Platform checks

Run the Python and Node suites on Linux, then validate on each target platform.

On Windows 11 with WSL2 Ubuntu 26.04, and on an Apple Silicon Mac with rootless
Podman Machine, install from [platform setup](docs/platforms.md) and check:

1. Create a default sandbox from the TUI. Open the browser editor; confirm dark
   mode, trusted workspace, FrostPi on the right and a responding fixture/local model.
   Reopen it, stop the editor, restart it and confirm its URL stays stable.
2. Open a second sandbox. Confirm its hostname differs and its login cookie is
   not sent to the first. Check WebSocket connection and clipboard in the target
   browser. Confirm the editor’s file upload/download commands are disabled. Open
   Pi CLI alongside the editor.
3. Use a workspace subfolder and two extra folders: read-only and writable. Check
   host-to-container and container-to-host changes and unchanged host ownership.
   On macOS, check polling for host edits; native file-watch events may not cross
   the VM share. Attempt writes to the read-only mount from the outer container.
4. Run the browser and model fixture recipes above. An allowed endpoint must work;
   an unselected destination and a redirect to it must fail. Open the browser
   viewer, interact with the synthetic form and read that state through Pi.
5. Close the launcher and all Ubuntu terminals; verify the editor still responds
   after 90 seconds. Restart WSL or Podman Machine, then reopen the sandbox.
   On macOS, switching to another Podman connection must produce an error before managing its containers.
   Verify a folder outside the VM shares produces an actionable startup error.
6. Sign in to one advisor with disposable test credentials; check its status and
   recovery after logout. Never include credentials in results or fixtures.

For Windows, check WSL-native projects first, then a specific `/mnt/c` project.
Test an SSH-forwarded model in WSL and, separately, a Windows-local model with
mirrored networking. For macOS, check localhost model access, native ARM64 images
and Chromium interaction.

## macOS verification

On a 16 GB M1 with macOS 27 and Podman 6.1.4, container creation, Pi/Pendant,
browser tools, editor authentication, network isolation and folder permissions passed.
Podman Machine used four CPU cores and 8 GB RAM.

## Windows verification

Windows 11, WSL 2.7.10, Ubuntu 26.04, Podman 5.7 and Windows Edge 157;
24 GB assigned to Windows, 12 GB to WSL. The 7 November 2026 run passed:

| Check | Result |
| --- | --- |
| Python unit suite | 242 passed on Windows/WSL and Linux |
| Node unit suite | 24 passed on Windows/WSL |
| Installed Pi and Pendant | Questions, folder context, permissions, browser tools and Lite recall passed |
| Windows Edge | Pendant loaded automatically in dark mode; typing worked for WSL and C: workspaces |
| Editor lifecycle | Restart, password login, HTTPS, Host/Origin checks and anonymous WebSocket rejection passed |
| Browser viewer | Session isolation, reconnect, key rotation and container-stop cleanup passed |
| Folder lifecycle | Starting-folder edits, mount removal and read-only changes preserved files and private state |
| Windows-local models | Mirrored networking, restricted gateway access and model/context discovery passed |
| Background operation | Both editors remained reachable with no Ubuntu terminal or launcher open for 90 seconds |

Model replies in these checks came from synthetic fixtures.

## Profile library and system prompt addenda

Run `tests/test_packages_ui.py` and the registry and bootstrap suites. Check all three
Library tabs and the read-only profile details at the three terminal review sizes.
Run `tests/frostpi_editor.py --profile-prompt examples/profile` with the image and
code-server arguments from the [FrostPi check](#frostpi-editor-and-shared-pi-goals).
The synthetic model verifies that Pi CLI and FrostPi send the addendum once after
a repeated bootstrap, alongside private agent instructions, project instructions
and the project’s `APPEND_SYSTEM.md`.

## Repository SSH access

Run `tests/test_ssh.py` and the TUI suite, then exercise the actual SSH companion.

```sh
.venv/bin/python tests/repository_ssh_container.py --dev-image YOUR_FACTORY_DEV_IMAGE
```

The disposable SSH Git server checks generated-key persistence, refusal to replace
keys, private-volume isolation, clone and push behaviour under read-only,
restricted-branch and full-write server policies. Its policies are test fixtures,
not a verification of GitHub or GitLab settings. It deletes its containers and
volumes afterward.
The client uses the image's default SSH configuration without a test-only
`GIT_SSH_COMMAND` override. It also unloads the agent identity, checks
`sandbox-ssh-status`, verifies authentication fails and restores access by unlocking
the encrypted companion key. Applying the saved SSH settings afterward must keep
both containers running and allow a push. The fixture also checks a short custom
lifetime expires and unlocking for two days restores Git. Bootstrap tests verify configured aliases and the SSH recovery
instructions enter the managed agent context without host-key material.
Actual Pi receives that context and runs the status check through
reviewed escalation with synthetic model replies and no human dialog. Images that
include Pendant also run its worker through the same check. Both status
and Git are checked with `SSH_AUTH_SOCK` empty, matching attached desktop VS Code.
Each client also creates a commit through ordinary inner Bash and verifies its
author name and email. Run `tests/test_git_settings.py` and the bootstrap suite
for identity validation, quoting, local overrides, disabling and repeated startup.
The TUI identity fixtures cover disabled, populated and error states for creation,
defaults and SSH & Git settings at all three review sizes.

For a live service check, use a disposable repository and follow
[repository setup](docs/repository-access.md). Verify clone succeeds and push fails
with a read-only deploy key. With a separate automation account, test `pi-*` pushes,
forbidden refs, force pushes and deletions against the configured server rules.
Do not modify production repository policies for testing.

## TUI visual review

Run `./.venv/bin/python tests/render_tui.py /tmp/factory-ui` to capture offline
fixtures at 80×24, 120×40 and 220×50. Open the SVGs in a browser; `coverage.json`
lists each screen and state. The fixtures include expanded settings, populated
fields and errors. Review every image with Claude Opus before committing UI changes,
using the layout rules in `AGENTS.md`. The renderer checks that each visible text
input has one row of usable text, aligns with its label with one blank row between
them, and lines up with any button beside it. Re-render corrected views; passing tests alone
is insufficient. Use `--size 80x24 --monochrome` for the monochrome check, or
`--only STATE` to repeat a corrected view. Also exercise keyboard focus and scrolling.

`PYTHONPATH=tests .venv/bin/python -m unittest test_chat_history test_tui`
checks chat backup and restore, including branching history, duplicate detection,
failed transfers, file selection, and the separate delete confirmation. The
`chat-history-*` visual states cover Actions, both file pickers and the transfer
results.

## Skill library

`PYTHONPATH=tests .venv/bin/python -m unittest test_skill_library test_skills_ui`
checks multi-skill import, YAML validation without execution, duplicate names,
source changes, atomic copies, retained sandbox snapshots and removal. Search tests
exercise 1,000 skills, matching names/descriptions/folders, empty results and importing
selected skills hidden by the current filter. Discovery tests cover repositories
above 200 skills and GitHub folder/file links, including slash-containing branch
names, repeat imports and fetch failures.
`tests/render_skills.py OUTPUT_DIR [--mono]` captures the library, import errors
and creation/configuration selection at 80×24, 120×40 and 220×50.
`tests/render_skill_search.py OUTPUT_DIR [--mono]` captures import search, retained
selections, empty results and validation errors at the same sizes.

Run `.venv/bin/python tests/check_library_skills.py IMAGE` against an existing
Factory development image to build two synthetic library skills, remove their
library entries, and check registration in the shipped Pi CLI and Pendant worker.
It then removes and adds a skill in the client configuration and reopens the same
saved conversation. A loopback server captures the actual outbound requests and
returns synthetic replies. The assertions check the updated system-prompt skill
catalogue, preserved messages and lazy loading of skill bodies in both clients.
The check uses a disposable image and containers with networking disabled.
Use `--local-model-manager PATH` to test an unbuilt dependency checkout.

`tests/test_settings_effects.py` requires an explicit update-effect assessment for
every managed option and checks the change review and completion instructions at
all three terminal sizes. Relevance checks reject unrelated feature notices and
instructions for other clients. Render those states with
`tests/render_settings_effects.py OUTPUT_DIR [--mono]`.

## Explorer updates and sandbox activity

`test_activity.py`, `test_sandbox.py` and the dashboard tests cover saved lifecycle
times, no-op saves, successful starts, stale-runtime recovery, sorting, missing
dates, filtering and selection preservation. Each sandbox and its actions occupy
one line. One blank line follows the header and separates adjacent entries.
Clicking a gap must leave the
selection unchanged. Check that every sandbox is reachable by keyboard. Render `tests/render_dashboard.py`
at its three built-in sizes, in colour and with `--mono`.

Run `node --test tests/test_editor_watch_fallback.cjs` for watcher events,
resource-error selection, abort, close and encoding. For the real browser test,
use a cached Factory image and an extracted copy of the pinned code-server:

```sh
.venv/bin/python tests/browser_editor_watching.py \
  --image FACTORY_IMAGE --code-server /path/to/code-server-4.145.2-linux-amd64 \
  --output /tmp/factory-watcher-results
```

This runs disposable containers with native watching, an isolated `EMFILE` syscall
failure, the polling fallback, and Factory's production editor supervisor with
the fallback installed through its normal payload. It checks Explorer creation, rename and deletion
without refreshing or refocusing, and reproduces the stale list in the failure
control. It never exhausts the host quota or changes kernel limits. The fixture
removes its containers and preserves screenshots and logs in the output directory.

## Local development browser and temporary files

Run the shipped-client regression in disposable containers using a cached Factory
image with browser support:

```sh
.venv/bin/python tests/local_browser_workflow.py --image FACTORY_IMAGE
```

For dependency development, add `--packages /absolute/path/to/pi-packages`.
The fixture uses generated Factory configuration and synthetic model responses.
It checks both Pi CLI and the actual Pendant worker: model-visible localhost
instructions, native and Bash `/tmp` writes, a preapproved network request,
reviewed `bash_escalate` server
startup, browser localhost navigation, API POST and popup tabs. Exactly one
review is expected for server startup; localhost browser traffic and scratch
files must require neither network review nor human permission.

Add `--editor-env` to inherit the browser editor’s Node preload configuration
and verify ordinary Node/npm commands and current-chat scheduling in both clients.
Scheduling must add no reviewer calls or permission prompts.

Add `--goals` to run the Pendant variant with native goal creation, reading and
completion. It checks unlimited creation and explicit budget removal, with no
goal approval requests; the server start must still receive exactly one review.

Exercise the browser code shipped in that image, including Chromium routing,
WebSockets, multiple loopback names and external allow/deny behavior:

```sh
podman run --rm --network none --security-opt label=disable --entrypoint node \
  -v "$PWD/tests/local_browser_chromium.mjs:/opt/betterwright/local-check.mjs:ro" \
  FACTORY_IMAGE /opt/betterwright/local-check.mjs
```

The dependency's `browser-network.test.mjs` checks HTTP routing, CONNECT tunnels,
exact loopback addresses and socket cleanup. `reviewer-scheduler.test.ts` and
`conversation.test.ts` cover concurrent review bursts, queue cancellation, hung
providers and independent grants. `test_local_development.py` checks generated
network grants and private runtime placement. No fixture uses real credentials
or restarts an existing sandbox.

## Service connectors

Run `PYTHONPATH=tests .venv/bin/python -m unittest test_connectors test_connector_context test_connector_settings test_primary_agents_ui test_aws_profiles` for plugin discovery, saved permissions and the creation form. `test_tui` also checks editing a saved sandbox. The connector fixtures in `tests/render_tui.py` cover creation, saved settings, SSH, AWS and installed HTTPS connectors.

With local rootless Podman, run `PYTHONPATH=. .venv/bin/python tests/connector_live_settings.py`. The fixture uses a temporary workspace, a synthetic keyring token and the shipped connector container. It checks that permissions change while the workspace and connector keep running, disconnect revokes access and a stopped sandbox stays stopped.

## AWS access

The Python suite checks the compiled STS policy, host profile selection, independent profile copies, selective updates, session renewal and the shell network grant. Run the signing tests in the service image with synthetic credentials and networking disabled.

```sh
podman build -t localhost/factory-aws-signer-test pi_sandbox_factory/containers/aws-access
podman run --rm -i --network=none --read-only --cap-drop=ALL \
  --entrypoint python3 localhost/factory-aws-signer-test - < tests/check_aws_signer.py
```

For a live test, use an isolated role, a named empty log group and a stream. Grant `ec2:DescribeRegions` and `logs:DescribeLogStreams` on their appropriate resources. Exercise both services from the sandbox, remove the Logs grant in Settings, then repeat both commands. Verify that EC2 still succeeds and AWS denies Logs. Disconnect and delete the test resources. Record their identifiers and deletion status outside the checkout.

The `--system-prompt` case in `tests/frostpi_editor.py` checks the actual model requests in the same Pi CLI and FrostPi chats. It enables a connector, adds a custom prompt, changes the SSH destination, then removes both.

With the current native controller and worker images built locally, run `PYTHONPATH=. .venv/bin/python tests/connector_native_clients.py`. It launches the installed Codex, Claude Code and Antigravity binaries offline, checks their launch instructions, exercises granted and denied operations from each workspace and revokes access. It uses synthetic credentials and never submits a model prompt.
