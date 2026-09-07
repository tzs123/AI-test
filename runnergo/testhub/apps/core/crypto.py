"""Shared helpers for encrypting stored integration credentials."""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


SECRET_PREFIX = 'fernet:v1:'


def _fernet():
    secret = str(getattr(settings, 'SECURITY_CREDENTIALS_KEY', '') or settings.SECRET_KEY)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode('utf-8')).digest())
    return Fernet(key)


def encrypt_json(value):
    payload = json.dumps(value or {}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return _fernet().encrypt(payload).decode('ascii')


def decrypt_json(value):
    if not value:
        return {}
    try:
        return json.loads(_fernet().decrypt(value.encode('ascii')).decode('utf-8'))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return {}


def encrypt_secret(value):
    if value in (None, ''):
        return ''
    text = str(value)
    if text.startswith(SECRET_PREFIX):
        return text
    encrypted = _fernet().encrypt(text.encode('utf-8')).decode('ascii')
    return f'{SECRET_PREFIX}{encrypted}'


def decrypt_secret(value):
    if value in (None, ''):
        return ''
    text = str(value)
    if not text.startswith(SECRET_PREFIX):
        return text
    try:
        payload = text[len(SECRET_PREFIX):]
        return _fernet().decrypt(payload.encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        return ''
