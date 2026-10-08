"""Transfer encodings of SPEC §4.4: a rule, not a heuristic [D11].

`text` and `html` are `quoted-printable` while the bytes outside 32-126 (not counting `CR` and
`LF`) are at most a third of all bytes, `CRLF` included, and `base64` above that; every other
part is always `base64`. `7bit` and `8bit` are never used.
"""

from __future__ import annotations

import base64
import re
from typing import Literal

TransferEncoding = Literal["quoted-printable", "base64"]

_LINE_ENDING = re.compile(r"\r\n|\r|\n")
_OUTSIDE_32_126 = re.compile(rb"[^\x20-\x7e\r\n]")
# Everything but printable ASCII other than '=', space and tab is written as =XX.
_QP_ESCAPE = re.compile(rb"[^\x21-\x3c\x3e-\x7e \t\r\n]")
_QP_TRAILING_WHITE_SPACE = re.compile(rb"[ \t](?=\r\n|\Z)")
_QP_LINE = 76


def normalise_line_endings(text: str) -> bytes:
    """`\\n`, `\\r` and `\\r\\n` all become `CRLF` — the only transformation of content [D8]."""
    return _LINE_ENDING.sub("\r\n", text).encode("utf-8")


def text_encoding(data: bytes) -> TransferEncoding:
    outside = len(_OUTSIDE_32_126.findall(data))
    # Equality is quoted-printable, so empty content is too.
    return "quoted-printable" if 3 * outside <= len(data) else "base64"


def encode(data: bytes, encoding: TransferEncoding, *, terminated: bool = False) -> bytes:
    """The encoded body. With `terminated`, it ends in `CRLF` without changing what it decodes
    to — needed when the body is the last thing in the message rather than followed by a
    boundary."""
    if encoding == "base64":
        return encode_base64(data, terminated=terminated)
    return encode_quoted_printable(data, terminated=terminated)


def encode_base64(data: bytes, *, terminated: bool = False) -> bytes:
    lines = base64.encodebytes(data).replace(b"\n", b"\r\n")
    return lines if terminated else lines.removesuffix(b"\r\n")


def encode_quoted_printable(data: bytes, *, terminated: bool = False) -> bytes:
    """RFC 2045 §6.7 over content whose line breaks are all `CRLF`.

    Lines are at most 76 characters, a soft break never splits an `=XX`, and white space at the
    end of a line is encoded. A terminated body that does not end in a line break ends in a
    soft one, so that the `CRLF` the message needs adds nothing to the decoded content.
    """
    escaped = _QP_ESCAPE.sub(lambda match: b"=%02X" % match.group()[0], data)
    escaped = _QP_TRAILING_WHITE_SPACE.sub(lambda match: b"=%02X" % match.group()[0], escaped)
    lines = escaped.split(b"\r\n")
    soft_end = terminated and data != b"" and not data.endswith(b"\r\n")
    out: list[bytes] = []
    for number, line in enumerate(lines):
        last = number == len(lines) - 1
        out.extend(_wrap(line, soft_end=soft_end and last))
    encoded = b"\r\n".join(out)
    if terminated and data != b"" and not encoded.endswith(b"\r\n"):
        encoded += b"\r\n"
    return encoded


def _wrap(line: bytes, *, soft_end: bool) -> list[bytes]:
    """One hard line as physical lines of at most 76, each but the last ending in a soft break."""
    pieces: list[bytes] = []
    start = 0
    # The last physical line may use all 76 characters, unless it takes a soft break too.
    final_room = _QP_LINE - 1 if soft_end else _QP_LINE
    while len(line) - start > final_room:
        cut = start + _QP_LINE - 1
        # An '=' is always the first character of an escape, so this never splits one.
        if line[cut - 1 : cut] == b"=":
            cut -= 1
        elif line[cut - 2 : cut - 1] == b"=":
            cut -= 2
        pieces.append(line[start:cut] + b"=")
        start = cut
    pieces.append(line[start:] + (b"=" if soft_end else b""))
    return pieces
