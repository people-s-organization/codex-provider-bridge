"""Read rotated Codex credentials without interactive login or POST replay."""
import base64
import json
import os
import time
from pathlib import Path


def expired(token):
    try:
        part = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return float(payload['exp']) <= time.time() + 60
    except (ValueError, KeyError, IndexError, TypeError):
        # Opaque credentials cannot safely be declared expired locally.
        return not token


def access_token(configured):
    if not expired(configured):
        return configured
    try:
        auth_file = Path(os.getenv('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
        candidate = json.loads(auth_file.read_text()).get('tokens', {}).get('access_token')
        if isinstance(candidate, str) and not expired(candidate):
            return candidate
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return configured
