"""Transfer encodings of SPEC §4.4: a rule, not a heuristic [D11].

`text` and `html` are `quoted-printable` while the bytes outside 32-126 (not counting `CR` and
`LF`) are at most a third of all bytes, `CRLF` included, and `base64` above that; every other
part is always `base64`. `7bit` and `8bit` are never used.
"""

from __future__ import annotations

import binascii
import re
from typing import Literal

TransferEncoding = Literal["quoted-printable", "base64"]

# Every byte outside 32-126 except CR and LF, which the numerator of [D11] leaves out.
_OUTSIDE_32_126 = bytes(b for b in range(256) if not (0x20 <= b <= 0x7E or b in b"\r\n"))
# Everything but printable ASCII other than '=', space and tab is written as =XX.
_QP_ESCAPE = re.compile(rb"[^\x21-\x3c\x3e-\x7e \t\r\n]")
_QP_TRAILING_WHITE_SPACE = re.compile(rb"[ \t](?=\r\n|\Z)")
_QP_LINE = 76
_BLOCK = 65536
_BASE64_LINE = 76
# 1024 lines: 57 bytes make exactly 76 characters, so blocks never split a line.
_BASE64_BLOCK = 57 * 1024


def normalise_line_endings(text: str) -> bytes:
    """`\\n`, `\\r` and `\\r\\n` all become `CRLF` — the only transformation of content [D8].

    Done on the UTF-8 bytes, in which CR and LF occur only as themselves: a string with a
    character beyond Latin-1 takes two or four bytes per character, its bytes one to four.
    """
    data = text.encode("utf-8")
    if b"\r" in data:
        # CRLF and a lone CR become LF first; the CRLF pairs are taken from the left, as a
        # single scan for `\r\n|\r|\n` would take them.
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    # bytes.replace sizes its result once; a regular expression would build a list of pieces,
    # one per line, that costs more memory than the content.
    return data.replace(b"\n", b"\r\n")


def text_encoding(data: bytes) -> TransferEncoding:
    outside = len(data) - len(data.translate(None, _OUTSIDE_32_126))
    # Equality is quoted-printable, so empty content is too.
    return "quoted-printable" if 3 * outside <= len(data) else "base64"


def encode(data: bytes, encoding: TransferEncoding, *, terminated: bool = False) -> bytes:
    """The encoded body. With `terminated`, it ends in `CRLF` without changing what it decodes
    to - needed when the body is the last thing in the message rather than followed by a
    boundary."""
    return b"".join(encode_pieces(data, encoding, terminated=terminated))


def encode_pieces(
    data: bytes, encoding: TransferEncoding, *, terminated: bool = False
) -> list[bytes]:
    """The encoded body as pieces of about 64 KiB, which the composer joins once.

    Encoding a block at a time keeps the transient objects small: a whole-content list of one
    object per line would cost more memory than the content itself (§7.3) [D48].
    """
    if encoding == "base64":
        return base64_pieces(data, terminated=terminated)
    return quoted_printable_pieces(data, terminated=terminated)


def base64_pieces(data: bytes, *, terminated: bool = False) -> list[bytes]:
    """Lines of 76 characters, each ending in `CRLF` (the last one too when `terminated`)."""
    view = memoryview(data)
    pieces = []
    for start in range(0, len(data), _BASE64_BLOCK):
        encoded = binascii.b2a_base64(view[start : start + _BASE64_BLOCK], newline=False)
        lines = range(0, len(encoded), _BASE64_LINE)
        pieces.append(b"".join(encoded[i : i + _BASE64_LINE] + b"\r\n" for i in lines))
    if pieces and not terminated:
        pieces[-1] = pieces[-1][:-2]
    return pieces


def encode_base64(data: bytes, *, terminated: bool = False) -> bytes:
    return b"".join(base64_pieces(data, terminated=terminated))


def quoted_printable_pieces(data: bytes, *, terminated: bool = False) -> list[bytes]:
    """RFC 2045 §6.7 over content whose line breaks are all `CRLF`, a block of lines at a time.

    Lines are at most 76 characters, a soft break never splits an `=XX`, and white space at the
    end of a line is encoded. A terminated body that does not end in a line break ends in a
    soft one, so that the `CRLF` the message needs adds nothing to the decoded content.
    """
    soft_end = terminated and data != b"" and not data.endswith(b"\r\n")
    pieces: list[bytes] = []
    start = 0
    while True:
        stop = min(start + _BLOCK, len(data))
        if stop < len(data):
            # Blocks end after a line break, so no line is split between two of them.
            newline = data.rfind(b"\n", start, stop)
            if newline < start:
                newline = data.find(b"\n", stop)
                stop = len(data) if newline < 0 else newline + 1
            else:
                stop = newline + 1
        last = stop == len(data)
        pieces.append(_quoted_printable_block(data[start:stop], soft_end=soft_end and last))
        start = stop
        if last:
            break
    if terminated and data != b"" and not pieces[-1].endswith(b"\r\n"):
        pieces.append(b"\r\n")
    return pieces


def encode_quoted_printable(data: bytes, *, terminated: bool = False) -> bytes:
    return b"".join(quoted_printable_pieces(data, terminated=terminated))


def _quoted_printable_block(block: bytes, *, soft_end: bool) -> bytes:
    escaped = _QP_ESCAPE.sub(lambda match: b"=%02X" % match.group()[0], block)
    escaped = _QP_TRAILING_WHITE_SPACE.sub(lambda match: b"=%02X" % match.group()[0], escaped)
    lines = escaped.split(b"\r\n")
    out: list[bytes] = []
    for number, line in enumerate(lines):
        out.extend(_wrap(line, soft_end=soft_end and number == len(lines) - 1))
    return b"\r\n".join(out)


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
