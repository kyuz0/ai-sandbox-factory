# Profiles

A profile sets up a sandbox for a particular kind of work, with the tools, skills,
extensions and instructions the agent needs. Providers are chosen separately,
so the same profile can use local or hosted models.

## What profiles inherit

**Default Developer Sandbox** is the built-in profile (`base`). It includes Pi,
FrostPi, Node.js, Python, Git, document tools and the sandbox security components.

Custom profiles extend that base image with their own dependencies, skills and
instructions. They do not inherit other profiles’ additions.

## Research profiles

- **Deep Web Research** enables Web Search and subagents. Its skill uses independent
  research questions, then writes a cited `report.md` and `sources.md` under
  `research/` in the workspace. It can use local files alongside web sources.
- **Local Knowledge** starts with Web Search off. Its skill searches workspace
  files and mounted folders and cites the relevant passages. You can enable Web
  Search or subagents in the same settings as any other profile.

Both profiles can search file contents and names without a model. To add semantic
search, open **Agent tools → File search** and enable **Search by meaning**.
Factory installs `pi-knowledge` and downloads roughly 470 MB of model weights
when building the image.
For sandboxes built before this setting was available, choose
**Actions → Update sources and recreate…** first.
Indexing and search then run inside the sandbox without an embedding service.
In Pi or FrostPi, run `/knowledge-index reference` with the path to a file or folder
in the workspace or a mounted folder. Run `/knowledge-refresh reference` to update
the index after editing its sources. Pi can search what you indexed. Your indexes
are stored in the sandbox's private home and survive a rebuild.

Mount local reference folders read-only when you only need to search them. An
index contains excerpts from its sources. Choose the material you want Pi to use.

The profile’s Dockerfile starts with `ARG BASE_IMAGE` and `FROM ${BASE_IMAGE}`.
The launcher supplies that image, then adds the selected browser support,
providers and extra skills after building the profile.

## Install and update

```sh
pi-sandbox-factory profile add reporting ./examples/profile
pi-sandbox-factory profile list
pi-sandbox-factory profile show reporting
pi-sandbox-factory create reports --profile reporting --workspace /absolute/path/to/reports \
  --local-model-ports 8000 --allow-domain local-model.internal:8000 --start
```

Replace the folder with a Git HTTPS or SSH URL to install from a repository.
For a repository containing several plugins, select the folder:

```sh
pi-sandbox-factory profile add reporting git@git.example.com:team/plugins.git --subdir reporting
```

Only the selected subfolder becomes the package’s build input; updates reuse it.
Use `--ref TAG_OR_COMMIT` to pin it. Local Git repositories install their committed
files; commit edits before updating. A plain folder installs its current files.

```sh
pi-sandbox-factory profile update reporting
pi-sandbox-factory recreate reports --profile reporting
```

Update fetches the selected branch or ref. Recreate builds the updated profile
before stopping the old containers, then preserves the workspace and private
home. Close the editor first. Updating a pinned commit keeps that commit.

`pi-sandbox-factory profile remove reporting` removes the registration while
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
| `check.py` | Optional tool checks, copied to `/opt/sandbox/profile-check.py`. |
| `resources.json` | Skills, prompts and extensions registered with Pi. |

`profile.json` can include a one-line `summary` for the profile picker and Library.
It can also set `defaults.web_search` and `defaults.subagents` for new sandboxes.
Each is a boolean. Users can change both in configuration.

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
Use `pi-sandbox-factory exec NAME -- COMMAND ...` to configure a tool inside its sandbox;
add required destinations through **Settings → Network and Web Search → Network** first.

## Check it

Install the example, create a disposable sandbox and run:

```sh
pi-sandbox-factory check reports --allowed-url http://local-model.internal:8000/v1/models
```

The endpoint must be running and allowed. This checks the container boundaries,
common tools and your `check.py`. Open Pi/FrostPi and confirm the skill loads.
Delete the disposable sandbox afterward; its workspace is retained.
