from pathlib import Path

import pytest
import structlog

from shoplegacy.config import Settings, handoff_settings, load_settings

DB = "postgresql://shoplegacy_app:shoplegacy_app@localhost:56322/postgres"
REQUIRED = {"SHOPLEGACY_DATABASE_URL": DB, "SHOPLEGACY_JWT_SECRET": "test-secret"}


def test_defaults() -> None:
    s = load_settings(REQUIRED)
    assert s == Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=Path("./data"),
        database_url=DB,
        jwt_secret="test-secret",
    )
    assert s.llm_base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert s.llm_model == "qwen3.7-flash"
    assert s.llm_api_key == ""
    assert s.llm_timeout_s == 20.0
    assert s.whisper_model == "small"
    assert s.stt_max_bytes == 26214400
    assert s.session_seconds == 1200
    assert s.sessions_planned == 5


def test_env_overrides() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_PROVIDER": "qwen",
            "SHOPLEGACY_STT_PROVIDER": "whisper",
            "SHOPLEGACY_DATA_DIR": "/tmp/shop",
            "SHOPLEGACY_LLM_BASE_URL": "http://llm.test/v1",
            "SHOPLEGACY_LLM_MODEL": "m1",
            "SHOPLEGACY_LLM_API_KEY": "k",
            "SHOPLEGACY_LLM_TIMEOUT": "3.5",
            "SHOPLEGACY_WHISPER_MODEL": "base",
            "SHOPLEGACY_STT_MAX_BYTES": "1000",
            "SHOPLEGACY_SESSION_SECONDS": "60",
            "SHOPLEGACY_SESSIONS_PLANNED": "3",
        }
    )
    assert s.provider == "qwen"
    assert s.stt_provider == "whisper"
    assert s.data_dir == Path("/tmp/shop")
    assert s.llm_base_url == "http://llm.test/v1"
    assert s.llm_model == "m1"
    assert s.llm_api_key == "k"
    assert s.llm_timeout_s == 3.5
    assert s.whisper_model == "base"
    assert s.stt_max_bytes == 1000
    assert s.session_seconds == 60
    assert s.sessions_planned == 3


def test_handoff_env_is_ignored() -> None:
    s = load_settings({**REQUIRED, "HANDOFF_PROVIDER": "qwen", "HANDOFF_DATA_DIR": "/x"})
    assert s.provider == "fake"
    assert s.data_dir == Path("./data")


def test_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_PROVIDER"):
        load_settings({**REQUIRED, "SHOPLEGACY_PROVIDER": "replay"})


def test_unsupported_stt_provider() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_STT_PROVIDER"):
        load_settings({**REQUIRED, "SHOPLEGACY_STT_PROVIDER": "cloud"})


def test_qwen_requires_api_key() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_LLM_API_KEY"):
        load_settings({**REQUIRED, "SHOPLEGACY_PROVIDER": "qwen", "SHOPLEGACY_LLM_API_KEY": "  "})


def test_handoff_settings_maps_llm_fields() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_PROVIDER": "qwen",
            "SHOPLEGACY_LLM_API_KEY": "k",
            "SHOPLEGACY_LLM_MODEL": "m1",
            "SHOPLEGACY_LLM_TIMEOUT": "5",
            "SHOPLEGACY_DATA_DIR": "/tmp/shop",
        }
    )
    h = handoff_settings(s)
    assert h.provider == "qwen"
    assert h.llm_api_key == "k"
    assert h.llm_model == "m1"
    assert h.llm_timeout_s == 5.0
    assert h.llm_base_url == s.llm_base_url
    assert h.collection == "unused"
    assert h.min_score == 0.4
    assert h.data_dir == Path("/tmp/shop")


def test_new_defaults() -> None:
    s = load_settings(REQUIRED)
    assert s.database_url == DB
    assert s.jwt_secret == "test-secret"
    assert s.jwks_url == ""
    assert s.jwt_audience == "authenticated"
    assert s.supabase_url == ""
    assert s.supabase_anon_key == ""
    assert s.min_supported_ratio == 0.8
    assert s.require_all_axes is False
    assert s.require_consent is True
    assert s.rate_limit == "60/minute"
    assert s.log_format == "console"
    assert s.sentry_dsn == ""


def test_new_overrides() -> None:
    s = load_settings(
        {
            "SHOPLEGACY_DATABASE_URL": DB,
            "SHOPLEGACY_JWKS_URL": "http://auth.test/jwks",
            "SHOPLEGACY_JWT_AUDIENCE": "aud",
            "SHOPLEGACY_SUPABASE_URL": "http://sb.test",
            "SHOPLEGACY_SUPABASE_ANON_KEY": "anon",
            "SHOPLEGACY_MIN_SUPPORTED_RATIO": "0.5",
            "SHOPLEGACY_REQUIRE_ALL_AXES": "yes",
            "SHOPLEGACY_REQUIRE_CONSENT": "false",
            "SHOPLEGACY_RATE_LIMIT": "5/second",
            "SHOPLEGACY_LOG_FORMAT": "json",
            "SHOPLEGACY_SENTRY_DSN": "http://sentry.test/1",
        }
    )
    assert s.jwt_secret == ""
    assert s.jwks_url == "http://auth.test/jwks"
    assert s.jwt_audience == "aud"
    assert s.supabase_url == "http://sb.test"
    assert s.supabase_anon_key == "anon"
    assert s.min_supported_ratio == 0.5
    assert s.require_all_axes is True
    assert s.require_consent is False
    assert s.rate_limit == "5/second"
    assert s.log_format == "json"
    assert s.sentry_dsn == "http://sentry.test/1"


def test_database_url_required() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_DATABASE_URL"):
        load_settings({"SHOPLEGACY_JWT_SECRET": "x"})
    with pytest.raises(ValueError, match="SHOPLEGACY_DATABASE_URL"):
        load_settings({"SHOPLEGACY_JWT_SECRET": "x", "SHOPLEGACY_DATABASE_URL": " "})


def test_jwt_secret_or_jwks_required() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_JWT_SECRET"):
        load_settings({"SHOPLEGACY_DATABASE_URL": DB})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("0", False),
        ("no", False),
        ("false", False),
        ("", False),
        ("on", False),
    ],
)
def test_boolean_parsing(raw: str, expected: bool) -> None:
    s = load_settings(
        {**REQUIRED, "SHOPLEGACY_REQUIRE_ALL_AXES": raw, "SHOPLEGACY_REQUIRE_CONSENT": raw}
    )
    assert s.require_all_axes is expected
    assert s.require_consent is expected


def test_secrets_not_in_repr() -> None:
    s = load_settings({**REQUIRED, "SHOPLEGACY_SENTRY_DSN": "http://sentry.test/1"})
    text = repr(s)
    assert DB not in text
    assert "test-secret" not in text
    assert "sentry.test" not in text


def test_hardening_defaults() -> None:
    s = load_settings(REQUIRED)
    assert s.pii_keys == ()
    assert s.retention_days == 1095
    assert s.ner == "none"
    assert s.ner_model == "ko_core_news_md"
    assert s.otel_endpoint == ""
    assert s.otel_service_name == "shoplegacy"
    assert s.trusted_proxies == ""
    assert s.csp_report_only is False
    assert s.sms_provider == "log"
    assert s.sms_api_key == ""
    assert s.sms_api_secret == ""
    assert s.sms_from == ""
    assert s.owner_link_ttl_hours == 72
    assert s.require_owner_approval is False
    assert s.public_base_url == "http://localhost:8830"
    assert s.report_max_rows == 10000


def test_hardening_overrides() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_RETENTION_DAYS": "30",
            "SHOPLEGACY_NER": "presidio",
            "SHOPLEGACY_NER_MODEL": "ko_core_news_sm",
            "SHOPLEGACY_OTEL_ENDPOINT": "http://otel.test:4318",
            "SHOPLEGACY_OTEL_SERVICE_NAME": "svc",
            "SHOPLEGACY_TRUSTED_PROXIES": "*",
            "SHOPLEGACY_CSP_REPORT_ONLY": "true",
            "SHOPLEGACY_OWNER_LINK_TTL_HOURS": "1",
            "SHOPLEGACY_REQUIRE_OWNER_APPROVAL": "yes",
            "SHOPLEGACY_REPORT_MAX_ROWS": "5",
        }
    )
    assert s.retention_days == 30
    assert s.ner == "presidio"
    assert s.ner_model == "ko_core_news_sm"
    assert s.otel_endpoint == "http://otel.test:4318"
    assert s.otel_service_name == "svc"
    assert s.trusted_proxies == "*"
    assert s.csp_report_only is True
    assert s.owner_link_ttl_hours == 1
    assert s.require_owner_approval is True
    assert s.report_max_rows == 5


def test_pii_keys_parsing() -> None:
    s = load_settings({**REQUIRED, "SHOPLEGACY_PII_KEYS": " k1 , k2,, "})
    assert s.pii_keys == ("k1", "k2")
    assert load_settings({**REQUIRED, "SHOPLEGACY_PII_KEYS": " "}).pii_keys == ()


def test_unsupported_ner() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_NER"):
        load_settings({**REQUIRED, "SHOPLEGACY_NER": "spacy"})


def test_unsupported_sms_provider() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_SMS_PROVIDER"):
        load_settings({**REQUIRED, "SHOPLEGACY_SMS_PROVIDER": "carrier"})


@pytest.mark.parametrize(
    "missing", ["SHOPLEGACY_SMS_API_KEY", "SHOPLEGACY_SMS_API_SECRET", "SHOPLEGACY_SMS_FROM"]
)
def test_solapi_requires_credentials(missing: str) -> None:
    env = {
        **REQUIRED,
        "SHOPLEGACY_SMS_PROVIDER": "solapi",
        "SHOPLEGACY_SMS_API_KEY": "key",
        "SHOPLEGACY_SMS_API_SECRET": "secret",
        "SHOPLEGACY_SMS_FROM": "0100000000",
    }
    env[missing] = " "
    with pytest.raises(ValueError, match=missing):
        load_settings(env)


def test_solapi_with_credentials() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_SMS_PROVIDER": "solapi",
            "SHOPLEGACY_SMS_API_KEY": "key",
            "SHOPLEGACY_SMS_API_SECRET": "secret",
            "SHOPLEGACY_SMS_FROM": "0100000000",
        }
    )
    assert s.sms_provider == "solapi"
    assert s.sms_api_key == "key"
    assert s.sms_api_secret == "secret"
    assert s.sms_from == "0100000000"


def test_owner_link_ttl_minimum() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_OWNER_LINK_TTL_HOURS"):
        load_settings({**REQUIRED, "SHOPLEGACY_OWNER_LINK_TTL_HOURS": "0"})


def test_retention_days_minimum() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_RETENTION_DAYS"):
        load_settings({**REQUIRED, "SHOPLEGACY_RETENTION_DAYS": "29"})


def test_report_max_rows_minimum() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_REPORT_MAX_ROWS"):
        load_settings({**REQUIRED, "SHOPLEGACY_REPORT_MAX_ROWS": "0"})


def test_public_base_url_strips_trailing_slash() -> None:
    s = load_settings({**REQUIRED, "SHOPLEGACY_PUBLIC_BASE_URL": "https://shop.test/app//"})
    assert s.public_base_url == "https://shop.test/app"


def test_hardening_secrets_not_in_repr() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_PII_KEYS": "pii-key-value",
            "SHOPLEGACY_SMS_PROVIDER": "solapi",
            "SHOPLEGACY_SMS_API_KEY": "sms-key-value",
            "SHOPLEGACY_SMS_API_SECRET": "sms-secret-value",
            "SHOPLEGACY_SMS_FROM": "0100000000",
        }
    )
    text = repr(s)
    for secret in ("pii-key-value", "sms-key-value", "sms-secret-value", "0100000000"):
        assert secret not in text


def test_remaining_defaults() -> None:
    s = load_settings(REQUIRED)
    assert s.audio_store == ""
    assert s.audio_s3_endpoint == ""
    assert s.stt_worker == "thread"
    assert s.stt_stream_max_seconds == 600
    assert s.stt_stream_max_concurrent == 4
    assert s.billing_provider == "fake"
    assert s.toss_client_key == ""
    assert s.toss_secret_key == ""
    assert s.toss_api_base == "https://api.tosspayments.com"


def test_remaining_overrides() -> None:
    s = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_AUDIO_STORE": "s3://bucket/audio",
            "SHOPLEGACY_AUDIO_S3_ENDPOINT": "http://s3.test",
            "SHOPLEGACY_STT_WORKER": "external",
            "SHOPLEGACY_STT_STREAM_MAX_SECONDS": "10",
            "SHOPLEGACY_STT_STREAM_MAX_CONCURRENT": "1",
            "SHOPLEGACY_BILLING_PROVIDER": "toss",
            "SHOPLEGACY_TOSS_CLIENT_KEY": "toss-client-value",
            "SHOPLEGACY_TOSS_SECRET_KEY": "toss-secret-value",
            "SHOPLEGACY_TOSS_API_BASE": "http://toss.test/",
        }
    )
    assert s.audio_store == "s3://bucket/audio"
    assert s.audio_s3_endpoint == "http://s3.test"
    assert s.stt_worker == "external"
    assert s.stt_stream_max_seconds == 10
    assert s.stt_stream_max_concurrent == 1
    assert s.billing_provider == "toss"
    assert s.toss_client_key == "toss-client-value"
    assert s.toss_secret_key == "toss-secret-value"
    assert s.toss_api_base == "http://toss.test"
    assert "toss-client-value" not in repr(s)
    assert "toss-secret-value" not in repr(s)
    assert load_settings({**REQUIRED, "SHOPLEGACY_AUDIO_STORE": "file:///x"}).audio_store == (
        "file:///x"
    )


def test_audio_store_scheme() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_AUDIO_STORE must start with"):
        load_settings({**REQUIRED, "SHOPLEGACY_AUDIO_STORE": "/var/audio"})


def test_unsupported_stt_worker() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_STT_WORKER"):
        load_settings({**REQUIRED, "SHOPLEGACY_STT_WORKER": "celery"})


def test_stream_limits_minimum() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_STT_STREAM_MAX_SECONDS"):
        load_settings({**REQUIRED, "SHOPLEGACY_STT_STREAM_MAX_SECONDS": "9"})
    with pytest.raises(ValueError, match="SHOPLEGACY_STT_STREAM_MAX_CONCURRENT"):
        load_settings({**REQUIRED, "SHOPLEGACY_STT_STREAM_MAX_CONCURRENT": "0"})


def test_unsupported_billing_provider() -> None:
    with pytest.raises(ValueError, match="SHOPLEGACY_BILLING_PROVIDER"):
        load_settings({**REQUIRED, "SHOPLEGACY_BILLING_PROVIDER": "paypal"})


@pytest.mark.parametrize("missing", ["SHOPLEGACY_TOSS_CLIENT_KEY", "SHOPLEGACY_TOSS_SECRET_KEY"])
def test_toss_requires_keys(missing: str) -> None:
    env = {
        **REQUIRED,
        "SHOPLEGACY_BILLING_PROVIDER": "toss",
        "SHOPLEGACY_TOSS_CLIENT_KEY": "ck",
        "SHOPLEGACY_TOSS_SECRET_KEY": "sk",
    }
    env[missing] = " "
    with pytest.raises(ValueError, match=missing):
        load_settings(env)


def test_feedback_secret_explicit_and_jwt_fallback() -> None:
    with structlog.testing.capture_logs() as logs:
        explicit = load_settings(
            {**REQUIRED, "SHOPLEGACY_FEEDBACK_SECRET": "feedback-secret-value"}
        )
        fallback = load_settings(REQUIRED)
    assert explicit.feedback_secret == "feedback-secret-value"
    assert fallback.feedback_secret == "test-secret"
    assert not any(e["event"] == "feedback_secret_generated" for e in logs)
    assert "feedback-secret-value" not in repr(explicit)


def test_feedback_secret_generated_without_jwt_secret() -> None:
    env = {"SHOPLEGACY_DATABASE_URL": DB, "SHOPLEGACY_JWKS_URL": "http://auth.test/jwks"}
    with structlog.testing.capture_logs() as logs:
        first = load_settings(env)
        second = load_settings(env)
    assert len(first.feedback_secret) == 64
    assert first.feedback_secret != second.feedback_secret
    events = [e for e in logs if e["event"] == "feedback_secret_generated"]
    assert len(events) == 2
    assert all(e["log_level"] == "warning" for e in events)
    assert first.feedback_secret not in repr(first)

