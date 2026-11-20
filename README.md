# Pi Sandbox Factory

Run Pi in a configurable sandbox with a project folder, its own tools and saved
chats, and restricted networking by default. The browser editor uses an
open-source [FrostPi fork](https://github.com/kyuz0/pi-vscode-ui/tree/pi-sandbox-factory)
built around the same mainline `pi` executable as Pi CLI. Earlier versions used
[Pendant](https://github.com/cdervis/Pendant). Pi CLI and VS Code on host are
also available.

![Pi Sandbox Factory dashboard](docs/sandboxes.png)

## Install

Requires **rootless Podman**, **Python 3.10+**, **Git**, and **pipx**. Use WSL2
on Windows or a rootless Podman Machine on macOS. Linux also supports Docker for
Pi CLI or VS Code on host. [Platform setup](docs/platforms.md)

```sh
pipx ensurepath
pipx install git+https://github.com/kyuz0/pi-sandbox-factory.git
pi-sandbox-factory
```

Open a new terminal if `pi-sandbox-factory` is not found.

## Create a sandbox

1. Choose **New sandbox**, select a **Profile** and **Workspace**, check the
   **Sandbox name**, then choose **Continue**.
2. In **Review sandbox → Model Providers**, choose how Pi connects to a model.
   - **Local models:** enable **Connect to models on this computer** and enter
     your server port, usually `8000`. If no model appears, check that the server
     is running on that port, then refresh the model list. [Local model setup](docs/usage.md#local-models)
   - **Hosted models:** choose **Connect hosted provider…** for OpenRouter,
     Together, Azure/Foundry or Bedrock. Factory keeps these credentials in a
     separate container. [Hosted model setup](docs/hosted-models.md)
3. Choose **Create and start**. For a hosted connection, choose a model, select
   **Use as default**, then **Continue**. Leave **Open sandbox after creation**
   checked to open the editor.

If a hosted-only setup hits a `pasta` error on Linux or WSL, turn off
**Connect to models on this computer**. [Linux local model requirements](docs/platforms.md#linux-local-models)

## What the sandbox can access

- **Files:** `/workspace` is your project folder. Edits change the host files.
  Extra folders are read-only until you give them write access. [Folder setup](docs/usage.md#workspace-and-additional-folders)
- **Network:** In **Restricted** mode, the sandbox has no direct network
  connection. HTTP and HTTPS go through a gateway that checks the destination
  hostname and port. Approved shell commands still follow those network rules.
  **Unrestricted** gives the container ordinary network access. [Isolation and approvals](docs/architecture.md#restricted-networking)
- **Allowed destinations:** In **Settings → Network and Web Search → Network**,
  select **Website categories** such as **Python**, **npm**, or **GitHub**, or
  add an exact host and port such as `docs.python.org:443`. If a package
  downloads a binary from another host, allow that host too. Editing rules keeps
  the sandbox running, though active connections may drop. [Network settings](docs/web-access.md#network-access)
- **Web search:** Enabling a search provider lets Pi query that service.
  For Pi to open a result in **Restricted** mode, allow the site separately. [Web search setup](docs/web-access.md#web-search)

## Use and update sandboxes

Click **Open** beside a sandbox’s name. **Open with…** launches another client,
such as Pi CLI. [Everyday use and creation recovery](docs/usage.md)

![FrostPi beside the updated Python route planner, with edits and a passing test run visible](docs/frostpi.png)

*FrostPi updates a hiking route planner and runs its tests in the sandbox.*

Update Factory with **Upgrade now** or `pipx upgrade pi-sandbox-factory`.
To update a sandbox, use **Actions… → Update sources and recreate…**. This
builds the replacement first, then restarts the sandbox with its workspace
and saved chats intact. **Show Alerts** offers **Update to FrostPi…** for older
Pendant sandboxes. [Sandbox updates](docs/usage.md#update-factory-and-sandboxes) · [Archived Pendant branch](https://github.com/kyuz0/pi-sandbox-factory/tree/pendant)

## Profiles and providers

A **Profile** can add tools, skills, and Pi instructions. Its Dockerfile can
extend Factory’s base image with its own software and dependencies. A
**Provider** connects Pi to a model service. Install either through **Library…**
from a Git repository or local folder. Install packages you trust because they
can run code. [Profiles](docs/profiles.md) · [Providers](docs/providers.md)

Save reusable instructions in **Library → System prompts** and assign a copy in
**Settings → System prompt**. Pi uses changes on its next reply, including in an
open chat. [System prompt settings](docs/usage.md#system-prompts)

- [Browser editor](docs/browser-editor.md) · [Agent browser and vault protection](docs/usage.md#see-and-interact-with-the-agents-browser) · [Slack](docs/communications.md)
- [Approval modes](docs/approvals.md) · [SSH](docs/ssh.md) · [Git repository permissions](docs/repository-access.md) · [Advisors](docs/advisors.md)
- [Development and tests](TESTING.md) · [Agent guide](AGENTS.md)
