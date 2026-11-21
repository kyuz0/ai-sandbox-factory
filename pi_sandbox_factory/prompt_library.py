"""Reusable system prompts and the saved copy assigned to each sandbox."""

import json
from pathlib import Path
import uuid

from . import registry
from .sandbox_options import write_json

LIBRARY_FILE = 'system-prompts.json'
SANDBOX_FILE = 'runtime-config/sandbox-system-prompt.json'


class InvalidPrompt(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def prompt(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {'name', 'text'}:
        raise ValueError('An add-on needs a name and instructions.')
    name, text = value['name'], value['text']
    if not isinstance(name, str) or not name.strip():
        raise InvalidPrompt('name', 'Enter a name.')
    if any(not character.isprintable() for character in name):
        raise InvalidPrompt('name', 'Use a single line for the name.')
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise InvalidPrompt('text', 'Enter the instructions to add.')
    return {'name': name.strip(), 'text': text.strip()}


def entries(base=None):
    path = (base or registry.root()) / LIBRARY_FILE
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
        raise ValueError('The system prompt add-on library has an invalid format.')
    return {key: prompt(value) for key, value in values.items()}


def save(name, text, key=None):
    value = prompt({'name': name, 'text': text})
    with registry.locked() as base:
        values = entries(base)
        if key is not None and key not in values:
            raise ValueError('This add-on was removed. Close the editor and add it again.')
        if any(saved['name'].casefold() == value['name'].casefold() for other, saved in values.items() if other != key):
            raise InvalidPrompt('name', 'An add-on with that name already exists.')
        values[key or uuid.uuid4().hex] = value
        write_json(base / LIBRARY_FILE, values)


def remove(key):
    with registry.locked() as base:
        values = entries(base)
        if values.pop(key, None) is not None:
            write_json(base / LIBRARY_FILE, values)


def _stored(state):
    try:
        return json.loads((Path(state) / SANDBOX_FILE).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def _custom(value):
    return prompt(value['custom'] if isinstance(value, dict) and 'custom' in value else value)


def selection(state):
    return _custom(_stored(state))


def _with_connectors(state, custom):
    try:
        summary = (state / 'runtime-config/connectors-prompt.md').read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return custom
    text = custom['text'] + '\n\n' + summary if custom else summary
    # Existing Pi extensions read "text". The picker and sandbox copies use the original selection.
    return {'name': custom['name'] if custom else 'Sandbox connectors', 'text': text, 'custom': custom}


def refresh_connectors(state):
    state = Path(state)
    if not (state / SANDBOX_FILE).exists() and not (state / 'runtime-config/connectors-prompt.md').exists():
        return
    with registry.locked():
        stored = _stored(state)
        combined = _with_connectors(state, _custom(stored))
        if stored != combined:
            write_json(state / SANDBOX_FILE, combined)


def assign(state, value):
    state = Path(state)
    value = prompt(value)
    with registry.locked():
        if selection(state) != value:
            write_json(state / SANDBOX_FILE, _with_connectors(state, value))
            return True
    return False
