# Web search and website access

## Network access

**Restricted** is the default. Sandbox HTTP and HTTPS traffic goes through
a gateway that checks allowed hostnames and ports. Shell approvals leave these
rules in effect. **Unrestricted** gives the container ordinary network access.
[How isolation works](architecture.md#restricted-networking)

Open **Network and Web Search → Network** during creation or under **Settings**
for an existing sandbox. Pick **Website categories** such as **Python** for PyPI,
**npm** for JavaScript packages, **GitHub** for repositories and release downloads,
or **Hugging Face** for models and datasets. For private registries, redirects or
binaries hosted elsewhere, add the relevant DNS name and port under
**Additional allowed destinations**, one per line, for example
`packages.example.com:443`.

Use `curl` against the service’s HTTP or HTTPS URL to test access. **Restricted**
mode blocks external `ping` and direct DNS queries. Configure Git over SSH
through **Connectors → SSH & Git**. [Managed SSH](ssh.md)

Choose **Apply** to save. Changing only hosts or categories restarts the gateway
and may disconnect active requests. The sandbox and its terminals keep running.
Changing **Network mode** or **Web Search** prompts you to recreate the sandbox.

## Web search

Enable **Web Search** under **Network and Web Search** to add `web_search` and
`fetch_content` in Pi and FrostPi. [Search extension](https://github.com/nicobailon/pi-web-access)

**Search mode** starts with **Automatic fallback** and keyless Exa. Add OpenAI,
Brave, Tavily or a SearXNG server with **Add provider**. Exa accepts an optional
API key in its Edit dialog. Automatic mode uses pi-web-access's built-in provider
selection and fallback among your configured providers; adding a provider does
not set its priority. Choose **Only Exa**, **Only Brave**, etc. to restrict searches
to one provider, including requests where the agent names a provider explicitly.

Each row shows whether its key or server URL is configured. Providers missing
required settings are inactive. In an **Only…** mode, other providers stay saved
for later use. **Edit** lets you replace or explicitly remove a key; leaving the
key field empty keeps it. Changing a custom endpoint requires its own key.

Factory allows the active search providers’ endpoints automatically, including
`mcp.exa.ai` for keyless Exa and `api.exa.ai` for keyed Exa. To read a result directly
in **Restricted** mode, allow the website through a category or an exact hostname
and port.

For example, enable Exa and the **Python** category, then ask:

> Find Python’s documentation on list comprehensions and read the relevant page.

Search requests go to Exa. Reading `docs.python.org` goes directly to that host
through the sandbox gateway. Exa can also include text from other sites in its
search response. Hosted page-reader fallbacks are off.

Pi can search and read the text the search provider returned without asking for
approval. Searching does not open the pages in the background. When Pi needs to
open a result, it uses `fetch_content`. Your approval mode still applies to that
request, even in **Unrestricted** mode.

## Website categories

Each group has an **Allow all** checkbox that selects every category listed in
that group. Clear it to deselect them together, or use the individual checkboxes.
Factory saves the selected categories by name.

Under **Developer services**, select **VS Code extensions** to allow extension
catalogue requests and downloads from Open VSX for the browser editor and the
Visual Studio Marketplace for desktop VS Code. An extension that downloads
additional tools or data may need another allowed destination.
[code-server FAQ](https://coder.com/docs/code-server/FAQ) ·
[VS Code network connections](https://code.visualstudio.com/docs/setup/network)

The other developer categories come from
[V2Fly domain-list-community](https://github.com/v2fly/domain-list-community):
Python, npm, Rust, GitHub, Hugging Face, Stack Exchange and Read the Docs.

Under **Authentication**, select **Microsoft Entra / Microsoft 365** for Microsoft
public-cloud sign-in, MFA, password reset and authentication CDN hosts. This
includes `login.microsoftonline.com`, `login.live.com` and the `aadcdn` hosts under
Microsoft's authentication CDN domains. The bundled list follows
[Microsoft's sign-in guidance](https://learn.microsoft.com/en-us/azure/azure-portal/azure-portal-safelist-urls?tabs=public-cloud)
and the authentication entries in its
[Microsoft 365 endpoint list](https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges?view=o365-worldwide).
It is updated with Factory releases. Add your application's host and any
company-specific federation/ADFS host as individual allowed destinations.

Factory installs these optional **Authentication** lists on your computer.

- **Google sign-in** allows Google Account sign-in and Sign in with Google. [Google Identity Services](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
- **Gmail** adds webmail and attachment hosts and includes the Google sign-in
  hosts. Remote images, add-ons and other Workspace services can require
  additional destinations. [Gmail firewall settings](https://knowledge.workspace.google.com/admin/gmail/advanced/gmail-firewall-settings) · [Gmail attachment help](https://support.google.com/mail/answer/6584)
- **GitHub sign-in** allows the browser login and consent pages on `github.com`
  and their assets. [GitHub login documentation](https://docs.github.com/en/apps/creating-github-apps/writing-code-for-a-github-app/building-a-login-with-github-button-with-a-github-app)
- **Facebook and Meta sign-in** allows Facebook Login and the Meta account
  sign-in site. [Facebook Login endpoints](https://developers.facebook.com/docs/graph-api/guides/versioning) · [Meta account help](https://www.meta.com/help/connected-experiences/1613279052885559/)

Add the destination app and any organization-specific login hosts separately.
The GitHub and Facebook sign-in grants also allow other pages on `github.com`
and `www.facebook.com`, which share the login hostnames.

Under **Topics**, **News and press** combines UT1 Toulouse and Curlie. Financial
information, banking and library resources come from UT1 Toulouse through its
public mirror. [UT1 Toulouse](https://dsi.ut-capitole.fr/blacklists/) ·
[Public mirror](https://github.com/olbat/ut1-blacklists)

Curlie also supplies Health, Science and environment, Government and law,
Business and economics, Technology, Education and reference, History and
society, Arts and culture, Sports and leisure, and Consumer information.
These categories use English-language directory branches, including regional
listings for health, education, government and law. Select the topics you need
and add any missing host under **Additional allowed destinations**.

With content from [Curlie.org](https://curlie.org/) - the largest human-edited
directory of the web. Contribute by submitting a website or becoming an editor.
[Curlie license](https://curlie.org/docs/en/license.html)

Factory checks for V2Fly and UT1 updates when you start a sandbox or apply
network rules, at most once a day. If a source is unavailable, Factory uses the
last copy it has. Factory downloads the Curlie website list the first time you
create a sandbox or apply network rules that use it. The download is about
180 MB. Future sandboxes use the saved copy.

In **Website categories → Topics**, **Curlie website list** shows the download
date. Choose **Check for newer list** to see if an update is available, then
choose whether to download it. This changes the saved copy for future sandboxes.
Each existing sandbox keeps its current list. To update one, open its Network
settings, select **Use newer list**, and choose **Apply**. Opening a website
never downloads a list.

Categories grant HTTP and HTTPS access, including the listed domains’ subdomains
unless the source specifies an exact hostname. Category grants exclude private
and loopback addresses; explicit host-and-port rules support local model
servers and internal services.

Some groups include user-published content, and UT1’s coverage varies between
topics. Allowed hosts can receive uploads as well as serve downloads. HTTPS rules restrict the
connection’s hostname and port, not individual pages or what the site returns.

## Reuse your configuration

**Save as default Web Search** saves providers, search mode and API keys for new
sandboxes.

**Copy from sandbox…** loads another sandbox's web settings into the form.
**Include search API keys** is selected initially when the source is running.
A stopped sandbox can supply its settings; start it first if you want its keys.
In an existing target sandbox, leaving this unchecked keeps that target's keys
for matching providers and endpoints. In a new sandbox, it copies no keys.

Reusable keys are stored separately in
`~/.config/pi-sandbox-factory/web-access-keys.json` with owner-only permissions
(or under `XDG_CONFIG_HOME`). Each new sandbox receives its own copy. Keys are
matched to the provider and endpoint and are never included in `cockpit.json`,
profile packages or exported options. Removing a key and saving defaults again
removes it from the reusable configuration.

## Pi and FrostPi settings

Both use the sandbox’s `~/.pi/agent/web-search.json`. The manager sets the chosen
providers, search mode, restricted-network proxy and direct page fetching. It preserves manual
advanced settings across ordinary restarts, but changing managed search settings
replaces the generated configuration. Reload an open Pi/FrostPi session after
changing it. See the extension’s [configuration reference](https://github.com/nicobailon/pi-web-access#configuration)
for additional providers. Add their endpoints in **Settings → Network and Web Search → Network**.

Each sandbox keeps its API keys in its private home. Saving reusable defaults
also stores a private copy on the workstation; keys never appear in command arguments. The coding agent runs as the same user and can read them.
For CLI use, enter a configured provider’s key at a masked prompt:

```sh
pi-sandbox-factory web-key my-project --provider brave
```

Use `--key-stdin` to pipe a key from a secret manager.

Sending an empty value removes that service’s saved key. Environment variables
supported by pi-web-access may override file settings for an allowed provider.
OpenAI supports an optional Responses endpoint URL and search model; SearXNG
requires its server URL. The OpenAI search model is selected independently of
Pi’s coding model.

For interactive navigation, enable [Browser · Betterwright](usage.md#see-and-interact-with-the-agents-browser).

## CLI configuration

Pass an options file during creation or as `options` in an `update` settings file:

```json
{
  "web_access": {"enabled": true, "mode": "auto", "providers": {"exa": {}, "brave": {}}},
  "website_groups": ["dev-python", "dev-npmjs"]
}
```

Save Brave’s key with the command above to activate it.

For an existing sandbox, keep your manual rules in a file and pass category IDs
such as `dev-python` and `ut1-press`. [Category IDs](../pi_sandbox_factory/network-catalog/groups.json)

```sh
pi-sandbox-factory network my-project --allowlist hosts.txt \
  --website-groups dev-python ut1-press
```

An empty `--website-groups` removes all category grants. Omitting the flag preserves
the selection.
