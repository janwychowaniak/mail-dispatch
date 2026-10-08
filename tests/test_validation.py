"""Steps 2-5 of SPEC §4.7: shape, control characters, grammars, limits, and their order [D36].

Cases 7, 8 and 9 are covered here at the level of the validator; the HTTP cases in
test_send.py prove the same through the service with zero connections.
"""

from __future__ import annotations

from typing import Any

import pytest
from builders import PNG, b64, request

from mail_dispatch.errors import ApiError
from mail_dispatch.validation import MessageRequest, validate


def ok(document: Any, max_recipients: int = 100) -> MessageRequest:
    return validate(document, max_recipients=max_recipients)


def fails(document: Any, max_recipients: int = 100) -> ApiError:
    with pytest.raises(ApiError) as caught:
        validate(document, max_recipients=max_recipients)
    return caught.value


def where(error: ApiError) -> tuple[str, object, object]:
    return error.code, error.fields.get("field"), error.fields.get("index")


def test_the_baseline_is_valid() -> None:
    message = ok(request())
    assert message.sender.address == "sender@example.org"
    assert message.sender.name == "Sender"
    assert [(r.mailbox.address, r.field) for r in message.recipients] == [("to@example.org", "to")]
    assert message.text == b"Hello, world."
    assert message.html is None


# Step 2: the shape ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "field", "index"),
    [
        (request(foo=1), "foo", None),
        (request(to=[{"address": "a@example.org", "foo": 1}]), "to[0].foo", 0),
        (request(**{"from": {"address": "a@example.org", "adress": "x"}}), "from.adress", None),
        (request(**{"from": ...}), "from", None),
        (request(**{"from": None}), "from", None),
        (request(**{"from": {"name": "x"}}), "from.address", None),
        (request(**{"from": {"address": None}}), "from.address", None),
        (request(**{"from": "a@example.org"}), "from", None),
        (request(subject=...), "subject", None),
        (request(subject=None), "subject", None),
        (request(subject=1), "subject", None),
        (request(text=True), "text", None),
        (request(to="a@example.org"), "to", None),
        (request(to=[{"address": "a@example.org"}, "b@example.org"]), "to[1]", 1),
        (request(to=[{"address": "a@example.org"}, {"address": 1}]), "to[1].address", 1),
        (request(to=[{"address": "a@example.org", "name": ["x"]}]), "to[0].name", 0),
        (request(cc=[None]), "cc[0]", 0),
        (request(headers=["X-A: b"]), "headers", None),
        (request(headers={"X-A": 1}), "headers.X-A", None),
        (request(headers={"X-A": None}), "headers.X-A", None),
        (request(subject="\ud800"), "subject", None),
        (request(text="a\udfffb"), "text", None),
        (request(headers={"X-\ud800": "v"}), "headers.X-\N{REPLACEMENT CHARACTER}", None),
        (request(inline=[{"cid": "a"}], html="<p>"), "inline[0].content_type", 0),
        (request(attachments=[{"filename": "a"}]), "attachments[0].content_base64", 0),
        (request(attachments=[{"content_base64": ""}]), "attachments[0].filename", 0),
    ],
)
def test_shape_errors(document: dict[str, Any], field: str, index: int | None) -> None:
    assert where(fails(document)) == ("INVALID_REQUEST", field, index)


def test_not_an_object() -> None:
    assert where(fails([])) == ("INVALID_REQUEST", None, None)


def test_unknown_keys_come_before_missing_ones() -> None:
    """A misspelt field is reported as unknown before its correct name as missing."""
    document = request(subjet="x", subject=...)
    assert where(fails(document)) == ("INVALID_REQUEST", "subjet", None)


def test_unknown_keys_in_request_order() -> None:
    document = {"zzz": 1, **request(), "aaa": 2}
    assert where(fails(document)) == ("INVALID_REQUEST", "zzz", None)


def test_depth_first_in_schema_order() -> None:
    document = request(
        to=[{"address": 1}], **{"from": {"address": "a@example.org", "x": 1}}, subject=2
    )
    assert where(fails(document)) == ("INVALID_REQUEST", "from.x", None)


@pytest.mark.parametrize(
    "document",
    [
        request(to=...),
        request(to=None),
        request(to=[]),
        request(to=[], cc=None, bcc=[]),
    ],
)
def test_no_recipient(document: dict[str, Any]) -> None:
    error = fails(document)
    assert where(error) == ("INVALID_REQUEST", None, None)
    assert "recipient" in error.message


def test_reply_to_does_not_count_as_a_recipient() -> None:
    error = fails(request(to=..., reply_to=[{"address": "r@example.org"}]))
    assert "recipient" in error.message


@pytest.mark.parametrize("document", [request(text=...), request(text=None, html=None)])
def test_neither_text_nor_html(document: dict[str, Any]) -> None:
    error = fails(document)
    assert where(error) == ("INVALID_REQUEST", None, None)
    assert "text or html" in error.message


def test_inline_without_html() -> None:
    inline = [{"cid": "a", "content_type": "image/png", "content_base64": ""}]
    assert where(fails(request(inline=inline))) == ("INVALID_REQUEST", "inline", None)
    assert ok(request(inline=inline, html="")).html == b""


def test_cross_field_rules_come_after_the_walk_in_order() -> None:
    document = request(to=..., text=..., subject=1)
    assert where(fails(document)) == ("INVALID_REQUEST", "subject", None)
    error = fails(
        request(
            to=..., text=..., inline=[{"cid": "a", "content_type": "a/b", "content_base64": ""}]
        )
    )
    assert "recipient" in error.message


def test_absent_null_and_empty_mean_none() -> None:
    message = ok(request(cc=[], bcc=None, reply_to=[], inline=None, attachments=[], headers={}))
    assert [r.field for r in message.recipients] == ["to"]
    assert message.reply_to == ()
    assert message.headers == ()
    assert ok(request(html=[])).html is None


def test_empty_strings_are_present() -> None:
    message = ok(request(subject="", text="", html=""))
    assert (message.subject, message.text, message.html) == ("", b"", b"")


# Step 3: control characters ------------------------------------------------------------------


@pytest.mark.parametrize("char", ["\r", "\n", "\t", "\x00", "\x7f", "\x85", "\x9f"])
@pytest.mark.parametrize(
    ("make", "field", "index"),
    [
        (lambda c: request(subject="a" + c), "subject", None),
        (lambda c: request(**{"from": {"address": "a@example.org", "name": c}}), "from.name", None),
        (lambda c: request(to=[{"address": "a" + c + "@example.org"}]), "to[0].address", 0),
        (lambda c: request(reply_to=[{"address": "a@example.org" + c}]), "reply_to[0].address", 0),
        (
            lambda c: request(attachments=[{"filename": "a" + c, "content_base64": ""}]),
            "attachments[0].filename",
            0,
        ),
        (
            lambda c: request(
                html="",
                inline=[{"cid": "a" + c, "content_type": "image/png", "content_base64": ""}],
            ),
            "inline[0].cid",
            0,
        ),
        (
            lambda c: request(
                html="",
                inline=[{"cid": "a", "content_type": "image/png" + c, "content_base64": ""}],
            ),
            "inline[0].content_type",
            0,
        ),
        (lambda c: request(headers={"X-A": "b" + c + "c"}), "headers.X-A", None),
        (lambda c: request(headers={"X-A" + c: "b"}), "headers.X-A{c}", None),
    ],
)
def test_control_characters_are_invalid_header(
    char: str, make: Any, field: str, index: int | None
) -> None:
    """Case 7: a control character anywhere a header is made is INVALID_HEADER [D16]."""
    assert where(fails(make(char))) == ("INVALID_HEADER", field.format(c=char), index)


def test_a_name_of_a_single_tab_is_a_control_character() -> None:
    document = request(**{"from": {"address": "a@example.org", "name": "\t"}})
    assert where(fails(document)) == ("INVALID_HEADER", "from.name", None)


def test_control_characters_come_before_grammars() -> None:
    """Case 7: for an address, INVALID_HEADER and not INVALID_ADDRESS."""
    document = request(to=[{"address": "not an address\r\n"}])
    assert where(fails(document)) == ("INVALID_HEADER", "to[0].address", 0)
    document = request(to=[{"address": "bad"}], subject="\n")
    assert where(fails(document)) == ("INVALID_HEADER", "subject", None)


def test_control_characters_are_allowed_in_content() -> None:
    assert ok(request(text="a\tb\x00c\x1b", html="\x7f")).text == b"a\tb\x00c\x1b"


@pytest.mark.parametrize(
    "char",
    [
        "\N{ZERO WIDTH SPACE}",
        "\N{LINE SEPARATOR}",
        "\N{PARAGRAPH SEPARATOR}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
        "\N{SOFT HYPHEN}",
    ],
)
def test_format_characters_and_separators_are_not_control(char: str) -> None:
    assert ok(request(subject="a" + char)).subject == "a" + char


# Step 4: grammars ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "a@b@example.org",
        "Name <a@example.org>",
        "a.@example.org",
        "a@-example.org",
        "a@example-.org",
        "x" * 65 + "@example.org",
        "a@" + "b" * 64 + ".org",
        "zażółć@example.org",
        "a@bücher.example",
    ],
)
def test_addresses_out_of_grammar(address: str) -> None:
    """Case 8: one address breaking each rule of §4.2."""
    document = request(to=[{"address": "ok@example.org"}, {"address": address}])
    assert where(fails(document)) == ("INVALID_ADDRESS", "to[1].address", 1)


def test_sender_and_reply_to_addresses_are_checked() -> None:
    assert where(fails(request(**{"from": {"address": "x"}}))) == (
        "INVALID_ADDRESS",
        "from.address",
        None,
    )
    assert where(fails(request(reply_to=[{"address": "x"}]))) == (
        "INVALID_ADDRESS",
        "reply_to[0].address",
        0,
    )


def test_duplicate_recipient_points_at_the_second_occurrence() -> None:
    """Case 8: the same address in to and bcc, the domain in another letter case."""
    document = request(
        to=[{"address": "a@example.org"}],
        bcc=[{"address": "x@x.example"}, {"address": "a@EXAMPLE.org"}],
    )
    assert where(fails(document)) == ("INVALID_REQUEST", "bcc[1].address", 1)


def test_duplicate_within_reply_to() -> None:
    document = request(reply_to=[{"address": "r@example.org"}, {"address": "r@example.org"}])
    assert where(fails(document)) == ("INVALID_REQUEST", "reply_to[1].address", 1)


def test_quoted_and_plain_local_parts_are_two_addresses() -> None:
    document = request(to=[{"address": "a@example.org"}, {"address": '"a"@example.org'}])
    assert len(ok(document).recipients) == 2


def test_local_part_is_compared_literally() -> None:
    document = request(to=[{"address": "a@example.org"}, {"address": "A@example.org"}])
    assert len(ok(document).recipients) == 2


def test_sender_and_reply_to_may_coincide_with_recipients() -> None:
    document = request(
        **{"from": {"address": "to@example.org"}}, reply_to=[{"address": "to@example.org"}]
    )
    assert ok(document).sender.address == "to@example.org"


def test_addresses_come_before_duplicates() -> None:
    document = request(
        to=[{"address": "a@example.org"}, {"address": "a@example.org"}], cc=[{"address": "bad"}]
    )
    assert where(fails(document)) == ("INVALID_ADDRESS", "cc[0].address", 0)


@pytest.mark.parametrize(
    ("headers", "field"),
    [
        ({"Bcc": "x@example.org"}, "headers.Bcc"),
        ({"bcc": "x@example.org"}, "headers.bcc"),
        ({"content-type": "text/plain"}, "headers.content-type"),
        ({"Content-Foo": "bar"}, "headers.Content-Foo"),
        ({"MIME-Version": "1.0"}, "headers.MIME-Version"),
        ({"Return-Path": "<a@b.example>"}, "headers.Return-Path"),
        ({"Received": "x"}, "headers.Received"),
        ({"Sender": "x"}, "headers.Sender"),
        ({"Message-Id": "<a@b>"}, "headers.Message-Id"),
        ({"X-A": "1", "x-a": "2"}, "headers.x-a"),
        ({"X A": "1"}, "headers.X A"),
        ({"X:A": "1"}, "headers.X:A"),
        ({"": "1"}, "headers."),
        ({"X-Ä": "1"}, "headers.X-Ä"),
        ({"X-A": "zażółć"}, "headers.X-A"),
        ({"X-A": ""}, "headers.X-A"),
        ({"X-A": "   "}, "headers.X-A"),
        ({"References": "<" + "x" * 1000 + ">"}, "headers.References"),
        ({"X-" + "a" * 1000: "1"}, "headers.X-" + "a" * 1000),
    ],
)
def test_custom_header_errors(headers: dict[str, str], field: str) -> None:
    """Case 6: reserved names in any letter case, two keys differing in case, bad shapes."""
    assert where(fails(request(headers=headers))) == ("INVALID_HEADER", field, None)


def test_custom_headers_are_kept_verbatim_in_order() -> None:
    headers = {
        "In-Reply-To": "<id@example.org>",
        "References": " ".join("<" + "r" * 498 + ">" for _ in range(3)),
        "X-Encoded": "=?utf-8?q?caf=C3=A9?=",
        "X-Spaces": "  padded  ",
    }
    assert ok(request(headers=headers)).headers == tuple(headers.items())


def test_custom_headers_come_before_parts() -> None:
    document = request(
        headers={"Bcc": "x"}, attachments=[{"filename": "a/b", "content_base64": ""}]
    )
    assert where(fails(document)) == ("INVALID_HEADER", "headers.Bcc", None)


def inline(**changes: Any) -> dict[str, Any]:
    part = {"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}
    part.update(changes)
    return part


def attachment(**changes: Any) -> dict[str, Any]:
    part = {"filename": "report.pdf", "content_base64": b64(b"%PDF")}
    part.update(changes)
    return part


@pytest.mark.parametrize(
    ("document", "code", "field", "index"),
    [
        (
            request(html="", inline=[inline(content_base64="!!!!")]),
            "INVALID_CONTENT",
            "inline[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="QUJ")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="QQ")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="QQ=A")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="Q===")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="QUJD\tREVG")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (
            request(attachments=[attachment(content_base64="QUJD-_==")]),
            "INVALID_CONTENT",
            "attachments[0].content_base64",
            0,
        ),
        (request(html="", inline=[inline(), inline()]), "INVALID_CONTENT", "inline[1].cid", 1),
        (request(html="", inline=[inline(cid="")]), "INVALID_CONTENT", "inline[0].cid", 0),
        (request(html="", inline=[inline(cid="a b")]), "INVALID_CONTENT", "inline[0].cid", 0),
        (request(html="", inline=[inline(cid="<a>")]), "INVALID_CONTENT", "inline[0].cid", 0),
        (request(html="", inline=[inline(cid="x" * 1000)]), "INVALID_HEADER", "inline[0].cid", 0),
        (
            request(attachments=[attachment(content_type="multipart/mixed")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="Message/RFC822")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; boundary=x")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; NAME=x")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; filename*0*=x")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; name*=x")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; a=1; A=2")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain;")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain (comment)")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; a")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type='text/plain; a="b')]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; a=ü")]),
            "INVALID_CONTENT",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(content_type="text/plain; a=" + "x" * 1000)]),
            "INVALID_HEADER",
            "attachments[0].content_type",
            0,
        ),
        (
            request(attachments=[attachment(filename="a/b")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename="a\\b")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename="..")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename=".")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename="")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename="\N{IDEOGRAPHIC SPACE} \N{NO-BREAK SPACE}")]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(attachments=[attachment(filename="x" * 256)]),
            "INVALID_CONTENT",
            "attachments[0].filename",
            0,
        ),
        (
            request(html="", inline=[inline(filename="../x")]),
            "INVALID_CONTENT",
            "inline[0].filename",
            0,
        ),
    ],
)
def test_part_errors(document: dict[str, Any], code: str, field: str, index: int) -> None:
    """Case 9 (and case 6 for a cid that cannot be folded)."""
    assert where(fails(document)) == (code, field, index)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("", b""),
        ("QUJD", b"ABC"),
        ("QQ==", b"A"),
        ("QR==", b"A"),
        ("QUI=", b"AB"),
        ("QU\r\nJD", b"ABC"),
        (" Q U J D \n", b"ABC"),
    ],
)
def test_base64_accepted(encoded: str, expected: bytes) -> None:
    assert (
        ok(request(attachments=[attachment(content_base64=encoded)])).attachments[0].data
        == expected
    )


def test_a_name_that_merely_contains_two_dots_is_valid() -> None:
    assert (
        ok(request(attachments=[attachment(filename="report..pdf")])).attachments[0].filename
        == "report..pdf"
    )


def test_a_file_name_of_255_characters_is_valid() -> None:
    assert (
        ok(request(attachments=[attachment(filename="x" * 255)])).attachments[0].filename
        == "x" * 255
    )


def test_content_type_is_kept_as_given() -> None:
    message = ok(request(attachments=[attachment(content_type='Text/X-Thing ;  a=b;c="d e"  ')]))
    content_type = message.attachments[0].content_type
    assert content_type is not None
    assert (content_type.type, content_type.subtype, content_type.params) == (
        "Text",
        "X-Thing",
        ("a=b", 'c="d e"'),
    )


def test_parts_in_schema_order() -> None:
    document = request(
        html="",
        inline=[inline(cid="a b", content_type="multipart/x")],
        attachments=[attachment(filename="a/b")],
    )
    assert where(fails(document)) == ("INVALID_CONTENT", "inline[0].cid", 0)
    document = request(html="", inline=[inline(content_type="x", filename="a/b")])
    assert where(fails(document)) == ("INVALID_CONTENT", "inline[0].content_type", 0)


@pytest.mark.parametrize(
    ("parts", "field"),
    [
        ({"inline": [inline(cid="x" * 1000)]}, "inline[0].cid"),
        (
            {"inline": [inline(content_type="image/png; a=" + "x" * 1000)]},
            "inline[0].content_type",
        ),
        (
            {"attachments": [attachment(content_type="text/plain; a=" + "x" * 1000)]},
            "attachments[0].content_type",
        ),
    ],
)
def test_a_part_header_too_long_is_checked_with_its_part(parts: dict[str, Any], field: str) -> None:
    """[D36]: a check belongs to the category of the field it examines, not of the code it
    returns, so an unfoldable `cid` or `content_type` parameter loses to a bad custom header."""
    assert where(fails(request(html="", **parts)))[:2] == ("INVALID_HEADER", field)
    both = request(html="", headers={"Bcc": "x"}, **parts)
    assert where(fails(both)) == ("INVALID_HEADER", "headers.Bcc", None)


def test_a_part_header_too_long_keeps_its_place_in_the_schema_order() -> None:
    """[D36]: within the parts it is checked at the field's place, so it wins over a later field
    of its part and loses to an earlier part."""
    long_cid = inline(cid="x" * 1000, content_base64="!")
    assert where(fails(request(html="", inline=[long_cid]))) == (
        "INVALID_HEADER",
        "inline[0].cid",
        0,
    )
    earlier = request(html="", inline=[inline(content_base64="!"), inline(cid="y" * 1000)])
    assert where(fails(earlier)) == ("INVALID_CONTENT", "inline[0].content_base64", 0)


# Names [D35] -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", " ", "   ", "\N{IDEOGRAPHIC SPACE}", "\N{NO-BREAK SPACE} \N{EM SPACE}"]
)
def test_a_white_space_name_is_absent(name: str) -> None:
    assert ok(request(**{"from": {"address": "a@example.org", "name": name}})).sender.name is None


def test_a_name_is_not_trimmed() -> None:
    assert (
        ok(request(to=[{"address": "a@example.org", "name": " A "}])).recipients[0].mailbox.name
        == " A "
    )


# Step 5 -------------------------------------------------------------------------------------


def test_too_many_recipients() -> None:
    """Case 10: limit, actual and the source of the limit."""
    document = request(
        to=[{"address": f"t{n}@example.org"} for n in range(2)],
        cc=[{"address": "c@example.org"}],
        bcc=[{"address": "b@example.org"}],
        reply_to=[{"address": f"r{n}@example.org"} for n in range(5)],
    )
    error = fails(document, max_recipients=3)
    assert error.code == "TOO_MANY_RECIPIENTS"
    assert error.fields == {"limit": 3, "actual": 4, "limit_source": "max_recipients"}
    assert len(ok(document, max_recipients=4).recipients) == 4


def test_grammars_come_before_the_recipient_limit() -> None:
    document = request(to=[{"address": "a@example.org"}, {"address": "bad"}])
    assert fails(document, max_recipients=1).code == "INVALID_ADDRESS"


def test_recipients_in_order_to_cc_bcc() -> None:
    document = request(
        bcc=[{"address": "b@example.org"}],
        cc=[{"address": "c@example.org"}],
        to=[{"address": "t@example.org"}],
    )
    assert [r.field for r in ok(document).recipients] == ["to", "cc", "bcc"]
