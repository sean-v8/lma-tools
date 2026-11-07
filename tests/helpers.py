"""Shared terminal streams."""

import io


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True
