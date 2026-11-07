import unittest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lma_tools.upload.ui import edit_document


class EditorTests(unittest.TestCase):
    def test_ctrl_s_returns_in_memory_buffer(self):
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            pipe.send_text("\x13")
            self.assertEqual(
                edit_document('source = "Microphones"', lambda text: text),
                'source = "Microphones"',
            )

    def test_ctrl_c_cancels(self):
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            pipe.send_text("\x03")
            with self.assertRaises(KeyboardInterrupt):
                edit_document("unchanged", lambda text: text)


if __name__ == "__main__":
    unittest.main()
