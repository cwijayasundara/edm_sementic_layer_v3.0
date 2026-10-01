"""Runtime configuration, read from PRISM_* environment variables and the repo-root .env.

`env=production` (PRISM_ENV) refuses every dev-default secret below; dev and test accept them.
"""
from datetime import date
from pathlib import Path
from typing import Literal

from psycopg.conninfo import make_conninfo
from pydantic import AliasChoices, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGICAL_DBS = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
APP_DB = "app"
MODELS_DIR = Path(__file__).resolve().parents[1] / ".models"  # <repo>/backend/.models, independent of cwd
# Roles the source databases depend on: never usable as the app role (migrate_app would rewrite them).
RESERVED_ROLES = frozenset({"postgres", "prism_svc", "bi_reader", "prism_view_owner"})
# Secrets whose shipped default must never reach production. Every secret is a SecretStr (repr shows `**********`);
# read it with .get_secret_value() at the point of use only.
DEV_SECRET_FIELDS = ("ctx_hmac_key", "jwt_secret", "audit_hmac_key", "pg_app_password", "pg_svc_password",
                     "pg_admin_password", "neo4j_password")
MIN_KEY_CHARS = 32  # signing / HMAC keys (the dev defaults are longer, so dev and test accept them)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRISM_", env_file=("../.env", ".env"), extra="ignore")

    pg_host: str = "localhost"
    pg_port: int = 5434
    pg_admin_user: str = "postgres"
    pg_admin_password: SecretStr = SecretStr("postgres")
    pg_svc_user: str = "prism_svc"
    pg_svc_password: SecretStr = SecretStr("prism_svc_dev")
    pg_app_user: str = "prism_app"  # gateway's role: reaches only the app DB (audit, query log), never source data
    pg_app_password: SecretStr = SecretStr("prism_app_dev")
    store_questions: bool = True  # keep raw question text in app.query_log (demo personas all allow it)
    db_prefix: str = ""
    ctx_hmac_key: SecretStr = Field(SecretStr("dev-only-ctx-key-change-me-0123456789abcdef"), min_length=MIN_KEY_CHARS)
    jwt_secret: SecretStr = Field(SecretStr("dev-only-jwt-secret-change-me-0123456789abcdef"),
                                  min_length=MIN_KEY_CHARS)
    # keys question_hash (HMAC-SHA256): separate from ctx_hmac_key, which is copied into every source database
    audit_hmac_key: SecretStr = Field(SecretStr("dev-only-audit-key-change-me-0123456789abcdef"),
                                      min_length=MIN_KEY_CHARS)
    env: Literal["dev", "test", "production"] = "dev"
    as_of: date = date(2026, 9, 30)
    seed: int = 42
    statement_timeout_ms: int = 5000
    refmaster_api_url: str = "http://127.0.0.1:8101"
    marketmaster_api_url: str = "http://127.0.0.1:8102"
    mcp_query_max_rows: int = 500
    mcp_allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]

    neo4j_uri: str = "bolt://127.0.0.1:7688"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("prism-dev-neo4j")
    neo4j_timeout_s: float = 5.0
    agent_port: int = 8000
    agent_supervisor_model: str = "claude-sonnet-5-5"
    agent_escalation_model: str = "claude-opus-5-5"
    agent_subagent_model: str = "claude-haiku-4-5-20251001"
    agent_max_turns: int = Field(8, ge=1)
    agent_max_tool_calls: int = Field(24, ge=1)
    agent_wall_clock_s: float = Field(90.0, gt=0)
    anthropic_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "PRISM_ANTHROPIC_API_KEY"))
    agent_dev_token_enabled: bool = False   # POST /dev/token mints persona tokens; local demo only
    gateway_port: int = 8200
    gateway_url: str = "http://127.0.0.1:8200"
    # Result store: 128 MB in all, 16 MB per caller, so 8 callers can hold a full quota at once; past that a new
    # result is refused (result_store_full) until results expire (15 min TTL), never by evicting another caller's.
    gateway_store_mb: int = Field(128, ge=1)
    gateway_store_per_sub_mb: int = Field(16, ge=1)
    gateway_catalog_refresh_s: float = Field(30.0, gt=0)
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_cache_dir: str = str(MODELS_DIR)
    graph_ns: str = "prism"
    # Query-history distillation: a question (and each of its plans) becomes a few-shot example only once this many
    # DISTINCT callers recorded it verified, so one caller cannot plant examples for everyone. A single-user demo
    # needs PRISM_HISTORY_MIN_CALLERS=1 (or two callers).
    history_min_callers: int = Field(2, ge=1)

    @model_validator(mode="after")
    def _check_roles_and_secrets(self) -> "Settings":
        reserved = RESERVED_ROLES | {self.pg_admin_user, self.pg_svc_user}
        if self.pg_app_user in reserved:
            raise ValueError(f"pg_app_user must not be a reserved or source role ({sorted(reserved)})")
        if self.env == "production":
            fields = type(self).model_fields
            for name in DEV_SECRET_FIELDS:
                if _plain(getattr(self, name)) == _plain(fields[name].default):
                    raise ValueError(f"{name} is the dev default; set PRISM_{name.upper()} in production")
        return self

    def dbname(self, logical: str) -> str:
        return logical if logical == "postgres" else f"{self.db_prefix}{logical}"

    def dsn(self, logical: str, admin: bool = False) -> str:
        user, password = (
            (self.pg_admin_user, self.pg_admin_password) if admin else (self.pg_svc_user, self.pg_svc_password)
        )
        return make_conninfo(host=self.pg_host, port=self.pg_port, dbname=self.dbname(logical),
                             user=user, password=password.get_secret_value())

    def neo4j_auth(self) -> tuple[str, str]:
        """(user, password) for the Neo4j drivers."""
        return self.neo4j_user, self.neo4j_password.get_secret_value()

    def app_dsn(self, logical: str = APP_DB, **extra: object) -> str:
        """Connection string for the app role (the gateway's audit / query-log writer); values are escaped."""
        return make_conninfo(host=self.pg_host, port=self.pg_port, dbname=self.dbname(logical),
                             user=self.pg_app_user, password=self.pg_app_password.get_secret_value(), **extra)


class ConfigError(Exception):
    """Settings refused its configuration; the message names fields and rules only, never a value."""


def config_error_message(exc: ValidationError) -> str:
    """Field names and rule messages only: pydantic's own text quotes the input (for a model-level refusal, the whole
    environment, secrets included)."""
    parts = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(p) for p in e.get("loc", ()) if isinstance(p, (str, int)))[:64]
        msg = str(e.get("msg", "invalid value"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "invalid configuration: " + "; ".join(parts)


def load_settings(**overrides: object) -> Settings:
    """Settings() for CLIs and factories: a refusal is a ConfigError with a messages-only text (no traceback, no
    input values)."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(config_error_message(exc)) from None


def redact_uri(uri: str) -> str:
    """A URI safe to print: any user:password@ part is dropped (bolt://neo4j:pw@host:7687 -> bolt://host:7687)."""
    scheme, sep, rest = str(uri).partition("://")
    if not sep:
        return uri.rpartition("@")[2]
    authority, slash, path = rest.partition("/")
    return f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"


def _plain(value: object) -> object:
    return value.get_secret_value() if isinstance(value, SecretStr) else value
