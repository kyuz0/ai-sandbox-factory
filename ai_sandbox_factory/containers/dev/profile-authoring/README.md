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
They cannot be shared between services through a generated-value reference. For
applications that need a shared password, a literal value in both services'
`environment` settings works, but is visible in the profile source.

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

## Edit and version a profile

The applied files at `/opt/sandbox/base-profile` are read-only. They describe the
running image; editing an installed Library package does not change an existing
sandbox. Edit a separate writable copy. If you are an agent
working inside a sandbox, read this guide, propose the required profile changes,
and ask the user for a separate writable profile folder. The user can share it
through **Settings → Folders** as a writable additional folder, for example
`/mnt/profile`. Applying a new folder mount recreates the coding-agent container
and interrupts its running processes; Factory asks for confirmation first.
Edit that copy, not the project's `/workspace` or Factory's
read-only build records. Host installation
and application of that profile remain actions for the user or Factory.

To start from the current profile, copy only
`/opt/sandbox/base-profile/profile/` to the writable profile folder. Do not use
`base/`, `workspace/` or `build.json` as the editable profile: those describe
Factory's own build inputs. The authoring example is another complete starting
point. The writable copy may be its own Git repository even if its original
profile came from a subfolder of a larger repository. For an existing writable
`/mnt/profile` folder, copy its contents without nesting another directory:

```sh
cp -R /opt/sandbox/base-profile/profile/. /mnt/profile/
```

Set a new manifest `name` and an explicit `agents` list in the copy, including
when starting from Factory's `base` profile. The host folder mounted at
`/mnt/profile` is the folder to commit and register with `profile add`. The full
authoring example is also available at
`/opt/sandbox/base-profile/authoring/example/`.

When adding a host:

1. Add a Dockerfile and its build files, or reuse an existing build context.
2. Add the host under a new `services` key, with its application settings,
   persistent paths, readiness check and agent-readable connection details.
3. Update `profile.md`, `start-here.md` and the profile's `description`
   so they describe the new host and its purpose.
4. Commit and install the revised profile, create a disposable sandbox and run
   the checks above before using it for project data.

The separate profile repository starts without the original repository's remote.
Add your own Git remote if you want to share it; this does not change the
repository the original profile came from.

After editing, commit the new files. A new Git tag and registration can retain
both versions:

```sh
git -C "$HOME/sandbox-profiles/team-dev" add .
git -C "$HOME/sandbox-profiles/team-dev" commit -m "Add an application service"
git -C "$HOME/sandbox-profiles/team-dev" tag v2
ai-sandbox-factory profile add team-dev-v2 "$HOME/sandbox-profiles/team-dev" --ref v2
```

For a registration added without `--ref`, `ai-sandbox-factory profile update team-dev` imports
its latest committed revision. Updating a registration pinned to a tag or commit
keeps that revision. An ordinary non-Git folder uses its current files instead.
Existing sandboxes keep their own saved profiles in all cases.

In this version, any change to a service declaration requires choosing
**New sandbox** with the updated profile registration. This includes its image,
build settings or any file in its build context, environment, command, volumes,
healthcheck, connections, SSH settings and description, as well as adding or
removing hosts. Factory detects these changes and refuses them before stopping
anything. **Create from existing** reuses the saved profile; it does not adopt an
updated registration. Changing a PostgreSQL initialization environment variable
such as `POSTGRES_PASSWORD` does not change a password in an initialized database.
The new sandbox initializes new application data; it does not copy the original
sandbox's databases. Export/import any data you need to move, and keep the old
sandbox until you have verified the replacement.

For Pi, changes confined to the coding-agent container can be applied from an
updated installed profile with **Update sources and recreate…**, provided the
profile services stay unchanged. For Codex, Claude Code and Antigravity,
**Rebuild sandbox…** refreshes Factory software using the saved profile. To use
an edited profile with one of those agents, create a new sandbox with that profile.
