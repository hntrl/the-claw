import unittest

from server import _parse_client_input


class ServerInputTests(unittest.TestCase):
    def test_raw_text_message_is_accepted(self) -> None:
        self.assertEqual(
            _parse_client_input('{"type":"raw_text","text":" move left "}'), "move left"
        )

    def test_utterance_alias_is_accepted(self) -> None:
        self.assertEqual(
            _parse_client_input('{"type":"utterance","text":"home"}'), "home"
        )

    def test_plain_text_is_accepted(self) -> None:
        self.assertEqual(_parse_client_input("home"), "home")

    def test_invalid_payloads_are_rejected(self) -> None:
        self.assertIsNone(_parse_client_input('{"type":"audio","text":"home"}'))
        self.assertIsNone(_parse_client_input('{"type":"raw_text","text":"  "}'))
