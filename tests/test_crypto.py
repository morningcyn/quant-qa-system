from backend.utils import crypto


class TestCrypto:
    def test_roundtrip(self):
        secret = "sk-test-123456-中文密钥"
        encrypted = crypto.encrypt_secret(secret)
        assert encrypted.startswith("dpapi:")
        assert secret not in encrypted
        assert crypto.decrypt_secret(encrypted) == secret

    def test_empty(self):
        assert crypto.encrypt_secret("") == ""
        assert crypto.decrypt_secret("") == ""

    def test_fallback_roundtrip_when_dpapi_unavailable(self, monkeypatch):
        monkeypatch.setattr(crypto, "crypt32", None)
        secret = "sk-fallback-中文密钥"
        encrypted = crypto.encrypt_secret(secret)
        assert encrypted.startswith("dpapi:")
        assert crypto.decrypt_secret(encrypted) == secret

    def test_corrupted_returns_none(self):
        assert crypto.decrypt_secret("dpapi:!!not-base64!!") is None
        assert crypto.decrypt_secret("plaintext") is None
