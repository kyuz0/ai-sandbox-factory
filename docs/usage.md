# Usage

Use Factory to create, open, and manage your Pi sandboxes. Start with the
**Default Developer Sandbox** for Pi, FrostPi, goal tools, and document tools.
For a different setup, pick **Custom profile…** or install a profile from
**Library…**. [Create a sandbox](../README.md#create-a-sandbox)

## Open a sandbox

Choose **Open** on the dashboard. On supported hosts, Factory opens the browser
editor on this computer without a password by default. Use **Open with…** to
pick a different client for this launch. During creation, expand **Editor** to
choose **Pi CLI** or **VS Code on host** as the default. Clear **Open sandbox after
creation** if you want to open it yourself later. For an existing sandbox,
**Settings → Editor** lets you change the default client, address, port, or password.

Pi CLI opens in your current terminal. Exit Pi to return to Factory. Pi CLI and
FrostPi use the same files, model settings, and saved chats, so you can open
either client. They start in the folder you chose under **Settings → Folders**.
Use a separate sandbox for VS Code on your computer after using the browser
editor. [Editor setup and access](browser-editor.md)

Pi uses your first prompt to name a new chat. It asks the current model for a
short title and falls back to the first few words if the request fails. The
title appears in both clients. Use `/name` in Pi or **Rename** in FrostPi to set
it yourself.

## Copy a setup to a new sandbox

Choose **Create from existing…** beside **New sandbox**. Pick the sandbox to
copy, then enter a new name and workspace. Review the profile, model providers,
network rules, features, and advisors before creating it. Factory includes the
source’s added skills. Clear any you don’t need under **Skills**.

Select **Include search API keys** if you want to copy those keys from a running
source. The new sandbox has its own package copies. It starts with fresh chats,
browser sessions, model and advisor logins, and SSH credentials. Select any
environment file or Pi configuration folder you want to use again.

## Stop, inspect, or delete a sandbox

Use **Actions…** to start, stop, inspect, or delete a sandbox. **Containers…**
shows what each container does and whether it’s running. Press **D** on the
dashboard to change the defaults for new sandboxes.

Closing Factory or the editor leaves the sandbox running. Deleting it removes
its containers, private home, saved chats, and editor settings. Your workspace
folder stays on your computer.

## Back up and restore chats

- **Save a backup.** Choose **Actions… → Export chat history…**. Factory saves
  the original Pi and FrostPi chats, including branches, to a ZIP file on this
  computer. The same action is available when you confirm deletion. Export
  works while the sandbox is stopped and leaves deletion up to you.
- **Restore to another sandbox.** Choose that sandbox, then **Actions… → Import
  chat history…**. Select the ZIP backup or a Pi `.jsonl` file. Factory adds
  chats to the destination’s starting folder and skips chats already saved
  there. Use **Resume session** in FrostPi or `/resume` in Pi to open them.

A backup contains chats only. Keep it private because transcripts can include
tool output and file contents. Project files and sandbox settings are managed
separately.

## Recover from a failed creation

Factory reports missing host dependencies at startup. The first build downloads
Pi, FrostPi and their dependencies. If creation fails, use
**Retry creation** to keep your settings or **Back to configuration** to edit
them. If cleanup fails, select the failed sandbox and choose **Actions… →
Delete sandbox…**.

**Cancel creation** removes that attempt’s containers, private volumes and
saved settings. Cancellation and failure cleanup keep your project files and
the build cache.

## Update Factory and sandboxes

`pi-sandbox-factory --version` shows the installed Factory version. With pipx,
use **Upgrade now** or `pipx upgrade pi-sandbox-factory`. For a source checkout,
pull the changes and reinstall the package.

Update a sandbox with **Actions… → Update sources and recreate…**. Use **Cancel
update** while Factory prepares the image to keep the current sandbox running.
Once Factory starts applying the update, it finishes before returning to the
sandbox list. Reopen the editor and resume your saved chat.

For a sandbox that still uses Pendant, open **Show Alerts → Update to FrostPi…**
when you’re ready to switch.

## Change approval and sandbox settings

| Settings you change | When they apply | What to do afterwards |
| --- | --- | --- |
| Approvals & sandbox, Agent tools, Document search, Skills, Web Search, Communications, Local models & provider plugins, Resources & environment, or Network mode | Apply builds the image, then recreates the sandbox and stops active tasks, terminals and browser processes. Files and saved chats remain. | Choose **Open** in Factory, reload VS Code or restart Pi CLI, then resume your saved chat. Reopen the browser viewer and sign in again if prompted. |
| Network rules: allowed hosts and website categories | Only the gateway restarts. Sandbox processes keep running. | Retry interrupted connections. |
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

Use **Resources & environment** for the container memory limit and environment file.

Reloading tools and instructions does not rewrite earlier chat messages. Engine,
profile, workspace and seed-home choices are fixed for an existing sandbox; create
another sandbox to change them. A failed image build leaves the current runtime
in place.

For a sandbox you have already created:

1. Open `pi-sandbox-factory` on the host and select the sandbox.
2. Open **Settings → Approvals & sandbox**.
3. Set **Use inner command sandbox** and **Protect common credential files**,
   then choose **Approval mode**. **Explain** compares all four modes. Leave
   credential protection enabled unless the agent may read and change `.env`,
   private keys and similar workspace files.
4. Select **Apply and recreate**, then reopen Pi or FrostPi and resume your saved chat.

Applying these settings stops running commands and tmux sessions. Workspace files,
private configuration and saved conversations are preserved. Finish active work
before applying the change.

Factory applies these settings at creation or recreation. In either automatic
mode, `/auto-review-settings` changes the reviewer model or thinking effort for
the open chat. `/reload` restores the saved reviewer defaults for that chat.
When **Protect common credential files** is disabled, the agent can read and
write those files like other workspace files. Changing defaults on the
manager's home screen affects future sandboxes, not existing ones.

See [approval modes and shell behavior](approvals.md#configuration-and-updates).
The outer container and its network policy still apply when the inner sandbox is off.

## Agent tools and skills

**Agent tools** groups conversation recall, the agent browser,
question forms, goals, scheduled prompts and optional subagents. Its collapsed
heading shows the current recall, browser and goals settings.

Select **Enable optional subagents** to let Pi delegate work in either the
terminal or FrostPi. Pi chooses a role for each task. An assistant can use Pi's
native tools, while a specialist can declare a smaller tool list. Background
workers can also load enabled Pi extensions. Workers share the sandbox's
workspace and load its managed permission rules.

Use **Library… → Skills → Add skills…** to import a Git repository
or local folder. GitHub folder and `SKILL.md` links are accepted too.
**Find and validate skills** lists the discovered skills and any validation errors.
Use **Search skills** to filter by name, description or folder; selections stay
checked when hidden by a search. Select several and choose **Add selected skills**. Validation
reads frontmatter and checks files without running skill code. Git sources use
host authentication; local Git folders import committed content.

During creation or in **Settings → Skills**, select any number
of library skills. **Manage skill library…** opens the Library on the Skills tab without
losing the form. Profile skills appear alongside the library selection.

Each sandbox keeps its own skill snapshots. Updating or removing a library entry
does not change saved sandboxes. To use an updated version, deselect its saved
copy and select the new library version. Applying skill changes rebuilds the
sandbox while preserving project files and private home state.

### System prompts

Save reusable instructions in **Library → System prompts**, then select a prompt
during creation or in **Settings → System prompt**. Each sandbox keeps an
editable copy. Changes apply the next time Pi starts a reply, including in open
chats. If this setting shows an update notice, run **Actions… → Update sources and
recreate…** once.

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

Pi and FrostPi receive the container paths and access modes through managed
context in their private `~/.pi/agent/AGENTS.md`. Project instructions remain in
place. With the inner command sandbox enabled, ordinary Bash and Pi file tools
can read the workspace and added folders without an approval prompt for the path.
They can write to the workspace, folders mounted read/write, and the container's
`/tmp`. Read-only mounts stay read-only even for approved commands, and
configuration and credential files keep their usual protections.

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
private volume. Pi and FrostPi connect through the inference proxy with
placeholder credentials.

## Local models

During creation or in **Settings → Model Providers → Local models & provider plugins**,
expand **Local models**, enable **Connect to models on this computer**, and enter
the server ports, such as `8000` or `8000-8002`.
Factory uses [Pi Local Model Manager](https://github.com/kyuz0/pi-local-model-manager)
to discover models and configure their context, image and thinking support in
Pi and FrostPi.

If a local server requires a bearer API key, open **Local server keys…** and
choose its port. Enter a key or pick a saved key for that port. Leave the other
ports empty if their servers allow anonymous requests. Factory checks the key
with that server's `/v1/models` endpoint and supplies it for discovery and chat.
For a sandbox created before this feature, use **Actions → Update sources and
recreate…** once. Later key changes take effect on the next request.

Choose a model in FrostPi’s picker or with `/model`. After changing the running
model, use `/local-models refresh` or **Settings → Model Providers → Local models → Refresh**.

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

Node.js and npm are already installed. Yarn and pnpm can use the same npm registry.
Allow package registries and download hosts in the sandbox’s network settings.
Configure SSH destinations separately to use an isolated Git key.
[Network settings](web-access.md#network-access) · [SSH and Git](ssh.md)

### Included command-line tools

Alongside Git, SSH, Python and Node.js, development containers include:

- Downloads and networking: `curl`, `wget`, `ping`, `ip`, `ss`, `getent`, `dig`, `nslookup`, `nc`.
- Text and files: `jq`, `less`, `nano`, `vi`, `file`, `ripgrep`, `rsync` and the standard shell utilities.
- Archives: `tar`, `gzip`, `zip`, `unzip`, `bzip2`, `xz`.

## See and interact with the agent’s browser

Enable **Agent browser · Betterwright** under **Agent tools**. Ask Pi or
FrostPi to show its browser. Its local link opens the live browser window,
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

If a link stops loading, reopen the sandbox in the browser, then ask
for a fresh link. This repairs the viewer without restarting the browser. Stopping
the sandbox ends its running browser sessions.

### Browser credential protection

**Protect saved browser vault files** blocks Pi’s file tools from reading
`~/.betterwright/vault` by default. Browser account selection and login still work.
An authorized service login can be passed directly to a local script without
exposing the saved vault.

The inner Bash sandbox hides the private home, and reviewed commands follow
the credential policy. Disabling vault protection removes the extra file-tool
guard. With the inner sandbox and approval reviews both off, Bash can read the
vault even with the checkbox enabled. Editor and Pi extensions running as the
same user can access container files and active browser sessions.
[VS Code extension permissions](https://code.visualstudio.com/docs/configure/extensions/extension-runtime-security)

## Conversation recall

New sandboxes include [pi-compaction-recall](https://github.com/shttty/compaction-recall)
in **Lite** mode. Pi can search conversation history removed by compaction with
`history_grep` and retrieve a passage with `history_expand`.
Lite adds no automatic history hints.

Choose **Conversation recall** under **Agent tools** during creation
or under **Settings → Agent tools** for an existing sandbox. **Full**
adds automatic hints and may reduce prefix-cache reuse. **Off** disables recall.
The selection applies to both Pi CLI and FrostPi.
For CLI creation, use `"compaction_recall": "lite"`, `"full"` or `"off"` in the
JSON passed to `--options-file`.

## Sandbox activity

The dashboard shows when you created the sandbox, last saved its Factory
settings, and last started its containers. Editing a project file leaves
**Last edited** as it was. Opening a sandbox that’s already running leaves
**Last started** as it was. Dates use your local timezone.

Use **Sort by** or select a date column to sort the list. Choose **Newest first**
or **Oldest first** to change the order. A dash means Factory has no date for
that event, and rows missing the selected date stay at the bottom.

## Testing a local web application

To preview a local web app, ask Pi to start its development server and open
`http://localhost:PORT` in the agent browser. Pi starts the server with
`bash_escalate` because the browser can’t reach servers started with ordinary
`bash`. The server runs inside the development container. You can also start
it yourself from the editor terminal.

With `bash_escalate`, run the server in the background and write its output to
a log so Pi can continue. For an npm app, use
`nohup npm run dev >/tmp/dev.log 2>&1 </dev/null &`.

Local browser pages, API requests, WebSockets and popup tabs work without network
approval. External websites still use your network rules. Container `/tmp` is
writable scratch space shared between commands; `$TMPDIR` is private to one
command and removed when it ends.
