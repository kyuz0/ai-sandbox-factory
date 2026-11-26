"""Shared terminal controls."""
from textual.content import Content
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets._collapsible import Collapsible as TextualCollapsible, CollapsibleTitle
from rich.table import Table
from rich.text import Text
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


class Details(Static):
    """Read-only facts with distinct labels and literal, unstyled values."""

    def __init__(self, rows, **kwargs):
        kwargs['markup'] = False
        super().__init__('', **kwargs)
        self.set_rows(rows)

    def set_rows(self, rows):
        content = Text()
        for label, value in rows:
            if content:
                content.append('\n')
            content.append(str(label) + ': ', style='bold')
            content.append(str(value))
        self.update(content)


class Command(Static):
    """A literal command, visually separate from explanatory prose."""

    def __init__(self, command, **kwargs):
        kwargs['markup'] = False
        super().__init__(command, **kwargs)
        self.add_class('command-text')


class Bullet(Horizontal):
    """Keep wrapped list text aligned after its marker."""
    DEFAULT_CSS = """
    Bullet { height: auto; width: 1fr; }
    Bullet > .bullet-marker { width: 2; height: auto; }
    Bullet > .bullet-text { width: 1fr; height: auto; }
    """

    def __init__(self, text, **kwargs):
        super().__init__(**kwargs)
        self.text = text

    def compose(self):
        yield Static('•', classes='bullet-marker', markup=False)
        yield Static(self.text, classes='bullet-text', markup=False)


class WrappingCollapsibleTitle(CollapsibleTitle):
    """Wrap the label independently from the disclosure marker."""
    DEFAULT_CSS = "WrappingCollapsibleTitle { width: 1fr; }"

    def _update_label(self):
        row = Table.grid(padding=0)
        row.add_column(width=2, no_wrap=True)
        row.add_column(ratio=1)
        row.add_row(self.collapsed_symbol if self.collapsed else self.expanded_symbol, Text(self.label.plain, overflow="fold"))
        self.update(row)


class WrappingCollapsible(TextualCollapsible):
    """A normal collapsible whose long summary keeps its hanging indent."""
    def __init__(self, *children, **kwargs):
        super().__init__(*children, **kwargs)
        title = self._title
        self._title = WrappingCollapsibleTitle(label=title.label, collapsed=self.collapsed,
            collapsed_symbol=title.collapsed_symbol, expanded_symbol=title.expanded_symbol)
