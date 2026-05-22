import signal
import unittest

from app.tool_call import extract_tool_call


class ToolCallTests(unittest.TestCase):
    def _call_with_timeout(self, fn, seconds=2):
        if not hasattr(signal, "SIGALRM"):
            return fn()

        class _Timeout(Exception):
            pass

        def _handler(signum, frame):
            raise _Timeout("timed out")

        old_handler = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            return fn()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    def test_non_tool_json_returns_promptly(self):
        result = self._call_with_timeout(
            lambda: extract_tool_call('{"foo":1}', ["bar"])
        )
        self.assertEqual(result, (None, '{"foo":1}'))

    def test_valid_tool_call_still_parses(self):
        result = self._call_with_timeout(
            lambda: extract_tool_call('{"name":"x","arguments":{}}', ["x"])
        )
        self.assertIsNotNone(result[0])
        self.assertEqual(result[0][0]["function"]["name"], "x")


if __name__ == "__main__":
    unittest.main()
