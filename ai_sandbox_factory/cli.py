"""Application command and optional launcher notice."""
import os
import sys
import subprocess
import errno

PRODUCT_NAME = 'AI Sandbox Factory'
COMMAND = 'ai-sandbox-factory'


def main():
    from .sandbox import main as run
    from .cli_support import _secret_values, redact, silence_closed_stream
    token = _secret_values.set(())
    try:
        run(startup_notice=os.environ.pop('AI_FACTORY_STARTUP_NOTICE', ''))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, OSError) and error.errno in (errno.EPIPE, errno.EIO):
            silence_closed_stream(sys.stdout)
        detail = redact(str(error))
        if isinstance(error, subprocess.CalledProcessError):
            detail += '\n' + redact(error.stderr or '')
        try:
            print('Error: ' + detail, file=sys.stderr, flush=True)
        except OSError as output_error:
            if output_error.errno not in (errno.EPIPE, errno.EIO):
                raise
            silence_closed_stream(sys.stderr)
        raise SystemExit(1) from None
    finally:
        _secret_values.reset(token)
