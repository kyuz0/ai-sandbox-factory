"""Layout checks for the controls in rendered Factory screens."""
from textual.containers import Horizontal
from textual.widgets import Button, Input, Label


def check_checkbox_alignment(screen):
    for checkbox in screen.query('.form > Checkbox, .setting > Checkbox'):
        if isinstance(checkbox.parent, Horizontal) or not checkbox.region.width:
            continue
        if not all(widget.display and widget.visible for widget in (checkbox, *checkbox.ancestors)):
            continue
        if checkbox.region.x != checkbox.parent.content_region.x:
            raise AssertionError(f'{type(screen).__name__} #{checkbox.id} should line up with its form group')


def check_input_layout(screen):
    checked = []
    for field in screen.query(Input):
        if not all(widget.display and widget.visible for widget in (field, *field.ancestors)) or not field.region.width:
            continue
        name = f'{type(screen).__name__} #{field.id}'
        if field.region.height != 1 or field.content_region.height != 1:
            raise AssertionError(f'{name} must show a single row of text: {field.region}, content {field.content_region}')
        anchor = field
        if isinstance(field.parent, Horizontal):
            anchor = field.parent
            for action in anchor.children:
                if isinstance(action, Button) and action.display and action.region.width:
                    if (action.region.y, action.region.height) != (field.region.y, 1):
                        raise AssertionError(f'{name} and {action.id} should be on the same row: {field.region}, {action.region}')
        siblings = [child for child in anchor.parent.children if child.display and child.region.width]
        index = siblings.index(anchor)
        if index and isinstance(label := siblings[index - 1], Label):
            gap = field.region.y - label.region.bottom
            if gap != 1 or label.region.x != field.region.x:
                raise AssertionError(f'{name} should start one row below its label at the same column: {label.region}, {field.region}')
        checked.append(field.id)
    return checked
