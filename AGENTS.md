# Agent guide

AI Sandbox Factory manages Pi, Codex, Claude Code and Antigravity sandboxes with a Python CLI and Textual UI. FrostPi provides the Pi browser editor.
Read `README.md` and `TESTING.md`. For package work, use `docs/profiles.md`,
`docs/providers.md` and the runnable `examples/`.

## Task completion

Keep an explicit list of assigned tasks and their remaining checks. A new question
or side task does not cancel unfinished work: answer briefly, record follow-up work,
and finish the active task unless the user explicitly changes priorities. Before
moving on, complete the authorized implementation, relevant tests, documentation
and local commit. If blocked, state the specific blocker and what remains. Never
report a diagnosis or proposed fix as completed implementation. Keep publication
and user-environment changes subject to the user's current authorization.

## Public repository

HARD GATE — this repository is public. Review every staged file and commit message
as material that will be published. Permission to research or experiment does not
authorize publishing internal notes.

Commit maintained product code, reusable tests and documentation needed by users
or future maintainers. Put test helpers under `tests/` and use synthetic data.
Keep private research notes, advisor discussions, experiment chronology, raw logs,
conversation transcripts, account details and credentials outside this checkout.
Retain a design explanation only when it is necessary for maintenance and has been
written for public readers. Remove obsolete experiments instead of publishing
them as an archive. Check new and generated files explicitly before every push;
a folder called `research/` or a temporary branch does not make publication private.

## Code map

Implementation and shipped assets live in `ai_sandbox_factory/`:

- `sandbox.py`: lifecycle, ownership, immutable snapshots and image identities.
- `folders.py` and `folders_ui.py`: validated host mounts, starting directory and shared folder controls.
- `sandbox_tui.py`: terminal forms; `sandbox_options.py`: shared validation.
- `operations.py`: shared non-UI workflows. `cli_lifecycle.py` supervises foreground jobs;
  `cli_management.py` and `cli_models.py` adapt commands to those operations.
- `communications.py` and `communications_ui.py`: optional channel tools and private credential configuration.
- `web_access.py`: search modes, provider settings and reusable private keys; `network_groups.py`: trusted category updates and gateway rules.
- `updates.py`: GitHub tags, installed version and pipx upgrade detection.
- `editor_settings.py` and `editor_ui.py`: client choice, private connection settings and shared controls.
- `browser_editor.py` and `containers/editor/launch.py`: code-server, browser connections and process supervision.
- `host_platform.py`: WSL/macOS adapters, Podman Machine checks and host browser launching.
- `browser_viewer.py`: authenticated loopback viewer and container-bound lifetime.
- `registry.py`: Git/local packages, revisions and installation.
- `model_providers.py` and `providers/`: provider image layer and container runner.
- `hosted_models.py`, `models_ui.py`, `providers/hosted-models.mjs`: native Pi hosted-provider setup and model selection.
- `inference.py`, `containers/inference/`: credential companion, fixed routes and streaming proxy.
- `connectors/builtin/` holds one folder per built-in service. SSH & Git and AWS provide settings and agent instructions. Azure and Google Cloud contain only metadata for the planned services.
- `connector_fields.py` provides the shared list for creation and Settings. `connector_dialogs.py` edits installed service grants. `connector_settings.py` applies changes. See [the connector author guide](docs/connectors.md).
- `sandbox_advisors.py`, `sandbox_ssh.py` and `aws_access.py` manage their credential companions.
- `repository_ui.py`: SSH repository setup and public-key controls; repository permissions are enforced by the Git server.
- `advisor_credentials.py`, `advisors_ui.py`: explicit host sign-in copies and advisor selection.
- `containers/`: images, bootstrap and extension pins; `profiles/base/`: generic profile.

The root `sandbox.py` is a checkout convenience wrapper. The installed command
is `ai-sandbox-factory`; `python -m ai_sandbox_factory` is equivalent.
The distribution and command are `ai-sandbox-factory`. The old command and module
are maintained by the separate migration launcher in `kyuz0/pi-sandbox-factory`.
Host data uses `ai-sandbox-factory` folders. Keep existing container, volume,
protocol and keyring identifiers unchanged; they preserve live environments.

## Design

Profiles provide tools, skills and instructions. Providers configure models.
Neither requires a new launcher branch for a particular company or use case.
Keep private providers and specialist profiles outside this application. A plugin
repository can contain several packages, each installed with `--subdir`.

Keep service-specific options together under Connectors during creation and in Settings. For example, the Git commit author belongs with SSH & Git even when no SSH destinations are configured. Derive agent instructions from the saved grants. Never include tokens, private keys or host sign-in paths. Test the enforced permissions separately from the prompt.

Git authentication happens on the host. Managed inference credentials stay in a
separate companion volume; never send them to provider adapters, Pi configuration,
environment variables or editor containers. Adapters with an `inference` declaration
receive a proxy endpoint and placeholder. Profiles and adapters are trusted executable
packages; the registry is not a security sandbox. Saved sandboxes use snapshots, not mutable
registry directories. Failed updates must preserve the running environment.

Keep the inner command sandbox, outer container, gateway and credential companions
separate. Approval never grants host access. Automatic approval returns failures
to Pi without a human dialog; assisted mode enables human fallback.
`ask_user_question` goes directly to the user without reviewer authorization.

Search defaults and sandbox copies must keep credentials separate from options.
Match keys to both provider and endpoint; a copy without keys must not inherit
workstation defaults. Full configuration copies reuse package snapshots, with fresh
workspace selection and private state; never copy credential paths or seed homes.
Exercise fallback and Only-provider behavior in both clients.

Preserve workspace files and private home state. Model discovery
must work before a Pi session exists and allow opening the editor while offline.
Extra folders use `/mnt/<name>` and default to read-only. The starting directory
is relative to `/workspace` and applies at launch. Keep mount validation, inner
write roots, native tool permissions and agent context derived from the same
folder configuration. The managed global `AGENTS.md` block adds context without
replacing project instructions. Folder grants never become workstation defaults.
Keep the UI usable at 80×24. Test the shipped FrostPi webview and standalone Pi for bootstrap or
extension changes, not just mocked APIs. Never restart a user's sandbox to test.

Host-specific behavior belongs in `host_platform.py`. Windows runs inside WSL2;
macOS uses a local rootless Podman Machine. Keep native Linux routing unchanged.
Use container architecture for binary downloads, not the Python host architecture.
Browser supervisors must work without `fork`, `/proc` or Linux pidfds on the host.
Keep generated browser origins separate per sandbox and validate Host and Origin.
Do not solve VM mount failures with recursive ownership changes or wider host mounts.

### Trust boundaries for files

- Factory trusts the host and the data folders the user selects. Honor host
  readability, the sandbox's mount modes and the granted roots. Do not require
  the user's UID or private-directory modes on a selected project or data folder.
  Protect Factory-managed credentials at their storage and mount boundaries.
  A file or folder explicitly supplied for Knowledge search defines the scope.
  Do not add filename filters or interpret Git ignore patterns as search rules.
  Keep Factory-managed credentials outside the files, processes and configuration
  exposed to sandbox agents and editor extensions.
- Check what a file rule actually protects. Test a permitted case alongside the
  denied case. For mount and indexing changes, exercise group-writable folders,
  read-only mounts and Windows-backed WSL2 paths when affected. Use the real
  client path so dependency checks and image build steps are exercised.
- When generated client settings depend on multiple saved choices, test changes
  to each choice independently. Confirm that editing one setting gives the same
  effective permissions as a fresh setup with those choices.

## Async work and test isolation

- Publish model-visible settings only after the owning change succeeds. Test
  resumed conversations with the pinned clients' actual model requests, not
  just generated hook files. Cached instructions must not contradict current
  settings. Verify the payloads emitted by the installed hook implementation.
- A delivery receipt records what the completed turn saw, not what the host
  currently wants. Test A → B → A changes during completion and cancellation,
  overlapping callbacks, failure/retry and unchanged-turn suppression.
- Keep initial sandbox instructions intact. Later notices identify changed
  settings and point to Factory's current read-only reference; do not repeat
  standing guidance or unrelated settings. Keep full values and relevant examples
  in that reference, and test prompt size, readable access and credential exclusion.

- Give every queued UI event, timer and worker result an owner. Resolve widgets
  on the UI thread and check that the owner is still attached before rendering.
  Check request identity on success and failure so an older result cannot
  overwrite a newer one. When a list has changed, verify the option itself
  before using a queued event’s index.
- Test the affected workflow with work still in flight. Cover dismissal,
  replacement or application exit as applicable, plus failure and retry.
  Use a gate or event to force the ordering and assert which screen, data and
  feedback remain after both callbacks finish.
- Acknowledge accepted actions in their handler. Update buttons and status when
  the action changes their state. Deliver known worker results to any open
  details view. Polling can read ongoing progress. Tests for these transitions
  should pause the poll and assert the visible state. When a test expects a
  genuinely asynchronous change, wait for that specific result instead of
  assuming a timer ran during `pilot.pause()`. For real subprocesses, wait for
  a worker milestone and include its status and recent output on timeout.
- Give tests that mock lifecycle work a private lock directory and pass the same
  directory to their subprocesses. Real-engine tests use production lock paths
  and unique sandbox names. Arrange contention explicitly when testing it.
  Exercise affected tests alongside real jobs before calling the workflow finished.
- When adding process supervision, check which process each interruption test
  targets. Test worker death separately from cancellation of its supervisor.
- Check the saved state and engine names before starting a creation job. Recheck
  under the lifecycle lock before allocating resources. Test stopped containers
  and volumes left after container deletion, including cancellation of either
  confirmation and preservation of similarly named resources.

## Capacity and recovery

- HARD GATE — actionable errors throughout AI Sandbox Factory: never hide a
  failure behind “invalid”, “failed” or “unavailable” alone. Identify the operation
  and rejected value or affected object, explain the exact constraint or underlying
  cause, and retain available exit codes and diagnostic output. Provide a useful
  next step. Redact credentials, not the reason for failure. Test the visible error
  with realistic invalid input and upstream failures; generic replacements block release.

- HARD GATE — preserve diagnostic errors: never replace an upstream or proxy
  failure with an unexplained generic error. Return the actual upstream status
  and error, or the transport error code and message, after removing credentials.
  Record the failure stage, time, request ID and underlying cause in container
  logs. Use the same request ID in the client response. Mark any truncated error
  explicitly. If a stream has already started, log its failure without changing
  the streaming protocol. Test diagnostic visibility and credential redaction
  together, including connection resets and failures during streaming.

- HARD GATE — no elephant-in-a-crystal-shop workflows. Use the smallest change
  that does what the user asked. Preserve workspace files, saved conversations,
  sign-ins, and running work wherever the feature allows it. Replacing a
  container or erasing state requires a concrete technical reason. Prepare and
  validate the replacement before interrupting work. When replacing a running
  sandbox, keep the previous runtime available until the new one works. Test
  what happens when it fails. Preserve whether the sandbox was running or stopped.
- When keeping data for recovery, provide a named UI entry with its source and
  date, and tested export and restore actions. Job output alone is not a usable
  recovery workflow. State who keeps the data, when and where it is stored, and
  what deletes it.
- Check conditional actions inside their parent's visible area. A button with
  the right height can still be hidden by a zero-width or clipped container.
- Before applying an unavoidable restart or loss, name the sandbox, the work
  that will stop, the data that will be removed, and the data that will survive.
  Put those consequences and a clearly named action together. Offer a backup or
  export before confirmation when saved data is at risk. For Pi chat history,
  use **Actions… → Chat history… → Export chat history…**. Test that the confirmed action matches
  the lifecycle path and that unrelated settings changes skip the warning.

- HARD GATE — every validation rule needs a concrete reason. Reject input only
  when it would violate a supported format, dependency or platform constraint,
  security boundary, or a demonstrated resource limit. Name the constraint
  beside the check and test its boundary. Remove unexplained caps on counts,
  lengths and ranges. A convenient round number or a pre-existing assertion
  is insufficient.
- Reconcile declared profile services through reviewed Apply: additions,
  replacements and removals are supported changes. Preserve recovery data and
  test rollback; do not replace missing lifecycle implementation with a blanket refusal.
- Treat an ordinary sustained editor, agent and browser session as the minimum
  workload. Capacity changes must exercise at least ten times the measured
  baseline in the affected layer, with cancellation, cleanup and fault recovery.
  Record the workload and results; a component stress test is not proof of an
  untested end-to-end workload.
- Do not invent fixed process counts, memory quotas, concurrency caps or duration
  cutoffs. Use explicit user settings or a documented protocol/resource need.
  Use backpressure for transient pressure rather than rejecting valid work.
- A transient relay failure must not stop active editor or agent processes.
  Retrying must preserve the same destination and configured network rules.
- Container replacement checks must begin with the actual browser editor open.
  Preserve its address and connection settings on success and rollback; verify
  an authenticated request at the original address. Also cover no open editor,
  a stopped sandbox and service-only operations that leave the workspace alone.
  Exercise ordinary settings updates and rebuilds without profile services as
  well as profile Apply. A profile-only test does not cover the ordinary path.
- Exercise the shipped clients with the editor's inherited environment, including
  Node preload settings. Mocked tool calls alone do not establish usability.

## Terminal layout and navigation

### Relevance and cognitive load

- HARD GATE — keep review forms to the current choices and concise summaries.
  Put background explanations and lifecycle reference text behind a nearby
  More information… control. Do not fill ordinary forms with help paragraphs
  or bullet lists. Test that this text is absent until help is opened and that
  dismissing help preserves the form. Keep necessary consequences beside the
  affected choice or its confirmation.

- Start with what the user wants to do and give each common workflow a usable default.
  Show the effect of a choice beside it. Keep provider, model and tuning options in
  clearly labeled advanced settings the user can open during setup or revisit later.
  Apply this principle when reviewing the rest of Factory, including existing screens.

- HARD GATE — clear, consistent labels: use plain, direct names for controls. The same concept must have the same label across screens and engines. Never expose an internal implementation term where an ordinary user-facing name exists. Keep a shared name for each repeated concept and regression-test rendered labels and errors. Review visible copy before release; inconsistent or opaque labels block release.

- Give text a visible hierarchy throughout the TUI. Use section headings, bold
  field labels, plain values, secondary help and distinct command blocks. Use
  the shared `Details` and `Command` widgets for read-only facts and commands;
  never guess a text role from punctuation or style untrusted values as markup.
  Check that the roles remain distinguishable in monochrome.

- HARD GATE — plain, complete instructions: give the reader enough context to
  know what the action affects. Reserve short verb phrases for controls and
  headings. Never write “Create and start downloads about 180 MB.” It omits
  the sandbox and makes a button label sound like the thing that downloads
  the file. Write “The Curlie list (about 180 MB) will be downloaded when you
  create this sandbox.” Use the screen’s context to keep copy short. Write
  “This sandbox uses the list downloaded on 19 Nov 2026” and “A newer list
  downloaded on 20 Nov 2026 is available.” Repeat the product name only when
  it gives the reader information they need. Apply this rule to all UI copy,
  documentation, progress messages and errors.

- Explain unfamiliar terms briefly when they help a choice; for example, connect
  “embedding model” with semantic search. Keep longer explanations in contextual help.

- HARD GATE — changeable configuration: identify summaries of selected behavior as
  current or selected settings and make the existing control for changing them clear.
  Do not describe a selected option as a permanent capability or limitation of Pi.
  Explain a mode by naming it or using “With this mode selected…”. Keep permanent
  security guarantees distinct from configurable defaults. Preserve stable checkbox
  labels and help; their descriptions still explain what checking the box does.

- HARD GATE — stable checkbox copy: the label and help describe what checking
  the box does. Keep both unchanged when the box is toggled. Explain the
  unchecked behavior only when the checked behavior leaves a genuine ambiguity.
  Keep any such explanation visible in both states. Test both states and
  compare the rendered label and help before release.

- Show only information that helps the user make the current decision or take the
  next required action. Derive change notices from the actual diff and selected
  client. Never show unrelated feature exceptions, credentials guidance or advice
  for clients the user is not using.
  Hide an optional copy choice when the source has nothing to copy. Show a
  disabled choice only when a concrete action can make it available.
- HARD GATE — coherent treatment across the UI. Use the same labels, grouping,
  defaults, initial keyboard focus, shortcut hints and feedback for equivalent
  choices. Compare shared settings across coding agents and keep their labels
  and relative order consistent where they serve the same purpose. Group a
  setting differently when the available choices change what the user can do.
  Keep keyboard focus distinct from the primary action as the layout adapts
  to different terminal sizes. Highlight, move or add a separate notice for
  a setting only when it affects the current decision or requires a separate
  action. Apply the same rule to every setting in that situation. If a disabled
  option requires action, explain how to enable it and what happens if the user
  continues without it. Show a setting’s value beside its control, including
  when it is off. Section headings can simply name the settings they open.
  Explain what a copy includes when the user chooses what to copy, and show
  individual values in the review step. A separate “Web Search is off” line
  on the first screen of the copy wizard fails this rule. Show the value beside
  Enable Web Search in Review sandbox.
  When reviewing a change, compare the affected screens and states side by
  side. For every difference in wording, visibility, emphasis or behavior,
  check that it helps the user make a different decision or take a different
  action. Remove differences that serve no purpose.
- Keep short explanations beside their controls. Put longer explanations behind
  a labeled help control near the setting. Use natural, connected language in
  the help.
- Do not add help text to a self-explanatory control unless it explains a
  consequence the user needs to know. “Allow file uploads” needs no sentence
  underneath about copying files into a writable folder in Explorer. Put upload
  instructions in the relevant help or documentation.
- Preserve existing details when moving or condensing settings help or
  documentation. Removing a detail requires explicit user authorization.
- Render independent actions, effects and changed settings as separate bullets.
  Never compress a list into a paragraph or a semicolon-separated sentence.
  Use a short sentence only for one coherent fact.
- Keep completion feedback to the result and necessary next steps. Put background
  explanations in help or documentation; do not repeat all possible caveats on
  every successful operation. Show restart/reload requirements before Apply and
  actionable reconnect steps after success, never after failure.
- Include a notice only when its condition is true: for example, Slack credential
  guidance requires a Slack credential change; Git identity guidance requires an
  identity change. Choose the reconnect instruction for the selected client.
- QA must test both presence and absence of conditional notices, inspect narrow
  screens for readable bullets and visible actions, and remove irrelevant or
  repeated text before release.
  After focus and layout settle, check that validation errors are actually visible,
  including short errors in scrolling forms and monochrome terminals.

- Group each label, control and explanation in one `.setting`. Put the help
  below its control and leave one blank row between groups. In compact forms,
  leave one clear row above and below a borderless input or dropdown so its
  value is distinct from the label and help. Count the visible rows once:
  the space below the last field also separates it from the next group.
  Use the shared `.spaced-form-fields` style. Test for both missing and
  doubled gaps using rendered positions. Check the whole ordinary form,
  from its heading through its buttons, at 80×24, 120×40 and 220×50.
  Long forms and errors can scroll inside the dialog while actions stay visible.
- Single-line text inputs and closed dropdowns use one terminal row at every
  terminal size, including when focused. Opening a dropdown shows its option list.
  Leave one blank row between its label and the field. Keep adjacent Browse,
  Generate or other field buttons on the same row and at the same height.
  Check that the text stays visible on focus and after validation errors.
  Align checkboxes with the other controls in their group. Indent controls
  within an explicitly nested section. Use the shared checkbox from `tui_widgets.py`.
  Test alignment against the visible label or adjacent field. An existing
  offset or screenshot is not a layout requirement. When the design changes,
  correct conflicting assertions and review the rendered result before updating screenshots.
- Keep related choices in a consistent order and use the same label pattern.
  Advisors are Codex (OpenAI), Claude (Anthropic), Antigravity (Google).
- Give table headers and data rows one line each. Leave exactly one blank line
  below the header and between adjacent rows. Use the same dark background for
  every data row and a visibly different background for the gaps. A gap must
  ignore clicks. Check a populated table with the first, middle and last rows
  visible, then scroll and check the fixed header again.
- Open text-entry dialogs with the cursor in the first editable field. If the
  user must choose a provider or a saved key first, focus that selector. Test by
  opening the dialog and typing immediately, including while search results load.
- After a user chooses Upgrade, Apply or Recreate, highlight that action and
  focus it in the confirmation. For deletion, disconnection or cancelling an
  update in progress, focus the choice that preserves the current state. Keep
  Cancel visually secondary. Test Enter immediately after opening and Escape.
- Give inputs persistent labels. Use placeholders for examples and keep titles
  and fields aligned at the left edge. Size setup forms to their contents and
  give them a maximum width. Let the form shrink in a narrow terminal, and
  keep its actions aligned to the form edges. Size short fields for the
  values they hold. Give paths and longer text room where it helps. Use extra
  width where it improves readability, such as a table with many columns.
  Stack dependent choices in order, with Profile below Coding agent. Check
  that widening the terminal leaves compact controls and actions together.
- Align page Back/Cancel left and the primary action right, at the form edges.
  Group dialog actions on the right, Cancel before Save. Keep contextual actions
  beside their object. Every button uses one terminal row at every screen size. Keep vertical padding
  outside the button. A taller button requires explicit user consent and a
  named exception in the build check. There are no approved exceptions.
  Dock page actions below scrollable
  fields, with three columns of side padding and a blank row above and below
  their contents. Reserve the status-bar row separately; it must not cover the
  bottom padding. Put after-save choices, such as opening the editor, beside the final
  action rather than between unrelated sections. Keep buttons visibly distinct
  from their section background; constrain dialogs and scroll their fields so errors cannot hide Save.
  Escape cancels dialogs without applying changes.
- Put common tasks in the visible Settings menu. A shortcut should open and focus
  the existing form, not duplicate its save logic. Keep optional setup collapsed
  and show its state in the heading. Open Model Providers on initial sandbox review.
  Separate adjacent review sections with one blank row. Disable fields whose controlling option is off.
- Review rendered screens at 80×24, 120×40 and 220×50, including expanded sections, long
  paths, errors and keyboard focus. Check monochrome checkbox states too. Geometry
  tests complement visual inspection; neither replaces the other.

For TUI changes, capture and visually inspect the affected screens and states
at the sizes above. Check alignment, padding, label/control/help grouping,
scrolling and visible actions. Fix layout problems before committing and record
the findings. Passing automated tests alone does not complete visual review.
Prefer shared layout rules over per-screen spacing overrides. Contextual buttons need
two columns of inner padding and separation from surrounding prose; modal actions use
the same responsive height as page actions. Indentation must express a parent/child
relationship, never compensate for unrelated alignment errors. In Textual CSS, use
complete margin values when overriding one side; a more specific side override can
clear the other margins. Verify the rendered gap, not just the stylesheet.
Application CSS can override a screen's `DEFAULT_CSS` regardless of selector
specificity. Put overrides at the appropriate stylesheet precedence and check
rendered control text as well as widget bounds after a label changes.

## Changes and docs

Keep CLI and TUI parity for new or changed Factory operations. Share behavior
and validation in non-UI code and document the CLI path; UI-only navigation needs
no command. Test domain behavior through shared tests, then check each affected
interface; retain the real lifecycle and recovery gates rather than duplicating
the whole suite per interface.

- HARD GATE — fresh sandboxes: release checks must exercise first launch with
  existing project settings in both Pi CLI and FrostPi, without manually seeding
  trust in the test. Project resources must load, ordinary workspace operations
  must work, and project configuration must not override managed permission rules.
  A failing fresh-sandbox check blocks release.

- HARD GATE — lifecycle jobs: test the real creation worker with a provider
  selected. The fixture must exercise startup, provider setup, a diagnostic
  failure, rollback, and retry with the same name. For lock changes, test both
  the Factory child that must reuse the lock and an independent command that
  must be refused. Route subprocesses that call Factory from a job through
  the lock handoff, including commands whose output is captured. Mocked calls
  and a successful build do not establish that creation or an update works.
  The fresh-sandbox CI job runs this check before any release.

Identify repositories as `owner/repository`, with a link when available. Name fork
parents and upstream repositories separately. Never use an unqualified repository name.

Keep a checklist of outstanding implementation, verification and Git work for
each affected repository. Recheck it before reporting completion. Commit the
required source, tests, assets and dependency pins on the authorized branch.
Verify them from the commit in a fresh checkout. Check for untracked files in
each repository, and record any test that still needs a result. For sign-in
features, test an authenticated action with the promised credential isolation.
A sign-in screen alone verifies only that the client opened.

### Definition of finished

A request to make a change includes committing and pushing the verified change
to the intended remote branch. Do this without waiting for a separate request.
Follow any instruction to hold the push or show the change for review first.
Keep unrelated changes in the checkout out of the commit.

Before calling the job finished, check the requested behavior, commit the
changes, push them and confirm the commit is on the remote branch. Verify any
release or upstream pull request included in the request. Tell the user what
shipped, where it landed and what checks passed.

Check `git status` before taking on the next job. Finish, commit and push the
current job's changes before switching tasks. If the user asks you to pause or
review the work, or if a decision, test or push is blocked, report the exact
uncommitted files, what is still pending and why. Keep the paused work accounted
for so it can be picked up without being forgotten.

Production changes must land on `main` after testing, both here and in every
dependency repository changed for Factory. Temporary branches are allowed, but
publishing Factory with production work left only on a dependency branch is
forbidden. Pinned Git dependencies must be reachable from their repository's
`main` branch; the release checks enforce this gate.
Before committing a dependency pin, verify the exact SHA is published and is an
ancestor of remote `main`. Public PRs for separate commits do not establish that
the combined pinned source is published. Test the exact pinned source.

During pre-release development, use the current configuration format and defaults.
Do not add migrations or legacy fallbacks for disposable local test sandboxes.

Run the relevant suites in `TESTING.md`. Extension implementation and unit tests
belong in the extension repository; this project pins and integrates them.
Async, thread-pool, browser and proxy checks require local socket IPC. A restricted
shell can allow creating a socket pair but block writes, leaving Python waiting
at thread-pool shutdown. Use execution permissions that allow local sockets;
verify a minimal control before diagnosing a codebase hang. Never count an
interrupted check as passed or add a product workaround for a shell restriction.
Update pins in `ai_sandbox_factory/containers/dev/extensions.lock.json` or the
security directory’s `sources.json`.
FrostPi uses the standalone Pi already installed in the container. Factory pins
FrostPi source in the extension lock and builds its VSIX from that commit.
Conversation recall defaults to Lite in both clients; keep its mode tied to the
saved sandbox options. Its dependency lock lives in `containers/dev/compaction-recall/`.

Write short, direct documentation explaining supported behavior and actual setup.
**Hard gate for user-facing copy:** assume readers know what a local model is.
Remove reminders about where local chat models process data from docs, starter
guides and UI help. Document the actual controls and setup steps.

Each sentence should explain an action, mechanism, contract or actionable limitation.
User setup guides describe normal installations on supported hosts. Keep test
infrastructure and validation history in `TESTING.md`, not in user prerequisites.
Keep prerequisites specific and examples runnable. Remove repeated explanations,
generic cautions and statements that merely restate a control’s label. Link to
an existing explanation instead of duplicating it.
Keep private names, credentials, conversation history and investigation logs out of
this public project. Add an example or test when it explains a contract better
than another paragraph.

Release versions use UTC CalVer. On pushes to `main`, GitHub starts the release
workflow for changes to `ai_sandbox_factory/`, `pyproject.toml` or `MANIFEST.in`.
Pull requests and manual runs still run the checks. When publishing a release,
use the latest commit on `main` if its package matches the tested commit.
Keep update checks off the UI thread. Upgrades require confirmation and must
preserve running sandboxes.
