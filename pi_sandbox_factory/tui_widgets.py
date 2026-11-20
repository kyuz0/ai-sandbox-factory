"""Shared terminal controls."""
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Checkbox as TextualCheckbox, Input, Select, Static, TextArea


class EntryDialog(ModalScreen):
    """Start at the first available field after the dialog configures its controls."""

    def on_mount(self):
        for field in self.focus_chain:
            if isinstance(field, (Input, Select, TextArea)):
                self.set_focus(field)
                break


class Checkbox(TextualCheckbox):
    """Distinguish both states by shape, including in monochrome terminals."""
    @property
    def _button(self):
        return Content('[✓]' if self.value else '[ ]').stylize(self.get_visual_style('toggle--button'))


class Feedback(Static):
    """Validation feedback occupies space only while there is a message."""

    def __init__(self, content='', **kwargs):
        super().__init__(content, **kwargs)
        self.add_class('error')
        self.display = bool(content)

    def update(self, content=''):
        super().update(content)
        self.display = bool(content)
