# Agent guide

Pi Sandbox Factory manages Pi/FrostPi containers with a Python CLI and Textual UI.
Read `README.md` and `TESTING.md`. For package work, use `docs/profiles.md`,
`docs/providers.md` and the runnable `examples/`.

## Code map

Implementation and shipped assets live in `pi_sandbox_factory/`:

- `sandbox.py`: lifecycle, ownership, immutable snapshots and image identities.
- `folders.py` and `folders_ui.py`: validated host mounts, starting directory and shared folder controls.
- `sandbox_tui.py`: terminal forms; `sandbox_options.py`: shared validation.
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
- `sandbox_advisors.py`, `sandbox_ssh.py`: credential companions.
- `repository_ui.py`: SSH repository setup and public-key controls; repository permissions are enforced by the Git server.
- `advisor_credentials.py`, `advisors_ui.py`: explicit host sign-in copies and advisor selection.
- `containers/`: images, bootstrap and extension pins; `profiles/base/`: generic profile.

The root `sandbox.py` is a checkout convenience wrapper. The installed command
is `pi-sandbox-factory`; `python -m pi_sandbox_factory` is equivalent.

## Design

Profiles provide tools, skills and instructions. Providers configure models.
Neither requires a new launcher branch for a particular company or use case.
Keep private providers and specialist profiles outside this application. A plugin
repository can contain several packages, each installed with `--subdir`.

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

Preserve workspace files, ownership checks and private home state. Model discovery
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

## Capacity and recovery

- HARD GATE — every validation rule needs a concrete reason. Reject input only
  when it would violate a supported format, dependency or platform constraint,
  security boundary, or a demonstrated resource limit. Name the constraint
  beside the check and test its boundary. Remove unexplained caps on counts,
  lengths and ranges. A convenient round number or a pre-existing assertion
  is insufficient.
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
- Exercise the shipped clients with the editor's inherited environment, including
  Node preload settings. Mocked tool calls alone do not establish usability.

## Terminal layout and navigation

### Relevance and cognitive load

- Start with what the user wants to do and give each common workflow a usable default.
  Show the effect of a choice beside it. Keep provider, model and tuning options in
  clearly labeled advanced settings the user can open during setup or revisit later.
  Apply this principle when reviewing the rest of Factory, including existing screens.

- HARD GATE — clear, consistent labels: use plain, direct names for controls. The same concept must have the same label across screens and engines. Never expose an internal implementation term where an ordinary user-facing name exists. Keep a shared name for each repeated concept and regression-test rendered labels and errors. Review visible copy before release; inconsistent or opaque labels block release.

- HARD GATE — stable checkbox copy: the label and help describe what checking
  the box does. Keep both unchanged when the box is toggled. Explain the
  unchecked behavior only when the checked behavior leaves a genuine ambiguity.
  Keep any such explanation visible in both states. Test both states and
  compare the rendered label and help before release.

- Show only information that helps the user make the current decision or take the
  next required action. Derive change notices from the actual diff and selected
  client. Never show unrelated feature exceptions, credentials guidance or advice
  for clients the user is not using.
- Keep short explanations beside their controls. Put longer explanations behind
  a labeled help control near the setting. Use natural, connected language in
  the help.
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
- Give inputs persistent labels; placeholders are examples, not labels. Use the
  same title style and left edge across screens. Full-page forms and action bars
  use the available terminal width with consistent side padding; constrain modal
  dialogs separately.
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

Visual review is a hard gate for TUI changes. Capture every screen and its expanded,
populated and error states at the sizes above; review the images with Claude Opus.
Check shared left edges, panel padding, button padding, label/control/help grouping,
scrolling and visible actions. Fix every deviation before committing. A passing test
suite or a general approval without screen-by-screen findings does not satisfy this gate.
Prefer shared layout rules over per-screen spacing overrides. Contextual buttons need
two columns of inner padding and separation from surrounding prose; modal actions use
the same responsive height as page actions. Indentation must express a parent/child
relationship, never compensate for unrelated alignment errors. In Textual CSS, use
complete margin values when overriding one side; a more specific side override can
clear the other margins. Verify the rendered gap, not just the stylesheet.

## Changes and docs

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

During pre-release development, use the current configuration format and defaults.
Do not add migrations or legacy fallbacks for disposable local test sandboxes.

Run the relevant suites in `TESTING.md`. Extension implementation and unit tests
belong in the extension repository; this project pins and integrates them.
Update pins in `pi_sandbox_factory/containers/dev/extensions.lock.json` or the
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
workflow for changes to `pi_sandbox_factory/`, `pyproject.toml` or `MANIFEST.in`.
Pull requests and manual runs still run the checks. When publishing a release,
use the latest commit on `main` if its package matches the tested commit.
Keep update checks off the UI thread. Upgrades require confirmation and must
preserve running sandboxes.
