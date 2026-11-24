# Providers

A provider tells Pi where to find models. Your provider choice applies
regardless of the profile you pick. A provider package can discover models and
add a command to refresh the model list while you’re in a chat.

## Local inference servers

The included [Pi Local Model Manager](https://github.com/kyuz0/pi-local-model-manager)
discovers models on the host ports you select. See [local setup](usage.md#local-models).

## Hosted services

For OpenRouter, Together, Azure/Foundry and AWS Bedrock, choose **Connect hosted
provider…** under **Built-in hosted providers** in **Model Providers**. Factory
keeps their keys in a separate inference container and configures Pi/FrostPi for you.
See [hosted model setup](hosted-models.md) for deployment names and model selection.

To connect another service, install a provider package. If the package declares
which API requests it needs, Factory can keep the key in a separate container
and let the package discover models through a local proxy.

You can also configure a service by editing Pi’s `models.json`. In that case the
key lives in the development container. If your sandbox uses **Restricted**
networking, allow the service’s host and port under
**Settings → Network and Web Search → Network**.

## Install and select

```sh
ai-sandbox-factory provider add company git@git.example.com:team/company-models.git
ai-sandbox-factory provider list
ai-sandbox-factory provider show company
```

Add `--subdir FOLDER` to install from a shared plugin repository. Updates retain
that folder and ref. The same option is available in **Library…**.

In **Settings → Model Providers → Local models & provider plugins**, enable the
package and fill in its non-secret settings. Factory adds the package's declared
development-container destinations to the allowlist. Inference routes use the
separate companion. Applying the changes rebuilds the sandbox and preserves the
workspace and private home.
During creation, enter the provider credential in its masked field, or leave it
empty to configure later. Providers with isolated inference store it in the
separate companion before discovery; the key is excluded from sandbox settings
and workstation defaults. A missing credential does not prevent opening the editor.
Use **Settings → Model Providers** to check, preview, refresh or replace a credential.

When you first assign a managed key, Factory opens the model picker. Choose
**Use as default** to use that model in new chats, or **Skip** to keep your
current default. To pick a different model later, open
**Settings → Model Providers → Provider plugins → Choose model…**.

For the CLI, put settings in a JSON file:

```json
{"local":{},"company":{"model":"your-model-id"}}
```

```sh
ai-sandbox-factory create project --workspace /absolute/path/to/project \
  --providers-file providers.json --local-model-ports 8000 \
  --allow-domain local-model.internal:8000 --start
ai-sandbox-factory configure project --provider company --key
ai-sandbox-factory configure project --provider company --preview
ai-sandbox-factory configure project --provider company --status
```

`--key` prompts without echo. `--key-stdin` accepts a key from a credential tool.

With an `inference` declaration, keys pass over stdin into the companion and never
reach the adapter. Without it, the adapter receives the key and owns its storage
inside the development container.

Omit `local` to use only other sources; use `{}` to configure no providers. Already
saved model entries and credentials are preserved when a source is disabled.
Local discovery adopts model entries at the selected host ports. Give each
endpoint one provider package to avoid competing catalogue owners.

Reload an open Pi/FrostPi session after a launcher refresh, or use the provider's
in-session refresh command.

Run `ai-sandbox-factory provider update NAME` or choose **Update** under
**Library → Providers** to fetch the registered source. Factory keeps the saved
branch or ref and subfolder. **Actions → Update sources
and recreate…** applies installed package updates to an existing sandbox.
`recreate NAME --profile PROFILE` does the same from the CLI. Ordinary starts and
settings changes retain saved versions. Removing a registration leaves existing
sandboxes usable.

## Write an adapter

Copy [examples/provider](../examples/provider). Its manifest declares:

- `version: 1`, `name`, `description`, and a relative `entry` module.
- Optional `settings`: text fields, plus one secret field for the credential.
  Set `network: true` on a text field containing an HTTP(S) API endpoint.
- Optional `network`: fixed `host:port` destinations for the development container.
- Optional `inference`: an HTTPS origin, API base path, authentication and allowed operations.
- Optional `extensions` and `skills`: paths relative to the package.
- Optional `git_source` and `git_ref`: another repository exported into `source/`.

The entry exports:

```js
export async function run({action, agentDir, settings, secret, endpoint}) {
  return {state: 'ready', message: 'Configured the requested model.'};
}
```

`action` is `status`, `preview` or `refresh`. Text defaults are merged with saved
settings. For isolated inference, `endpoint` is the local proxy URL and `secret` is a harmless
placeholder on every call. Use them for discovery and saved Pi model configuration.
Other adapters receive `secret` only during credential entry. Return one of
`ready`, `offline`, `needs-auth` or `error`, with a useful message that contains no
credentials. Status should describe saved state; preview must not change it.
Refresh writes `${agentDir}/models.json` using Pi's model schema.

Managed API-key providers can use Factory's saved-key picker. Declare a
`credentialCheck` object with a fixed authenticated GET `path` on the inference
origin. Factory uses it to check a new key before changing the active assignment.
If omitted, Factory uses the first declared GET operation ending in `/models`.
Use a check that returns 401 or 403 for invalid credentials. An explicit check
also lets Pi request a status-only test of the saved connection. Factory sends
no prompt and discards the provider’s response body and headers.

Preserve other providers and user customizations. Validate responses before
writing, retain the old catalogue on failure, and replace files atomically.
The runner serializes adapter calls and supplied extension commands using a shared
lock. Use those paths for catalogue changes; background writers must coordinate
with the same lock. Provider extensions load inside Pi/FrostPi, never on the host.

The example configures one text-only model. Replace its explicit context/output
limits, input capabilities and API compatibility with your service's documented
values.

Run `node --test tests/test_provider_runtime.mjs` to exercise the example contract,
then test your adapter against a fixture service and in a disposable sandbox.

## Declare an isolated API

The [example provider](../examples/provider/provider.json) declares:

```json
"inference": {
  "origin": "https://models.example.com",
  "basePath": "/v1",
  "auth": {"type": "bearer"},
  "operations": [
    {"method": "GET", "path": "/v1/models"},
    {"method": "POST", "path": "/v1/chat/completions"}
  ]
}
```

Paths are exact; declare required query values
as `"query": {"model_type": ["ALL_TEXT"]}`. Header authentication uses
`{"type":"header","header":"api-key"}` or `x-api-key`. The base path tells the
adapter where the API begins; it does not grant additional routes. Factory checks
all routes before starting or replacing the companion.

An in-session extension must reuse the saved proxy endpoint and placeholder.
Credential replacement belongs in Factory's **Settings → Model Providers → Configure
credentials**, rather than asking for a key inside Pi.

### List embedding models in Document search

Add `embeddingCatalogue` to the provider’s `inference` declaration. Give it
the API base path and a GET path that lists embedding models. The optional
`label` names the provider in the picker.

```json
{
  "embeddingCatalogue": {"label": "Example Models", "basePath": "/v1", "path": "/v1/embeddings/models"},
  "operations": [{"method": "GET", "path": "/v1/embeddings/models"}]
}
```

Declare that GET in `operations`. If the catalogue requires a fixed filter,
set `query` on the catalogue and allow the same value on the operation.
Factory reads model IDs from the response’s `data` array. For providers that
limit batch sizes, `embeddingBatchSizes` maps model IDs to the number of texts
allowed per request, for example `{"example-model": 1}`.
