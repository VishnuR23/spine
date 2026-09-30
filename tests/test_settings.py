import pytest
from pydantic import ValidationError

from spine.config.settings import Settings

STRONG_ADMIN = "admin-key-that-is-long-and-random-0123456789"
STRONG_JWT = "jwt-secret-that-is-long-and-random-0123456789"


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch):
    # CI and the Makefile export these; the tests need the unset defaults.
    for name in ("ENVIRONMENT", "ADMIN_API_KEY", "JWT_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs) -> Settings:
    # _env_file=None keeps a developer's local .env out of the test.
    return Settings(_env_file=None, **kwargs)


def test_local_environment_accepts_defaults():
    s = _settings(environment="local")
    assert s.admin_api_key == "change-me"


def test_production_accepts_strong_distinct_secrets():
    s = _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET=STRONG_JWT)
    assert s.is_production


def test_production_rejects_default_admin_key():
    with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
        _settings(environment="production", JWT_SECRET=STRONG_JWT)


def test_production_rejects_missing_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET must be set"):
        _settings(environment="production", admin_api_key=STRONG_ADMIN)


def test_production_rejects_placeholder_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET must be set"):
        _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET="change-me-long-random-string")


def test_production_rejects_jwt_secret_equal_to_admin_key():
    with pytest.raises(ValidationError, match="should differ"):
        _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET=STRONG_ADMIN)


def test_production_rejects_short_jwt_secret():
    # RFC 7518 §3.2: an HS256 key must be at least 256 bits.
    with pytest.raises(ValidationError, match="at least 32"):
        _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET="x" * 31)


def test_production_accepts_32_char_jwt_secret():
    _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET="x" * 32)


def test_production_rejects_deploy_example_jwt_placeholder():
    # The value shipped in deploy/vps/env.production.example.
    with pytest.raises(ValidationError, match="JWT_SECRET must be set"):
        _settings(environment="production", admin_api_key=STRONG_ADMIN, JWT_SECRET="REQUIRED_long_random_jwt_secret")
