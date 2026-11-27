"""Command-line adapters for model connections and saved provider keys.

The adapters parse arguments, read secrets from stdin or a masked prompt and
call the shared operations. Business rules stay in ``operations.Service``,
``hosted_models``, ``inference`` and ``provider_credentials``.
"""
from contextlib import contextmanager, redirect_stdout
import json
from pathlib import Path
import sys

from . import cli_support, hosted_models, inference, operations, provider_credentials

COMMAND = 'ai-sandbox-factory'
# Fields that carry credentials. They are accepted only on stdin or from a
# masked prompt so that they never appear in argv, shell history or files
# that describe the nonsecret connection.
SECRET_FIELDS = ('key', 'access_key', 'secret_key', 'session_token')
IAM_FIELDS = ('access_key', 'secret_key', 'session_token')
# Plugin providers use the same model actions as hosted providers only where
# providers/hosted-models.mjs accepts ``plugin: true``.
PLUGIN_ACTIONS = ('catalogue', 'select')


def register(subparsers):
    models = subparsers.add_parser('models', help='Manage hosted model connections and model selection')
    actions = models.add_subparsers(dest='models_command', metavar='COMMAND', required=True)

    command = _leaf(actions, 'connections', 'Show the hosted model connections of a sandbox', connections)
    command.add_argument('name')

    command = _leaf(actions, 'connect', 'Add or update a hosted model connection', connect)
    command.add_argument('name')
    command.add_argument('--settings-file', required=True, metavar='FILE',
                         help='JSON file with nonsecret settings, for example {"provider": "openrouter"}; '
                              'use - to read stdin')
    supplied = command.add_mutually_exclusive_group()
    supplied.add_argument('--key-stdin', action='store_true', help='Read the API key or bearer token from stdin')
    supplied.add_argument('--secrets-stdin', action='store_true',
                          help='Read AWS IAM credentials as JSON from stdin: '
                               '{"access_key": ..., "secret_key": ..., "session_token": ...}')
    supplied.add_argument('--saved-key', metavar='REF', help='Use a key saved on this computer')
    command.add_argument('--remember', metavar='LABEL', help='Also save the entered key on this computer under LABEL')
    command.add_argument('--replace', metavar='REF', help='Replace this saved key with the entered key (requires --remember)')

    command = _leaf(actions, 'disconnect', 'Remove a hosted model connection', disconnect)
    command.add_argument('name')
    command.add_argument('--provider', required=True, metavar='ID')

    command = _leaf(actions, 'list', 'List the models of a provider', list_models)
    command.add_argument('name')
    providers = command.add_mutually_exclusive_group(required=True)
    providers.add_argument('--provider', metavar='ID')
    providers.add_argument('--local', action='store_true', help='List models discovered on configured local server ports')
    command.add_argument('--plugin', action='store_true', help='The provider is an installed provider plugin')
    command.add_argument('--refresh', action='store_true', help='Fetch the current model list from the provider first')

    for leaf, text, handler in (('select', 'Choose the default chat model', select),
                                ('test', 'Send a short test request to a model', test)):
        command = _leaf(actions, leaf, text, handler)
        command.add_argument('name')
        command.add_argument('--provider', required=True, metavar='ID')
        command.add_argument('--model', required=True, metavar='ID')
        source = command.add_mutually_exclusive_group()
        source.add_argument('--plugin', action='store_true', help='The provider is an installed provider plugin')
        if leaf == 'select':
            source.add_argument('--local', action='store_true', help='Choose a provider and model from models list --local')
        command.add_argument('--deployment', default='', metavar='NAME', help='Azure deployment name for the model')
        command.add_argument('--profile-id', default='', metavar='ID',
                             help='AWS Bedrock model or inference profile ID or ARN')

    command = _leaf(actions, 'approval-models', 'List the models available for approvals', approval_models)
    command.add_argument('name')

    command = _leaf(actions, 'select-approval-model', 'Choose the model that reviews approvals', select_approval_model)
    command.add_argument('name')
    command.add_argument('--model', required=True, metavar='ID',
                         help='PROVIDER/MODEL from approval-models, or current to use the chat model')

    credentials = subparsers.add_parser('credentials', help='Manage provider API keys saved on this computer')
    actions = credentials.add_subparsers(dest='credentials_command', metavar='COMMAND', required=True)

    _leaf(actions, 'list', 'List saved provider keys (names and endpoints only)', list_credentials)

    command = _leaf(actions, 'status', 'Show which providers of a sandbox have keys', credential_status)
    command.add_argument('name')
    command.add_argument('--local', action='store_true', help='Also check that local server keys are enabled')

    command = _leaf(actions, 'save', 'Save a key on this computer for a provider of a sandbox', save_credential)
    command.add_argument('name')
    command.add_argument('--provider', required=True, metavar='ID')
    command.add_argument('--label', required=True)
    command.add_argument('--key-stdin', action='store_true', help='Read the API key from stdin')
    command.add_argument('--replace', metavar='REF', help='Replace this saved key')

    command = _leaf(actions, 'assign', 'Give a sandbox provider an API key', assign_credential)
    command.add_argument('name')
    command.add_argument('--provider', required=True, metavar='ID')
    source = command.add_mutually_exclusive_group()
    source.add_argument('--ref', metavar='REF', help='Use a key saved on this computer')
    source.add_argument('--key-stdin', action='store_true', help='Read the API key from stdin')
    command.add_argument('--remember', metavar='LABEL', help='Also save the entered key on this computer under LABEL')
    command.add_argument('--replace', metavar='REF', help='Replace this saved key with the entered key (requires --remember)')
    command.add_argument('--no-refresh', action='store_true', help='Assign the key without refreshing the provider catalogue')

    command = _leaf(actions, 'disconnect', 'Remove the API key of a sandbox provider', disconnect_credential)
    command.add_argument('name')
    command.add_argument('--provider', required=True, metavar='ID')

    command = _leaf(actions, 'forget', 'Delete a saved key from this computer', forget_credential)
    command.add_argument('ref')


def _leaf(actions, name, text, handler):
    parser = actions.add_parser(name, help=text, description=text)
    parser.add_argument('--json', action='store_true', help='Print the result as JSON')
    parser.set_defaults(cli_handler=handler)
    return parser


def _service(args):
    return operations.Service(Path(args.state_dir))


def _log(message):
    print(message, file=sys.stderr)


@contextmanager
def _shared(args, secrets=()):
    """Run a shared operation: progress goes to stderr and supplied secrets are redacted."""
    values = sorted({s for s in secrets if s}, key=len, reverse=True)
    try:
        if getattr(args, 'json', False):
            # Keep stdout a single JSON value even if a shared operation prints.
            with redirect_stdout(sys.stderr):
                yield
        else:
            yield
    except Exception as error:
        chain, seen = [], error
        while seen is not None and seen not in chain:
            chain.append(seen)
            seen = seen.__cause__ or seen.__context__
        if not values or not any(value in str(item) for item in chain for value in values):
            raise
        text = str(error)
        for value in values:
            text = text.replace(value, '[redacted]')
        # The chained exceptions contain a supplied secret, so they are not kept.
        raise ValueError(f'{text} (credential removed from this message)') from None


def _read_key(from_stdin, prompt):
    key = cli_support.read_secret(from_stdin, prompt)
    if not key:
        source = 'stdin' if from_stdin else 'the prompt'
        raise ValueError(f'No {prompt} was read from {source}. Enter the key, or pipe it with --key-stdin.')
    return key


def _check_hosted(provider):
    if provider not in hosted_models.PROVIDERS:
        raise ValueError(f'Provider {json.dumps(provider)} is not a hosted model connection. Hosted providers are: '
                         f'{", ".join(hosted_models.PROVIDERS)}. For an installed provider plugin, add --plugin.')


def _remember_choice(args, key, binding, provider):
    if args.replace and not args.remember:
        raise ValueError('--replace needs --remember LABEL to name the replacement key.')
    if args.remember and not binding:
        raise ValueError(f'Keys for {provider} with these settings cannot be saved on this computer: '
                         'the connection does not use a reusable API key.')
    choice = {'key': key}
    if args.remember:
        choice['label'] = args.remember
    if args.replace:
        choice['replace'] = args.replace
    return choice


def connections(args):
    with _shared(args):
        result = _service(args).hosted_request(args.name, 'status')
    if args.json:
        return cli_support.emit(args, result)
    rows = result.get('providers', [])
    if not rows:
        return cli_support.emit(args, f'{args.name} has no hosted model connections. Add one with: '
                                      f'{COMMAND} models connect {args.name} --settings-file FILE --key-stdin')
    lines = []
    for row in rows:
        details = [row['provider'], 'key assigned' if row.get('configured') else 'no key']
        if row.get('assignment'):
            details.append(f'saved key “{row["assignment"].get("label", "")}”')
        details += [f'{field}: {row[field]}' for field in ('endpoint', 'region', 'method') if row.get(field)]
        lines.append(' · '.join(details))
    cli_support.emit(args, '\n'.join(lines))


def connect(args):
    if args.settings_file == '-' and (args.key_stdin or args.secrets_stdin):
        raise ValueError('Stdin can carry either the settings file or the credentials. '
                         'Write the nonsecret settings to a file and pass its path with --settings-file.')
    settings = cli_support.read_json(args.settings_file)
    if not isinstance(settings, dict):
        raise ValueError(f'{args.settings_file} must contain a JSON object with the connection settings, '
                         'for example {"provider": "azure", "endpoint": "https://example.openai.azure.com"}.')
    rejected = [field for field in (*SECRET_FIELDS, 'credential') if field in settings]
    if rejected:
        raise ValueError(f'{args.settings_file} contains credential fields ({", ".join(rejected)}). Remove them: '
                         'supply an API key with --key-stdin, AWS IAM credentials with --secrets-stdin, '
                         'or a saved key with --saved-key REF.')
    provider = settings.get('provider')
    _check_hosted(provider)
    iam = provider == 'amazon-bedrock' and settings.get('method') == 'iam'
    if iam and (args.key_stdin or args.saved_key or args.remember):
        raise ValueError('AWS Bedrock with "method": "iam" uses access keys, not an API key. '
                         'Supply them as JSON with --secrets-stdin.')
    if args.secrets_stdin and not iam:
        raise ValueError('--secrets-stdin supplies AWS access keys and applies only to "provider": "amazon-bedrock" '
                         'with "method": "iam". Use --key-stdin for an API key or bearer token.')
    if (args.remember or args.replace) and not args.key_stdin:
        raise ValueError('--remember and --replace save an entered API key on this computer. Add --key-stdin.')
    service = _service(args)
    value = {**settings, **{field: '' for field in SECRET_FIELDS}}
    if args.secrets_stdin:
        secrets = cli_support.read_json('-')
        if not isinstance(secrets, dict):
            raise ValueError('--secrets-stdin expects a JSON object with access_key, secret_key '
                             'and, for temporary credentials, session_token.')
        unknown = sorted(set(secrets) - set(IAM_FIELDS))
        if unknown:
            raise ValueError(f'--secrets-stdin received unsupported fields ({", ".join(unknown)}). '
                             f'Use only {", ".join(IAM_FIELDS)}.')
        wrong = [field for field, item in secrets.items() if not isinstance(item, str)]
        if wrong:
            raise ValueError(f'--secrets-stdin fields must be strings: {", ".join(wrong)}.')
        value.update(secrets)
    key = _read_key(True, 'API key') if args.key_stdin else ''
    if not (key or args.secrets_stdin or args.saved_key):
        existing = service.config(args.name).get('hosted_connections', {})
        if provider not in existing:
            if iam:
                raise ValueError(f'{hosted_models.PROVIDERS[provider]} needs AWS credentials for a new connection. '
                                 'Supply them as JSON with --secrets-stdin.')
            key = _read_key(False, f'{hosted_models.PROVIDERS[provider]} API key')
    value['key'] = key
    binding = None if iam else hosted_models.credential_scope(value)
    if args.saved_key:
        if not binding:
            raise ValueError(f'{hosted_models.PROVIDERS[provider]} with these settings does not use a reusable '
                             'API key, so --saved-key cannot be used. Supply the credential on stdin.')
        value['credential'] = {'ref': args.saved_key}
    elif key and binding:
        value['credential'] = _remember_choice(args, key, binding, provider)
    elif args.remember or args.replace:
        _remember_choice(args, key, binding, provider)  # Raises: these settings have no reusable key.
    with _shared(args, [value.get(field) for field in SECRET_FIELDS]):
        result = service.hosted_save(args.name, value)
    if args.json:
        return cli_support.emit(args, result)
    lines = [f'Connection to {hosted_models.PROVIDERS[provider]} saved for {args.name}.']
    if result.get('assignment'):
        lines.append(f'It uses the saved key “{result["assignment"].get("label", "")}”.')
    lines.append(f'Choose a model with: {COMMAND} models list {args.name} --provider {provider}')
    cli_support.emit(args, '\n'.join(lines))


def disconnect(args):
    _check_hosted(args.provider)
    with _shared(args):
        result = _service(args).hosted_remove(args.name, args.provider)
    cli_support.emit(args, result if args.json else
                     f'Removed the {hosted_models.PROVIDERS[args.provider]} connection from {args.name}.')


def list_models(args):
    service = _service(args)
    with _shared(args):
        if args.local:
            if args.plugin:
                raise ValueError('--plugin requires --provider ID. To list discovered local models, use --local alone.')
            result = service.local_models(args.name, refresh=args.refresh)
        elif args.plugin:
            result = service.plugin_model(args.name, 'catalogue', provider=args.provider, refresh=args.refresh)
        else:
            _check_hosted(args.provider)
            values = {'provider': args.provider}
            if args.refresh:
                values['refresh'] = True
            result = service.hosted_request(args.name, 'catalogue', **values)
    if args.json:
        return cli_support.emit(args, result)
    if result.get('warning'):
        _log(result['warning'])
    models = result.get('models', [])
    if not models:
        hint = '' if args.refresh else ' Add --refresh to fetch the current list from the provider.'
        if args.local:
            return cli_support.emit(args, f'No local models were discovered in {args.name}. '
                f'Check the configured servers and run {COMMAND} models list {args.name} --local --refresh.')
        return cli_support.emit(args, f'No models are saved for {args.provider} in {args.name}.{hint}')
    if args.local:
        return cli_support.emit(args, '\n'.join(f'{m["provider"]}/{m["id"]}  {m.get("name") or ""}'.rstrip() for m in models))
    cli_support.emit(args, '\n'.join(f'{m["id"]}  {m.get("name") or ""}'.rstrip() for m in models))


def _plugin_unsupported(args, action):
    extra = [flag for flag, item in (('--deployment', args.deployment), ('--profile-id', args.profile_id)) if item]
    if extra:
        raise ValueError(f'{", ".join(extra)} applies only to hosted Azure and AWS Bedrock connections, '
                         'not provider plugins. Remove it or drop --plugin.')
    if action not in PLUGIN_ACTIONS:
        raise ValueError(f'Testing a model of provider plugin {args.provider} is not supported: the sandbox model '
                         'tool tests only hosted connections. Select the model and send a message in Pi instead.')


def select(args):
    service = _service(args)
    if args.local:
        if args.deployment or args.profile_id:
            raise ValueError('Local models use the provider and model IDs from models list --local. Remove --deployment and --profile-id.')
        with _shared(args):
            result = service.local_select(args.name, args.provider, args.model)
    elif args.plugin:
        _plugin_unsupported(args, 'select')
        with _shared(args):
            result = service.plugin_model(args.name, 'select', provider=args.provider, model=args.model)
    else:
        _check_hosted(args.provider)
        with _shared(args):
            result = service.hosted_select(args.name, args.provider, args.model,
                                           deployment=args.deployment, profile_id=args.profile_id)
    if args.json:
        return cli_support.emit(args, result)
    chosen = f'{result.get("provider", args.provider)}/{result.get("model", args.model)}'
    cli_support.emit(args, f'Default chat model for {args.name}: {chosen}. '
                           'An open Pi session keeps its current model until you choose this one with /model.')


def test(args):
    if args.plugin:
        _plugin_unsupported(args, 'test')
    _check_hosted(args.provider)
    with _shared(args):
        result = _service(args).hosted_test(args.name, args.provider, args.model,
                                            deployment=args.deployment, profile_id=args.profile_id)
    cli_support.emit(args, result if args.json else f'{args.provider}/{args.model} answered the test request.')


def approval_models(args):
    with _shared(args):
        result = _service(args).approval_models(args.name)
    if args.json:
        return cli_support.emit(args, result)
    if not result:
        return cli_support.emit(args, f'No models with a key are available for approvals in {args.name}.')
    cli_support.emit(args, '\n'.join(f'{m["provider"]}/{m["id"]}  {m.get("name") or ""}'.rstrip() for m in result))


def select_approval_model(args):
    with _shared(args):
        _service(args).save_approval_model(args.name, args.model, _log)
    target = 'the chat model' if args.model == 'current' else args.model
    cli_support.emit(args, {'name': args.name, 'approval_model': args.model} if args.json
                     else f'Approvals in {args.name} use {target}.')


def _credential_rows(records):
    return [{'ref': ref, 'label': row['label'], **row['scope']} for ref, row in sorted(
        records.items(), key=lambda item: (item[1]['scope'].get('provider', ''), item[1]['label'].casefold()))]


def list_credentials(args):
    rows = _credential_rows(provider_credentials.entries())
    if args.json:
        return cli_support.emit(args, rows)
    if not rows:
        return cli_support.emit(args, 'No provider keys are saved on this computer.')
    cli_support.emit(args, '\n'.join(f'{row["ref"]}  {row["label"]} · {row.get("provider", "")} · '
                                     f'{row.get("endpoint", "")}' for row in rows))


def credential_status(args):
    with _shared(args):
        result = _service(args).provider_key_status(args.name, local=args.local)
    if args.json:
        return cli_support.emit(args, result)
    configured = result.get('configured', [])
    if not configured:
        return cli_support.emit(args, f'No provider in {args.name} has a key.')
    assignments = result.get('assignments') or {}
    cli_support.emit(args, '\n'.join(
        provider + (f' · saved key “{assignments[provider].get("label", "")}”' if assignments.get(provider) else ' · key entered')
        for provider in configured))


def save_credential(args):
    config = _service(args).config(args.name)
    routes = inference.routes(config)
    route = routes.get(args.provider)
    if not route:
        available = ', '.join(sorted(routes)) or 'none'
        raise ValueError(f'{args.name} has no model provider {json.dumps(args.provider)}. '
                         f'Providers with managed keys in this sandbox: {available}.')
    binding = provider_credentials.scope('local' if route.get('local') else args.provider, route)
    if not binding:
        raise ValueError(f'Provider {args.provider} in {args.name} does not use a reusable API key, '
                         'so its key cannot be saved on this computer.')
    key = _read_key(args.key_stdin, f'{args.provider} API key')
    with _shared(args, [key]):
        ref = provider_credentials.save(binding, args.label, key, replace=args.replace)
    result = {'ref': ref, 'label': args.label.strip(), **binding}
    cli_support.emit(args, result if args.json else
                     f'Saved key “{result["label"]}” ({ref}) for {binding["provider"]} at {binding["endpoint"]}. '
                     f'Assign it with: {COMMAND} credentials assign {args.name} --provider {args.provider} --ref {ref}')


def assign_credential(args):
    if args.ref and (args.remember or args.replace):
        raise ValueError('--remember and --replace apply to an entered key. Remove them when using --ref.')
    if args.ref:
        choice, key = {'ref': args.ref}, ''
    else:
        key = _read_key(args.key_stdin, f'{args.provider} API key')
        choice = _remember_choice(args, key, True, args.provider)
    with _shared(args, [key]):
        assignment = _service(args).assign_provider_key(args.name, args.provider, choice, _log,
                                                       **({'refresh': False} if args.no_refresh else {}))
    result = {'name': args.name, 'provider': args.provider, 'assignment': assignment}
    if args.json:
        return cli_support.emit(args, result)
    source = f'saved key “{assignment.get("label", "")}”' if assignment else 'the entered key'
    cli_support.emit(args, f'{args.provider} in {args.name} now uses {source}.')


def disconnect_credential(args):
    with _shared(args):
        result = _service(args).disconnect_provider_key(args.name, args.provider)
    cli_support.emit(args, result if args.json else f'Removed the {args.provider} key from {args.name}.')


def forget_credential(args):
    if args.ref not in provider_credentials.entries():
        raise ValueError(f'No saved key has the reference {json.dumps(args.ref)}. '
                         f'List saved keys with: {COMMAND} credentials list')
    provider_credentials.forget(args.ref)
    cli_support.emit(args, {'ref': args.ref, 'forgotten': True} if args.json else
                     f'Forgot saved key {args.ref}. Sandboxes that use it keep their copy of the key.')
