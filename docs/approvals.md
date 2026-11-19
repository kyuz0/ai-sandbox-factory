# Approvals

Both automatic modes use the current chat model by default to review actions that
need approval. The reviewer has its own conversation, separate from the agent doing
the work. It receives the exact action, original user instructions from the
active Pi branch and relevant tool evidence already available to Pi.

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
    GRANT --> RUN[Execute inside the development container]
```

The reviewer makes one model request per approval and has no tools. It judges the
proposed action against the user’s instructions; earlier approvals do not authorize
different actions.

You can authorize an interactive browser login followed by private local session
reuse in a CLI for the same service. Session values must stay out of model output,
logs, Git and other destinations; local session files need mode 0600 in a private
directory. Existing browser-tool and inner-sandbox restrictions still apply.
Advisor consultations can discuss this workflow; the actual helper or command is
reviewed separately before execution.

Enabling browser support allows native `browser` calls without per-action review.
The outer network allowlist still applies, and downloads keep their separate approval.

Built-in goal creation, updates, task tracking and goal questions need no approval.
Actions performed to achieve a goal keep their normal permissions. Goals have no
token budget unless you request one; zero is a literal zero-token limit. To remove
an existing budget, the agent can use `update_goal` with `token_budget: null`.

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
the development container. The outer network allowlist and protected credentials
remain enforced. Installed-file read access does not grant write access.

## Common credential files

**Protect common credential files** is enabled by default. It blocks agent reads
and writes to files such as `.env`, private keys and certificates through file
tools and the inner Bash sandbox. See the explanation beside the checkbox in
**Settings → Approvals & sandbox**.

Clear the checkbox only when the agent may read and change those workspace files.
Their contents can reach the model. The workspace permissions and the outer
container’s network rules still apply.

## Conversation and limits

Original user messages are read from Pi's active branch, including originals
before compaction. A generated summary is not treated as user authorization.
New messages and actions are appended to the reviewer conversation. The
controller ignores appended compaction summaries and display metadata. New original
instructions or edits still invalidate an in-flight decision. Parallel reviews use separate copies; branch, model and configuration
changes invalidate reuse.

Original user text and nearby assistant context share an estimated 32,768-token
reviewer input budget. Screenshot messages retain their text; images are marked
as uninspected rather than sent to the text-only reviewer. Missing visual facts
can require deferral when they matter to the action. Omissions are reported.
When history fills, it is rebuilt from original instructions; if those cannot
be reconstructed intact, automatic approval is withheld. Reviewer history is
held in memory and resets on Pi restart.

A review can produce up to 32,768 output tokens and has a ten-minute maximum
deadline. Invalid replies, unexpected tool calls,
timeouts and provider failures do not grant permission. Default failure handling
returns the reason to the agent without opening a dialog. The agent can retry a failed
review or choose an allowed alternative. Thinking defaults to Low. If the model
does not offer Low, the reviewer uses its lowest enabled level. Thinking tokens share
the output allowance.

## Configuration and updates

For an existing sandbox, select it in the Factory manager, open
**Settings → Approvals & sandbox**. Choose **Approval mode**, change
**Use inner command sandbox** or **Protect common credential files**, then select **Apply and recreate**.
This stops running processes, including tmux sessions, but preserves files and
saved chats. Reopen Pi or FrostPi to resume the conversation with the new settings.
Choose **Explain** beside **Approval mode** to compare all four modes.

- **Auto (non-interactive)** uses a reviewer model to check actions that need
  approval. If the reviewer blocks an action, the agent gets the reason and can
  try another way. You won’t get an approval prompt. This is the default.
- **Auto (interactive)** uses a reviewer model to check actions that need
  approval. If the reviewer rejects an action or leaves the decision to you,
  Pi shows you the exact action and the reviewer’s reason. You can approve
  that action once or deny it. If the reviewer rates the action critical, Pi
  also asks you to type `APPROVE`.
- **Manual approval (ask me)** with the inner sandbox on, Pi can run Bash
  inside it without your approval but if a command needs to run outside the
  inner sandbox, Pi asks you to approve the escalation. With the inner
  sandbox off, you need to approve every Bash command. File tools such as
  `read` and `write` ask for approval when their permission rules require it.
- **YOLO (no approvals)** skips approval prompts and reviewer checks. To run
  Bash directly in the development container, turn off the inner sandbox too.
  If you leave it on, Bash stays inside it.

Pi can ask in its terminal or in FrostPi. Reviewer failures block the action in
both Auto modes. Explicit sandbox and permission blocks and the container’s
network rules apply in every mode.

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

In both automatic modes, `/opt/pi-security/config/auto-review.json` selects the
current chat model and enables `retainedContext` and `reviewerConversation`. Inspection tools cannot be
enabled. Project files cannot disable this context handling or choose another
reviewer. Configure trusted settings through the image, not through agent-edited
workspace files.

For extension updates, see [Extensions](extensions.md). The outer container,
network and credential boundaries are described in [Architecture](architecture.md).
