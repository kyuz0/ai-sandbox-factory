"""Small input/output helpers for the public command-line adapters."""
import getpass
import argparse
import os
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import warnings


_secret_values = ContextVar('factory_cli_secrets', default=())


def supplied_options(parser, args, argv):
    """Keep explicit overrides, including argparse's accepted option abbreviations."""
    names = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            names.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                selected = action.choices.get(getattr(args, action.dest, None))
                if selected is not None:
                    pending.append(selected)
    result = set()
    for token in argv:
        if token == '--':
            break
        flag = token.split('=', 1)[0]
        if flag in names:
            result.add(flag)
        elif flag.startswith('--'):
            matches = [name for name in names if name.startswith(flag)]
            if len(matches) == 1:
                result.add(matches[0])
    return result


def remember_secret(value):
    if isinstance(value, str) and value:
        _secret_values.set((*_secret_values.get(), value))
    return value


def redact(text):
    from .model_providers import diagnostic
    value = str(text)
    for secret in _secret_values.get():
        value = diagnostic(value, secret)
    return diagnostic(value)


def read_json(path):
    try:
        text = sys.stdin.read() if str(path) == '-' else Path(path).expanduser().read_text()
        return json.loads(text)
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read JSON from {path}: {error}. Check the file or stdin input.') from error


def read_secret(from_stdin, prompt='API key'):
    if from_stdin:
        return remember_secret(sys.stdin.read().rstrip('\r\n'))
    if not sys.stdin.isatty():
        raise ValueError(f'{prompt} is required. Supply it with --key-stdin or run in an interactive terminal.')
    return remember_secret(getpass.getpass(prompt + ': '))


def prompt_secret(prompt):
    """Use the controlling terminal even when JSON is arriving through stdin."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return remember_secret(getpass.getpass(prompt + ': '))
    except (EOFError, OSError, getpass.GetPassWarning) as error:
        raise ValueError(f'{prompt} requires masked terminal input. Run this command in a terminal '
                         'or save settings in a file and supply credentials using the command’s documented stdin option.') from error


def emit(args, value):
    if getattr(args, 'json', False):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    elif isinstance(value, str):
        print(value, flush=True)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def silence_closed_stream(stream):
    """Prevent a second flush of an already failed pipe during interpreter shutdown."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), descriptor)
