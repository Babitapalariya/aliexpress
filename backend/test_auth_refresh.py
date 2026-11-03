import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

# Load only the handler so these tests do not need a configured database.
source = ast.parse((Path(__file__).parent / 'app/auth.py').read_text(encoding='utf-8'))
handler = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'refresh_token')
handler.decorator_list = []
handler.args.defaults = []
handler.args.args[0].annotation = None

class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.token = SimpleNamespace(refresh_token='old-refresh', access_token='old-access')
        self.db = Mock()
        self.db.query.return_value.order_by.return_value.first.return_value = self.token
        self.client = Mock()
        self.request = Mock()
        self.save = Mock(return_value=SimpleNamespace(id=1, expires_in=3600))
        self.scope = dict(IopClient=Mock(return_value=self.client), IopRequest=Mock(return_value=self.request),
            settings=SimpleNamespace(ALIEXPRESS_API_URL='gateway', ALIEXPRESS_APP_KEY='key', ALIEXPRESS_APP_SECRET='secret'),
            AliExpressToken=Mock(), HTTPException=HTTPError, save_token=self.save)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<handler>', 'exec'), self.scope)

    def test_signed_refresh_and_persist_rotated_token(self):
        data = dict(access_token='new-access', refresh_token='new-refresh', expires_in=3600)
        self.client.execute.return_value.body = data
        result = self.scope['refresh_token'](self.db)
        self.scope['IopRequest'].assert_called_once_with('/auth/token/refresh', 'POST')
        self.request.add_api_param.assert_any_call('app_key', 'key')
        self.request.add_api_param.assert_any_call('refresh_token', 'old-refresh')
        self.save.assert_called_once_with(self.db, data)
        self.assertEqual(result['token_id'], 1)
        self.assertNotIn('access_token', result)

    def test_rejection_preserves_tokens_and_redacts_error(self):
        self.client.execute.return_value.body = {'code':'InvalidToken', 'message':'Expired old-refresh'}
        with self.assertRaises(HTTPError) as error:
            self.scope['refresh_token'](self.db)
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn('Reconnect AliExpress', error.exception.detail)
        self.assertNotIn('old-refresh', error.exception.detail)
        self.save.assert_not_called()

    def test_transport_failure_preserves_tokens(self):
        self.client.execute.side_effect = RuntimeError('request failed')
        with self.assertRaises(HTTPError) as error:
            self.scope['refresh_token'](self.db)
        self.assertEqual(error.exception.status_code, 502)
        self.save.assert_not_called()

    def test_missing_refresh_token(self):
        self.token.refresh_token = None
        with self.assertRaises(HTTPError) as error:
            self.scope['refresh_token'](self.db)
        self.assertEqual(error.exception.status_code, 404)
        self.client.execute.assert_not_called()

if __name__ == '__main__':
    unittest.main()
