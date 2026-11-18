# Usage

Run `pi-sandbox-factory` to open the manager. See [installation](../README.md#install)
and [platform setup](platforms.md) for prerequisites. `--version` prints the
installed version. Pipx installations can update from the manager; source checkouts
use Git and a package reinstall.

Select **New sandbox**, choose a name and workspace, review settings, then create the sandbox. The base profile includes Pi, Pendant with built-in goals, and document tools. Install additional profiles through **Library…**, or select a custom profile folder.

**Create from existing…**, beside **New sandbox**, lets you reuse a saved setup.
Choose the source, enter a new name and workspace, then review its profile, model
providers, network rules, features and advisor settings before creating. Added
skills are included; you can deselect them in **Skills**.

Select **Include search API keys** to copy those keys from a running source.
Model and advisor logins, SSH credentials, browser sessions and conversations
start fresh. Environment files and Pi seed directories must be selected again.
The new sandbox keeps independent copies of its packages.

**Open in browser** is the default on supported hosts. The editor opens locally
without a password. During creation, expand **Editor** to choose Pi CLI or
VS Code on host; clear **Open sandbox after creation** to launch later.
Use **Settings → Editor** to change the launch choice, address, port or password.

**Open with…** opens another client without changing the default. Pi CLI runs in
the current terminal; exit Pi to return to the manager. You can use it alongside
the browser editor. Both share files and model settings, with separate conversations,
and start in the folder configured under **Settings → Folders**.

Use a separate sandbox for host VS Code after browser-editor use.
See [browser editor setup](browser-editor.md) for connection settings and access boundaries.

Use **Settings** for configuration and **Actions** for start/stop, shell, logs,
rebuild and deletion. Press **D** on the home screen to configure defaults for
new sandboxes.

Deleting a sandbox removes its containers, private home and editor state while preserving the workspace. Closing the manager or editor leaves containers running.

## Change approval and sandbox settings

Settings show the effect before applying and the reconnect steps afterwards.

| Settings you change | When they apply | What to do afterwards |
| --- | --- | --- |
| Approvals & sandbox, Agent tools, Skills, Web access, Communications, Local models & provider plugins, Resources & environment, or Network mode | Apply builds the image, then recreates the sandbox and stops active tasks, terminals and browser processes. Files and saved chats remain. | Choose Open in browser in Factory, reload an open VS Code window or restart Pi CLI. Resume your saved chat; a new conversation is optional for a clean context. Reopen the browser viewer; websites may require login. |
| Network rules: allowed hosts and website groups | The gateway reloads. Development processes continue; connections may disconnect. | Retry interrupted connections. No editor reload or new conversation is needed. |
| Folders: add/remove mounts or change write access | The sandbox is recreated. Removed host folders are not deleted. | Reopen the client and resume your saved chat. |
| Folders: starting directory only | The next client launch uses it. Existing terminals keep their directory. | Reopen Pi or VS Code. |
| SSH & Git: destinations, repository setup or commit identity | Apply recreates the sandbox. Identity affects future commits; repository-local settings take precedence. | Reopen the client; unlock the SSH key again if needed. Import and unlock buttons themselves take effect without a client reload. |
| Advisors: selection, models, limits or copied sign-ins | Apply recreates the sandbox. Sign in and Check advisors operate without recreation. | Reopen the client after Apply; saved chats can be resumed. |
| Editor: default client and browser-editor connection | Saving stops a running browser editor. | Use Open with… to reopen with the saved settings. Host VS Code requires a separate sandbox after browser-editor use. |
| Model Providers: hosted connection or endpoint | Saved without recreating the development container; active inference may be interrupted. Replacement keys apply to subsequent requests. | Reopen Pi or reload VS Code for new connections/endpoints, then select a model. Removed connections stop serving requests immediately. |
| Model Providers: default model | Used by new conversations. | Change an existing chat's model in its model selector. |
| Model Providers: provider-plugin Configure/Refresh | Updates the provider's model configuration. Preview and Status are read-only. | Reopen Pi or reload VS Code to load the refreshed catalogue. |
| Communications: Slack credentials only | The next Slack tool call reads the saved credentials. | No restart or new conversation is needed. |
| Library or workstation defaults | Library changes do not alter saved sandbox snapshots; defaults affect future sandboxes. | Select updated library skills in Settings → Skills and apply them to an existing sandbox. |

Reloading tools and instructions does not rewrite earlier chat messages. Engine,
profile, workspace and seed-home choices are fixed for an existing sandbox; create
another sandbox to change them. A failed image build leaves the current runtime
in place.

For a sandbox you have already created:

1. Open `pi-sandbox-factory` on the host and select the sandbox.
2. Open **Settings → Approvals & sandbox**.
3. Choose **Approval mode**, set **Use inner command sandbox**, and leave
   **Protect common credential files** enabled unless `.env`, private keys and
   similar workspace files may be shown to the agent.
4. Select **Apply and recreate**, then reopen Pi or Pendant and resume your saved chat.

Applying these settings stops running commands and tmux sessions. Workspace files,
private configuration and saved conversations are preserved. Finish active work
before applying the change.

Factory applies these settings at creation or recreation. In either automatic
mode, `/auto-review-settings` changes the reviewer model or thinking effort for
the open chat. When **Protect common credential files** is disabled, the agent
can read and write those files like other workspace files. Changing defaults on the manager's home screen affects future
sandboxes, not existing ones.

See [approval modes and shell behavior](approvals.md#configuration-and-updates).
The outer container and its network policy still apply when the inner sandbox is off.

## Agent tools and skills

**Agent tools** groups conversation recall, the agent browser,
question forms, goals, scheduled prompts and optional subagents. Its collapsed
heading shows the current recall, browser and goals settings.

Use **Library… → Open skill library… → Add skills…** to import a Git repository
or local folder. GitHub folder and `SKILL.md` links are accepted too.
**Find and validate skills** lists the discovered skills and any validation errors.
Use **Search skills** to filter by name, description or folder; selections stay
checked when hidden by a search. Select several and choose **Add selected skills**. Validation
reads frontmatter and checks files without running skill code. Git sources use
host authentication; local Git folders import committed content.

During creation or in **Settings → Skills**, select any number
of library skills. **Manage skill library…** opens the same library without
losing the form. Profile skills appear alongside the library selection.

Each sandbox keeps its own skill snapshots. Updating or removing a library entry
does not change saved sandboxes. To use an updated version, deselect its saved
copy and select the new library version. Applying skill changes rebuilds the
sandbox while preserving project files and private home state.

## Workspace and additional folders

The project you select is available at `/workspace`. In **Folders**, you can
choose a starting subfolder, such as `apps/api`, for Pi and VS Code. Leave `.`
to open the whole project. The rest of `/workspace` remains accessible.

Use **Add folder…** to share another host directory. It appears at
`/mnt/<folder-name>`, with a name you can edit. Folders start read-only; clear
**Read-only** to allow changes to the host files. System folders,
private sandbox state and overlapping mounts are rejected.

For an existing sandbox, open **Settings → Folders**. Changing mounts recreates
the container and interrupts running processes; its private home and host files
are retained. Changing only the starting folder takes effect the next time you
open Pi or VS Code. Removing a folder disconnects it without deleting its files.
**Create from existing…** includes the extra folders for review and resets the
starting folder to the new workspace root.

Pi and Pendant receive the container paths and access modes through managed
context in their private `~/.pi/agent/AGENTS.md`. Project instructions remain in
place. Read-only access is enforced by the container, including for approved
commands; writable folders also receive the inner sandbox’s usual protections
for configuration and secret files.

For CLI creation, use `--working-directory apps/api` and optionally
`--folders-file folders.json`:

```json
[
  {"source": "/home/user/Documents/reference", "name": "reference", "read_only": true},
  {"source": "/home/user/Documents/reports", "name": "reports", "read_only": false}
]
```

To edit these later, pass an object containing `working_directory` and/or
`extra_folders` to `pi-sandbox-factory update NAME --settings-file settings.json`.
Additional folders require Docker Engine 25+ when using Docker.
Separately mounted filesystems nested inside an added host folder are excluded;
add each filesystem as its own folder if needed.

## Saved model keys

In **Model Providers**, use **Choose key…** or **Change key…** for a managed
provider, or connect a hosted service. Choose a saved key for that endpoint or
enter a new one. Select **Remember on this computer** and name the key to reuse
it in other sandboxes. Factory uses macOS Keychain or a Linux Secret Service
when available. WSL can use an available Linux Secret Service.

Each sandbox has its own active key in its inference companion. Factory checks a
replacement before assigning it. Subsequent requests use the new assignment
without recreating the development container. Replacing or forgetting a saved
key affects the workstation picker. Other sandboxes keep their current copies
until you change or disconnect their assignments.

The workstation index stores names and endpoints. The inference companion
stores its active keys in a permission-restricted, unencrypted file in its
private volume. Pi and Pendant connect through the inference proxy with
placeholder credentials.

## Local models

During creation or in **Settings → Model Providers → Local models & provider plugins**,
enable **Connect to models on this computer** and enter the server ports, such as `8000` or `8000-8002`.
Factory uses [Pi Local Model Manager](https://github.com/kyuz0/pi-local-model-manager)
to discover models and configure their context, image and thinking support in
Pi and Pendant.

If a local server requires a bearer API key, open **Local server keys…** and
choose its port. Enter a key or pick a saved key for that port. Leave the other
ports empty if their servers allow anonymous requests. Factory checks the key
with that server's `/v1/models` endpoint and supplies it for discovery and chat.
For a sandbox created before this feature, use **Actions → Update sources and
recreate…** once. Later key changes take effect on the next request.

Choose a model in Pendant’s picker or with `/model`. After changing the running
model, use `/local-models refresh` or **Settings → Model Providers → Refresh**.

## CLI and storage

```sh
pi-sandbox-factory create example --workspace /absolute/path/to/project \
  --engine podman --memory 8g --local-model-ports 8000 \
  --allow-domain local-model.internal:8000 --start
pi-sandbox-factory open example
pi-sandbox-factory pi example
pi-sandbox-factory down example
pi-sandbox-factory up example
pi-sandbox-factory delete example
```

Saved state lives in `~/.local/state/pi-sandbox-factory`. Pass `--state-dir PATH`
before the command for a separate inventory. TUI defaults live in
`~/.config/pi-sandbox-factory/cockpit.json` (or under `XDG_CONFIG_HOME`).

### Package downloads and website categories

During creation, under **Network → Website categories**, select **Python** for PyPI,
**npm** for JavaScript packages, **GitHub** for repositories and release downloads,
or **Hugging Face** for models and datasets.
For Microsoft sign-in and MFA, select **Authentication → Microsoft Entra / Microsoft 365**.
For an existing sandbox, use **Settings → Network → Website categories**.
[Web access](web-access.md) describes the category sources and automatic updates.

Node.js and npm are already installed. Yarn and pnpm can use the same npm registry.
Packages whose install scripts fetch binaries elsewhere may need additional
`hostname:port` entries, as may private registries.

Choose **Apply rules** to update an existing sandbox. The gateway restarts and
active requests may need to reconnect. Change the network mode in
**Settings → Network → Change network mode…** or host model ports in **Settings → Model Providers → Local models & provider plugins**.

CLI equivalent:

```sh
pi-sandbox-factory network NAME --allowlist /path/to/allowlist.txt \
  --website-groups dev-python dev-npmjs dev-github
```

For SSH hosts or authenticated Git access with an isolated key, see
[SSH and Git repositories](ssh.md).

### Included command-line tools

Alongside Git, SSH, Python and Node.js, development containers include:

- Downloads and networking: `curl`, `wget`, `ping`, `ip`, `ss`, `getent`, `dig`, `nslookup`, `nc`.
- Text and files: `jq`, `less`, `nano`, `vi`, `file`, `ripgrep`, `rsync` and the standard shell utilities.
- Archives: `tar`, `gzip`, `zip`, `unzip`, `bzip2`, `xz`.

Restricted mode forwards allowed HTTP/HTTPS destinations through the gateway;
external ICMP (`ping`) and direct DNS queries remain blocked. Use `curl` against
an allowed endpoint to check connectivity in that mode.

## See and interact with the agent’s browser

Enable **Browser** when creating the sandbox, then open it normally. Ask Pi or
Pendant to show its browser. Its local link opens the live browser window,
including the tab bar and popup windows. Type, click, navigate and switch tabs
normally. When you have finished, tell the agent to continue; it keeps the same
browser state.

Use **Observe** to stop sending mouse and keyboard input, **Paste text** or
**Copy text** for explicit clipboard transfers, and **Reconnect** after a dropped
connection. Small windows scale the desktop; larger windows resize it.
Manual sign-ins are not saved automatically.

The viewer starts automatically and stops with the container. It listens only on
host loopback and requires a session-specific link. Keep that link private: it
allows browser control and appears in the conversation. Website traffic still
passes through the sandbox’s configured network policy.

If a link stops loading, use **Open in browser** in the launcher again, then ask
for a fresh link. This repairs the viewer without restarting the browser. Stopping
the sandbox ends its running browser sessions.

## Conversation recall

New sandboxes include [pi-compaction-recall](https://github.com/shttty/compaction-recall)
in **Lite** mode. Pi can search conversation history removed by compaction with
`history_grep` and retrieve a passage with `history_expand`.
Lite adds no automatic history hints.

Choose **Conversation recall** under **Agent tools** during creation
or under **Settings → Agent tools** for an existing sandbox. **Full**
adds automatic hints and may reduce prefix-cache reuse. **Off** disables recall.
The selection applies to both Pi CLI and Pendant.
For CLI creation, use `"compaction_recall": "lite"`, `"full"` or `"off"` in the
JSON passed to `--options-file`.

## Sandbox activity

The dashboard records **Created**, **Last edited** (saved Factory settings) and
**Last started** (a successful container start). Dates use your local timezone.
Opening an already-running sandbox does not change its start time.

Use **Sort by** or click a date column heading; **Newest first / Oldest first**
changes the order. Missing dates stay at the bottom. **—** means the event was
not recorded, including events before this version. Project file edits do not
change **Last edited**.

## Testing a local web application

Ask Pi to start the development server with `bash_escalate`, then open
`http://localhost:PORT` with the browser tool. You can also start the server in
the editor terminal. When using `bash_escalate`, keep the server in the background
and redirect output to a log, for example `nohup npm run dev >/tmp/dev.log 2>&1 </dev/null &`.
Ordinary `bash` has a separate network namespace, so its
servers are not reachable by the managed browser. Escalation runs inside the
development container, not on your computer.

Local browser pages, API requests, WebSockets and popup tabs work without network
approval. External websites still use your network rules. Container `/tmp` is
writable scratch space shared between commands; `$TMPDIR` is private to one
command and removed when it ends.
