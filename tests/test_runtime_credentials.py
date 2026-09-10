import base64
import json
import time

from runtime_credentials import access_token, expired


def jwt(exp):
    data = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'header.{data}.signature'


def test_rotation_only_after_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    fresh = jwt(time.time() + 3600)
    (tmp_path / 'auth.json').write_text(json.dumps({'tokens': {'access_token': fresh}}))
    assert access_token(jwt(0)) == fresh
    assert access_token('opaque-configured-token') == 'opaque-configured-token'


def test_missing_or_invalid_rotated_credentials_do_not_hide_error(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    old = jwt(0)
    assert expired(old)
    assert access_token(old) == old
    (tmp_path / 'auth.json').write_text('not json')
    assert access_token(old) == old
