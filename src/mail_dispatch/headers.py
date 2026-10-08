"""Header lines: folding, RFC 2047 encoded-words, RFC 2231 parameters (SPEC §4.5) [D34].

A header is built from *pieces*: the value after `Name:` cut at the places where a fold may
go. Every piece but possibly the first starts with white space, and a fold is a `CRLF` placed
before a piece, so unfolding gives back the value unchanged. No piece consists of white space
only, so no line does either.

Every line is at most 998 characters; the folder aims at 78, and keeps the first piece on the
`Name:` line unless only a fold right after the colon makes it fit.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence

MAX_LINE = 998
TARGET_LINE = 78

_PIECE = re.compile(r"[ \t]*[^ \t]+")
_PHRASE = re.compile(r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]+(?: [A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]+)*")
# RFC 2231 attribute-char: CHAR except SPACE, CTLs, "*", "'", "%" and tspecials.
_ATTRIBUTE_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$&+-.^_`|~"
)
_ENCODED_WORD_OVERHEAD = len("=?utf-8?b??=")
MAX_ENCODED_WORD = 75


def fold(name: str, pieces: Sequence[str]) -> list[str] | None:
    """The lines of `Name:` followed by `pieces`, or None when one would exceed 998."""
    lines: list[str] = []
    current = name + ":"
    first = True
    for piece in pieces:
        if first:
            first = False
            if len(current) + len(piece) > MAX_LINE:
                lines.append(current)
                current = piece
            else:
                current += piece
        elif len(current) + len(piece) <= TARGET_LINE:
            current += piece
        else:
            lines.append(current)
            current = piece
    lines.append(current)
    if any(len(line) > MAX_LINE for line in lines):
        return None
    return lines


def render(lines: Sequence[str]) -> bytes:
    return "".join(line + "\r\n" for line in lines).encode("ascii")


def split_at_white_space(text: str) -> list[str]:
    """Pieces cut before every run of white space; trailing white space joins the last piece."""
    pieces = _PIECE.findall(text)
    rest = text[sum(map(len, pieces)) :]
    if rest:
        if pieces:
            pieces[-1] += rest
        else:
            pieces = [rest]
    return pieces


def fits(pieces: Sequence[str]) -> bool:
    """Whether the pieces can be folded within 998, whatever header they open."""
    return all(len(piece) <= MAX_LINE for piece in pieces)


def needs_encoding(text: str) -> bool:
    """RFC 2047 is needed beyond printable ASCII, for a literal `=?`, and for a run that no
    fold can bring within 998 [D34]."""
    if not all(" " <= c <= "~" for c in text) or "=?" in text:
        return True
    return not fits(split_at_white_space(" " + text))


def encoded_words(text: str, first_max: int = MAX_ENCODED_WORD) -> list[str]:
    """`text` as `B` encoded-words in UTF-8, each at most 75 characters, never splitting a
    character; the first one at most `first_max`, so it can share the `Name:` line."""
    data = text.encode("utf-8")
    words: list[str] = []
    start = 0
    # Room for at least one character of four bytes.
    limit = max(first_max, _ENCODED_WORD_OVERHEAD + 8)
    while start < len(data) or not words:
        budget = (limit - _ENCODED_WORD_OVERHEAD) // 4 * 3
        end = min(start + budget, len(data))
        # Step back over UTF-8 continuation bytes so a character is never split.
        while end < len(data) and end > start and data[end] & 0xC0 == 0x80:
            end -= 1
        words.append("=?utf-8?b?" + base64.b64encode(data[start:end]).decode("ascii") + "?=")
        start = end
        limit = MAX_ENCODED_WORD
    return words


def unstructured(name: str, text: str) -> list[str]:
    """Pieces of an unstructured value such as `Subject`: as written, or encoded-words."""
    if needs_encoding(text):
        first_max = TARGET_LINE - len(name) - 2
        return [" " + word for word in encoded_words(text, first_max)]
    return split_at_white_space(" " + text)


def display_name(name: str, first_max: int = MAX_ENCODED_WORD) -> list[str]:
    """Pieces of a display name: atoms, a quoted string, or encoded-words (§4.5).

    The quotes and quoted pairs count towards the length, so whether the plain form can be
    folded is decided on the form that would be written.
    """
    if all(" " <= c <= "~" for c in name) and "=?" not in name:
        if _PHRASE.fullmatch(name):
            pieces = split_at_white_space(" " + name)
        else:
            quoted = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
            pieces = split_at_white_space(" " + quoted)
        if fits(pieces):
            return pieces
    return [" " + word for word in encoded_words(name, first_max)]


def mailbox(address: str, name: str | None, first_max: int = MAX_ENCODED_WORD) -> list[str]:
    if name is None:
        return [" " + address]
    return [*display_name(name, first_max), " <" + address + ">"]


def address_list(header: str, mailboxes: Sequence[tuple[str, str | None]]) -> list[str]:
    """`From`, `To`, `Cc`, `Reply-To`: mailboxes separated by commas, folded between them."""
    pieces: list[str] = []
    for position, (address, name) in enumerate(mailboxes):
        first_max = TARGET_LINE - len(header) - 2 if position == 0 else MAX_ENCODED_WORD
        own = mailbox(address, name, first_max)
        if position < len(mailboxes) - 1:
            own[-1] += ","
        pieces.extend(own)
    return pieces


def parameters(head: str, params: Sequence[str]) -> list[str]:
    """`value; a=b; c=d` as pieces, folded only between parameters."""
    pieces = [" " + head]
    for param in params:
        pieces[-1] += ";"
        pieces.append(" " + param)
    return pieces


def filename_parameters(filename: str) -> list[str]:
    """The `filename` parameter as one or more pieces (§4.5).

    An ASCII name is `filename="…"`, with RFC 2231 continuations only when that one parameter
    does not fit in 78 on its line. A non-ASCII name is always `filename*=utf-8''…`.
    """
    if filename.isascii():
        escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
        single = f'filename="{escaped}"'
        if len(single) + 1 <= TARGET_LINE:
            return [single]
        units = re.findall(r"\\.|.", escaped)
        return _continuations(units, quoted=True)
    encoded = "".join(_percent_encode(char) for char in filename)
    single = "filename*=utf-8''" + encoded
    if len(single) + 1 <= TARGET_LINE:
        return [single]
    return _continuations([_percent_encode(char) for char in filename], quoted=False)


def _percent_encode(char: str) -> str:
    return "".join(
        chr(byte) if byte in _ATTRIBUTE_CHARS else f"%{byte:02X}" for byte in char.encode("utf-8")
    )


def _continuations(units: Sequence[str], *, quoted: bool) -> list[str]:
    """Sections `filename*N="…"` (or `filename*N*=…`) of at most 78 with their separator."""
    sections: list[str] = []
    index = 0
    position = 0
    while position < len(units) or not sections:
        if quoted:
            prefix, suffix = f'filename*{index}="', '"'
        elif index == 0:
            prefix, suffix = "filename*0*=utf-8''", ""
        else:
            prefix, suffix = f"filename*{index}*=", ""
        # One space before the section and one ';' after it share the line.
        budget = TARGET_LINE - 2 - len(prefix) - len(suffix)
        chunk = ""
        while position < len(units) and len(chunk) + len(units[position]) <= budget:
            chunk += units[position]
            position += 1
        sections.append(prefix + chunk + suffix)
        index += 1
    return sections
