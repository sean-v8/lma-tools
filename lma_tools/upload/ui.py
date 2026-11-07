"""Edit metadata in memory without temporary files or backups."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Label, TextArea


def edit_document(text, validate):
    area = TextArea(
        text=text,
        multiline=True,
        scrollbar=True,
        wrap_lines=False,
        focus_on_click=True,
        line_numbers=True,
    )
    status = Label(
        "Ctrl-S: accept metadata   Ctrl-C: cancel   Arrows / PageUp / "
        "PageDown: navigate"
    )
    keys = KeyBindings()

    @keys.add("c-s")
    def accept(event):
        try:
            result = validate(area.text)
        except (ValueError, KeyError, TypeError) as exc:
            status.text = "Cannot accept: " + str(exc)
            return
        event.app.exit(result=result)

    @keys.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    app = Application(
        layout=Layout(HSplit([status, area]), focused_element=area),
        key_bindings=keys,
        full_screen=True,
        mouse_support=False,
    )
    return app.run()
