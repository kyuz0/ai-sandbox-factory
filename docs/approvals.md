# Approvals

The approval mode tells Pi who decides when a tool needs permission. You can
let a reviewer decide, have Pi ask you, or turn approval prompts off.

For Codex, Claude Code and Antigravity, open **Settings → Approvals**. Choose
the agent’s automatic mode or **Ask for approval**. Restart the sandbox when
you want the new mode to take effect. [Native agent approval controls](browser-editor.md#approvals-and-web-search)

## Configuration and updates

1. Select your sandbox in Factory and open **Settings → Approvals & sandbox**.
2. Choose an **Approval mode**. Select **Explain** to compare the four modes.
   You can also change **Use inner command sandbox** and
   **Protect common credential files** here.
3. When you’re ready to stop running work, choose **Apply and recreate**. This
   stops active processes, including tmux sessions. Your files and saved chats
   stay in place. Reopen Pi or FrostPi and resume your chat.

| Mode | Who decides |
| --- | --- |
| **Auto (non-interactive)** | The reviewer decides. If it declines, Pi gets the reason. This is the default. |
| **Auto (interactive)** | The reviewer goes first. If it declines or asks you to decide, Pi shows the action and the reason. You can allow it once or refuse. If the reviewer rates it critical, Pi asks you to type `APPROVE` to allow it. |
| **Manual approval (ask me)** | You decide. With the inner sandbox on, ordinary Bash can run there without asking. Pi asks before running a command outside it. With the inner sandbox off, Pi asks before every Bash command. File tools ask when their own rules require permission. |
| **YOLO (no approvals)** | Pi skips approval prompts and reviewer checks. Ordinary Bash stays in the inner sandbox while it’s on. Turning it off runs Bash directly in the main agent sandbox. |

Pi shows approval prompts in its terminal or in FrostPi. If the reviewer fails,
the action stays blocked in both Auto modes. File and network restrictions still
apply in all four modes.

## What the reviewer checks

By default, both Auto modes use your chat model for a separate review. It sees the
exact action, your instructions from the current chat, and relevant tool results.
The reviewer makes one model request for each action and has no tools of its own.
Each approval covers one action.

Your configured chat and embedding services are trusted for their normal work.
Chat services receive the conversation and file content included in model requests;
embedding services receive document passages when indexing and queries when searching.
The reviewer treats this processing as authorized use of the connections you selected
and does not ask for separate permission because a service is remote. This applies to
the configured services, including their managed proxies, rather than arbitrary upload sites.

```mermaid
flowchart LR
    USER[Original user instructions] --> REVIEW[Separate reviewer conversation]
    ACTION[Exact proposed action + evidence] --> REVIEW
    REVIEW -->|allow| GRANT[One-use command grant]
    REVIEW -->|deny or defer in Auto interactive| ASK[Pi asks about the exact action]
    ASK -->|approve once| GRANT
    ASK -->|deny or cancel| STOP[Action stays blocked]
    REVIEW -->|deny or defer in Auto non-interactive| STOP
    REVIEW -->|reviewer failure| STOP
    GRANT --> RUN[Execute inside the main agent sandbox]
```

You can sign in through the agent browser, then authorize a command-line tool to
reuse that session for the same service. Keep session tokens out of chat, logs,
Git, and other destinations. If a tool needs to save a session file, put it in a
private directory and set its permissions to `0600`. Browser and sandbox rules
still apply. An advisor can help plan the setup. Pi requests approval separately
for any command or helper it needs to run.

Enabling browser support allows native `browser` calls without per-action review.
The outer network allowlist still applies, and downloads keep their separate approval.

You can manage a goal with `/goal`, and Pi can report completion or ask for help
with `goal_update` without an approval prompt. Actions taken to achieve the goal
keep their normal permissions. The [goal extension](extensions.md#goals) also
accepts optional turn and cost limits.

## Choosing a shell

| Task | Tool |
| --- | --- |
| Read installed skill instructions and references | `read`, `grep`, `find`, `ls`, or a read-only Bash command |
| Edit workspace files or run ordinary commands | `bash` inside the inner sandbox |
| Create files for this command only | `bash`, using `$TMPDIR` |
| Keep files for later commands | Use a workspace directory such as `.tmp/` |
| Browser automation with Betterwright enabled | Native `browser` tool |
| Python Playwright or `sandbox-browser` in the spikee profile | `bash_escalate` when the inner sandbox is enabled; otherwise `bash` |
| Create, inspect or stop a tmux session | `bash_escalate`, using the same explicit `tmux -S` socket path each time |
| Reach an outer-container service on loopback | `bash_escalate` when the inner network namespace cannot reach it |

The inner sandbox blocks Unix sockets, so changing tmux's socket directory does
not make it work there. Escalated commands still require review and run inside
the main agent sandbox. Its network rules still apply. Keys held in separate
SSH and inference containers stay there. Permission to read an installed file
does not also let Pi edit it.

When Pi needs to inspect its own resources, it can request to read one instruction,
skill or prompt text file or list one resource folder. The request shows the path,
the action and why Pi needs it. Approval covers that action once. Folder listings
show eligible names one level deep. Credentials, settings, chats and links stay
outside this access. Pi can explain which Local Knowledge features are available
without requesting file access.

### Check a saved provider key

Ask Pi “Can you check my OpenRouter connection?” Pi uses your selected approval
mode to get permission before it contacts the provider. Once approved, Factory
uses the saved key in its inference container. Pi tells you whether the check
succeeded, the provider refused access, or the check couldn’t run. The key stays
in the inference container, and the check sends no chat messages or files.

The check also works with managed provider plugins that supply their own
connection test. It’s unavailable when approvals are off.
[Hosted provider setup](hosted-models.md)

The approval applies to the request made by Pi’s tool. The inference socket is
also available to container processes that already have permission to use it.
Escalated Bash can access files the container user can read. Use Factory-managed
provider keys to keep the real credential in the separate inference container.

To add these tools to an existing sandbox, choose **Actions… → Update sources
and recreate…**. [What updating a sandbox does](usage.md#update-factory-and-sandboxes)

## Common credential files

**Protect common credential files** is enabled by default. It blocks agent reads
and writes to files such as `.env`, private keys and certificates through file
tools and the inner Bash sandbox. See the explanation beside the checkbox in
**Settings → Approvals & sandbox**.

Clear the checkbox only when the agent may read and change those workspace files.
Their contents can reach the model. The workspace permissions and the outer
container’s network rules still apply.

## Conversation and limits

The reviewer reads your original instructions from the current branch, including
messages from before Pi shortened the chat. Pi’s summary does not count as your
permission. If you send a new instruction or edit one while a review is running,
Pi discards that decision. Switching branches, changing models or changing
reviewer settings also stops Pi from reusing the review history. Parallel reviews
keep separate copies.

The reviewer can read up to about 32,768 tokens of your instructions and nearby
chat context. It sees the text from messages with screenshots and knows it could
not inspect the images. It can leave the decision open if the missing image
matters. When it must leave out context, Pi reports the omission. When the review
history fills, Pi rebuilds it from your original messages. If it cannot recover
those messages, it withholds automatic approval. Restarting Pi clears the review
history.

A review has up to 32,768 output tokens and ten minutes to finish. If the reply
is invalid, the model tries to call a tool, or the request fails or times out,
permission is withheld. Pi gets the reason and can retry or use an allowed
alternative. The reviewer uses **Low** thinking effort by default, or the
lowest setting the model supports. Thinking uses part of the same output budget.

## Reviewer settings

By default, the current chat model reviews approval requests. To use a separate model for this sandbox, open **Settings → Approvals & sandbox**. Clear **Use current model for approvals**, choose a provider and model, then select **Use for approvals** and **Apply**. The picker lists models available to Pi in that sandbox. A different provider can review requests even when the chat stays on its original model.

Reopen Pi to load the saved choice. Changing only the approval model keeps the container and its chats in place. The chosen provider receives the instructions and chat context needed for each review. If that model becomes unavailable, automatic review cannot approve the request.

Pi and FrostPi show the approval mode when you open a chat. In either automatic
mode, use `/auto-review-settings` to change the reviewer model or thinking effort
for that chat. Choose **Current chat model** to follow the model selected in Pi or
FrostPi. Choose **Off** under **Thinking effort** to send a review without
requesting thinking. New chats and resource reloads restore the saved defaults.

In Factory’s **Auto (interactive)** mode, decide on the original action when Pi
or FrostPi asks. The standalone extension’s `/auto-review-approve` command does
not apply to Factory’s approval modes. Change the approval mode, the inner sandbox, and credential
protection in Factory and apply them by recreating the sandbox. `/reload` reloads
Pi resources and restores the default per-chat reviewer settings.

In both automatic modes, `/opt/pi-security/config/auto-review.json` stores the
sandbox’s approval model and enables `retainedContext` and `reviewerConversation`. Inspection tools cannot be
enabled. Project files cannot disable this context handling or choose another
reviewer. Factory manages this file outside the workspace.

For extension updates, see [Extensions](extensions.md). The outer container,
network and credential boundaries are described in [Architecture](architecture.md).
