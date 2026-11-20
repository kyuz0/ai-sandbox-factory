"""Run the Python suite and fail on oversized buttons in any rendered test screen."""
import sys
import unittest
from unittest.mock import patch

from textual.widgets import Button


# Exceptions require explicit user approval. Key by (screen class, button ID).
APPROVED_HEIGHTS = {}


def main():
    resize = Button._size_updated
    violations = set()

    def check_size(button, size, *args, **kwargs):
        declared = button.styles.height
        height = max(size.height, declared.value if declared and declared.is_cells else 0)
        if height > 1:
            screen = type(button.screen).__name__
            if not button.id or APPROVED_HEIGHTS.get((screen, button.id)) != height:
                violations.add(f'{screen}: {button.id or button.label} requests or uses {height:g} rows')
        return resize(button, size, *args, **kwargs)

    # Textual supplies the allocated rectangle here, including borders and padding.
    with patch.object(Button, '_size_updated', check_size):
        suite = unittest.main(module=None, exit=False)
    for violation in sorted(violations):
        print(f'Button height violation: {violation}', file=sys.stderr)
    return not violations and suite.result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
