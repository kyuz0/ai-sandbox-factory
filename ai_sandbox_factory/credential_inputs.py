"""Public declarations for masked credential input, without credential values."""
import copy

from .cli_support import prompt_secret

SECRET_FIELDS = {'provider_keys', 'provider_credentials', 'local_model_keys', 'hosted',
                 'communication_keys', 'web_keys', 'editor'}


def declarations(value):
    if not isinstance(value, list):
        raise ValueError('credential_prompts must be a list of masked input declarations.')
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) - {'path', 'label', 'options', 'required'}:
            raise ValueError('Each credential prompt needs path and label, with optional saved-key options.')
        path, label, settings = row.get('path'), row.get('label'), row.get('options', {})
        if (not isinstance(path, (list, tuple)) or not path
                or any(not isinstance(part, str) or not part for part in path)):
            raise ValueError('A credential prompt path must contain the credential field and provider ID.')
        path = tuple(path)
        group = path[0]
        valid = (len(path) == 2 and group in ('provider_keys', 'web_keys')
                 or len(path) == 2 and group == 'communication_keys' and path[1] in ('botToken', 'userToken')
                 or path == ('editor', 'password')
                 or len(path) == 3 and group in ('provider_credentials', 'local_model_keys') and path[2] == 'key'
                 or len(path) == 3 and group == 'hosted' and path[2] in ('key', 'access_key', 'secret_key', 'session_token')
                 or len(path) == 4 and group == 'hosted' and path[2:] == ('credential', 'key'))
        if not valid:
            raise ValueError('Unsupported credential prompt path: ' + '/'.join(path) + '. Use a documented creation credential field.')
        if not isinstance(label, str) or not label.strip() or any(not c.isprintable() for c in label):
            raise ValueError('A credential prompt label must be non-empty single-line text.')
        if (not isinstance(settings, dict) or set(settings) - {'label', 'replace'}
                or any(not isinstance(item, str) for item in settings.values())):
            raise ValueError('Credential prompt options may contain only the saved-key label and replacement reference.')
        if settings and not (group in ('provider_credentials', 'local_model_keys') or len(path) == 4):
            raise ValueError('Saved-key options apply only to a credential choice with a key field.')
        if type(row.get('required', True)) is not bool:
            raise ValueError('A credential prompt required setting must be true or false.')
        result.append({'path': path, 'label': label, 'options': copy.deepcopy(settings),
                       'required': row.get('required', True)})
    return result


def collect(value, supplied=None):
    """Read required inputs before creating resources; supplied stdin values win."""
    result = copy.deepcopy(supplied or {})
    for row in declarations(value):
        supplied_value = result
        for part in row['path']:
            supplied_value = supplied_value.get(part) if isinstance(supplied_value, dict) else None
        secret = supplied_value if supplied_value is not None else prompt_secret(row['label'])
        if not secret:
            if row['required']:
                raise ValueError(row['label'] + ' is empty. Enter the credential before creating the sandbox.')
            parent, ancestors = result, []
            for part in row['path'][:-1]:
                child = parent.get(part)
                if not isinstance(child, dict):
                    break
                ancestors.append((parent, part))
                parent = child
            else:
                parent.pop(row['path'][-1], None)
                for parent, part in reversed(ancestors):
                    if parent[part]:
                        break
                    del parent[part]
            continue
        parent = result
        for part in row['path'][:-1]:
            child = parent.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError('Credential input is not an object at ' + part)
            parent = child
        for key, setting in row['options'].items():
            parent.setdefault(key, setting)
        field = row['path'][-1]
        parent[field] = secret
    return result
