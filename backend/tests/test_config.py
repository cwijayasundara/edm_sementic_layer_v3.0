from datetime import date

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from prism.config import APP_DB, LOGICAL_DBS, Settings, redact_uri


def test_dbname_applies_prefix_except_for_maintenance_db():
    s = Settings(db_prefix="test_")
    assert s.dbname("cashrecon") == "test_cashrecon"
    assert s.dbname("postgres") == "postgres"


def test_dsn_uses_service_role_unless_admin():
    s = Settings()
    assert "user=prism_svc" in s.dsn("feedhub")
    assert "user=postgres" in s.dsn("feedhub", admin=True)
    assert f"port={s.pg_port}" in s.dsn("feedhub")


def test_defaults_are_deterministic():
    s = Settings()
    assert s.as_of == date(2026, 9, 30)
    assert s.seed == 42
    assert LOGICAL_DBS == ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")


def test_mcp_settings_defaults():
    s = Settings()
    assert s.refmaster_api_url == "http://127.0.0.1:8101"
    assert s.marketmaster_api_url == "http://127.0.0.1:8102"
    assert s.mcp_query_max_rows == 500
    assert "127.0.0.1:*" in s.mcp_allowed_hosts and "localhost:*" in s.mcp_allowed_hosts


def test_app_password_is_secret_and_not_in_repr():
    s = Settings(pg_app_password="ZZAPPSECRET pw")
    assert "ZZAPPSECRET" not in repr(s) and "ZZAPPSECRET" not in str(s)
    assert s.pg_app_password.get_secret_value() == "ZZAPPSECRET pw"


@pytest.mark.parametrize("password", ["has space", "a=b", "it's", 'dq"x', "back\\slash", "mix 'a'=\\ \"b\""])
def test_app_dsn_quotes_awkward_passwords(password):
    s = Settings(pg_app_password=password)
    parsed = conninfo_to_dict(s.app_dsn())
    assert parsed["password"] == password
    assert parsed["user"] == s.pg_app_user and parsed["dbname"] == s.dbname(APP_DB)
    assert conninfo_to_dict(s.dsn("feedhub"))["user"] == s.pg_svc_user


def test_audit_hmac_key_has_a_dev_default_of_at_least_32_chars():
    assert len(Settings().audit_hmac_key.get_secret_value()) >= 32
    with pytest.raises(ValueError, match="audit_hmac_key"):
        Settings(audit_hmac_key="short")


SECRET_FIELDS = ("audit_hmac_key", "ctx_hmac_key", "jwt_secret", "pg_app_password", "pg_svc_password",
                 "pg_admin_password", "neo4j_password")
STRONG = {"audit_hmac_key": "a" * 64, "ctx_hmac_key": "c" * 64, "jwt_secret": "j" * 64, "pg_app_password": "p" * 32,
          "pg_svc_password": "s" * 32, "pg_admin_password": "d" * 32, "neo4j_password": "n" * 32}


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_production_mode_refuses_dev_default_secrets(field):
    strong = dict(STRONG)
    Settings(env="production", **strong)  # all real secrets: accepted
    with pytest.raises(ValueError, match=field):
        Settings(env="production", **{**strong, field: Settings.model_fields[field].default})
    Settings(env="dev", **{**strong, field: Settings.model_fields[field].default})  # dev: defaults are fine


def test_env_is_restricted():
    with pytest.raises(ValueError):
        Settings(env="prod-ish")


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_every_secret_is_a_secretstr_and_never_in_repr(field):
    marker = f"ZZ{field.upper()}-SECRET-0123456789abcdef0123456789"
    s = Settings(**{field: marker})
    assert isinstance(getattr(s, field), SecretStr)
    assert getattr(s, field).get_secret_value() == marker
    for text in (repr(s), str(s), repr(s.model_dump())):
        assert "ZZ" + field.upper() not in text


def test_repr_of_settings_shows_no_secret_values():
    text = repr(Settings(**STRONG)) + str(Settings(**STRONG))
    for value in STRONG.values():
        assert value not in text


@pytest.mark.parametrize("field", ["ctx_hmac_key", "jwt_secret"])
def test_signing_keys_need_32_characters(field):
    with pytest.raises(ValueError, match=field):
        Settings(**{field: "k" * 31})
    Settings(**{field: "k" * 32})
    assert len(Settings.model_fields[field].default.get_secret_value()) >= 32   # dev defaults qualify in dev/test


def test_dsns_still_carry_the_real_passwords():
    s = Settings(pg_svc_password="svc pw'x", pg_admin_password="adm pw")
    assert conninfo_to_dict(s.dsn("feedhub"))["password"] == "svc pw'x"
    assert conninfo_to_dict(s.dsn("feedhub", admin=True))["password"] == "adm pw"


@pytest.mark.parametrize("uri,safe", [
    ("bolt://neo4j:pw@127.0.0.1:7688", "bolt://127.0.0.1:7688"),
    ("neo4j+s://u:p@w@host:7687/db", "neo4j+s://host:7687/db"),
    ("bolt://127.0.0.1:7688", "bolt://127.0.0.1:7688"),
    ("user:pw@host", "host"),
])
def test_redact_uri_drops_credentials(uri, safe):
    assert redact_uri(uri) == safe
