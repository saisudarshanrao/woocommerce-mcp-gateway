"""Passwords, tokens and the encryption of stored upstream credentials.

Three kinds of secret pass through the gateway, and each is stored differently:

  * a user's password is chosen by a person, so it may be guessable. It is
    stored as an Argon2id hash, which is deliberately slow to compute.
  * API keys, session tokens and invite tokens are 256 random bits made here.
    There is nothing to guess, so a plain SHA-256 is enough, and being fast it
    can be recomputed and looked up by index on every request.
  * an upstream server's credential has to be sent to that server again, so a
    hash will not do. It is encrypted, with a key that never enters the database.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken

API_KEY_PREFIX = "mgw_"
# How much of a key is kept in clear so its owner can recognise it: "mgw_" plus 8 characters.
DISPLAY_PREFIX_LENGTH = 12

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


def new_token() -> str:
    """256 bits from the operating system's random source, URL-safe."""
    return secrets.token_urlsafe(32)


def new_api_key() -> str:
    return API_KEY_PREFIX + new_token()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def display_prefix(api_key: str) -> str:
    return api_key[:DISPLAY_PREFIX_LENGTH]


class CredentialCipher:
    """Encrypts upstream credentials at rest.

    Fernet is AES in CBC mode with an HMAC over the result, so a stored value
    cannot be read or altered without the key. The key is derived from secret
    material held in a Kubernetes Secret, not in the database: a database dump
    alone reveals no upstream credential.
    """

    def __init__(self, key_material: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(ciphertext)).decode()
        except InvalidToken as error:
            raise ValueError("stored credential cannot be decrypted with the current encryption key") from error
