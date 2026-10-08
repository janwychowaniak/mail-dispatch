"""Configuration, from environment variables only (SPEC §7).

A contradictory or unreadable configuration stops startup with a readable message [D28]; only
`SMTP_HOST` is required. The rules for reading a value are stricter than pydantic's own, so
every field parses its raw string itself: an empty variable is an unset one, booleans are
`true`/`false`/`1`/`0`, and numbers must be greater than zero.
"""

from __future__ import annotations

import math
import re
import socket
import ssl
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .addresses import is_address_domain, is_ehlo_domain

TlsMode = Literal["starttls", "starttls-opportunistic", "implicit", "none"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_INTEGER = re.compile(r"[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+")
_BOOLEANS = {"true": True, "1": True, "false": False, "0": False}


class ConfigError(Exception):
    """The configuration cannot be used; the message names every variable at fault."""


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("must be a whole number greater than zero")
    if isinstance(value, str):
        if not _INTEGER.fullmatch(value):
            raise ValueError("must be a whole number greater than zero")
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("must be a whole number greater than zero")
    return value


def _positive_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("must be a number greater than zero")
    if isinstance(value, str):
        if not _DECIMAL.fullmatch(value):
            raise ValueError("must be a number greater than zero")
        value = float(value)
    if not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError("must be a number greater than zero")
    return float(value)


def _port(value: object) -> int:
    try:
        port = _positive_int(value)
    except ValueError:
        raise ValueError("must be a port number from 1 to 65535") from None
    if port > 65535:
        raise ValueError("must be a port number from 1 to 65535")
    return port


def _utf8(value: str, what: str) -> str:
    # The environment is decoded with surrogateescape, so bytes that are not UTF-8 arrive as
    # lone surrogates.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{what} is not valid UTF-8") from None
    return value


class Settings(BaseSettings):
    """Every variable of SPEC §7.1, with its default [D41]."""

    model_config = SettingsConfigDict(env_ignore_empty=True, validate_default=True)

    smtp_host: str
    smtp_port: int = 587
    smtp_tls: TlsMode = "starttls"
    smtp_tls_verify: bool = True
    smtp_ca_file: str | None = None
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_password_file: str | None = None
    smtp_ehlo_name: str = Field(default_factory=lambda: socket.gethostname())
    smtp_timeout_seconds: float = 60.0
    message_id_domain: str | None = None
    max_request_bytes: int = 33_554_432
    max_message_bytes: int = 26_214_400
    max_recipients: int = 100
    health_cache_ttl_seconds: float = 10.0
    health_probe_timeout_seconds: float = 5.0
    port: int = 8000
    log_level: LogLevel = "INFO"

    _password: SecretStr | None = PrivateAttr(default=None)
    _ca_pem: str | None = PrivateAttr(default=None)

    @field_validator("smtp_host", mode="before")
    @classmethod
    def _check_host(cls, value: object) -> object:
        if isinstance(value, str):
            if value == "":
                raise ValueError("is required")
            if not value.isascii():
                raise ValueError("must be ASCII: a name or an IP address")
            if value.startswith("[") or value.endswith("]"):
                raise ValueError("an IPv6 address is written without brackets")
            if any(c.isspace() or not c.isprintable() for c in value):
                raise ValueError("must be a name or an IP address, without white space")
        return value

    @field_validator("smtp_port", "port", mode="before")
    @classmethod
    def _check_port(cls, value: object) -> int:
        return _port(value)

    @field_validator("max_request_bytes", "max_message_bytes", "max_recipients", mode="before")
    @classmethod
    def _check_positive_int(cls, value: object) -> int:
        return _positive_int(value)

    @field_validator(
        "smtp_timeout_seconds",
        "health_cache_ttl_seconds",
        "health_probe_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _check_positive_number(cls, value: object) -> float:
        return _positive_number(value)

    @field_validator("smtp_tls_verify", mode="before")
    @classmethod
    def _check_bool(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in _BOOLEANS:
            return _BOOLEANS[value.lower()]
        raise ValueError("must be true, false, 1 or 0")

    @field_validator("log_level", mode="before")
    @classmethod
    def _check_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("smtp_username", mode="before")
    @classmethod
    def _check_username(cls, value: object) -> object:
        return _utf8(value, "the user name") if isinstance(value, str) else value

    @field_validator("smtp_password", mode="before")
    @classmethod
    def _check_password(cls, value: object) -> object:
        return _utf8(value, "the password") if isinstance(value, str) else value

    @field_validator("smtp_ehlo_name")
    @classmethod
    def _check_ehlo_name(cls, value: str) -> str:
        if not is_ehlo_domain(value):
            raise ValueError(
                "must be a domain (labels of letters, digits and hyphens) or an address literal"
            )
        return value

    @field_validator("message_id_domain")
    @classmethod
    def _check_message_id_domain(cls, value: str | None) -> str | None:
        if value is not None and not is_address_domain(value):
            raise ValueError("must be a domain of at least two labels, or an address literal")
        return value

    @model_validator(mode="after")
    def _check_credentials_and_tls(self) -> Settings:
        self._password = self._resolve_password()
        if self._password is not None and self.smtp_tls == "none":
            raise ValueError(
                "SMTP_TLS=none cannot be combined with credentials: they travel only under TLS"
            )
        if self.smtp_ca_file is not None and self.smtp_tls != "none" and self.smtp_tls_verify:
            self._ca_pem = _read_ca_file(self.smtp_ca_file)
        return self

    def _resolve_password(self) -> SecretStr | None:
        if self.smtp_password is not None and self.smtp_password_file is not None:
            raise ValueError("SMTP_PASSWORD and SMTP_PASSWORD_FILE cannot both be set")
        password = self.smtp_password
        if self.smtp_password_file is not None:
            password = SecretStr(_read_password_file(self.smtp_password_file))
        if password is not None and self.smtp_username is None:
            raise ValueError("a password is set without SMTP_USERNAME")
        if self.smtp_username is not None:
            if password is None:
                raise ValueError("SMTP_USERNAME is set without a password")
            if password.get_secret_value() == "":
                raise ValueError("the password is empty")
        return password

    @property
    def password(self) -> SecretStr | None:
        """The password, from `SMTP_PASSWORD` or from `SMTP_PASSWORD_FILE`."""
        return self._password

    @property
    def credentials_configured(self) -> bool:
        return self._password is not None

    @property
    def ca_pem(self) -> str | None:
        """The extra certificate authority, when it applies (§7.3)."""
        return self._ca_pem


def _read_password_file(path: str) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"SMTP_PASSWORD_FILE cannot be read: {error.strerror}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("SMTP_PASSWORD_FILE is not valid UTF-8") from None
    # Exactly one trailing line ending is removed (§7.2).
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _read_ca_file(path: str) -> str:
    try:
        pem = Path(path).read_text(encoding="ascii")
    except OSError as error:
        raise ValueError(f"SMTP_CA_FILE cannot be read: {error.strerror}") from None
    except UnicodeDecodeError:
        raise ValueError("SMTP_CA_FILE is not a PEM file") from None
    try:
        ssl.create_default_context().load_verify_locations(cadata=pem)
    except (ssl.SSLError, ValueError):
        raise ValueError("SMTP_CA_FILE holds no certificate that can be loaded") from None
    return pem


def describe(error: ValidationError) -> str:
    """A readable account of what is wrong, naming variables and never repeating a value.

    Pydantic's own rendering quotes the input, which for `SMTP_PASSWORD` would print the
    password into the log.
    """
    lines = []
    for item in error.errors(include_input=False, include_url=False):
        message = item["msg"].removeprefix("Value error, ")
        if item["type"] == "missing":
            message = "is required"
        variable = ".".join(str(part) for part in item["loc"]).upper()
        lines.append(f"{variable}: {message}" if variable else message)
    return "; ".join(lines)


def load_settings(**overrides: Any) -> Settings:
    """Read the environment; a configuration that cannot be used raises ConfigError."""
    try:
        return Settings(**overrides)
    except ValidationError as error:
        raise ConfigError(describe(error)) from None
