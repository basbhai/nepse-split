import os
from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise ValueError(
            "CREDENTIAL_ENCRYPTION_KEY not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except Exception:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEY is invalid — must be a 32-byte URL-safe base64 Fernet key")


def encrypt_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("plaintext password cannot be empty")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    if not ciphertext:
        raise ValueError("ciphertext cannot be empty")
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("decryption failed — ciphertext is corrupt or key has changed")
