# Profiles

Choose a profile for the kind of work you want to do. A profile can add
software, skills, extensions, and instructions for Pi. Choose your model
provider separately so the same profile can work with a local or hosted model.

## What profiles inherit

**Default Developer Sandbox** is the built-in profile (`base`). It includes Pi,
FrostPi, Node.js, Python, Git, document tools and the sandbox security components.

Custom profiles extend that base image with their own dependencies, skills and
instructions. They do not inherit other profiles’ additions.

## Research profiles

- **Local Knowledge** answers questions about your documents and code. Keyword
  search is available immediately. Web Search starts off. [Local Knowledge](local-knowledge.md)
- **Deep Web Research** enables Web Search and subagents. Ask it to investigate a
  question and write a cited report using web or local sources. [Deep Web Research](deep-web-research.md)

For an existing research sandbox, **Actions… → Update sources and recreate…**
installs the current profile and adds its **Getting started…** guide.

The profile’s Dockerfile starts with `ARG BASE_IMAGE` and `FROM ${BASE_IMAGE}`.
The launcher supplies that image, then adds the selected browser support,
providers and extra skills after building the profile.

## Install and update

```sh
ai-sandbox-factory profile add reporting ./examples/profile
ai-sandbox-factory profile list
ai-sandbox-factory profile show reporting
ai-sandbox-factory create reports --profile reporting --workspace /absolute/path/to/reports \
  --local-model-ports 8000 --allow-domain local-model.internal:8000 --start
```

Replace the folder with a Git HTTPS or SSH URL to install from a repository.
For a repository containing several plugins, select the folder:

```sh
ai-sandbox-factory profile add reporting git@git.example.com:team/plugins.git --subdir reporting
```

Factory installs the selected subfolder and uses that same folder when you
update the profile. To stick to a tag or commit, pass `--ref TAG_OR_COMMIT`.
Factory uses committed files from a local Git repository, so commit your edits
before updating. For an ordinary folder it uses the files as they are.

```sh
ai-sandbox-factory profile update reporting
ai-sandbox-factory recreate reports --profile reporting
```

Update fetches the selected branch or ref. Recreate builds the updated profile
before stopping the old containers, then preserves the workspace and private
home. Close the editor first. Updating a pinned commit keeps that commit.

`ai-sandbox-factory profile remove reporting` removes the registration while
preserving existing sandboxes and their snapshots.
**Library → Profiles** provides the same install, update and remove operations in the TUI.
Choose **Details…** on a profile to read its system prompt addendum.

## Write a profile

Copy [examples/profile](../examples/profile) into a new repository. It contains:

| File | Purpose |
| --- | --- |
| `profile.json` | `version: 1`, a lowercase `name`, and a short `description`. |
| `Dockerfile` | Extends `ARG BASE_IMAGE` / `FROM ${BASE_IMAGE}`; finishes as `USER node`. |
| `profile.md` | Instructions for the agent, copied to `/opt/sandbox/profile.md`. |
| `system-prompt.md` | Optional UTF-8 Markdown describing what the profile is intended for. Factory includes it in the Pi system prompt. |
| `start-here.md` | Optional UTF-8 user guide shown under **Getting started…** in Factory and `/profile-guide` in Pi and FrostPi. |
| `check.py` | Optional tool checks, copied to `/opt/sandbox/profile-check.py`. |
| `resources.json` | Skills, prompts and extensions registered with Pi. |

`profile.json` can include a one-line `summary` for the profile picker and Library.
It can also set `defaults.web_search` and `defaults.subagents` for new sandboxes.
Each is a boolean. Users can change both in configuration.

Start `start-here.md` with a short plain-text paragraph that gives the user a
first action. Pi shows that paragraph when it opens a fresh chat. Put examples
and setup steps below it. Factory serves the guide from the saved profile and
keeps the workspace available for the user’s own files.

Keep the addendum short. Factory identifies the profile and explains that the user
can take the work in another direction. The addendum comes from the saved profile
snapshot and is included with the existing instructions in Pi CLI and FrostPi.
Factory reads it directly from the profile, so its Dockerfile can stay as it is.

Install dependencies during the image build. Put profile resources under `/opt`
and copy `resources.json` to `/opt/sandbox/resources.json`, for example:

```json
{"skills":["/opt/reporting"],"prompts":[],"extensions":[]}
```

Skills are folders containing `SKILL.md`. Extensions are Pi extension files or
folders. Prompts are Pi prompt files or folders. They must exist in the built
image. Pi and FrostPi read the same registrations. Replacing a profile removes
its old launcher-managed registrations while preserving user-added resources.

If the tools live in another repository, add `git_source` and optionally `git_ref`
to `profile.json`. Installation exports that repository and its submodules into
`source/`, with the dependency commit recorded in the registry. Reserve that directory
for the dependency; use `COPY source/ ...` in the Dockerfile. Git credentials and
`.git` directories are excluded from the image inputs.

Keep service-specific setup in the profile's README and agent instructions.
Use `ai-sandbox-factory exec NAME -- COMMAND ...` to configure a tool inside its sandbox;
add required destinations through **Settings → Network and Web Search → Network** first.

## Check it

Install the example, create a disposable sandbox and run:

```sh
ai-sandbox-factory check reports --allowed-url http://local-model.internal:8000/v1/models
```

The endpoint must be running and allowed. This checks the container boundaries,
common tools and your `check.py`. Open Pi/FrostPi and confirm the skill loads.
Delete the disposable sandbox afterward; its workspace is retained.
