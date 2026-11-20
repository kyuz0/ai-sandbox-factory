"""Run the Python suite and fail on oversized buttons in any rendered test screen."""
import sys
import unittest
from unittest.mock import patch

from textual.widgets import Button


# Exceptions require explicit user approval. Key by (screen class, button ID).
APPROVED_HEIGHTS = {}


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def run_tests():
    if sys.argv[1:2] != ['--shard']:
        return unittest.main(module=None, exit=False).result

    index, count = map(int, sys.argv[2:])
    if not 1 <= index <= count:
        raise ValueError('The shard number must be between 1 and the shard count.')
    cases = sorted(test_cases(unittest.defaultTestLoader.discover('tests')), key=lambda test: test.id())
    selected = cases[index - 1::count]
    print(f'Group {index}/{count}: {len(selected)} of {len(cases)} tests', flush=True)
    return unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))


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
        result = run_tests()
    for violation in sorted(violations):
        print(f'Button height violation: {violation}', file=sys.stderr)
    return not violations and result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
