"""Configuration and startup validation (SPEC §7, [D28], case 17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mail_dispatch.settings import ConfigError, load_settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("SMTP_HOST", "mail.example.org")
    return monkeypatch


def test_only_the_host_is_required(env: pytest.MonkeyPatch) -> None:
    settings = load_settings(smtp_ehlo_name="client.example.org")
    assert settings.smtp_host == "mail.example.org"
    assert settings.smtp_port == 587
    assert settings.smtp_tls == "starttls"
    assert settings.smtp_tls_verify is True
    assert settings.smtp_timeout_seconds == 60
    assert settings.message_id_domain is None
    assert settings.max_request_bytes == 33_554_432
    assert settings.max_message_bytes == 26_214_400
    assert settings.max_recipients == 100
    assert settings.health_cache_ttl_seconds == 10
    assert settings.health_probe_timeout_seconds == 5
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.credentials_configured is False
    assert settings.password is None
    assert settings.ca_pem is None


def test_missing_host_stops_startup() -> None:
    with pytest.raises(ConfigError, match="SMTP_HOST: is required"):
        load_settings()


def test_an_empty_variable_is_an_unset_one(env: pytest.MonkeyPatch) -> None:
    env.setenv("SMTP_PORT", "")
    env.setenv("SMTP_TLS", "")
    assert load_settings().smtp_port == 587
    env.setenv("SMTP_HOST", "")
    with pytest.raises(ConfigError, match="SMTP_HOST: is required"):
        load_settings()


def test_the_default_ehlo_name_is_the_hostname(
    env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "123456789012")
    assert load_settings().smtp_ehlo_name == "123456789012"


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("SMTP_TLS_VERIFY", "FALSE", False),
        ("SMTP_TLS_VERIFY", "0", False),
        ("SMTP_TLS_VERIFY", "True", True),
        ("SMTP_TLS_VERIFY", "1", True),
        ("SMTP_TIMEOUT_SECONDS", "0.25", 0.25),
        ("SMTP_TIMEOUT_SECONDS", ".5", 0.5),
        ("HEALTH_CACHE_TTL_SECONDS", "3", 3.0),
        ("HEALTH_PROBE_TIMEOUT_SECONDS", "1.5", 1.5),
        ("SMTP_PORT", "465", 465),
        ("PORT", "65535", 65535),
        ("MAX_RECIPIENTS", "7", 7),
        ("LOG_LEVEL", "debug", "DEBUG"),
        ("SMTP_TLS", "starttls-opportunistic", "starttls-opportunistic"),
        ("SMTP_TLS", "implicit", "implicit"),
        ("SMTP_EHLO_NAME", "[192.0.2.1]", "[192.0.2.1]"),
        ("MESSAGE_ID_DOMAIN", "[IPv6:::1]", "[IPv6:::1]"),
        ("SMTP_HOST", "2001:db8::1", "2001:db8::1"),
    ],
)
def test_values_are_read(
    env: pytest.MonkeyPatch, variable: str, value: str, expected: object
) -> None:
    env.setenv(variable, value)
    assert getattr(load_settings(), variable.lower()) == expected


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SMTP_TLS_VERIFY", "yes"),
        ("SMTP_TLS_VERIFY", "on"),
        ("SMTP_TIMEOUT_SECONDS", "0"),
        ("SMTP_TIMEOUT_SECONDS", "-1"),
        ("SMTP_TIMEOUT_SECONDS", "inf"),
        ("SMTP_TIMEOUT_SECONDS", "nan"),
        ("SMTP_TIMEOUT_SECONDS", "1e3"),
        ("SMTP_TIMEOUT_SECONDS", "ten"),
        ("MAX_REQUEST_BYTES", "1.5"),
        ("MAX_REQUEST_BYTES", "0"),
        ("MAX_RECIPIENTS", "1_000"),
        ("SMTP_PORT", "0"),
        ("SMTP_PORT", "65536"),
        ("PORT", "http"),
        ("SMTP_TLS", "STARTTLS"),
        ("SMTP_TLS", "tls"),
        ("LOG_LEVEL", "verbose"),
        ("SMTP_HOST", "mäil.example.org"),
        ("SMTP_HOST", "[2001:db8::1]"),
        ("SMTP_HOST", "mail example"),
        ("SMTP_EHLO_NAME", "client_host"),
        ("SMTP_EHLO_NAME", "client.example.org."),
        ("SMTP_EHLO_NAME", "client example"),
        ("MESSAGE_ID_DOMAIN", "localhost"),
        ("MESSAGE_ID_DOMAIN", "example.123"),
    ],
)
def test_unreadable_values_stop_startup(env: pytest.MonkeyPatch, variable: str, value: str) -> None:
    env.setenv(variable, value)
    with pytest.raises(ConfigError, match=f"^{variable}: "):
        load_settings()


def test_the_message_never_repeats_a_value(env: pytest.MonkeyPatch) -> None:
    env.setenv("SMTP_TIMEOUT_SECONDS", "s3cr3t-looking-value")
    env.setenv("SMTP_TLS", "s3cr3t-looking-tls")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "s3cr3t" not in str(caught.value)
    assert "SMTP_TIMEOUT_SECONDS" in str(caught.value)
    assert "SMTP_TLS" in str(caught.value)


def test_credentials_from_the_environment(env: pytest.MonkeyPatch) -> None:
    env.setenv("SMTP_USERNAME", "user")
    env.setenv("SMTP_PASSWORD", "pässwörd")
    settings = load_settings()
    assert settings.credentials_configured is True
    assert settings.password is not None
    assert settings.password.get_secret_value() == "pässwörd"
    assert "pässwörd" not in repr(settings)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"secret\n", "secret"),
        (b"secret\r\n", "secret"),
        (b"secret\n\n", "secret\n"),
        (b"secret", "secret"),
        (b" secret \n", " secret "),
        ("pässwörd\n".encode(), "pässwörd"),
    ],
)
def test_password_file_loses_exactly_one_line_ending(
    env: pytest.MonkeyPatch, tmp_path: Path, content: bytes, expected: str
) -> None:
    path = tmp_path / "password"
    path.write_bytes(content)
    env.setenv("SMTP_USERNAME", "user")
    env.setenv("SMTP_PASSWORD_FILE", str(path))
    password = load_settings().password
    assert password is not None
    assert password.get_secret_value() == expected


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_PASSWORD_FILE": "/x"}, "both be set"),
        ({"SMTP_USERNAME": "u"}, "without a password"),
        ({"SMTP_PASSWORD": "p"}, "without SMTP_USERNAME"),
        ({"SMTP_USERNAME": "u", "SMTP_PASSWORD_FILE": "/nonexistent"}, "cannot be read"),
        ({"SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_TLS": "none"}, "SMTP_TLS=none"),
        ({"SMTP_TLS": "none", "SMTP_USERNAME": "u", "SMTP_PASSWORD_FILE": "{empty}"}, "empty"),
        ({"SMTP_USERNAME": "u", "SMTP_PASSWORD_FILE": "{latin1}"}, "not valid UTF-8"),
        ({"SMTP_CA_FILE": "/nonexistent"}, "SMTP_CA_FILE cannot be read"),
        ({"SMTP_CA_FILE": "{empty}"}, "no certificate"),
    ],
)
def test_contradictory_configuration_stops_startup(
    env: pytest.MonkeyPatch, tmp_path: Path, variables: dict[str, str], message: str
) -> None:
    (tmp_path / "empty").write_bytes(b"\n")
    (tmp_path / "latin1").write_bytes("pässwörd".encode("latin-1"))
    for name, value in variables.items():
        env.setenv(name, value.format(empty=tmp_path / "empty", latin1=tmp_path / "latin1"))
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_none_with_credentials_does_not_start(env: pytest.MonkeyPatch) -> None:
    """Case 17: `SMTP_TLS=none` with credentials → the service does not start [D26]."""
    env.setenv("SMTP_TLS", "none")
    env.setenv("SMTP_USERNAME", "user")
    env.setenv("SMTP_PASSWORD", "password")
    with pytest.raises(ConfigError, match="travel only under TLS"):
        load_settings()


@pytest.mark.parametrize(("tls", "verify"), [("none", "true"), ("starttls", "false")])
def test_ca_file_is_ignored_and_not_read_without_verification(
    env: pytest.MonkeyPatch, tls: str, verify: str
) -> None:
    env.setenv("SMTP_TLS", tls)
    env.setenv("SMTP_TLS_VERIFY", verify)
    env.setenv("SMTP_CA_FILE", "/nonexistent")
    assert load_settings().ca_pem is None
