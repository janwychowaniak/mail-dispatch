# mail-dispatch — functional specification

**Status: DRAFT (2026-10-07), clarified in three rounds; awaiting approval.**
Once approved, this file is the source of truth for implementation and the authority on the
contract. Design decisions are marked `[D#]` inline and recorded in
[§13](#13-decision-record); defects are reported against those numbers.

## 1. Purpose

mail-dispatch is a self-hosted HTTP microservice that takes a JSON description of **one** email
message, composes a correct RFC 5322 / MIME message from it, sends it through **one** configured
SMTP server, and returns a faithful report of that conversation, recipient by recipient.

The consumer says **what** and **to whom**; the service knows **how**: header encoding,
alternative parts, embedded images, attachments, the SMTP conversation (TLS, authentication), and
a result per recipient. The service is complete in itself, and its measure of quality is how
useful it is to any consumer.

## 2. Hard boundaries (by design)

- **It is a transport.** One instance represents **one** SMTP server, given in the environment
  configuration. A consumer that needs two servers runs two instances. `[D1]`
- **It has no policy.** Nothing that depends on who sends and why: no content templates, no
  addressing rules, no usage quotas. It does not read or rewrite content: HTML goes out as it came
  in — the service is not a sanitiser and does not pretend to be one. It does not decide about
  retries: no queue, no retry, no schedule.
- **It has no memory.** The service is stateless: request, SMTP conversation, response, and
  nothing remains. It follows that there is **no idempotency key** — repeating a request sends a
  second message. Only the consumer knows whether a repeat is intended, because only the consumer
  knows whether the first send counted; §5.4 says when a response settles that unambiguously and
  when it does not. An idempotency key would be a contract extension in the sense of §11. `[D2]`
- **It reaches nothing but the configured server.** No MX or other DNS lookups beyond resolving
  the server's name, no fetching of resources from the network, no mailbox verification. It does
  not receive mail, track delivery or process bounces. It does not sign (DKIM, S/MIME) — that is
  the job of the server or of a separate component.
- **It does not authenticate callers (v1).** The service is meant to run in a trusted network
  neighbourhood (the same container network, not exposed to the outside); the README says so
  explicitly and shows how not to publish the port. API authentication is a possible extension,
  not part of v1. `[D3]`

**The deciding rule when in doubt:** does it follow from the message itself and from mail
standards, or from who sends it and why? The first belongs to the service (form, encoding, the
SMTP conversation, the fidelity of the report); the second belongs to the consumer.

## 3. API conventions

| Endpoint | Role |
| --- | --- |
| `POST /v1/send` | compose and send one message |
| `GET /v1/health` | state of the service and of the SMTP server (§6) |

- Requests and responses are JSON in UTF-8.
- The `Content-Type: application/json` request header is checked **before the body is read**. The
  media type and the value of `charset` are compared case-insensitively; the only parameter
  allowed is `charset=utf-8` (quoted or not, with optional white space around `;`). A missing
  header, any other type or any other parameter, invalid JSON, a byte-order mark at the start of
  the body, or a key repeated within an object (including `headers{}`) is `INVALID_REQUEST`.
- Paths are matched exactly: a query string is ignored, a trailing slash (`/v1/send/`) is
  `NOT_FOUND`. Each path accepts only its listed method; anything else, `HEAD` included, is
  `METHOD_NOT_ALLOWED` with an `Allow` header.
- The response is JSON on error too, **always** — including an unknown path (`NOT_FOUND`, 404), a
  method not allowed (`METHOD_NOT_ALLOWED`, 405) and an unexpected exception (`INTERNAL_ERROR`,
  500). No error leaves the envelope of §5.3.
- Every request to `/v1/send` gets a random `dispatch_id` that cannot be derived from the content.
  It is returned in the response, in every error envelope and in the logs — it is the only
  correlation key between a consumer and the service's log. **Every error envelope** carries a
  fresh `dispatch_id`, whatever the path and method — `NOT_FOUND`, `METHOD_NOT_ALLOWED`, and an
  `INTERNAL_ERROR` raised while handling health included — so there is one envelope shape. Only a
  successful health response has none. `[D33]`
- The `/v1` path is part of the contract (§11).

## 4. Request: `POST /v1/send`

```jsonc
{
  "from":        {"address": "sender@example.org", "name": "Display Name"},   // required; name optional
  "to":          [{"address": "...", "name": "..."}],                          // to/cc/bcc: at least one recipient in total
  "cc":          [...],
  "bcc":         [...],
  "reply_to":    [{"address": "...", "name": "..."}],                          // optional
  "subject":     "...",                                                        // required, may be empty
  "text":        "...",                                                        // text and/or html: at least one
  "html":        "...",
  "inline":      [{"cid": "logo", "content_type": "image/png", "content_base64": "...", "filename": "logo.png"}],
  "attachments": [{"filename": "report.pdf", "content_type": "application/pdf", "content_base64": "..."}],
  "headers":     {"In-Reply-To": "<id@example.org>", "References": "<id@example.org>"}   // optional, §4.5
}
```

### 4.1 Shape

- The shape is **strict**: unknown fields and values of the wrong type are `INVALID_REQUEST`.
- An absent field, `null` and an empty array mean the same thing ("there is none"); so does an
  empty `headers` object. A required field given as `null` is therefore missing
  (`INVALID_REQUEST`).
- An empty string in `text`, `html` or `subject` means "present, empty".
- A `name` that is empty or consists only of characters with the Unicode `White_Space` property is
  treated as absent. `[D35]` Any other `name` is used as given: it is not trimmed, and leading and
  trailing spaces go into the encoding.
- `inline` without `html` is `INVALID_REQUEST`.
- Every text field must be valid Unicode that can be encoded in UTF-8 (a lone surrogate is
  `INVALID_REQUEST`).

### 4.2 Addresses

An address is **always an object** `{address, name?}`. The service does not parse the
`Name <address>` notation, because parsing that syntax is a source of ambiguity and injection.
`[D4]`

`address` must match the RFC 5321 `addr-spec` grammar within ASCII:

- **local part:** a `dot-atom` or a non-empty `quoted-string`, no longer than 64 characters
  counted as written (quotes and quoting backslashes included); `""` is refused;
- **domain:** either labels separated by dots — at least two labels; a label is 1–63 characters of
  letters, digits and hyphens, with no hyphen at either end; the last label is not all digits; no
  trailing dot; the domain is no longer than 255 characters — or an address literal in square
  brackets: a valid IPv4 address without leading zeros, or the tag `IPv6:` (case-insensitive)
  followed by a valid IPv6 address, the forms with an embedded IPv4 address included; a zone
  identifier (`%eth0`) and a `General-address-literal` are refused;
- the whole `addr-spec` is no longer than 254 characters.

Non-ASCII addresses (SMTPUTF8, IDN in Unicode form) are rejected in v1 as `INVALID_ADDRESS`:
supporting them needs negotiation with the server and is outside v1. `[D5]`

`name` may contain any Unicode character except control characters. The service encodes it
according to RFC 2047 and quotes it where the syntax requires.

**Duplicates** are counted only among the envelope recipients (`to`, `cc` and `bcc` together; the
local part compared literally, the domain — an address literal too — case-insensitively, so
`"a"@example.org` and `a@example.org` are two addresses). The same address twice is
`INVALID_REQUEST` pointing at the second occurrence: two `RCPT TO` of the same address give a
server-dependent result, and the per-entry report stops being unambiguous. `[D6]` `from` and
`reply_to` may coincide with recipients (sending to oneself is legitimate). A duplicate within
`reply_to` is also `INVALID_REQUEST`. A `reply_to` with several entries produces one `Reply-To`
header listing them in request order, and does not count towards `MAX_RECIPIENTS`.

The `bcc` list goes **only into the SMTP envelope**, never into headers. When `to` is empty, the
service **does not fabricate** a `To` header. The envelope sender (`MAIL FROM`) is
`from.address`. The message is composed before the conversation and is not rebuilt after it: a
`to` or `cc` recipient rejected at `RCPT TO` stays in the header. `[D7]`

### 4.3 Bodies and parts

- **`text` and `html`** are finished UTF-8 content; the service does not transform them (it does
  not turn newlines into `<br>`, does not add an HTML head, does not strip scripts). The only
  exception is a transport one, and it is documented: line endings (`\n`, `\r`, `\r\n`) are
  normalised to `CRLF`, because the message format requires it. Every "identical content"
  comparison in §10 is meant after this normalisation. Content may contain any Unicode
  character, control characters included — it goes through a transfer encoding, not into headers.
  Empty content is allowed and produces a part with an empty body. `[D8]`
- **`inline[]`** are embedded parts that `html` refers to by `cid:<cid>`. The service **does not
  read the HTML to check that the reference exists** — every `inline[]` entry is attached as a
  part with `Content-ID: <cid>` and `Content-Disposition: inline`, whether the HTML uses it or
  not. `[D9]` `cid` is a non-empty string of RFC 5322 `atext` characters plus `.` and `@`; a
  duplicate within one request (compared case-sensitively) is `INVALID_CONTENT`. `content_type` is
  required here, `filename` optional (without it the part has no name parameter). The
  `multipart/related` part gets the parameter `type="text/html"` (RFC 2387).
- **`attachments[]`** are parts with `Content-Disposition: attachment`. `filename` is required,
  `content_type` optional — when it is absent, the service guesses it from the name's extension
  using a **built-in** table (not the system's tables, because the result must not depend on the
  host), and falls back to `application/octet-stream`. `[D10]` The table covers a few dozen common
  extensions; only the last extension counts, compared case-insensitively; no parameter is added to
  a guessed type (no `charset` for `text/*`). The table never yields `message/*` or `multipart/*`,
  so `.eml`, `.msg` and `.mht` come out as `application/octet-stream`.
- **A file name** (in `attachments[]` and `inline[]`) is a value, not a path. Rejected as
  `INVALID_CONTENT`: a name containing `/` or `\`; a name that is empty or consists only of
  characters with the Unicode `White_Space` property `[D35]`; a name equal to `.` or `..`; a name
  longer than 255 characters. A name that merely contains `..` (`report..pdf`) is valid: without a
  separator it cannot step out of anything. `[D32]` The name goes only into `Content-Disposition`
  (§4.5, folding).
- **`content_type`** (where given) has the strict shape `type/subtype` followed by optional
  `; attribute=value` parameters per RFC 2045, in ASCII, with no comments. Parameters are copied
  verbatim, except `boundary`, `name` and `filename` — in their RFC 2231 forms too (`name*`,
  `filename*0*`, …) — which the service does not accept because it sets structure and names itself
  (`INVALID_CONTENT`). A parameter given twice is `INVALID_CONTENT`. `multipart/*` and `message/*`
  (compared case-insensitively) are rejected — the first because structure is the service's
  business, the second because RFC 2046 does not allow encoding them in `base64`, and the service
  encodes parts only that way (§4.4); a message as an attachment is sent as
  `application/octet-stream`. `[D12]` Any other malformed shape is `INVALID_CONTENT`. The value is
  written as `type/subtype; a=b; c="d"`: type, subtype, parameter names and values as given, one
  `; ` between parameters, and folding allowed only between parameters. The `text` and `html`
  parts get `charset=utf-8` from the service.
- **Binary content** arrives as base64: the standard alphabet with padding. `CR`, `LF` and spaces
  (U+0020) are removed from anywhere before decoding; anything else, a tab included, is
  `INVALID_CONTENT`. Padding is required: input whose length after that removal is not a multiple
  of 4, or with `=` anywhere but at the end, is `INVALID_CONTENT`. Non-zero padding bits (`QR==`)
  are accepted. Empty content (a 0-byte file) is allowed.

### 4.4 MIME structure and transfer encoding

**The MIME structure follows from which fields are present, always the same way:**

| Fields present | Result |
| --- | --- |
| `text` | `text/plain` |
| `html` | `text/html` |
| `html` + `inline` | `multipart/related` (html, inline parts) |
| `text` + `html` [+ `inline`] | `multipart/alternative` (text, html or related) |
| anything + `attachments` | `multipart/mixed` (the above, attachments) |

**The transfer encoding is a rule, not a heuristic.** `[D11]`

- `text` and `html` go as `quoted-printable` when the number of UTF-8 bytes outside the range
  32–126 does not exceed one third of all bytes of the content; otherwise as `base64`. Both counts
  are taken after line-ending normalisation; the numerator does not count `CR` and `LF` (after
  normalisation they occur only as `CRLF`), the denominator counts every byte, `CR` and `LF`
  included. Equality means `quoted-printable`, so **empty content is `quoted-printable`**.
- `inline[]` and `attachments[]` go **always** as `base64`, `text/*` types included.
- `7bit` and `8bit` are never used, so the message is valid whether or not the server announces
  `8BITMIME`, and the service passes neither `BODY=` nor `SMTPUTF8` in `MAIL FROM`.

Multipart bodies have no preamble and no epilogue. The order of parts, their headers and encodings
are deterministic for the same request; the only variable elements are `Date`, `Message-ID` and
the MIME boundaries. `[D13]`

### 4.5 Headers

The service sets the message headers itself, **in a fixed order**: `From`, `To`, `Cc`, `Reply-To`,
`Subject`, `Date`, `Message-ID`, `MIME-Version`, then the headers from `headers{}` in request
order, then the structure's `Content-*` headers. `[D13]`

- `Subject` is always present, with an empty value too.
- The subject and names are encoded per RFC 2047 when they contain anything outside printable
  ASCII, and also when they are ASCII but contain `=?` — otherwise a recipient would decode that
  text as an encoded-word, and it would not read back identical.
- `Date` is an RFC 5322 `date-time` of the moment of composition, in UTC written as `+0000`.
- `Message-ID` has the form `<random-token@domain>`: the token is random and cannot be derived from
  the content; the domain comes from `MESSAGE_ID_DOMAIN` (validated at startup like an address
  domain) and, when that variable is not set, from the sender's domain (an address literal in the
  sender goes into `Message-ID` verbatim). It is returned in the response, because it is the only
  identifier of the message that survives on the recipient's side.

**`headers{}`** adds headers **outside the reserved set** (typically `In-Reply-To`,
`References`, `X-*`). `[D14]`

- **Reserved** (compared case-insensitively): `From`, `Sender`, `To`, `Cc`, `Bcc`, `Reply-To`,
  `Subject`, `Date`, `Message-ID`, `MIME-Version`, `Return-Path`, `Received`, and every
  `Content-*` header. Setting any of them, or two keys differing only in letter case, is
  `INVALID_HEADER`. `[D15]`
- The header name must match RFC 5322 `field-name` (printable ASCII without a colon).
- The value must be ASCII text, non-empty and not consisting of spaces (U+0020) only (encoding
  non-ASCII values in custom headers is not part of v1).
- Values are written **verbatim**, as `Name: ` followed by the value, so leading and trailing
  spaces stay on the wire: the service neither decodes nor re-encodes them (an encoded-word in a
  value stays an encoded-word), and it folds them only at existing white space.

**Folding and line length.** `[D34]`

- Every resulting header line is at most **998** characters without `CRLF` (the hard limit), the
  first line counting together with the name and `: `. The service aims at **78** (RFC 5322
  SHOULD) wherever folding allows it, and never produces a line consisting of white space only.
- Headers that the service encodes itself (`Subject`, names, file names) can always be split
  (encoded-words, RFC 2231 continuations).
- `Content-Disposition` and `Content-Type` fold **between parameters** first
  (`Content-Disposition: attachment;` ⏎ ` filename="…"`).
- In a quoted string that the service writes (an ASCII display name, or `filename="…"`), a `"` is
  written as the quoted pair `\"` (RFC 5322 / RFC 2045 `quoted-string`), and a `\` in a display
  name as `\\`; the escaped form is what counts towards line length.
- An ASCII file name is written as `filename="…"`. RFC 2231 continuations (`filename*0=`,
  `filename*1=`, …) are used only when that single parameter does not fit in 78 on its line. A
  common ASCII name of up to about 60 characters thus keeps the most widely understood form. A
  non-ASCII file name is always `filename*` (`utf-8''…`), with continuations where needed.
- A `headers{}` value folds only at its existing white space, aiming at 78.
- A segment without white space in a `headers{}` value, a `cid`, or a `content_type` parameter that
  cannot fit in 998 is `INVALID_HEADER` pointing at the field. So three 500-character identifiers
  separated by spaces in `References` are valid; one 1000-character string is not.

### 4.6 Control characters

**Checked before the field grammars** (§4.7: step 3 before step 4). `[D16]` No control character
— `CR`, `LF`, the other C0 characters, `DEL`, C1, the tab included — may appear in any field that
ends up in headers or in protocol commands: addresses (`from` and `reply_to` included), names, the
subject, file names, `cid`, `content_type`, and the keys and values of `headers{}`. A violation is
always `INVALID_HEADER` pointing at the field, regardless of which field it occurred in and
whether the field would have passed its grammar (so a tab in `name` is `INVALID_HEADER`, not "a
name of white space only"). The control characters are exactly the Unicode category Cc (C0, `DEL`,
C1). Unicode format characters (category Cf) and the line and paragraph separators U+2028 and
U+2029 (categories Zl, Zp) are not control characters: they are allowed wherever Unicode is allowed,
and are encoded with the rest of the field.

### 4.7 Validation and limits — order

Everything below happens **before a connection is opened**, in this order, and the first error
ends the handling:

1. the content type, then the size of the request body against `MAX_REQUEST_BYTES` — also when the
   request has no `Content-Length` → `INVALID_REQUEST` / `REQUEST_TOO_LARGE`;
2. JSON validity and the strict shape (§4.1: types, unknown fields, no recipient, `inline` without
   `html`, Unicode) → `INVALID_REQUEST`;
3. control characters (§4.6) → `INVALID_HEADER`; step 3 sees the raw value, and the
   `White_Space` rule of §4.1 applies only to fields that passed it — the tab, VT, FF and NEL
   belong to both sets, so a `name` of a single tab is `INVALID_HEADER`, not an absent name;
4. field grammars: addresses → `INVALID_ADDRESS`, then duplicates (§4.2) → `INVALID_REQUEST`,
   custom headers → `INVALID_HEADER`, bodies and parts → `INVALID_CONTENT`;
5. the total number of recipients (`MAX_RECIPIENTS`) → `TOO_MANY_RECIPIENTS`;
6. the size of the composed message (bytes on the wire with `CRLF`, without dot-stuffing; this
   number is returned as `size_bytes`) against `MAX_MESSAGE_BYTES` and — when the last health probe
   is **no older than `HEALTH_CACHE_TTL_SECONDS`** and the server announced `SIZE` with a value —
   against that value; the smaller one wins → `MESSAGE_TOO_LARGE`. Without a fresh probe only the
   service's own limit applies, and a possible refusal by the server reaches the consumer as a
   result of the conversation (§5). `[D19]`

**Which error wins.** `[D36]` Steps are taken in the order above, and within step 4 the categories
in the order given there. Between fields of the same category, a **fixed schema order** decides
— the first failing
field in this order is reported:

1. `from`, `to`, `cc`, `bcc`, `reply_to` (list by list, element by element, `address` before
   `name`), `subject`, `text`, `html`;
2. `inline[]`, element by element, each in the order `cid`, `content_type`, `filename`,
   `content_base64`;
3. `attachments[]`, element by element, each in the order `filename`, `content_type`,
   `content_base64`;
4. `headers{}`, in request order.

Step 2 follows the same order, depth first. A byte-order mark, invalid JSON and a repeated key are
found while parsing, before anything else. Then, for every object, starting with the top level:

1. its unknown keys, in request order — a misspelt field is reported as unknown before its correct
   name is reported as missing;
2. its known fields in schema order, each checked for presence and type, a string also for valid
   Unicode, before the check descends into the field's contents and moves on to the next field.

The rules that span fields come after the walk, in this order: no recipient; neither `text` nor
`html`; `inline` without `html`.

The same order determines `field` and `index` in the envelope (§5.3). It is a rule, not an
example: the same invalid request always yields the same error.

The limit envelopes carry the threshold, the measured value and the source of the threshold
(`limit_source`) in `error{}`, so that the consumer does not have to guess which limit was hit:

- `REQUEST_TOO_LARGE` and `MESSAGE_TOO_LARGE`: `limit_bytes` / `actual_bytes`, with the source
  `max_request_bytes`, `max_message_bytes` or `server_size`. For a request without
  `Content-Length`, `actual_bytes` is the number of bytes read until reading stopped.
- `TOO_MANY_RECIPIENTS`: `limit` / `actual`, with the source `max_recipients`.

## 5. Response

### 5.1 Success (HTTP 200)

```jsonc
{
  "ok": true, "dispatch_id": "...", "message_id": "<...@...>",
  "upstream": {"host": "...", "port": 587, "tls": true, "authenticated": true},
  "recipients": [
    {"address": "a@example.org", "field": "to",  "status": "accepted", "smtp": {"code": 250, "message": "..."}},
    {"address": "b@example.org", "field": "bcc", "status": "rejected", "smtp": {"code": 550, "message": "..."}},
    {"address": "c@example.org", "field": "cc",  "status": "deferred", "smtp": {"code": 450, "message": "..."}}
  ],
  "accepted": 1, "rejected": 1, "deferred": 1, "size_bytes": 12345, "duration_ms": 420
}
```

- `status` is a closed set: `accepted` (the server answered 2xx to `RCPT TO`), `rejected` (5xx — a
  permanent refusal), `deferred` (4xx — a temporary refusal; retrying is the consumer's decision).
- Entries come in the order `to`, `cc`, `bcc`, as in the request.
- The response is a success when the server accepted the message for **at least one** recipient.
  The per-recipient result is always complete and verbatim (the server's code and text), because
  partial acceptance is a normal SMTP state and the consumer must see it without guessing. `[D17]`
- `accepted` means "the server accepted it for onward delivery", not "delivered".
- Once the server has answered 2xx after the end of `DATA`, the message is sent — a broken
  connection afterwards (for example at `QUIT`, whose reply is ignored) does not change the result.
- `duration_ms` is the duration of the conversation: from the start of resolving the server's name
  until the connection is closed — validation and composition are not included.

### 5.2 The SMTP conversation

- The conversation is sequential (`PIPELINING` is only reported in health). The service does not
  send `HELO` as a fallback for `EHLO`. `[D20]`
- When the `EHLO` of the current conversation (after STARTTLS, the second one) announced `SIZE`,
  with or without a value, `MAIL FROM` carries `SIZE=<size_bytes>`, so a server with a hard limit
  refuses at the `mail_from` stage, before the content is sent. `[D19]`
- When the server accepted no recipient, `DATA` **is not sent**. `[D18]`
- The content is sent with dot-stuffing (RFC 5321 §4.5.2): a line starting with `.` gets a second
  `.`. `size_bytes` and `SIZE=` count the message without it (RFC 1870 allows an approximation).
- Multi-line server replies are joined with `\n` without the codes, and decoded from UTF-8 with
  invalid bytes replaced; the code of a multi-line reply is taken from its last line.
- **A reply outside the expected course** — 3xx to `MAIL FROM` or `RCPT TO`, 2xx to `DATA`
  instead of 354, a 1xx, and a 2xx or a 3xx other than 334 in the middle of `AUTH LOGIN` — is
  `UPSTREAM_ERROR` at the current stage. A 4xx or 5xx at **any** step of `AUTH`, between the steps
  of `LOGIN` included, keeps its meaning from the table of §5.5 (`UPSTREAM_TRANSIENT`,
  `UPSTREAM_AUTH`). After a 2xx to `DATA` the content **is not sent** (the server would read it as
  commands). A 3xx to `RCPT TO` fits none of `accepted`/`rejected`/`deferred`, so it ends the
  whole send: that reply goes to `upstream.code`/`message`, and the earlier replies to
  `recipients[]`. `[D39]`
- After a failed conversation the service sends `QUIT` on a best-effort basis, with a short limit
  of its own (1 second), so that it never prolongs a response past the moment its stage failed.
  After `UPSTREAM_TIMEOUT` or a broken connection it sends no `QUIT` at all — the socket has just
  shown that it does not answer. Then the connection is closed.
- `SMTP_TIMEOUT_SECONDS` is the idle limit of every socket operation (not a total per stage, so a
  large body over a slow link does not exceed it as long as data keeps flowing). `[D21]`
- **The `connect` stage has one deadline** of `SMTP_TIMEOUT_SECONDS`, shared by name resolution
  and all the connection attempts together — not a budget per address. When the name resolves to
  several addresses, they are tried in the order the resolver returned them.
  `UPSTREAM_UNREACHABLE` means that every attempt failed within the deadline; `UPSTREAM_TIMEOUT`
  with `stage:"connect"` means that the deadline passed before an attempt was settled. In
  `implicit` mode the TLS handshake that follows is under the idle limit per operation, like any
  other socket operation, and its stage is still `connect`.

### 5.3 Error envelope

```jsonc
{"ok": false, "dispatch_id": "...",
 "error": {"code": "...", "message": "...",
           "field": "to[1].address", "index": 1,                              // validation errors: path and list index
           "upstream": {"stage": "rcpt_to", "code": 550, "message": "..."},   // conversation errors: stage and server reply
           "message_id": "<...@...>", "size_bytes": 12345, "duration_ms": 420, // conversation errors: the composed message
           "recipients": [...]}}                                             // as on success, without the counters; see below
```

"Conversation errors" are every `UPSTREAM_*` code, and an `INTERNAL_ERROR` raised once the message
was composed (below).

- `field` is a path in the request (`from.address`, `to[1].name`, `reply_to[0].address`,
  `inline[0].cid`, `attachments[2].filename`, `headers.X-Foo`, `subject`); `index` is present
  only for list elements. An unknown field is reported at its own path (`to[0].foo`), a missing
  required field at the path where it belongs (`from.address`); `inline` without `html` has
  `field: "inline"`; "no recipient" and "neither `text` nor `html`" have no `field`. The key of a
  custom header follows `headers.` verbatim.
- **Every `UPSTREAM_*` envelope carries `message_id`, `size_bytes` and `duration_ms`.** `[D42]`
  The message is composed by then, and with an unknown outcome (§5.4) its `Message-ID` is the
  only way for the consumer to check whether it arrived after all; `duration_ms` (§5.1) tells
  how long the conversation lasted before it failed.
- **An `INTERNAL_ERROR` envelope carries what was reached when the exception was raised**: once the
  message was composed, `message_id` and `size_bytes`; once the conversation had started, also
  `duration_ms` and `upstream` with the stage reached (`code: null`, a fixed descriptive
  `message` — never the exception's own text), and `recipients[]` by the rule below. Fields that
  were not reached are absent, never `null`. What was reached stays reached until the HTTP response
  is written: after the final 2xx the stage is still `data_end` (§5.4). `[D42]`
- `recipients[]` is present — possibly empty — **exactly when `stage` is `rcpt_to`, `data` or
  `data_end`**, and absent at earlier stages. It lists only the recipients that received a reply;
  a recipient the conversation did not reach is not listed. `recipients[].status` describes **only
  the `RCPT TO` stage**, and there are no counters. `[D38]`
- `upstream.code` is `null` and `message` is descriptive when the server answered nothing (refused
  connection, name resolution, handshake, certificate verification, broken connection, silence),
  or when it did not announce a required capability (STARTTLS, `AUTH`) — `stage` is then the
  logical stage (`starttls`, `auth`).
- With zero accepted recipients, `upstream.code`/`message` is the first reply of the deciding class
  (the first 4xx for `UPSTREAM_TRANSIENT`, the first 5xx for `UPSTREAM_REJECTED`), and the full set
  is in `recipients[]`.
- `stage` is a closed set: `connect` (including the TLS handshake in `implicit` mode), `greeting`,
  `ehlo`, `starttls` (including certificate verification after STARTTLS), `auth`, `mail_from`,
  `rcpt_to`, `data` (from the command until the write of the final `CRLF.CRLF` is issued),
  `data_end` (from that write on: waiting for the final reply, and after it until the HTTP response
  is written, §5.4).

### 5.4 What a response settles, and what it does not

The README says this explicitly. `[D22]`

- **Handed to nobody:** `ok:false` without a `stage`; with a `stage` earlier than `data_end`,
  whatever the code, `INTERNAL_ERROR` included; or with `stage:"data_end"` and a 4xx or 5xx reply
  from the server (`UPSTREAM_TRANSIENT`, `UPSTREAM_REJECTED`) — the server answered, and its answer
  was a refusal.
- **Unknown outcome:** `stage:"data_end"` without a usable reply — `UPSTREAM_TIMEOUT`,
  `UPSTREAM_ERROR` (a broken connection, a reply outside the expected course) or `INTERNAL_ERROR`.
  The server may have accepted the message, and a retry may duplicate it.

The two cases cover every `ok:false` response. They rest on two guarantees of the implementation:

- The stage becomes `data_end` when the write of the final `CRLF.CRLF` is **issued** (§5.6), not
  when a reply arrives, and a failure at an earlier stage — an exception in the service included —
  closes the connection without that write. So an `INTERNAL_ERROR` at an earlier stage never
  followed a final dot.
- **A stage once reached stays in the envelope until the HTTP response is written.** After the
  final 2xx the stage remains `data_end` (the set has nothing beyond it), so an exception raised
  after the server accepted the message — while the response is built, for instance — is an
  `INTERNAL_ERROR` with `stage:"data_end"` and `message_id`: an unknown outcome, never "handed to
  nobody". The message did go out, but an `INTERNAL_ERROR` cannot say "success", and "unknown" is
  the only truth on the safe side.

This is the only SMTP ambiguity that a transport cannot remove, and the consumer must know it when
deciding about a retry (§2). A consumer whose own HTTP timeout runs out meets the same boundary
(§5.6).

### 5.5 Error codes

| Situation | HTTP | `code` |
| --- | --- | --- |
| wrong content type, invalid JSON, BOM, repeated key, unknown field, wrong value type, missing required field, no recipient, duplicate recipient or `reply_to` entry, neither `text` nor `html`, `inline` without `html`, invalid Unicode | 400 | `INVALID_REQUEST` |
| address not matching the grammar | 400 | `INVALID_ADDRESS` |
| control character in a header field, reserved header, bad name or value of a custom header, header that cannot be folded | 400 | `INVALID_HEADER` |
| base64 that cannot be decoded, bad `cid`, bad `content_type` or parameter, `multipart/*`, `message/*`, bad file name | 400 | `INVALID_CONTENT` |
| request body over `MAX_REQUEST_BYTES` | 413 | `REQUEST_TOO_LARGE` |
| recipients over `MAX_RECIPIENTS` | 400 | `TOO_MANY_RECIPIENTS` |
| message over `MAX_MESSAGE_BYTES` or the server's fresh `SIZE` | 413 | `MESSAGE_TOO_LARGE` |
| unknown path | 404 | `NOT_FOUND` |
| method not allowed | 405 | `METHOD_NOT_ALLOWED` |
| unexpected exception | 500 | `INTERNAL_ERROR` |
| server name does not resolve, TCP connection refused or not established | 502 | `UPSTREAM_UNREACHABLE` |
| `SMTP_TIMEOUT_SECONDS` exceeded at any stage (name resolution and a missing greeting included) | 504 | `UPSTREAM_TIMEOUT` |
| any situation in which the TLS rules of §7.2 do not allow continuing: STARTTLS not announced (when required), refused (4xx or 5xx) or failed; TLS handshake failed (`implicit` mode too); certificate fails verification; credentials configured and the session is not under TLS | 502 | `UPSTREAM_TLS` |
| authentication configured and none of the supported mechanisms announced after TLS, or 5xx to `AUTH` | 502 | `UPSTREAM_AUTH` |
| 5xx to `MAIL FROM`, to `DATA`, after the end of `DATA`; or zero accepted recipients and only 5xx to `RCPT TO` | 502 | `UPSTREAM_REJECTED` |
| 4xx to the greeting, `EHLO`, `AUTH`, `MAIL FROM`, `DATA` or after `DATA`; zero accepted recipients with at least one 4xx to `RCPT TO` | 503 | `UPSTREAM_TRANSIENT` |
| 5xx to the greeting or `EHLO`, a code outside the 2xx/4xx/5xx classes, connection closed before the greeting or broken during the conversation before the final 2xx, any other protocol violation | 502 | `UPSTREAM_ERROR` |

- 4xx HTTP errors never open a connection.
- `UPSTREAM_*` errors always carry the stage and the server's verbatim reply when there was one,
  because that is the only diagnostic the consumer has.
- Authentication is the service's configuration, not the caller's fault — hence 502, not 401.
  `[D23]`
- SMTP carries no "when to retry" information, so 503 responses have no `Retry-After` header.
  `[D24]`

### 5.6 When the HTTP client goes away

`/v1/send` has **no hard upper bound on its duration**: the idle limit is renewed by every socket
operation (`[D21]`), and the number of operations grows with the recipients, while the content
takes as long as it takes to flow. A consumer's own HTTP timeout can therefore always run out, and
the consumer then closes the connection. What the service does depends on one moment: **the
issuing of the write of the final `CRLF.CRLF` to the socket**. `[D47]`

- **The client went away before that write** (every stage up to and including `data`): the service
  aborts the conversation — it closes the SMTP socket at once, without `QUIT` and without finishing
  `DATA`. Without the end-of-data marker the server does not accept the message (RFC 5321), so the
  message was handed to nobody, and a retry by the consumer does not duplicate it. A client that is
  gone before the connection is opened gets no connection opened at all.
- **The client went away after that write** (`data_end`): nothing can be taken back. The service
  waits for the server's reply as usual and logs the result. For the consumer this is the unknown
  outcome of §5.4.
- A disconnection noticed before the write is issued aborts; one noticed after it waits. The race
  of one write between the two cannot be removed, as in case 14, where the stage is `data` or
  `data_end` depending on buffers.

The log is then the only trace, so the log event records the outcome **"aborted by the client"** as
a result of its own, with the stage at which it happened — distinct from any failure on the server's
side. In the second case the event carries the server's final reply, as for any send — its codes,
never its text (`[D30]`) — under the same `dispatch_id`.

## 6. `GET /v1/health`

Returns **200 always** (the service is alive; the body says how the server is), without
`dispatch_id`: `[D25]`

```jsonc
{
  "ok": true, "version": "1.2.0", "uptime_seconds": 864,
  "upstream": {
    "host": "...", "port": 587, "tls_mode": "starttls", "auth_configured": true,
    "status": "ok" | "down" | "timeout" | "tls_failed", "checked_age_seconds": 7,
    "capabilities": {"size_max_bytes": 52428800, "starttls": true, "auth_methods": ["PLAIN", "LOGIN"],
                     "eightbitmime": true, "smtputf8": true, "pipelining": true},
    "error": {"stage": "...", "code": null, "message": "..."}     // only when status is not "ok"
  }
}
```

- **The probe** is: connect (in `implicit` mode straight into TLS), `EHLO`, and in the STARTTLS
  modes `STARTTLS` and `EHLO` again (many servers announce different capabilities after STARTTLS
  than before, authentication mechanisms in particular); **no** `AUTH`, no `MAIL FROM`; then
  `QUIT`.
- **The probe has one deadline**, `HEALTH_PROBE_TIMEOUT_SECONDS`, for all of it together: name
  resolution, connection, `EHLO`, `STARTTLS`, `EHLO`, `QUIT` — a health response never comes later
  than that. The idle limit of a send (`SMTP_TIMEOUT_SECONDS`) does not apply to the probe. `[D37]`
- **The measurement ends with the reply to the last `EHLO`** (in the STARTTLS modes, the second
  one), or with the failure that ends the probe earlier; `checked_age_seconds` counts from that
  moment. `QUIT` lies outside the measurement both ways: it is sent on a best-effort basis in
  whatever time the deadline leaves, and neither its failure nor the deadline passing during it
  changes the result or the age of the measurement.
- `capabilities` is present only with `status:"ok"`. `starttls` comes from the first `EHLO`, the
  rest from the last one. `SIZE` without a value, or with zero, means no limit
  (`size_max_bytes: null`, and no threshold in §4.7). `auth_methods` lists every announced
  mechanism, upper-cased, in the order announced; the legacy `AUTH=` form is read too.
- `status` is a closed set:
  - `ok`;
  - `timeout` — exactly: the probe's deadline passed before the measurement ended;
  - `tls_failed` — any situation in which a send would give `UPSTREAM_TLS`; the probe applies the
    same TLS and credential rules as a send, in `implicit` mode to the handshake too;
  - `down` — any other failure: connection, greeting, `EHLO`.
- **With any status other than `ok`, `upstream.error` says why**, in the shape of `upstream` in the
  send envelope (§5.3): `stage`, the server's `code` and `message`, or `code: null` and a
  descriptive `message` when the server said nothing. `[D43]` The password never appears in it in
  any form — exception messages from TLS and authentication libraries are not passed through as
  they are.
- The probe is **lazy**: run on request, no more often than once per `HEALTH_CACHE_TTL_SECONDS`;
  the response carries the age of the measurement. A failed probe is cached like a successful one.
  Concurrent requests that find the cache stale wait for one shared probe.
- `uptime_seconds` and `checked_age_seconds` are integers, rounded down.
- `ok` is `false` for every status other than `ok`.
- Every send is its own fresh conversation, independent of the probe. From the probe's cache a
  send takes only the announced `SIZE`, and only when the measurement is fresh (§4.7).
- The password never appears in health or in the logs, in any form.

## 7. Configuration (environment variables only)

### 7.1 Variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `SMTP_HOST` | the server: a name or an IP address, ASCII | **required** (the only variable without a default) |
| `SMTP_PORT` | the server's port | `587`, in `implicit` mode too (the README says to set `465` then) |
| `SMTP_TLS` | `starttls` (required; without TLS nothing is sent) / `starttls-opportunistic` / `implicit` (TLS from the connection on) / `none` | `starttls` |
| `SMTP_TLS_VERIFY` | certificate verification: the name is checked against `SMTP_HOST`, an IP address against the IP in the certificate; `false` also disables the name check, and `SMTP_CA_FILE` is then ignored | enabled |
| `SMTP_CA_FILE` | an optional own certificate authority, **added** to the system's trusted authorities | — |
| `SMTP_USERNAME`, `SMTP_PASSWORD` (or `SMTP_PASSWORD_FILE`) | authentication (§7.2); no variables = no `AUTH` | — |
| `SMTP_EHLO_NAME` | the name presented in `EHLO`; validated at startup as an RFC 5321 `Domain` (at least one label of letters, digits and hyphens — one label is enough, so the default container hostname passes) or an address literal, with no white space or control characters | the container's hostname |
| `SMTP_TIMEOUT_SECONDS` | the idle limit of every socket operation, and the deadline of the `connect` stage (§5.2); fractions allowed | `60` |
| `MESSAGE_ID_DOMAIN` | the domain in `Message-ID` (§4.5), validated at startup with the whole domain grammar of §4.2 (an address literal included) | the sender's domain |
| `MAX_REQUEST_BYTES` | the request body limit (§4.7) | `33554432` (32 MiB) |
| `MAX_MESSAGE_BYTES` | the composed message limit (§4.7) | `26214400` (25 MiB) |
| `MAX_RECIPIENTS` | `to` + `cc` + `bcc` together (§4.7) | `100` |
| `HEALTH_CACHE_TTL_SECONDS` | how long a probe result is reused (§6); fractions allowed | `10` |
| `HEALTH_PROBE_TIMEOUT_SECONDS` | the deadline of the whole probe (§6); fractions allowed | `5` |
| `PORT` | the HTTP listening port | `8000` |
| `LOG_LEVEL` | the log level | `INFO` |

The defaults are a decision `[D41]`. `SMTP_TIMEOUT_SECONDS` is a compromise: RFC 5321 §4.5.3.2
suggests waiting up to 10 minutes for the reply after `DATA`, while servers scan the content; a
short limit makes an unknown outcome (§5.4) more likely, a long one holds the consumer's HTTP
connection open.

**`SMTP_EHLO_NAME` is a `Domain`, not an address domain.** `[D40]` The rule of §4.2 that the last
label is not all digits does not apply to it: that rule keeps an address domain from being mistaken
for an IPv4 address, and the definition above is complete without it. A default container hostname
(12 hexadecimal characters) is all digits about once in 300 starts; under that rule the image would
fail to start at random. `MESSAGE_ID_DOMAIN` keeps the whole grammar of §4.2, because it ends up in a
header as a domain.

The README warns that a name without a dot in `SMTP_EHLO_NAME` is sometimes refused by servers that
require an FQDN.

### 7.2 TLS and credentials

- TLS no older than 1.2; SNI is `SMTP_HOST` when it is a name.
- **Credentials go only over TLS.** `[D26]` `SMTP_TLS=none` together with credentials is a
  configuration error, and the service refuses to start.
- In `starttls-opportunistic` mode, a server that does not announce STARTTLS means sending in
  plain text only when no credentials are configured; with credentials such a session ends with
  `UPSTREAM_TLS` before `AUTH` — the password never goes in plain text.
- STARTTLS that is **announced** and fails (refusal, handshake, verification) is `UPSTREAM_TLS` in
  every STARTTLS mode — the service never falls back to plain text after a failed TLS attempt.
- Authentication: `PLAIN` (RFC 4616, with an empty authorisation identity, sent as the initial
  response on the `AUTH` line) when announced, otherwise `LOGIN`. One attempt, no switching of
  mechanism after a refusal. `[D27]` The password is UTF-8. From a file, exactly one trailing line
  ending (`\n` or `\r\n`) is removed.

### 7.3 Startup validation

A contradictory or unreadable configuration stops startup with a readable message: `[D28]`
`SMTP_PASSWORD` and `SMTP_PASSWORD_FILE` together; a user without a password or with an empty
password; an unknown TLS mode; an unreadable CA file; a non-ASCII `SMTP_HOST`; an invalid
`SMTP_EHLO_NAME` or `MESSAGE_ID_DOMAIN`; `SMTP_TLS=none` with credentials. Otherwise the image starts
with sensible defaults: only `SMTP_HOST` is needed to run it.

How values are read:

- An empty variable means the same as an unset one.
- Booleans are `true`/`false`/`1`/`0`, case-insensitive.
- Numbers must be greater than zero.
- `SMTP_TLS` takes exactly the values of §7.1, in lower case.
- An IPv6 address in `SMTP_HOST` is written without brackets.
- `SMTP_CA_FILE` is ignored, and not read, when `SMTP_TLS=none` or `SMTP_TLS_VERIFY=false`.

Anything else that cannot be read stops startup too: a value that is not a valid number or boolean,
a port outside 1–65535, a password without a user.

**Shutdown.** On `SIGTERM` the service stops accepting requests and lets the sends in progress
finish, within the server's graceful-shutdown time. The README tells operators to give the
container a stop grace period (`stop_grace_period` / `--stop-timeout`) with a margin over
`SMTP_TIMEOUT_SECONDS`: otherwise a send in progress is killed in the middle of `DATA`, and the
consumer gets a broken HTTP connection with no response — the same kind of uncertainty §5.4
describes for `data_end`, on the HTTP leg.

**Concurrency and memory.** `[D48]` v1 sets no limit on concurrent sends and has no "busy" code.
The consumer controls how many sends run in parallel; the operator bounds memory with the
container's limits. The README gives the memory account of one send in progress, naming the parts
that live at the same time — the request body up to `MAX_REQUEST_BYTES`, the decoded attachments,
the composed message up to `MAX_MESSAGE_BYTES` — because an operator sizes a container from the
ceilings, not from a typical message. It also says what happens when the limit is too tight: a
container killed for exceeding its memory limit leaves an **unknown outcome for every send in
progress** at that moment — the class of §5.4, wholesale. A tight limit does not refuse the
excess; it loses responses.

## 8. Security

- **Header injection** is the only vector by which a consumer could damage somebody else's mail;
  hence the control-character rule for every field that ends up in headers (§4.6), the reserved
  header set (§4.5) and the `content_type` grammar (§4.3). Addresses being objects rather than
  strings to parse is part of the same protection (`[D4]`). The same care applies to
  configuration: `SMTP_EHLO_NAME` goes into a protocol command and is validated (§7.1).
- **Nothing is written to disk:** the message is composed in memory, within `MAX_MESSAGE_BYTES`;
  the container runs without root and with a read-only file system. `[D29]`
- **No traffic other than to the SMTP server** (§2): apart from resolving the server's name, the
  service opens no other connection.
- The service **is not an HTML sanitiser** and does not claim to be one; the README says that
  content goes out verbatim.

## 9. Logging

JSON lines on stdout, no log files; one event line per request; the HTTP server's own access log is
off (it would duplicate that line).

Logs carry `dispatch_id`, the sender, the number of recipients and their domains, the size, the
stage and result of the conversation — the stage, the reply codes (with the enhanced status code,
such as `5.1.1`, when there is one) and the counters — and the duration. They **never** carry the
content, the subject, the attachments, the full recipient lists, the password, or **the text of a
server reply**: SMTP replies often quote a recipient's address (`550 5.1.1 <…>: user unknown`).
The reply text goes only to the HTTP response. `[D30]` A send aborted because the HTTP client went
away is logged as an outcome of its own, with its stage (§5.6). The event of an `INTERNAL_ERROR`
names the stage reached.

## 10. Testing contract

### 10.1 Harness

- Tests use a **fake SMTP server inside the test process** — a server library or a scripted fake
  of its own, because the greeting, silence and connection-break tests need control over every
  reply — which records the envelopes and raw messages it receives.
- No test needs a real server or the network. Name resolution is substituted in tests, so "a name
  that does not resolve" never reaches DNS.
- The fake can answer any code at any stage, a multi-line greeting included; announce or not
  announce STARTTLS/AUTH/SIZE; set up TLS (implicit too) with a certificate generated in the test,
  whose name matches the `SMTP_HOST` used in the test; and stay silent.
- Every case **in which the message reached the fake** ends with an assertion on the raw message
  (parsed independently with the standard library), not only on the HTTP response.
- Settings (limits, TTL, timeouts in fractions of a second) are injected per test; "the service
  does not start" means that loading the configuration ends in an error.
- The read-only file system property concerns the service, not the test harness, which may keep
  certificates in a temporary directory. `[D46]` It is proven twice:
  - the full suite runs in CI inside a `test` stage of the `Dockerfile` (the runtime plus the
    development dependencies) started with `--read-only --tmpfs /tmp --network none` — the
    `tmpfs` is for the harness's certificates, and since the fake listens on loopback, the missing
    network also proves the isolation;
  - a smoke test runs the **release image** with `--read-only` and **without** any `tmpfs`, which
    proves that the service itself needs no writable path at all.
- The fake SMTP server of the example `docker-compose` (§12) is a convenience for local trials, not
  a dependency of the project or of its tests.

### 10.2 Acceptance cases

Case numbers are a shared language for reporting defects, so they never shift; new cases are
appended.

1. `text` only → `text/plain`, content identical after decoding (after line-ending
   normalisation); pure ASCII → `quoted-printable`; content dominated by non-ASCII bytes →
   `base64`; `Date` in the format of §4.5, `Message-ID` per §4.5, `MIME-Version`, `Subject`
   present for `subject:""` too; `size_bytes` equal to the number of §4.7 step 6. **Dot
   transparency:** a content with a line starting with `.` and a line consisting of a single `.`
   comes out identical after decoding — which proves the client's dot-stuffing and its removal on
   the fake's side (§5.2).
2. `html` only → `text/html`; `html` + `inline` → `multipart/related` with `type="text/html"` and a
   part with `Content-ID: <cid>` and inline disposition, image bytes identical to the input;
   `inline` without `filename` → a part without a name parameter.
3. `text` + `html` + `inline` + `attachments` →
   `multipart/mixed(alternative(text, related(html, inline)), attachments)` in this order; the
   `sha256` of every attachment on the fake's side equals the `sha256` of the input; a `text/plain`
   attachment → `base64`; an attachment without `content_type` → the type from the built-in table,
   an unknown extension and `.eml` → `application/octet-stream`; `content_type` with a `boundary`
   parameter → `INVALID_CONTENT`.
4. Non-ASCII subject and `name` → headers encoded per RFC 2047, identical after decoding; a `name`
   of spaces only → as if absent; a non-ASCII file name and a 200-character file name → RFC 2231,
   identical after decoding.
5. `bcc` → the address in the SMTP envelope (`RCPT TO`), **absent** from every header; `cc` → in the
   envelope and in `Cc`; empty `to` → no `To` header; `reply_to` with two entries → one `Reply-To`
   with two addresses in request order; `from` equal to a recipient → success.
6. `headers{}` with `In-Reply-To` and `References` → present verbatim (an encoded-word value too,
   not re-encoded); `References` with three 500-character identifiers separated by spaces →
   accepted and folded at the spaces; one 1000-character string in `References` or in `cid` →
   `INVALID_HEADER`; a reserved header, `Bcc` and `Content-Type` in any letter case included, and
   two keys differing only in letter case → `INVALID_HEADER`; the order of message headers per
   §4.5.
7. `CR`, `LF`, tab or `NUL` in the subject, in `name` (a `name` of a single tab too), in
   `to[0].address`, in a file name, in `cid`, in `content_type` and in a `headers{}` value →
   `INVALID_HEADER` with the right `field` (for an address `INVALID_HEADER`, not
   `INVALID_ADDRESS`), **zero connections** to the fake (asserted on its connection counter).
8. One address breaking each grammar rule of §4.2 (two `@` outside a `quoted-string`, a display
   name written into `address`, a trailing dot, a hyphen at the edge of a label, the length limits
   exceeded among them) → `INVALID_ADDRESS` with `field` and `index`, zero connections; the same
   address in `to` and `bcc` (with a different letter case in the domain) and twice in `reply_to`
   → `INVALID_REQUEST` pointing at the second occurrence; no recipient, neither `text` nor `html`,
   `inline` without `html`, `from` without `address`, an unknown field, a repeated JSON key, a lone
   surrogate → `INVALID_REQUEST`.
9. `content_base64` that cannot be decoded, `content_base64` without its padding, a duplicate
   `cid`, `content_type: "multipart/mixed"` and `"message/rfc822"`, a file name containing `/` or
   `\`, a file name equal to `..` → `INVALID_CONTENT`; a file name that merely contains `..`
   (`report..pdf`) → accepted.
10. Limits: a request over `MAX_REQUEST_BYTES` (also without `Content-Length`; a wrong
    `Content-Type` with a body over the limit → `INVALID_REQUEST`, because the type is checked
    first) and a message over `MAX_MESSAGE_BYTES` → the right code with
    `limit_bytes`/`actual_bytes`/`limit_source`; recipients over `MAX_RECIPIENTS` →
    `TOO_MANY_RECIPIENTS` with `limit`/`actual`/`limit_source`; all without a connection; a message
    over the `SIZE` announced by the fake **after a fresh health probe** → `MESSAGE_TOO_LARGE`
    (`limit_source:"server_size"`) without a connection, and **without** a fresh probe →
    `MAIL FROM` with `SIZE=` and the server's 552 refusal as `UPSTREAM_REJECTED` with
    `stage:"mail_from"`.
11. The fake rejects one of three recipients with 550 and another with 450 → 200, `accepted:1`,
    `rejected:1`, `deferred:1`, codes and texts verbatim, order `to`, `cc`, `bcc`; the rejected
    `cc` recipient still present in the `Cc` header.
12. The fake rejects all recipients with 5xx → `UPSTREAM_REJECTED`, `stage:"rcpt_to"`,
    `recipients[]` without counters, **no `DATA` in the fake's record**; 5xx to the sender / `DATA`
    / after `DATA` → `UPSTREAM_REJECTED` with the right `stage`, and with a refusal after `DATA`,
    `recipients[]` with the results of the `RCPT TO` stage.
13. The fake answers 4xx to the greeting / `MAIL FROM` / all `RCPT TO` / after `DATA` →
    `UPSTREAM_TRANSIENT`, 503, with the stage and the first 4xx in `upstream`; a multi-line 220
    greeting → success; 5xx to `EHLO` → `UPSTREAM_ERROR`; 4xx to `STARTTLS` in `starttls` mode →
    `UPSTREAM_TLS`.
14. A closed port → `UPSTREAM_UNREACHABLE` with `upstream.code: null`; a name that does not resolve
    (substituted resolver) → `UPSTREAM_UNREACHABLE`; the fake closes the connection without a
    greeting → `UPSTREAM_ERROR` with `stage:"greeting"`; the fake breaks the connection while the
    content is being sent → `UPSTREAM_ERROR` with `stage` `data` or `data_end` (both acceptable,
    because they depend on buffers); the fake breaks the connection after the final 2xx → success.
15. The fake stays silent after `EHLO` → `UPSTREAM_TIMEOUT` within `SMTP_TIMEOUT_SECONDS` plus a
    small margin; the fake sends no greeting → `UPSTREAM_TIMEOUT` with `stage:"greeting"`.
16. `SMTP_TLS=starttls` with a fake without STARTTLS → `UPSTREAM_TLS` with `stage:"starttls"` and
    `code:null`, **without** `MAIL FROM`; `starttls-opportunistic` without announced STARTTLS and
    without credentials → sent without TLS; `starttls-opportunistic` with STARTTLS announced but
    refused → `UPSTREAM_TLS`; `starttls` with a certificate outside the trusted set → `UPSTREAM_TLS`
    with `stage:"starttls"`, with `SMTP_CA_FILE` → success; `implicit` with a fake in TLS from the
    connection on → success, and with a failed handshake or a bad certificate → `UPSTREAM_TLS`
    with `stage:"connect"`.
17. Authentication configured, the fake announces `AUTH` only after STARTTLS → success (the service
    reads the `EHLO` after STARTTLS); a fake with `PLAIN` and `LOGIN` → `PLAIN` used; a fake without
    `AUTH` → `UPSTREAM_AUTH` with `stage:"auth"` and `code:null`; a wrong password → `UPSTREAM_AUTH`
    after one attempt, the password absent from the logs and from the response; a fake offering
    only `LOGIN` that answers `535` right after the user-name step → `UPSTREAM_AUTH` with
    `stage:"auth"` and no second attempt; credentials in
    `starttls-opportunistic` mode without TLS → `UPSTREAM_TLS` and **no** `AUTH` in the fake's
    record; `SMTP_TLS=none` with credentials → the service does not start.
18. Health: two calls within the TTL → **one** probe; a fake announcing `SIZE` and `AUTH` after
    STARTTLS → matching `capabilities`, `starttls:true` from the first `EHLO`; `SIZE` without a
    value → `size_max_bytes: null`; the fake switched off → `ok:false`, `status:"down"`, no
    `capabilities`, measurement age no greater than the TTL; credentials configured in
    `starttls-opportunistic` mode and a fake without STARTTLS → `status:"tls_failed"`; `implicit`
    with a bad certificate → `status:"tls_failed"`; the probe sends neither `AUTH` nor `MAIL FROM`.
19. Determinism: the same request twice → identical MIME structure, headers, their order,
    encodings and content; only `Date`, `Message-ID` and the boundaries differ; the two
    `Message-ID`s differ.
20. "No traffic other than to the server": during every other case the only outgoing connections
    go to the fake, and the only name queries concern its name (a substituted resolver and a patch
    on socket opening). "Nothing written to disk": during every other case a patch on opening files
    for writing records no write outside the harness's temporary directory, and the service in a
    container with a read-only file system passes these tests.
21. The envelope always: an unknown path → `NOT_FOUND`, a wrong method → `METHOD_NOT_ALLOWED`, a
    forced exception inside the handling → `INTERNAL_ERROR` with `dispatch_id`, all as JSON per
    §5.3. The forced exception is placed on both sides of the boundary of §5.4:
    (a) before composition → no `message_id`, no `upstream`;
    (b) after composition, during the conversation, before the final write → `message_id`,
    `size_bytes`, `duration_ms` and `upstream.stage` of the stage reached, and the fake's record
    holds no final dot;
    (c) after the write of the final dot is issued, before the reply → `stage:"data_end"`,
    `message_id` present, and the fake's record holds the final dot (the fake may then answer
    `250` and record the message);
    (d) after the final `250`, while the HTTP response is built → `stage:"data_end"`, `message_id`
    present, and the message recorded on the fake's side.
22. The HTTP client goes away (§5.6): (a) the fake stays silent at `RCPT TO` and the HTTP client
    closes its connection after a short time → the fake sees its socket closed and records no
    message, and the log has an "aborted by the client" event with `stage:"rcpt_to"`; (b) the fake
    stays silent **after receiving the final `.`**, the HTTP client closes its connection, and the
    fake then answers `250` with a delay → the fake records the message, and the log has an event
    with the result `250` under the same `dispatch_id`. Together they prove that the boundary lies
    where §5.6 puts it.

### 10.3 Robustness

A separate kind of test: mutated requests (random bytes in base64, very long names, thousands of
recipients, headers with Unicode control characters, lone surrogates in strings, nested objects
where strings belong) never give anything other than a 4xx code in the envelope or a correct send.
In particular they never open a connection before validation and never end in an exception outside
the envelope.

## 11. Versioning

The `/v1` path is part of the contract. `[D31]` Adding a field is a backwards-compatible change.
Changing the meaning of a field, or narrowing or extending a closed value set
(`recipients[].status`, `recipients[].field`, `stage`, `code`, `tls_mode`, `upstream.status` in
health, `limit_source`), is not, and requires `/v2`. The container image carries a version tag;
`latest` is not a contract.

## 12. Delivery

- A public repository: code, a `Dockerfile` on a stock, slim Python image (without root), an
  example `docker-compose` with a fake SMTP server for local trials (Mailpit, which shows the
  accepted messages in a browser and speaks STARTTLS and AUTH), a README with the contract and
  `curl` examples, automated tests, the MIT licence, and this file as the authority on the contract
  with its decision record. `docs/spec-coverage.md` maps every acceptance case to its test.
- The implementation: Python 3.13, managed with uv; FastAPI on uvicorn; settings with
  pydantic-settings; ruff and `mypy --strict`; pytest with coverage of at least 90%. The SMTP client
  is the service's own, on asyncio. `[D44]`
- The image: a two-stage `Dockerfile` that also builds with the classic builder (no BuildKit-only
  syntax); the runtime on **`python:3.13-slim-trixie`** (Debian 13), pinned by its codename rather
  than the moving `python:3.13-slim`, and the build stage on the same Debian release (another glibc
  and other shared libraries under compiled wheels are a classic trap); a non-root user
  (uid 10001); a `HEALTHCHECK` written with the standard library. It is published for amd64 only,
  and runs on **baseline x86-64**, with no x86-64-v2 requirement — so a base image that needs v2 is
  ruled out. `[D45]`
- Release: an annotated version tag, starting at `0.1.0`; the image is published to GHCR by a
  workflow on that tag; a CHANGELOG with the image digest and its Id; **the image archive
  (`docker save` by the full tag, `.tar.gz` + `.sha256`) as a release file**, made by a separate
  job, for hosts without access to the registry.
- Configuration only through environment variables (§7).

The project has to stand on its own: someone who finds it without context should know from the
README what they get, what the service deliberately does not do, and why that boundary is good for
them too.

The README says explicitly, among the rest:

- that callers are not authenticated, and how to run the service without publishing its port (§2);
- which responses settle the outcome and which leave it unknown (§5.4);
- that content goes out verbatim and the service is not an HTML sanitiser (§8);
- that `SMTP_PORT` should be set to `465` for `implicit` mode (§7.1);
- that an `SMTP_EHLO_NAME` without a dot is refused by some servers (§7.1);
- that `/v1/send` has no hard upper bound on its duration (§5.6), so the consumer's own HTTP
  timeout should be generous; that running out of it before `data_end` sends nothing; and that
  `data_end` is the only window of uncertainty (§5.4);
- that the container's stop grace period needs a margin over `SMTP_TIMEOUT_SECONDS` (§7.3);
- the memory account of one send in progress, from the ceilings, and what a container killed for
  memory means for the sends in progress (§7.3).

## 13. Decision record

**Contract**

- **[D1] One instance represents one SMTP server.** A consumer with two servers runs two
  instances; the service never chooses between servers.
- **[D2] The service is stateless and has no idempotency key.** A repeated request sends a second
  message. Only the consumer knows whether the first send counted; §5.4 lists the cases in which
  the response settles it. A key would be a contract extension (§11).
- **[D3] Callers are not authenticated in v1.** The service runs in a trusted network
  neighbourhood; the README shows how not to publish the port.
- **[D4] An address is always an object `{address, name?}`.** Parsing `Name <address>` is a source
  of ambiguity and injection, so the service never does it.
- **[D5] Addresses are ASCII only in v1.** SMTPUTF8 and Unicode IDN need negotiation with the
  server; they are rejected as `INVALID_ADDRESS`.
- **[D6] A recipient repeated in the envelope is rejected.** Two `RCPT TO` of the same address give
  a server-dependent result and make the per-entry report ambiguous.
- **[D7] The message is composed before the conversation and never rebuilt.** A `to`/`cc` recipient
  refused at `RCPT TO` stays in the header; `bcc` never reaches a header; no `To` is fabricated.
- **[D8] Content is never transformed, except line endings normalised to `CRLF`.** The service is
  not a sanitiser.
- **[D9] Every `inline[]` part is attached whether or not the HTML refers to it.** The service does
  not read HTML.
- **[D10] A missing attachment type is guessed from a built-in table only.** The result must not
  depend on the host; the table never yields `message/*` or `multipart/*`.
- **[D11] The transfer encoding is a rule.** `text`/`html`: `quoted-printable` up to one third of
  bytes outside 32–126, `base64` above; parts always `base64`; never `7bit`/`8bit`, so the message
  is valid without `8BITMIME`, and no `BODY=`/`SMTPUTF8` is sent. The denominator counts every byte,
  `CRLF` included, and the numerator leaves `CRLF` out, so empty content is `quoted-printable` —
  no special case.
- **[D12] `multipart/*` and `message/*` are refused as part types.** Structure is the service's;
  RFC 2046 forbids `base64` for `message/*`, so a message travels as `application/octet-stream`.
- **[D13] Composition is deterministic.** Fixed header order and fixed part order; only `Date`,
  `Message-ID` and boundaries vary.
- **[D14] Custom header values are verbatim ASCII, folded only at existing white space.** A segment
  that cannot fit in 998 characters is `INVALID_HEADER`; non-ASCII values are not part of v1.
- **[D15] A fixed set of headers is reserved** (§4.5), compared case-insensitively.
- **[D16] Control characters are checked before any grammar and always yield `INVALID_HEADER`.**
  Header injection is the only way a consumer could damage somebody else's mail.

**Conversation and response**

- **[D17] A send succeeds when at least one recipient is accepted**, and the per-recipient result
  is always complete and verbatim; partial acceptance is a normal SMTP state.
- **[D18] `DATA` is not sent when no recipient was accepted.**
- **[D19] `SIZE` is used twice.** `MAIL FROM` carries `SIZE=` whenever the current `EHLO` announced
  it; a pre-check against the server's limit uses only a fresh health measurement.
- **[D20] The conversation is sequential, and there is no `HELO` fallback.** `PIPELINING` is only
  reported.
- **[D21] The SMTP timeout is an idle limit per socket operation, not a total per stage.** A large
  body over a slow link does not time out while data flows. The `connect` stage is the exception:
  one deadline for name resolution and every connection attempt together, addresses tried in the
  resolver's order; all failing within it is `UPSTREAM_UNREACHABLE`, running out of it is
  `UPSTREAM_TIMEOUT`.
- **[D22] The unknown-outcome cases are named in the contract** — `data_end` without a usable
  reply: `UPSTREAM_TIMEOUT`, `UPSTREAM_ERROR` or `INTERNAL_ERROR` — and the README states them.
  Everything else that is `ok:false` handed the message to nobody: a failure before the
  conversation or at an earlier stage, and a 4xx or 5xx refusal at `data_end`. The stage turns
  `data_end` when the final write is issued and stays there until the response is written, so an
  exception after acceptance reads as unknown, never as "nobody".
- **[D23] An authentication failure is 502, not 401.** It is the service's configuration, not the
  caller's fault.
- **[D24] 503 carries no `Retry-After`.** SMTP does not say when to retry.
- **[D25] Health answers 200 always and probes lazily behind a cache.** The probe never sends
  `AUTH` or `MAIL FROM`; a send takes nothing from it but a fresh `SIZE`.

**Configuration and operation**

- **[D26] Credentials travel only under TLS, and a failed TLS attempt never falls back to plain
  text.** `SMTP_TLS=none` with credentials refuses to start.
- **[D27] Authentication uses `PLAIN` when announced, otherwise `LOGIN`, in one attempt.** No other
  mechanism and no switching after a refusal.
- **[D28] A contradictory or unreadable configuration stops startup** with a readable message; only
  `SMTP_HOST` is required.
- **[D29] Nothing is written to disk.** Non-root, read-only file system.
- **[D30] Logs never carry content, the subject, attachments, full recipient lists, the password
  or the text of a server reply.** A reply often quotes a recipient's address, so logs keep the
  stage, the codes and the counters, and the text goes only to the HTTP response.
- **[D31] `/v1` and the closed value sets are the contract.** Changing a closed set needs `/v2`;
  `latest` is not a contract.

**Settled in clarification, 2026-10-07**

- **[D32] A file name is refused only when it is a path or a path step:** it contains `/` or `\`,
  or it equals `.` or `..`. A name that merely contains `..` cannot step out of anything without a
  separator, so it is valid.
- **[D33] Every error envelope carries a fresh `dispatch_id`**, whatever the path and method, so
  there is one envelope shape; only a successful health response has none.
- **[D34] Headers aim at 78 characters a line and never exceed 998.** Parameters fold between one
  another first; an ASCII file name uses RFC 2231 continuations only when the single parameter
  does not fit in 78, so common names keep the most widely understood `filename="…"` form; custom
  values fold only at their own white space, and never into a line of white space only.
- **[D35] "Spaces only" means the Unicode `White_Space` property** for names and file names, and
  U+0020 for the ASCII values of custom headers — named by the property, so that it does not depend
  on what a language's `isspace` happens to include.
- **[D36] The first error in a fixed schema order wins**, and the same order gives `field` and
  `index`: the same invalid request always gets the same error.
- **[D37] The health probe has one deadline for all of it**, and `timeout` means exactly that the
  deadline passed before the measurement ended; the idle limit of a send does not apply. The
  measurement ends with the reply to the last `EHLO`; the age counts from there, and `QUIT` lies
  outside it both ways — sent in whatever time is left, changing neither the result nor the age.
- **[D38] `recipients[]` in an error envelope is present exactly at the stages `rcpt_to`, `data`
  and `data_end`**, possibly empty, and lists only the recipients that received a reply — the closed
  status set has no "not attempted".
- **[D39] A reply outside the expected course is `UPSTREAM_ERROR` at the current stage.** After a
  2xx to `DATA` the content is not sent, because the server would read it as commands; a 3xx to
  `RCPT TO` ends the send, since it fits none of the recipient statuses. In `AUTH LOGIN` only a 2xx
  or a 3xx other than 334 is out of course; a 4xx or 5xx at any step keeps the meaning of the
  table, so a `535` after the user name is `UPSTREAM_AUTH`, exactly like one after the password.
- **[D40] `SMTP_EHLO_NAME` is an RFC 5321 `Domain`, and the all-digits rule for the last label of
  an address domain does not apply to it.** A default container hostname is all digits about once in
  300 starts, and the image must not fail to start at random. `MESSAGE_ID_DOMAIN` keeps the address
  grammar, because it lands in a header as a domain.
- **[D41] The defaults are fixed** (§7.1). `SMTP_TIMEOUT_SECONDS=60` trades the RFC's long wait
  after `DATA` against how long the consumer's HTTP connection is held.
- **[D42] Every `UPSTREAM_*` envelope carries `message_id`, `size_bytes` and `duration_ms`.** With an
  unknown outcome the `Message-ID` is the only way to check whether the message arrived; a
  backwards-compatible addition. An `INTERNAL_ERROR` carries the same fields for whatever had been
  reached — the composed message, the conversation and its stage — and leaves out, rather than
  nulls, what had not; an exception after the final dot is an unknown outcome too, and must not
  leave the consumer without the identifier. What was reached stays reached until the response is
  written, so an exception after the final 2xx still carries `data_end` and the `Message-ID`.
- **[D43] Health says why it is not `ok`**, in `upstream.error`, with the shape of the send
  envelope's `upstream`; the password never appears in it.
- **[D44] The SMTP client is the service's own, on asyncio, not `smtplib`.** `smtplib` breaks the
  contract in several places: it encodes `AUTH` as ASCII while the password is UTF-8, `login()`
  switches mechanisms after a refusal, name resolution has no time limit, and its helpers fall
  back to `HELO`.
- **[D45] The image runs on baseline x86-64, on Debian 13 (trixie) pinned by codename.** Debian
  builds amd64 for the baseline, so `python:3.13-slim-trixie` meets that; a base image that needs
  x86-64-v2 does not. The build stage runs on the same Debian release as the runtime. The codename
  pins Debian, not Python or the base: the image is rebuilt under the same tag with new 3.13.x
  releases and base fixes, and the release process already records what was shipped (the Image Id
  in the CHANGELOG, the archive as a release file). The proof in CI is the release image running
  on a standard runner — no CPU emulation.
- **[D46] The read-only file system is proven twice:** the suite runs in a `test` image stage with
  `--read-only --tmpfs /tmp --network none`, and the release image runs with `--read-only` and no
  `tmpfs` at all, which proves the service needs no writable path.
- **[D47] When the HTTP client goes away, the issuing of the final `CRLF.CRLF` write decides.**
  Before it, the conversation is aborted and nothing is handed over, so a consumer's retry after
  its own timeout does not duplicate; after it, the service waits and logs the result, and the
  consumer has the unknown outcome of §5.4. The log records an abort by the client as an outcome of
  its own. There is no overall send deadline: it would add unknown outcomes the server never
  caused.
- **[D48] v1 has no limit on concurrent sends and no "busy" code.** The consumer controls
  parallelism, the operator bounds memory with container limits, and the README gives the memory
  account from the ceilings. Refusing under load, under any code, would extend the closed `code`
  set and so needs `/v2` (`[D31]`) — left out on purpose, not overlooked.

## 14. Explicitly out of scope (v1)

Queues, retries and scheduling; idempotency keys; templates and any per-sender policy; HTML
sanitising; DKIM or S/MIME signing; MX lookups and direct delivery; mailbox verification;
receiving mail, delivery tracking and bounce processing; SMTPUTF8 and Unicode domain names;
non-ASCII values in custom headers; API authentication; more than one SMTP server per instance.
