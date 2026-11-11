"""Environment-based settings (``SHOPLEGACY_*`` only)."""

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import handoff.config
import structlog

SUPPORTED_PROVIDERS = frozenset({"fake", "qwen"})
SUPPORTED_STT_PROVIDERS = frozenset({"fake", "whisper"})
SUPPORTED_NER = frozenset({"none", "presidio"})
SUPPORTED_SMS_PROVIDERS = frozenset({"log", "solapi"})
SUPPORTED_STT_WORKERS = frozenset({"thread", "external", "off"})
SUPPORTED_BILLING_PROVIDERS = frozenset({"fake", "toss"})
DEFAULT_TOSS_API_BASE = "https://api.tosspayments.com"
DEFAULT_LLM_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class Settings:
    provider: str
    stt_provider: str
    data_dir: Path
    database_url: str = field(repr=False)
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = "qwen3.7-flash"
    llm_api_key: str = field(default="", repr=False)
    llm_timeout_s: float = 20.0
    whisper_model: str = "small"
    stt_max_bytes: int = 26214400
    session_seconds: int = 1200
    sessions_planned: int = 5
    jwt_secret: str = field(default="", repr=False)
    jwks_url: str = ""
    jwt_audience: str = "authenticated"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    min_supported_ratio: float = 0.8
    require_all_axes: bool = False
    require_consent: bool = True
    rate_limit: str = "60/minute"
    log_format: str = "console"
    sentry_dsn: str = field(default="", repr=False)
    pii_keys: tuple[str, ...] = field(default=(), repr=False)
    retention_days: int = 1095
    ner: str = "none"
    ner_model: str = "ko_core_news_md"
    otel_endpoint: str = ""
    otel_service_name: str = "shoplegacy"
    trusted_proxies: str = ""
    csp_report_only: bool = False
    sms_provider: str = "log"
    sms_api_key: str = field(default="", repr=False)
    sms_api_secret: str = field(default="", repr=False)
    sms_from: str = field(default="", repr=False)
    owner_link_ttl_hours: int = 72
    require_owner_approval: bool = False
    public_base_url: str = "http://localhost:8830"
    report_max_rows: int = 10000
    # Resolved by load_settings (may be generated), so it is left out of equality.
    feedback_secret: str = field(default="", repr=False, compare=False)
    audio_store: str = ""
    audio_s3_endpoint: str = ""
    stt_worker: str = "thread"
    stt_stream_max_seconds: int = 600
    stt_stream_max_concurrent: int = 4
    billing_provider: str = "fake"
    toss_client_key: str = field(default="", repr=False)
    toss_secret_key: str = field(default="", repr=False)
    toss_api_base: str = DEFAULT_TOSS_API_BASE


def _float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _int(source: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = source.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bool(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def _csv(source: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = source.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _feedback_secret(source: Mapping[str, str], jwt_secret: str) -> str:
    explicit = source.get("SHOPLEGACY_FEEDBACK_SECRET", "")
    if explicit.strip():
        return explicit
    if jwt_secret.strip():
        return jwt_secret
    structlog.get_logger("shoplegacy").warning("feedback_secret_generated")
    return secrets.token_hex(32)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load settings from ``env`` (defaults to ``os.environ``)."""
    source: Mapping[str, str] = os.environ if env is None else env
    provider = source.get("SHOPLEGACY_PROVIDER", "fake")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported SHOPLEGACY_PROVIDER: {provider}")
    stt_provider = source.get("SHOPLEGACY_STT_PROVIDER", "fake")
    if stt_provider not in SUPPORTED_STT_PROVIDERS:
        raise ValueError(f"unsupported SHOPLEGACY_STT_PROVIDER: {stt_provider}")
    api_key = source.get("SHOPLEGACY_LLM_API_KEY", "")
    if provider == "qwen" and not api_key.strip():
        raise ValueError("SHOPLEGACY_LLM_API_KEY is required for SHOPLEGACY_PROVIDER=qwen")
    database_url = source.get("SHOPLEGACY_DATABASE_URL", "")
    if not database_url.strip():
        raise ValueError("SHOPLEGACY_DATABASE_URL is required")
    jwt_secret = source.get("SHOPLEGACY_JWT_SECRET", "")
    jwks_url = source.get("SHOPLEGACY_JWKS_URL", "")
    if not jwt_secret.strip() and not jwks_url.strip():
        raise ValueError("SHOPLEGACY_JWT_SECRET or SHOPLEGACY_JWKS_URL is required")
    ner = source.get("SHOPLEGACY_NER", "none")
    if ner not in SUPPORTED_NER:
        raise ValueError(f"unsupported SHOPLEGACY_NER: {ner}")
    sms_provider = source.get("SHOPLEGACY_SMS_PROVIDER", "log")
    if sms_provider not in SUPPORTED_SMS_PROVIDERS:
        raise ValueError(f"unsupported SHOPLEGACY_SMS_PROVIDER: {sms_provider}")
    sms_api_key = source.get("SHOPLEGACY_SMS_API_KEY", "")
    sms_api_secret = source.get("SHOPLEGACY_SMS_API_SECRET", "")
    sms_from = source.get("SHOPLEGACY_SMS_FROM", "")
    if sms_provider == "solapi":
        for name, value in (
            ("SHOPLEGACY_SMS_API_KEY", sms_api_key),
            ("SHOPLEGACY_SMS_API_SECRET", sms_api_secret),
            ("SHOPLEGACY_SMS_FROM", sms_from),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required for SHOPLEGACY_SMS_PROVIDER=solapi")
    audio_store = source.get("SHOPLEGACY_AUDIO_STORE", "")
    if audio_store and not audio_store.startswith(("file://", "s3://")):
        raise ValueError("SHOPLEGACY_AUDIO_STORE must start with file:// or s3://")
    stt_worker = source.get("SHOPLEGACY_STT_WORKER", "thread")
    if stt_worker not in SUPPORTED_STT_WORKERS:
        raise ValueError(f"unsupported SHOPLEGACY_STT_WORKER: {stt_worker}")
    billing_provider = source.get("SHOPLEGACY_BILLING_PROVIDER", "fake")
    if billing_provider not in SUPPORTED_BILLING_PROVIDERS:
        raise ValueError(f"unsupported SHOPLEGACY_BILLING_PROVIDER: {billing_provider}")
    toss_client_key = source.get("SHOPLEGACY_TOSS_CLIENT_KEY", "")
    toss_secret_key = source.get("SHOPLEGACY_TOSS_SECRET_KEY", "")
    if billing_provider == "toss":
        for name, value in (
            ("SHOPLEGACY_TOSS_CLIENT_KEY", toss_client_key),
            ("SHOPLEGACY_TOSS_SECRET_KEY", toss_secret_key),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required for SHOPLEGACY_BILLING_PROVIDER=toss")
    return Settings(
        provider=provider,
        stt_provider=stt_provider,
        data_dir=Path(source.get("SHOPLEGACY_DATA_DIR", "./data")),
        database_url=database_url,
        llm_base_url=source.get("SHOPLEGACY_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        llm_model=source.get("SHOPLEGACY_LLM_MODEL", "qwen3.7-flash"),
        llm_api_key=api_key,
        llm_timeout_s=_float(source, "SHOPLEGACY_LLM_TIMEOUT", 20.0),
        whisper_model=source.get("SHOPLEGACY_WHISPER_MODEL", "small"),
        stt_max_bytes=_int(source, "SHOPLEGACY_STT_MAX_BYTES", 26214400, 1),
        session_seconds=_int(source, "SHOPLEGACY_SESSION_SECONDS", 1200, 1),
        sessions_planned=_int(source, "SHOPLEGACY_SESSIONS_PLANNED", 5, 1),
        jwt_secret=jwt_secret,
        jwks_url=jwks_url,
        jwt_audience=source.get("SHOPLEGACY_JWT_AUDIENCE", "authenticated"),
        supabase_url=source.get("SHOPLEGACY_SUPABASE_URL", ""),
        supabase_anon_key=source.get("SHOPLEGACY_SUPABASE_ANON_KEY", ""),
        min_supported_ratio=_float(source, "SHOPLEGACY_MIN_SUPPORTED_RATIO", 0.8),
        require_all_axes=_bool(source, "SHOPLEGACY_REQUIRE_ALL_AXES", False),
        require_consent=_bool(source, "SHOPLEGACY_REQUIRE_CONSENT", True),
        rate_limit=source.get("SHOPLEGACY_RATE_LIMIT", "60/minute"),
        log_format=source.get("SHOPLEGACY_LOG_FORMAT", "console"),
        sentry_dsn=source.get("SHOPLEGACY_SENTRY_DSN", ""),
        pii_keys=_csv(source, "SHOPLEGACY_PII_KEYS"),
        retention_days=_int(source, "SHOPLEGACY_RETENTION_DAYS", 1095, 30),
        ner=ner,
        ner_model=source.get("SHOPLEGACY_NER_MODEL", "ko_core_news_md"),
        otel_endpoint=source.get("SHOPLEGACY_OTEL_ENDPOINT", ""),
        otel_service_name=source.get("SHOPLEGACY_OTEL_SERVICE_NAME", "shoplegacy"),
        trusted_proxies=source.get("SHOPLEGACY_TRUSTED_PROXIES", ""),
        csp_report_only=_bool(source, "SHOPLEGACY_CSP_REPORT_ONLY", False),
        sms_provider=sms_provider,
        sms_api_key=sms_api_key,
        sms_api_secret=sms_api_secret,
        sms_from=sms_from,
        owner_link_ttl_hours=_int(source, "SHOPLEGACY_OWNER_LINK_TTL_HOURS", 72, 1),
        require_owner_approval=_bool(source, "SHOPLEGACY_REQUIRE_OWNER_APPROVAL", False),
        public_base_url=source.get("SHOPLEGACY_PUBLIC_BASE_URL", "http://localhost:8830").rstrip(
            "/"
        ),
        report_max_rows=_int(source, "SHOPLEGACY_REPORT_MAX_ROWS", 10000, 1),
        feedback_secret=_feedback_secret(source, jwt_secret),
        audio_store=audio_store,
        audio_s3_endpoint=source.get("SHOPLEGACY_AUDIO_S3_ENDPOINT", ""),
        stt_worker=stt_worker,
        stt_stream_max_seconds=_int(source, "SHOPLEGACY_STT_STREAM_MAX_SECONDS", 600, 10),
        stt_stream_max_concurrent=_int(source, "SHOPLEGACY_STT_STREAM_MAX_CONCURRENT", 4, 1),
        billing_provider=billing_provider,
        toss_client_key=toss_client_key,
        toss_secret_key=toss_secret_key,
        toss_api_base=source.get("SHOPLEGACY_TOSS_API_BASE", DEFAULT_TOSS_API_BASE).rstrip("/"),
    )


def handoff_settings(settings: Settings) -> handoff.config.Settings:
    """Build the handoff settings needed to reuse its LLM providers."""
    return handoff.config.Settings(
        provider=settings.provider,
        data_dir=settings.data_dir,
        min_score=0.4,
        collection="unused",
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_api_key=settings.llm_api_key,
        llm_timeout_s=settings.llm_timeout_s,
    )
