"""The address grammar of SPEC §4.2 (case 8) and the domain grammars of §7.1 [D40]."""

from __future__ import annotations

import pytest

from mail_dispatch.addresses import (
    envelope_key,
    is_address_domain,
    is_ehlo_domain,
    split_addr_spec,
)

VALID = [
    "a@example.org",
    "first.last@example.org",
    "a!#$%&'*+-/=?^_`{|}~@example.org",
    '"a b"@example.org',
    '"a@b"@example.org',
    '"a\\"b"@example.org',
    '"\\\\"@example.org',
    "a@sub.example.org",
    "a@x-y.example.org",
    "a@example.org1a",
    "a@[192.0.2.1]",
    "a@[IPv6:2001:db8::1]",
    "a@[ipv6:2001:db8::1]",
    "a@[IPv6:::ffff:192.0.2.1]",
    "a@[IPv6:2001:db8:0:0:0:0:0:1]",
    "x" * 64 + "@example.org",
    "a@" + ".".join(["b" * 63] * 3) + ".org",
]

INVALID = [
    "",
    "a",
    "a@",
    "@example.org",
    "a@b@example.org",
    "Name <a@example.org>",
    "a.@example.org",
    ".a@example.org",
    "a..b@example.org",
    '""@example.org',
    '"a"b@example.org',
    '"a\x01"@example.org',
    "a b@example.org",
    "a@example.org.",
    "a@example",
    "a@-example.org",
    "a@example-.org",
    "a@exa_mple.org",
    "a@example.123",
    "a@" + "b" * 64 + ".org",
    "x" * 65 + "@example.org",
    '"' + "x" * 63 + '"@example.org',
    "a@" + ".".join(["b" * 63] * 4),
    "a@[192.0.2.01]",
    "a@[192.0.2]",
    "a@[256.0.2.1]",
    "a@[2001:db8::1]",
    "a@[IPv6:2001:db8::1%eth0]",
    "a@[IPv6:::ffff:192.0.2.01]",
    "a@[tag:content]",
    "a@[]",
    "zażółć@example.org",
    "a@bücher.example",
]


@pytest.mark.parametrize("address", VALID)
def test_valid_addresses(address: str) -> None:
    assert split_addr_spec(address) is not None


@pytest.mark.parametrize("address", INVALID)
def test_invalid_addresses(address: str) -> None:
    assert split_addr_spec(address) is None


def test_the_whole_address_is_limited_to_254() -> None:
    # Each part within its own limit; the sum is not.
    domain = ".".join(["d" * 63] * 3) + "." + "e" * 59
    assert len(domain) == 251
    assert split_addr_spec("ab@" + domain) is not None
    assert len("abc@" + domain) == 255
    assert split_addr_spec("abc@" + domain) is None


def test_split_returns_the_parts_as_written() -> None:
    assert split_addr_spec('"a@b"@Example.ORG') == ('"a@b"', "Example.ORG")


def test_duplicates_compare_the_domain_in_any_case_and_the_local_part_literally() -> None:
    assert envelope_key("a", "Example.ORG") == envelope_key("a", "example.org")
    assert envelope_key("A", "example.org") != envelope_key("a", "example.org")
    assert envelope_key('"a"', "example.org") != envelope_key("a", "example.org")


@pytest.mark.parametrize(
    ("value", "address_domain", "ehlo_domain"),
    [
        ("example.org", True, True),
        ("localhost", False, True),
        ("0123456789ab", False, True),  # a default container hostname
        ("123456789012", False, True),  # all digits, about once in 300 starts [D40]
        ("mail.123", False, True),
        ("[192.0.2.1]", True, True),
        ("[IPv6:::1]", True, True),
        ("-mail", False, False),
        ("mail_host", False, False),
        ("mail.example.org.", False, False),
        ("mail example", False, False),
        ("", False, False),
    ],
)
def test_domain_grammars(value: str, address_domain: bool, ehlo_domain: bool) -> None:
    assert is_address_domain(value) is address_domain
    assert is_ehlo_domain(value) is ehlo_domain
