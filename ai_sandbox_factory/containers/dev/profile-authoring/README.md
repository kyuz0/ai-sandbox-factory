# Author a profile with multiple hosts

A profile describes the coding-agent container and any additional **profile
services**. Each service runs in its own container with its own hostname. These
hosts share the sandbox's private network; they are not separate virtual machines.
Start with the complete [two-database example](example/profile.json).

## Files and containers

```text
my-profile/
├── profile.json                 # Package identity, supported agents and services
├── Dockerfile                   # Tools in the coding-agent container
├── profile.md                   # Instructions delivered to the coding agent
├── start-here.md                 # Optional guide for the user
└── services/
    └── postgres/
        └── Dockerfile           # Image used by both example database hosts
```

The top-level Dockerfile declares `ARG BASE_IMAGE` before `FROM ${BASE_IMAGE}`,
extends Factory's supplied image and finishes as
`USER node`. It changes the container where the coding agent works. Service
Dockerfiles choose their own base images and supply their applications. A service
can use a published image instead, provided it meets the SSH requirements below.
Several services can use the same Dockerfile; each still gets its own container,
settings and data volumes. To add a different kind of host, add another build
folder and point the new service's `build.context` at it.

In `profile.json`, set `version: 1`, choose a `name` and give a short `description`. **`version` is the
manifest format, not the release number of your profile.** Keep it at `1` when
making a new Git revision. The name used by `profile add team-dev ...` is its
registration name in Factory and can differ from the manifest's `name`. Omit
`services` for a profile with only the coding-agent container.
Declare supported coding agents with `agents`, using
`pi`, `codex`, `claude` and/or `agy`. Without that list, an ordinary profile is
Pi-only. Declaring another agent does not translate Pi extensions or resources.

## Declare more than one host

The example's full [profile.json](example/profile.json) defines `database` and
`reporting` under `services`. Both use `services/postgres/Dockerfile`, but have
separate database names, users, generated passwords and volumes. Each entry has
this form:

```json
{
  "description": "Primary database for the project",
  "build": {"context": "services/postgres", "dockerfile": "Dockerfile"},
  "environment": {
    "POSTGRES_DB": "app",
    "POSTGRES_USER": "app",
    "POSTGRES_PASSWORD": {"generate": "database-password"}
  },
  "volumes": {"data": "/var/lib/postgresql/data"},
  "healthcheck": ["pg_isready", "-U", "app", "-d", "app"],
  "connections": {
    "port": 5432, "database": "app", "user": "app",
    "password": {"environment": "POSTGRES_PASSWORD"}
  }
}
```

| Field | Meaning |
| --- | --- |
| Service key, such as `database` | A lowercase DNS label: letters, digits and hyphens, starting with a letter and ending with a letter or digit; at most 63 characters. `workspace` and `proxy` are Factory endpoints, so choose other names. |
| `description` | Required, nonempty single-line explanation of this host's role. Factory includes it in the agent's service list. |
| `build` or `image` | Choose exactly one. `build.context` is a folder relative to the profile (default `.`); `build.dockerfile` is relative to that context and defaults to `Dockerfile`. `image` is a container image reference. Factory records the image ID used. |
| `environment` | Application environment variables. Values are strings or `{"generate": "name"}`. Use strings for numbers such as `"5432"`. Generated values persist for that sandbox and service. |
| `volumes` | Persistent volume names (`[a-z][a-z0-9-]*`) mapped to absolute paths inside this host, without `..` components, colons or commas. The two example services both call their volume `data`; Factory allocates different volumes for each host and sandbox. These are not host-folder mounts. |
| `command` | Optional nonempty argument list replacing the image's command. The image's entrypoint is retained. This is not shell syntax: use `sh -c` explicitly if your command requires a shell. |
| `healthcheck` | Optional nonempty argument list run inside the host until it succeeds. Factory waits for service readiness before completing startup. Without a healthcheck, startup checks that the container runs and accepts SSH; it does not check application readiness. A failed or exited service reports diagnostics. Applications must retry connections to peers while those peers initialize; there is no `depends_on` field. |
| `connections` | Application settings exposed to the agent in `connections.json`. Values are strings, integers or `{"environment": "VARIABLE"}` references to declared environment variables. Factory adds the hostname, description and SSH settings. This file supplies connection information; it does not configure applications or open ports. Use these connection details rather than hardcoding generated passwords. |
| `ssh` | Optional settings: `port` (default `22`), `user` (default `root`) and `server` (default `/usr/sbin/sshd`). The account must exist, allow public-key login and not be locked against it. It needs no usable password. `server` is the SSH server executable path. Use an account with the administrative rights the agent needs. |

A generated-value name is scoped to its service. The example uses
`database-password` in both entries, and receives **different passwords**. Factory
does not expand variable expressions such as `${NAME}` in environment strings,
and environment references in `connections` refer to that service's variables. Within
one service, reusing a generated-value name reuses that value. The example
[Dockerfile](example/services/postgres/Dockerfile) installs OpenSSH alongside
PostgreSQL and unlocks the root account for Factory's public key without setting a usable password.
Generated values contain 43 URL-safe letters, digits, underscores and hyphens.
They cannot be shared between services through a generated-value reference.

Factory creates ordinary named volumes. An application's entrypoint must prepare
its data directory for the user that runs the application, as PostgreSQL's does.
For a non-root application, initialize ownership before dropping privileges;
verify a fresh volume with that image rather than assuming the mount is writable.

## SSH, application ports and networking

Every service image needs a shell and an OpenSSH server. Factory supplies the
SSH server configuration, authorized public key and pinned host key at runtime.
Use Factory's runtime keys instead of baking your own into the image, and do
not replace the application's entrypoint with `sshd`. The application's
entrypoint stays PID 1; Factory starts the SSH server separately in that container.

The coding-agent container receives a client key and configuration for all profile
hosts. For the example:

```sh
ssh -F /run/profile-services/config database.profile.internal
ssh -F /run/profile-services/config reporting.profile.internal
```

Each hostname is `<service-key>.profile.internal`; the short service key also
resolves on the private network. The same hosts are reachable directly on their application ports, for example
`database.profile.internal:5432`. Service names are stable; use them instead of
container IP addresses. Hosts can reach each other directly on this private
network. Factory's SSH client configuration is supplied to the coding-agent
container; configure application-owned credentials if another service needs its
own authenticated connections to a peer.

With Pi's inner sandbox enabled, the agent uses the existing `bash_escalate`
path for direct SSH, raw database connections and ping. The selected approval
mode applies. If approvals are off, use a sandbox terminal or enable approvals.
Other coding agents use their normal shell tools and approval mechanism. These
internal connections do not go through the external SSH connector.

Profile services currently require rootless Podman and **Restricted** networking.
Their external HTTP/HTTPS requests use Factory's configured proxy destinations.
Their private network does not provide a direct external route. Factory sets
proxy environment variables and `NO_PROXY` for local profile hosts. Applications
that make external requests must honor the proxy settings.

Factory manages the network, container names, mounts and helper containers. The
service format does not accept Compose configuration, host-published `ports`,
`networks`, `privileged` or engine flags. Inference, connector and account-sign-in
credentials remain in Factory's helper containers; they are not profile services.
Application passwords declared in `connections` are deliberately readable by the
coding agent. Keep generated passwords out of source files and Git commits.

## Environment variables and application passwords

Use `services.<name>.environment` for variables in that service container. Use
literal strings for ordinary settings and `{"generate": "name"}` for passwords.
Factory saves generated values in the sandbox's private state, outside the
profile folder. Stop, start and rebuild retain them; a new sandbox gets new values.
Do not hardcode passwords in `profile.json`, Dockerfiles or `profile.md`.

The coding-agent container reads the declared `connections` values through
`/run/profile-services/connections.json`. An application running there can read
that file and pass the required values to its process at runtime. Factory does
not automatically set `DATABASE_URL`, `PGPASSWORD` or other application variables
in the coding-agent container. Its managed agent instructions identify the file
and available services; they do not contain the generated passwords.

For user-supplied variables in the coding-agent container, select a host file in
**Resources & environment → Credentials environment file**. That file does not
set variables in profile service containers. Keep it outside the profile repository.

Factory currently has no shared-secret reference or external credentials-file
setting for profile services. Another service container does not receive
`connections.json`; a profile cannot yet automatically inject one generated
password into both a database and a separate application container.

## Tell the agent how to use the hosts

Describe each host's purpose, hostname, application port, database/user names and
useful commands in `profile.md`. Refer to
`/run/profile-services/connections.json` for this sandbox's generated credentials.
Factory includes the full `profile.md` and a generated service inventory in the
selected agent's managed instructions, without changing project instruction files.
The example's [profile.md](example/profile.md) shows both hosts and their distinct
roles. The Dockerfile's `COPY profile.md /opt/sandbox/profile.md` supplies Pi's
bootstrap fallback when no applied profile snapshot is mounted. Factory normally
reads the saved profile snapshot instead. Its [start-here.md](example/start-here.md)
gives users commands to try.

The connection file is keyed by service name. For example, its `database` entry
looks like this (the actual file contains the generated password):

```json
{
  "database": {
    "host": "database.profile.internal",
    "description": "Primary database for the project",
    "ssh_user": "root",
    "ssh_port": 22,
    "port": 5432,
    "database": "app",
    "user": "app",
    "password": "[redacted]"
  }
}
```

## Install and verify the example

Run these commands **on the host**, from a Factory source checkout. Keep the
profile outside the project folder that your coding agent will work on:

```sh
mkdir -p "$HOME/sandbox-profiles"
cp -R ai_sandbox_factory/containers/dev/profile-authoring/example "$HOME/sandbox-profiles/team-dev"
git -C "$HOME/sandbox-profiles/team-dev" init
git -C "$HOME/sandbox-profiles/team-dev" add .
git -C "$HOME/sandbox-profiles/team-dev" commit -m "Add two database hosts"
git -C "$HOME/sandbox-profiles/team-dev" tag v1
ai-sandbox-factory profile add team-dev "$HOME/sandbox-profiles/team-dev" --ref v1
ai-sandbox-factory profile show team-dev
```

For an installed Factory without its source checkout, copy the bundled example
from an existing sandbox. Run `podman ps --format '{{.Names}}'` on the host and
replace `CONTAINER` below with its coding-agent container name: for example,
`pi-sandbox-factory-my-box` for Pi or `pi-sandbox-factory-my-box-agent` for another
coding agent. The sandbox must have been created or rebuilt with a Factory version
that includes this guide, and be running with the applied profile mounted:

```sh
mkdir -p "$HOME/sandbox-profiles/team-dev"
podman cp CONTAINER:/opt/sandbox/base-profile/authoring/example/. "$HOME/sandbox-profiles/team-dev"
```

Then initialize Git, commit and register the folder as above. The profile does
not belong in the user's project repository.

In Factory, choose **New sandbox**, select a coding agent and `team-dev`, choose
the project's workspace folder, configure model access or sign-in on **Review
sandbox**, and keep **Restricted** networking. Create and start the sandbox.
Open **Actions… → Profile and services…** and check both services are running.
Run the SSH and direct TCP SQL checks in `start-here.md`, then verify:

- Both hostnames resolve and each host accepts its documented SSH command.
- Each SSH SQL command returns its own database name (`app` or `reports`).
- Direct SQL from the coding-agent container uses the hostname, port and credentials from `connections.json`.
- Data written to one host is absent from the other.
- Stop/start keeps both hosts' data and generated credentials.
- Rebuilding the coding-agent container keeps the profile services and their data.

Use a disposable sandbox for these checks. **Delete sandbox** deletes its profile
service data as well as its other private data; it keeps host project files.

## Edit the profile of an existing sandbox

Profile customization is occasional environment setup and maintenance. Use it
when the project needs persistent system tools in the coding-agent container,
a service such as a database, a change to those requirements, or maintenance
requested by the user. Once the tools and services meet the task's needs, work
in the existing environment. Starting
a new task, session or day does not require a fork, rebuild or profile edit.
Manage ordinary project dependencies in the project. Reuse an attached editable
profile for later necessary changes unless a separate fork is wanted.

Choose **Actions… → Edit profile…**. Select a new or empty host folder outside
the project and choose **Create editable copy…**. Review the replacement notice
and confirm. Factory copies the sandbox's saved profile, including its saved
source files; it does not fetch a newer upstream revision or modify the Library.
A profile originally installed from a repository subfolder becomes a standalone
folder. The host folder is yours to keep and version, independently of the project.

Attaching this folder replaces the coding-agent container so it can acquire the
new mount. Running agent work and terminals stop. Project files, private home,
chats, sign-ins, existing services and their data are kept. A stopped sandbox
stays stopped. If attachment fails, Factory restores the previous runtime and
keeps the editable copy so the same action can be retried.

Keep the editable folder at its saved host path. Moving or deleting it prevents
the sandbox from starting because its writable mount would be missing. Restore
the folder at that path and retry; the applied profile and private data are kept.

Inside the sandbox:

- `/opt/sandbox/profile` is the editable source folder.
- `/opt/sandbox/base-profile` remains the read-only record of the applied build.
- `/workspace` remains the user's project; the profile is not added to it.

Open the coding agent again after attachment. Factory's managed instructions tell
it where to edit, where to read this guide, how to validate and what the user must
choose to apply the changes. Editing a file does not change the running image.
The agent works within the user's existing authorization and asks before
expanding it. It explains the proposed replacement before asking the user to
apply the changes.

To add packages, edit the top-level Dockerfile. To add another host:

1. Add its Dockerfile and build files, or reuse a service build context.
2. Add a new entry in `profile.json` under `services`, including its application
   settings, persistent paths, readiness check and connection details.
3. Explain the host's role and commands in `profile.md` and `start-here.md`.
4. Validate, then ask the user to review and apply the changes.

Keep service build contexts separate from the top-level profile files, as in the
example. Changing a file inside an existing service's build context counts as a
change to that service's image, even if its Dockerfile did not change.

### Validate and apply

The agent can run the shared static validator inside the sandbox. Substitute its
coding agent for `pi` (`codex`, `claude` or `agy`):

```sh
python3 -I - /opt/sandbox/profile --agent pi < /opt/sandbox/base-profile/authoring/validate.py
```

The same checks are available on the host:

```sh
ai-sandbox-factory profile validate "$HOME/sandbox-profiles/team-dev" --agent pi
```

Checks cover JSON syntax, required files, supported agents, service fields and
referenced build paths. Errors identify the file, JSON location or rejected
service field. These are static checks: Dockerfile commands and application
startup are tested when applying, not by the validator.

Choose **Actions… → Edit profile… → Review changes…**. Factory freezes the
working-tree files, including uncommitted edits, and shows their diff against the
applied profile, any added services and the replacement consequences. Choose
**Apply profile changes**, review the replacement notice and confirm to use
exactly that revision. If files change after
review, review them again before applying.

Factory builds images and checks new services while the previous coding-agent
container remains available. Preparation is a job and can be cancelled. Factory
stops the active image build or removes newly prepared service resources before
reporting cancellation. Once replacement starts, cancellation is unavailable so
Factory can finish or restore the previous
runtime. Build or preparation failures keep the previous runtime. Startup failure
restores the previous containers and settings. Newly created service resources
are removed on failure or cancellation; existing service data is preserved.
If Factory exits during preparation or replacement, the next lifecycle action
or client launch recovers the pending change before continuing.

Package changes and **new services** can be applied to this same sandbox. New
services initialize separate application data. Existing service containers and
passwords are retained. Changes confined to a service's `description` or
`connections` update its agent-readable metadata. Changing or removing an existing
service's image, build context contents, environment, command, volumes, healthcheck
or SSH settings is refused by name before replacement. Revert that change, or
create a separate sandbox and use the application's export/import tools to move
its data. Factory does not infer a database migration from a changed Dockerfile.

After success, open the coding agent again to use the new image and instructions.
Packages installed manually in the old container survive only if the profile's
Dockerfile installs them. A normal **Rebuild sandbox…** for an editable profile
uses the saved applied revision; it does not apply unreviewed edits.

The equivalent host commands are:

```sh
ai-sandbox-factory profile-fork my-sandbox --directory "$HOME/sandbox-profiles/team-dev"
ai-sandbox-factory profile-review my-sandbox
ai-sandbox-factory profile-apply my-sandbox --revision REVIEW_ID
```

Use the `revision` value returned by `profile-review` as `REVIEW_ID`. The TUI
shows the same value as **Review ID**; it is different from the profile's content digest.

### Version and share your profile

Factory does not initialize Git or publish the editable folder. After attaching
an editable copy, the agent asks whether the user wants Git tracking unless that
choice has already been made. The user can ask the agent to initialize a separate
repository, commit approved changes and maintain it at a supplied origin. Agree
whether commits stay local or are pushed, and whether that is a one-time action
or an ongoing maintenance instruction. A declined offer leaves an ordinary folder.

For an SSH origin, the agent uses the existing SSH & Git connector for authentication. If
access is missing, configure **Settings → Connectors → SSH & Git** and any required
**Network** destinations. Keep private keys and credential-bearing URLs out of the
profile. A Git push shares the profile; it does not apply it to the sandbox.

For Git tracking in WSL, use the default profile folder under your Linux home,
for example `~/sandbox-profiles/team-dev`. Some Windows drive mounts reject the
lock-file permission changes Git needs, even when ordinary file editing works.

The editable folder and its Git history survive restarts and image replacements.
Factory's managed context identifies the original profile name and revision, the
currently applied revision and the writable source. When resuming profile
maintenance, the agent checks Git status, log and remotes, preserving uncommitted
edits. Record the user's tracking choice and approved ongoing maintenance
instructions in `profile.md`. During profile maintenance, the agent reads the
editable file for choices saved since the last Apply. Unapplied package and
service changes remain proposals. After Apply, the saved
instructions are also included in future agent sessions. They must reflect the user's actual
choice, not turn a one-time push into standing authorization.

The user can also maintain the repository directly on the host. Keep manifest `version: 1`; use Git tags or
commits to identify profile revisions. For example, after editing:

```sh
git -C "$HOME/sandbox-profiles/team-dev" init
git -C "$HOME/sandbox-profiles/team-dev" add .
git -C "$HOME/sandbox-profiles/team-dev" commit -m "Add an application service"
git -C "$HOME/sandbox-profiles/team-dev" tag v2
ai-sandbox-factory profile add team-dev-v2 "$HOME/sandbox-profiles/team-dev" --ref v2
```

A copied profile with fetched source includes its saved `source/` files and
`source-revisions.json`. Registering that copy reuses those files without fetching
the original source again.

Registering the folder makes that version available for new sandboxes. It does
not change any running sandbox; use Review changes and Apply for the sandbox
attached to this editable folder. For a registration added without `--ref`,
`profile update NAME` imports its latest committed revision. A pinned registration
keeps its tag or commit. An ordinary non-Git folder uses its current files.

**Create from existing** starts the new sandbox with the profile version installed
in the selected sandbox. Edits in the folder are not installed automatically.
The new sandbox gets fresh private state and application data.
When the source has an editable profile, choose **Share the profile folder**:

- Leave it off for a separate copy of the installed profile. To edit it later, choose **Actions… → Edit profile…** in the new sandbox.
- Turn it on so both sandboxes use the same profile files and Git repository. Editing files does not update either sandbox automatically.

**Edit profile…** and **Review changes…** show which sandboxes share the folder.
Each review compares the files with the profile version installed in that sandbox.
To install the edits, choose **Actions… → Edit profile… → Review changes… → Apply
profile changes** in each sandbox you want to update. Sharing does not share databases,
private homes, chats or Factory-managed credentials.

To share the profile folder from `first` with another Pi sandbox on Podman:

```sh
mkdir -p "$HOME/projects/second"
ai-sandbox-factory create second --reuse-from first --reuse-editable-profile \
  --primary-agent pi --workspace "$HOME/projects/second" --engine podman --start
```

Configure the new sandbox's model access or sign-in as usual. The selected agent
must be supported by the installed profile. Deleting any sandbox keeps the host
profile folder and its Git history; other sandboxes using it continue to work.
