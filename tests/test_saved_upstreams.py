import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as panel


class SavedUpstreamsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.patchers = [
            patch.object(panel, 'CONFIG_DIR', root),
            patch.object(panel, 'CONFIG_FILE', root / 'upstreams.enc'),
            patch.object(panel, 'CONFIG_KEY_FILE', root / '.key'),
            patch.dict('os.environ', {'CONFIG_ENCRYPTION_KEY': '', 'PANEL_ACCESS_TOKEN': ''}),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self.directory.cleanup)
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)
        self.client = panel.app.test_client()
        self.config = dict(base_url='https://unit.invalid', model='test-model',
                           api_key='not-a-real-key-unit-test', protocol='responses',
                           max_tokens=8192, temperature=0.4)

    def save(self, **changes):
        response = self.client.post('/api/upstreams', json={**self.config, **changes})
        self.assertEqual(response.status_code, 200)
        return response.json['upstream']

    def test_save_without_dns_and_read_after_reload(self):
        with patch.object(panel.socket, 'getaddrinfo', side_effect=AssertionError('No DNS while saving')):
            saved = self.save()
        self.assertNotIn(self.config['api_key'].encode(), panel.CONFIG_FILE.read_bytes())
        self.assertEqual(panel.load_upstreams()[saved['id']]['api_key'], self.config['api_key'])
        listed = self.client.get('/api/info').json['upstreams']
        self.assertEqual(listed[0]['max_tokens'], 8192)
        self.assertNotIn('api_key', listed[0])
        self.assertNotIn(self.config['api_key'], str(listed))

    def test_update_model_without_resubmitting_key(self):
        saved = self.save()
        self.save(id=saved['id'], api_key='', model='another-model')
        stored = panel.load_upstreams()[saved['id']]
        self.assertEqual(stored['api_key'], self.config['api_key'])
        self.assertEqual(stored['model'], 'another-model')

    def test_failed_probe_preserves_saved_credentials(self):
        saved = self.save()
        token = self.client.post('/api/challenges', json={}).json['challenges'][0]['token']
        with patch.object(panel, 'validate_url', side_effect=panel.InputError('DNS blocked')):
            response = self.client.post('/api/probe', json={'upstream_id': saved['id'], 'token': token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(panel.load_upstreams()[saved['id']]['api_key'], self.config['api_key'])

    def test_changed_destination_requires_new_key(self):
        saved = self.save()
        response = self.client.post('/api/upstreams', json={**self.config, 'id': saved['id'],
                                   'base_url': 'https://other.invalid', 'api_key': ''})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(panel.load_upstreams()[saved['id']]['base_url'], self.config['base_url'])

    def test_same_config_does_not_duplicate(self):
        self.assertEqual(self.save()['id'], self.save()['id'])

    def test_effort_persists_and_old_config_defaults(self):
        saved = self.save()
        self.assertEqual(saved['reasoning_effort'], '')
        updated = self.save(id=saved['id'], api_key='', reasoning_effort='xhigh')
        self.assertEqual(updated['reasoning_effort'], 'xhigh')
        self.assertEqual(self.client.get('/api/info').json['upstreams'][0]['reasoning_effort'], 'xhigh')
        self.assertEqual(panel.load_upstreams()[saved['id']]['api_key'], self.config['api_key'])

    def test_invalid_effort_does_not_overwrite_config(self):
        saved = self.save(reasoning_effort='high')
        for effort in ('ultra', {}, None):
            response = self.client.post('/api/upstreams', json={**self.config, 'id': saved['id'], 'reasoning_effort': effort})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(panel.load_upstreams()[saved['id']]['reasoning_effort'], 'high')

    def test_probe_resolves_saved_key_on_server(self):
        saved = self.save(reasoning_effort='high')
        token = self.client.post('/api/challenges', json={}).json['challenges'][0]['token']
        answer = dict(text=' '.join(['123'] * 332), complete=True, finish_reason='completed', usage={})
        with patch.object(panel, 'validate_url', return_value=('https://unit.invalid', '8.8.8.8')), \
                patch.object(panel, 'complete', return_value=answer) as call:
            response = self.client.post('/api/probe', json={'upstream_id': saved['id'], 'token': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_args.args[2], self.config['api_key'])
        self.assertEqual(call.call_args.kwargs['reasoning_effort'], 'high')
        self.assertEqual(response.json['requested_reasoning_effort'], 'high')
        self.assertNotIn(self.config['api_key'], response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
