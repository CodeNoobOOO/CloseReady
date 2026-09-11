"""Customer-visible message controls; structured IDs remain server-side metadata."""
import unittest

from closeready.content_guard import validate_customer_visible_draft
from closeready.models import MessageDraft
from closeready.store import DomainError


class ContentGuardTests(unittest.TestCase):
    def draft(self, subject='July statement', body='Please upload the July statement.'):
        return MessageDraft(subject=subject, body=body, requirement_ids=['req_metadata_only'])

    def assert_unsafe(self, draft):
        with self.assertRaises(DomainError) as raised:
            validate_customer_visible_draft(draft)
        self.assertEqual(raised.exception.code, 'UNSAFE_DRAFT')
        self.assertEqual(raised.exception.status, 422)

    def test_rejects_internal_identifiers_case_insensitively(self):
        for value in ('case_123', 'REQ_abc', 'run_123', 'review_abc', 'event_123', 'proposal_abc'):
            with self.subTest(value=value):
                self.assert_unsafe(self.draft(body='Internal reference ' + value))

    def test_allows_structured_requirement_ids_when_visible_text_is_safe(self):
        validate_customer_visible_draft(self.draft(
            subject='Documents: July statement',
            body='Upload securely at https://portal.example.test or email mailto:team@example.test.'))

    def test_rejects_disallowed_url_and_executable_schemes(self):
        for value in ('http://example.test', 'ftp://example.test/file', 'javascript:alert(1)',
                      'data:text/plain,secret', 'file:///tmp/data', 'vbscript:msgbox(1)'):
            with self.subTest(value=value):
                self.assert_unsafe(self.draft(body='Open ' + value))

    def test_rejects_control_characters_but_allows_message_whitespace(self):
        self.assert_unsafe(self.draft(body='Bad\x00text'))
        validate_customer_visible_draft(self.draft(body='Line one\nLine two\tvalue\r\n'))

    def test_rejects_subject_and_body_over_limits(self):
        self.assert_unsafe(self.draft(subject='x' * 201))
        self.assert_unsafe(self.draft(body='x' * 10_001))
        validate_customer_visible_draft(self.draft(subject='x' * 200, body='x' * 10_000))


if __name__ == '__main__':
    unittest.main()
