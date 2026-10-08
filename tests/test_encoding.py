"""Transfer encodings [D11], header folding and encoded words [D34], checked by property."""

from __future__ import annotations

import binascii
import random
import re
from email.header import decode_header, make_header

import pytest

from mail_dispatch.bodies import (
    encode_base64,
    encode_quoted_printable,
    normalise_line_endings,
    text_encoding,
)
from mail_dispatch.headers import (
    MAX_LINE,
    encoded_words,
    filename_parameters,
    fold,
    needs_encoding,
    split_at_white_space,
    unstructured,
)

ALPHABET = [b" ", b"\t", b"=", b".", b"a", b"\r\n", b"\xc5\xbc", b"\x00", b"Z", b"\x7f", b"-"]


def random_content(rng: random.Random) -> bytes:
    return b"".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 300)))


@pytest.mark.parametrize("seed", range(20))
def test_quoted_printable_properties(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        data = random_content(rng)
        for terminated in (False, True):
            encoded = encode_quoted_printable(data, terminated=terminated)
            lines = encoded.split(b"\r\n")
            assert b"\r" not in encoded.replace(b"\r\n", b"")
            assert b"\n" not in encoded.replace(b"\r\n", b"")
            assert all(len(line) <= 76 for line in lines), encoded
            assert not any(line.endswith((b" ", b"\t")) for line in lines), encoded
            assert all(0x20 <= byte <= 0x7E or byte in b"\t\r\n" for byte in encoded)
            assert binascii.a2b_qp(encoded) == data
            if terminated and data:
                assert encoded.endswith(b"\r\n")


@pytest.mark.parametrize(
    ("data", "encoded"),
    [
        (b"", b""),
        (b"abc", b"abc"),
        (b"a=b", b"a=3Db"),
        (b"a \r\nb\t", b"a=20\r\nb=09"),
        (b"x" * 76, b"x" * 76),
        (b"x" * 77, b"x" * 75 + b"=\r\nxx"),
        (b"x" * 74 + b"\xc5\xbc", b"x" * 74 + b"=\r\n=C5=BC"),
    ],
)
def test_quoted_printable_examples(data: bytes, encoded: bytes) -> None:
    assert encode_quoted_printable(data) == encoded


def test_terminated_quoted_printable_adds_nothing_to_the_content() -> None:
    assert encode_quoted_printable(b"abc", terminated=True) == b"abc=\r\n"
    assert encode_quoted_printable(b"abc\r\n", terminated=True) == b"abc\r\n"
    assert encode_quoted_printable(b"", terminated=True) == b""
    # A last line that already uses all 76 characters is split before the soft break.
    long_line = encode_quoted_printable(b"x" * 76, terminated=True)
    assert long_line == b"x" * 75 + b"=\r\nx=\r\n"


def test_base64_lines() -> None:
    data = bytes(range(256)) * 10
    encoded = encode_base64(data)
    assert all(len(line) <= 76 for line in encoded.split(b"\r\n"))
    assert not encoded.endswith(b"\r\n")
    assert encode_base64(data, terminated=True) == encoded + b"\r\n"
    assert binascii.a2b_base64(encoded.replace(b"\r\n", b"")) == data
    assert encode_base64(b"") == b""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\nb", b"a\r\nb"),
        ("a\rb", b"a\r\nb"),
        ("a\r\nb", b"a\r\nb"),
        ("a\n\rb", b"a\r\n\r\nb"),
        ("a\r\r\nb", b"a\r\n\r\nb"),
        ("\N{LINE SEPARATOR}", "\N{LINE SEPARATOR}".encode()),
    ],
)
def test_line_endings_become_crlf(text: str, expected: bytes) -> None:
    assert normalise_line_endings(text) == expected


def test_encoding_rule_counts_bytes_outside_32_126() -> None:
    """[D11]: a third is still quoted-printable; CR and LF count only in the denominator."""
    assert text_encoding(b"") == "quoted-printable"
    assert text_encoding(b"\xc5ab") == "quoted-printable"
    assert text_encoding(b"\xc5\xc5ab") == "base64"
    assert text_encoding(b"\t" + b"\r\n") == "quoted-printable"
    assert text_encoding(b"\t\r\n\xc5") == "base64"
    assert (
        text_encoding("Dzień dobry, w załączniku przesyłam raport.".encode()) == "quoted-printable"
    )
    assert text_encoding("Здравствуйте".encode()) == "base64"


def unfold(lines: list[str]) -> str:
    return "".join(lines)


@pytest.mark.parametrize("seed", range(10))
def test_folding_never_changes_the_value(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        words = ["x" * rng.randint(1, 120) for _ in range(rng.randint(1, 30))]
        value = "".join(" " * rng.randint(1, 3) + word for word in words)
        lines = fold("X-Test", split_at_white_space(value))
        assert lines is not None
        assert unfold(lines) == "X-Test:" + value
        assert all(len(line) <= MAX_LINE for line in lines)
        assert not any(line.strip(" ") == "" for line in lines)
        for line in lines:
            # A line over 78 is a single piece that could not be split further.
            if len(line) > 78:
                assert len(split_at_white_space(line.removeprefix("X-Test:"))) <= 1 or (
                    line.startswith("X-Test:")
                )


def test_folding_aims_at_78() -> None:
    lines = fold("References", split_at_white_space(" " + " ".join(["<a@b>"] * 40)))
    assert lines is not None
    assert all(len(line) <= 78 for line in lines)
    assert len(lines) > 1


def test_folding_keeps_the_first_piece_on_the_name_line() -> None:
    assert fold("X", [" " + "y" * 200]) == ["X: " + "y" * 200]
    assert fold("X", [" " + "y" * 996]) == ["X:", " " + "y" * 996]
    assert fold("X", [" " + "y" * 998]) is None


def test_trailing_and_leading_white_space_stay_in_place() -> None:
    assert split_at_white_space("  a  b  ") == ["  a", "  b  "]
    assert split_at_white_space(" ") == [" "]


@pytest.mark.parametrize(
    "text",
    [
        "Zażółć gęślą jaźń",
        "🙂" * 40,
        "a" * 1000,
        "日本語のテキスト" * 20,
        " leading and trailing ",
        "=?utf-8?q?x?=",
    ],
)
def test_encoded_words_decode_to_the_text(text: str) -> None:
    words = encoded_words(text)
    assert all(len(word) <= 75 for word in words)
    for word in words:
        # Each word holds whole characters: it decodes on its own.
        str(make_header(decode_header(word)))
    assert str(make_header(decode_header(" ".join(words)))) == text


def test_the_first_encoded_word_can_be_shorter() -> None:
    words = encoded_words("ą" * 100, first_max=40)
    assert len(words[0]) <= 40
    assert all(len(word) <= 75 for word in words[1:])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello", False),
        ("", False),
        ("Zażółć", True),
        ("a =? b", True),
        ("x" * 996, False),
        ("x" * 998, True),
        ("a " + "x" * 998, True),
    ],
)
def test_when_a_text_is_encoded(text: str, expected: bool) -> None:
    assert needs_encoding(text) is expected


def test_a_subject_of_1000_characters_is_encoded_within_998() -> None:
    lines = fold("Subject", unstructured("Subject", "x" * 1000))
    assert lines is not None
    assert all(len(line) <= 78 for line in lines)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", ['filename="report.pdf"']),
        ('a"b.txt', ['filename="a\\"b.txt"']),
        ("x" * 66, ['filename="' + "x" * 66 + '"']),
        ("raport ą.pdf", ["filename*=utf-8''raport%20%C4%85.pdf"]),
    ],
)
def test_filename_forms(filename: str, expected: list[str]) -> None:
    assert filename_parameters(filename) == expected


@pytest.mark.parametrize(
    "filename", ["x" * 67, "y" * 200, '"' * 100, "ą" * 255, "a" * 150 + "€" * 50]
)
def test_long_filenames_use_continuations_within_78(filename: str) -> None:
    sections = filename_parameters(filename)
    assert len(sections) > 1
    assert all(len(" " + section + ";") <= 78 for section in sections)
    assert all(re.match(r"filename\*\d+\*?=", section) for section in sections)
