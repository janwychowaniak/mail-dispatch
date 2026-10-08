"""The address grammar of SPEC §4.2: an RFC 5321 `addr-spec` within ASCII.

The same domain grammar serves the configuration: `MESSAGE_ID_DOMAIN` takes it whole, and
`SMTP_EHLO_NAME` takes it as an RFC 5321 `Domain` — one label is enough and the last label may
be all digits [D40].
"""

from __future__ import annotations

import ipaddress
import re

MAX_LOCAL_PART = 64
MAX_DOMAIN = 255
MAX_ADDR_SPEC = 254
MAX_LABEL = 63

# RFC 5322 atext, as RFC 5321 uses it in a dot-atom.
_ATEXT = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]"
_DOT_ATOM = re.compile(rf"{_ATEXT}+(?:\.{_ATEXT}+)*")
# RFC 5321 quoted-string: qtextSMTP (32-33, 35-91, 93-126) or a quoted pair (92, 32-126).
_QUOTED_STRING = re.compile(r'"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])+"')
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_IPV6_TAG = "ipv6:"


def is_domain(value: str, *, min_labels: int = 2, numeric_last_label: bool = False) -> bool:
    """Dot-separated labels of letters, digits and hyphens, without a trailing dot.

    An address domain needs two labels and a last label that is not all digits, so that it
    cannot be mistaken for an IPv4 address; an EHLO `Domain` relaxes both [D40].
    """
    if not value.isascii() or len(value) > MAX_DOMAIN:
        return False
    labels = value.split(".")
    if len(labels) < min_labels:
        return False
    if not all(_LABEL.fullmatch(label) for label in labels):
        return False
    return numeric_last_label or not labels[-1].isdigit()


def is_address_literal(value: str) -> bool:
    """`[IPv4]` without leading zeros, or `[IPv6:...]` with the tag in any letter case.

    A zone identifier and a General-address-literal are refused.
    """
    if not value.isascii() or len(value) < 3 or value[0] != "[" or value[-1] != "]":
        return False
    inner = value[1:-1]
    if inner[: len(_IPV6_TAG)].lower() == _IPV6_TAG:
        address = inner[len(_IPV6_TAG) :]
        # ipaddress accepts a zone identifier (`%eth0`); RFC 5321 has none.
        if "%" in address:
            return False
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            return False
        return True
    try:
        # Leading zeros are refused by ipaddress itself.
        ipaddress.IPv4Address(inner)
    except ValueError:
        return False
    return True


def is_address_domain(value: str) -> bool:
    """The domain of an address, and of `MESSAGE_ID_DOMAIN`."""
    return is_address_literal(value) if value.startswith("[") else is_domain(value)


def is_ehlo_domain(value: str) -> bool:
    """An RFC 5321 `Domain` or an address literal, for `SMTP_EHLO_NAME` [D40]."""
    if value.startswith("["):
        return is_address_literal(value)
    return is_domain(value, min_labels=1, numeric_last_label=True)


def split_addr_spec(value: str) -> tuple[str, str] | None:
    """The local part and the domain of a valid `addr-spec`, or None.

    The local part is a dot-atom or a non-empty quoted string, at most 64 characters as
    written; the whole address is at most 254 characters.
    """
    if not value.isascii() or len(value) > MAX_ADDR_SPEC:
        return None
    if value.startswith('"'):
        quoted = _QUOTED_STRING.match(value)
        if quoted is None:
            return None
        local = quoted.group()
        rest = value[quoted.end() :]
    else:
        dot_atom = _DOT_ATOM.match(value)
        if dot_atom is None:
            return None
        local = dot_atom.group()
        rest = value[dot_atom.end() :]
    if not rest.startswith("@") or len(local) > MAX_LOCAL_PART:
        return None
    domain = rest[1:]
    if not is_address_domain(domain):
        return None
    return local, domain


def envelope_key(local: str, domain: str) -> tuple[str, str]:
    """How duplicates are compared: the local part literally, the domain in any case [D6]."""
    return local, domain.lower()
