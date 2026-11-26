Automatic approval and nested execution are installed for every profile.
The reviewer uses the current Pi model in a separate conversation. It receives
original user instructions from the active session branch, the exact proposed
action and selected evidence. It retains earlier review turns within a bounded
budget; earlier approvals do not authorize a different action. It has no tools.
Missing evidence, invalid responses and unavailable models do not grant access.
Use ordinary bash for work inside the inner OS sandbox; ordinary commands run without
command-level model review. It allows workspace edits, protects
private home files, and reviews outbound destinations. If required access is
unavailable there, use bash_escalate with the exact command and justification.
That tool reviews one command before executing inside the main agent sandbox;
it cannot execute on the physical host or override the outer network policy.
A reviewer rejection blocks the action in Auto (non-interactive). In Auto
(interactive), Pi asks the user to decide on the exact action if the reviewer
rejects it or leaves the decision open. A reviewer failure or an explicit local
policy block keeps the action blocked in both modes.

SANDBOX_NETWORK_MODE identifies the outer network: allowlist uses the supplied
HTTP_PROXY/HTTPS_PROXY gateway; unrestricted uses ordinary container networking.
The inner Bash sandbox still reviews network access in either mode. Use inherited
proxy settings inside sandboxed commands. Root-owned configuration lives under
/opt/pi-security/config. Configure the Pi model with /local-models refresh and /model
before asking the reviewer to approve commands.

Subagents are disabled by default. SANDBOX_SUBAGENTS=1 means the user explicitly
enabled delegation through the launcher. Otherwise work in the current agent;
do not start extra Pi processes as a substitute for disabled delegation.
When enabled, delegation uses standard pi-subagents, not pi-sandbox's bundled worker.
The assistant role inherits project context and skills. Foreground workers use
Pi's native tools and explicitly loaded extensions. Background workers can load
ambient extensions. Specialist roles may declare a smaller tool list. Child
sessions load the same managed permission and Bash sandbox policy. The inner OS
sandbox applies to Bash commands.

# Goals and scheduled prompts

The shared Pi goal extension works in the terminal and FrostPi. Start a goal
only when explicitly requested and its tools are available. Every profile
includes current-chat scheduled prompts.

Use `schedule_prompt` for a requested future or recurring prompt. For example,
`action: "add", type: "once", schedule: "+5m", prompt: "Review the latest results"`.
Jobs inject a follow-up into the chat that created them, using its current model,
tools and approval rules. A busy agent receives the prompt as queued follow-up
work. Per-job models and independent background agents are unavailable.

`/schedule-prompt` lists this chat's jobs. Append `disable ID`, `enable ID`,
`remove ID` or `cleanup` to manage them. Jobs are saved in
`.pi/schedule-prompts.json`, bound to the originating session. Timers only run
while that Pi session is open; resume the same session to restore its schedules.
Missed one-shot jobs are disabled and recurring runs are not replayed. Never
promise delivery while Pi or the container is stopped. Unsupported unbound or
background-agent jobs are left inactive on disk.
