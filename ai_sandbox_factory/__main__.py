import sys

from .cli import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        sys.exit(130)
