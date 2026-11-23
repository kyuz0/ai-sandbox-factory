# Extensions

AI Sandbox Factory installs extensions from their repositories:

| Extension | Repository |
| --- | --- |
| FrostPi editor | [Factory fork, kyuz0/pi-vscode-ui](https://github.com/kyuz0/pi-vscode-ui) |
| Goals | [@ethanthoma/pi-goal](https://github.com/ethanthoma/pi-goal) |
| Local model discovery | [pi-local-model-manager](https://github.com/kyuz0/pi-local-model-manager) |
| Questions and answer forms | [rpiv-ask-user-question, Factory fork](https://github.com/kyuz0/rpiv-mono/tree/main/packages/rpiv-ask-user-question) |
| Codex, Claude and Antigravity advisors | [pi-advisor](https://github.com/kyuz0/pi-advisor) |
| Automatic approval, inner sandbox and escalation | [pi-packages](https://github.com/kyuz0/pi-packages) |

[extensions.lock.json](../ai_sandbox_factory/containers/dev/extensions.lock.json) pins the editor, discovery, advisor and question-extension revisions. The manager downloads them over HTTPS and passes exported source files to the image builder. Downloaded revisions are cached under `~/.cache/ai-sandbox-factory/extensions`.

The security packages use the commit and archive checksum in [sources.json](../ai_sandbox_factory/containers/dev/security/sources.json). Updating a pin and rebuilding installs that revision. Existing containers keep their installed version until recreated.

Fixes and unit tests belong in the extension repositories. This repository manages container lifecycle, network rules, credentials and integration tests.

## Goals

**Goals** under **Agent tools** loads the same `@ethanthoma/pi-goal` extension in Pi CLI and FrostPi. Use `/goal <objective>` to start work on a goal, `/goal status` to see progress, and `/goal pause` or `/goal resume` to control continuation.

Use `/goal revise <new objective>` while paused to edit the objective and keep the goal's accumulated usage.

Use `/goal --turns 10 <objective>` to set a turn limit or `/goal --cost 1 <objective>`
to set a limit in US dollars. Omit these options to start without a limit.

Pause a goal before moving between clients, close the original chat, and resume the same Pi session in the other client. Previous Pendant chats can still be opened. To continue a Pendant goal, use its objective to create a goal in the shared extension.

## Questions in Pi and FrostPi

`rpiv-ask-user-question` loads by default in both clients. The agent uses `ask_user_question` to request a choice or clarification. Both clients accept option selections and free-text answers. The terminal displays a tabbed questionnaire with toggles for multiple choices. FrostPi shows successive prompts and asks for option numbers when multiple choices are allowed. Questions go directly to you, bypassing automatic permission review.

If Pi can keep working while it waits, it leaves the question pending. Open
the question in FrostPi or type `/questions` in Pi’s terminal. Pi gets the
answer in the chat that asked the question.

To disable these forms, open **Settings → Agent tools**,
clear **Question forms**, then **Apply and recreate**. The agent can still ask
questions in chat. This setting does not change permission approvals.

To update an existing sandbox, use **Actions → Update sources and recreate…**,
or run:

```sh
ai-sandbox-factory recreate NAME --profile base
```

Replace `base` with the sandbox’s current profile. Close the editor before rebuilding
and reopen it afterward; the workspace and private home are retained.
