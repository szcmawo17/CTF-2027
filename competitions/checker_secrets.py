"""Fernet outbox; the key is external to both SQLite and the container."""
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables


@sensitive_variables()
def _cipher():
    try:
        path = Path(settings.CHECKER_KEY_FILE)
        if not path.is_absolute() or path.resolve().is_relative_to(Path(settings.BASE_DIR).resolve()):
            raise ValueError
        return Fernet(path.read_bytes().strip())
    except (OSError, ValueError, TypeError):
        raise ValidationError("Настройте отдельный ключ checker вне репозитория.") from None


@sensitive_variables()
def seal(value, purpose):
    return _cipher().encrypt(json.dumps({"purpose": purpose, "value": value}).encode()).decode()


@sensitive_variables()
def unseal(ciphertext, purpose):
    try:
        payload = json.loads(_cipher().decrypt(ciphertext.encode()))
        if payload["purpose"] != purpose:
            raise ValueError
        return payload["value"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise ValidationError("Секрет checker недоступен или повреждён.") from None
