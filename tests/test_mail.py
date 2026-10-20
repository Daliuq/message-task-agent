import unittest
from taskagent.mail import (_decode_modified_utf7, _encode_modified_utf7, _list_folders,
                            _parse_list_line, _quote_imap_string)


class MailProtocolTests(unittest.TestCase):
    def test_folder_listing_uses_imaplib_quoted_default_reference_name(self):
        class FakeClient:
            args = None
            def list(self, *args):
                self.args = args
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        client = FakeClient()
        self.assertEqual(_list_folders(client), ["INBOX"])
        self.assertEqual(client.args, ())

    def test_list_folder_names_and_noselect(self):
        self.assertEqual(_parse_list_line(b'(\\HasNoChildren) "/" "INBOX"'), (False, "INBOX"))
        self.assertEqual(_parse_list_line(b'(\\HasNoChildren) "/" "Sent Messages"'), (False, "Sent Messages"))
        self.assertEqual(_parse_list_line(b'(\\Noselect) "/" "Archive"'), (True, "Archive"))

    def test_modified_utf7_roundtrip(self):
        for text in ("收件箱", "Archive/学生", "A&B"):
            self.assertEqual(_decode_modified_utf7(_encode_modified_utf7(text)), text)

    def test_mailbox_names_are_quoted_as_imap_strings(self):
        self.assertEqual(_quote_imap_string("Sent Messages"), '"Sent Messages"')


if __name__ == "__main__":
    unittest.main()

