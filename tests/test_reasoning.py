import json
import unittest
from unittest.mock import MagicMock, patch

from transport import build_request, complete


class ReasoningTest(unittest.TestCase):
    def test_wire_format_for_each_protocol(self):
        for protocol in ('responses', 'openai'):
            for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'none', 'minimal'):
                with self.subTest(protocol=protocol, effort=effort):
                    connection = MagicMock()
                    response = connection.getresponse.return_value
                    response.status = 200
                    payload = {'status': 'completed', 'output': []} if protocol == 'responses' else {
                        'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]}
                    response.read.return_value = json.dumps(payload).encode()
                    with patch('transport.http.client.HTTPSConnection', return_value=connection):
                        complete('https://fixture.invalid', '8.8.8.8', 'dummy-test-key', 'test-model',
                                 protocol, 'test', 4096, None, 'max_completion_tokens', effort)
                    body = json.loads(connection.request.call_args.kwargs['body'])
                    if protocol == 'responses':
                        self.assertEqual(body['reasoning'], {'effort': effort})
                        self.assertNotIn('reasoning_effort', body)
                    else:
                        self.assertEqual(body['reasoning_effort'], effort)
                        self.assertNotIn('reasoning', body)

    def test_default_omits_effort_for_all_protocols(self):
        for protocol in ('responses', 'openai', 'anthropic'):
            body, _ = build_request('test-model', 'dummy-test-key', protocol, 'test', 4096, None, 'max_completion_tokens')
            self.assertNotIn('reasoning', body)
            self.assertNotIn('reasoning_effort', body)

    def test_unsupported_effort_cannot_be_silently_ignored(self):
        for effort, protocol in [('high', 'anthropic'), ('ultra', 'responses')]:
            with self.assertRaises(ValueError):
                build_request('test-model', 'dummy-test-key', protocol, 'test', 4096, None, 'max_completion_tokens', effort)


if __name__ == '__main__':
    unittest.main()
