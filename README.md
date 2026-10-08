# mail-dispatch

A self-hosted HTTP microservice that takes a JSON description of **one** email message,
composes a correct RFC 5322 / MIME message from it, sends it through **one** configured SMTP
server, and returns a faithful report of that conversation, recipient by recipient.

You say **what** and **to whom**. The service knows **how**: header encoding, alternative
parts, embedded images, attachments, the SMTP conversation (TLS, authentication), and a result
for every recipient.

The contract is [`docs/SPEC.md`](docs/SPEC.md). Its design decisions are numbered `[D#]`, and
this README cites them.

## What it deliberately does not do

- **No policy.** No templates, no addressing rules, no quotas. Content goes out as it came in;
  the only change is line endings normalised to `CRLF`, which the message format requires.
  **It is not an HTML sanitiser** and does not pretend to be one.
- **No retries, no queue, no memory.** One request, one conversation, one response. There is
  no idempotency key: sending the same request twice sends two messages. Only you know whether
  a repeat is intended, so the decision stays with you, and the response tells you when it is
  safe (see [What a response settles](#what-a-response-settles)).
- **One server per instance.** Two servers mean two instances.
- **No traffic but the SMTP server.** Resolving the server's name is the only lookup: no MX
  lookups, no fetching of remote resources, no mailbox verification, no DKIM or S/MIME
  signing, no receiving, no bounce processing.
- **No caller authentication.** The service belongs on a trusted container network, with its
  port unpublished (see [Running it](#running-it)).

These boundaries keep the service small enough to trust: what it reports is exactly what the
server said, and everything that depends on who sends what and why stays in your code, where
it can be decided properly.

## Quick start: a local trial

[`compose.yml`](compose.yml) runs the service in front of [Mailpit](https://mailpit.axllent.org),
a fake SMTP server that keeps every message it accepts and shows them in a browser. Mailpit
speaks STARTTLS with a certificate it generates for itself and accepts any credentials, so the
trial uses STARTTLS and authentication as a real server would, with certificate verification
switched off. It publishes the service on `127.0.0.1:25587` and Mailpit's UI on
`127.0.0.1:25588`, host ports that no common service takes by default; inside their containers
both keep their usual ports (8000 and 8025).

```sh
docker compose up --build
```

Send a message:

```sh
curl -sS http://127.0.0.1:25587/v1/send \
  -H 'Content-Type: application/json' \
  -d '{
    "from": {"address": "sender@example.org", "name": "Sender"},
    "to": [{"address": "alice@example.org", "name": "Alice"}],
    "bcc": [{"address": "archive@example.org"}],
    "subject": "Quarterly report",
    "text": "Hello Alice,\nthe report is attached.\n",
    "html": "<p>Hello Alice,</p><p>the report is attached.</p>",
    "attachments": [{"filename": "report.txt", "content_base64": "UmVwb3J0Cg=="}]
  }'
```

Then open <http://127.0.0.1:25588> to read it. Mailpit lists `archive@example.org` as a Bcc
recipient because it records the envelope; the message itself carries no `Bcc` header.

Check the service and the server:

```sh
curl -sS http://127.0.0.1:25587/v1/health
```

## The API

| Endpoint | Role |
| --- | --- |
| `POST /v1/send` | compose and send one message |
| `GET /v1/health` | the state of the service and of the SMTP server |

Requests and responses are JSON in UTF-8. `POST /v1/send` needs
`Content-Type: application/json` (optionally with `charset=utf-8`). Every error, an unknown
path or a wrong method included, is a JSON envelope with a fresh `dispatch_id`, which is also
in the service's log line for that request.

### `POST /v1/send`

```jsonc
{
  "from":        {"address": "sender@example.org", "name": "Display Name"},   // required; name optional
  "to":          [{"address": "a@example.org", "name": "A"}],                   // to + cc + bcc: at least one recipient
  "cc":          [{"address": "b@example.org"}],
  "bcc":         [{"address": "c@example.org"}],                                // envelope only, never a header
  "reply_to":    [{"address": "replies@example.org"}],
  "subject":     "required, may be empty",
  "text":        "text and/or html: at least one",
  "html":        "<p>…<img src=\"cid:logo\"></p>",
  "inline":      [{"cid": "logo", "content_type": "image/png", "content_base64": "…", "filename": "logo.png"}],
  "attachments": [{"filename": "report.pdf", "content_type": "application/pdf", "content_base64": "…"}],
  "headers":     {"In-Reply-To": "<id@example.org>", "References": "<id@example.org>"}
}
```

- An address is always an object `{address, name?}`; `"Name <address>"` is never parsed
  `[D4]`. Addresses are ASCII in v1 `[D5]`.
- The structure follows from the fields: `text`, `html`, `html` + `inline` (related),
  `text` + `html` (alternative), and anything + `attachments` (mixed). Names, subjects and file
  names are encoded as the standards require; every header line stays within 998 characters
  `[D34]`.
- A missing attachment `content_type` is guessed from the file name's extension with a
  built-in table, falling back to `application/octet-stream` `[D10]`.
- `headers{}` adds headers outside the reserved set (`From`, `To`, `Subject`, `Content-*`,
  and the rest of §4.5); values are ASCII and written verbatim `[D14]` `[D15]`.
- Control characters in anything that becomes a header are refused, so a request cannot
  inject headers into somebody else's mail `[D16]`.

A success (HTTP 200) means the server accepted the message for **at least one** recipient
`[D17]`, and reports every recipient:

```jsonc
{
  "ok": true, "dispatch_id": "…", "message_id": "<…@example.org>",
  "upstream": {"host": "smtp.example.org", "port": 587, "tls": true, "authenticated": true},
  "recipients": [
    {"address": "a@example.org", "field": "to",  "status": "accepted", "smtp": {"code": 250, "message": "2.1.5 OK"}},
    {"address": "b@example.org", "field": "cc",  "status": "rejected", "smtp": {"code": 550, "message": "5.1.1 …"}},
    {"address": "c@example.org", "field": "bcc", "status": "deferred", "smtp": {"code": 450, "message": "4.2.0 …"}}
  ],
  "accepted": 1, "rejected": 1, "deferred": 1, "size_bytes": 12345, "duration_ms": 420
}
```

`accepted` means accepted for onward delivery, not delivered. `deferred` is a temporary
refusal: retrying that recipient is your decision. `message_id` is the one identifier that
survives on the recipient's side.

An error has one shape:

```jsonc
{"ok": false, "dispatch_id": "…",
 "error": {"code": "UPSTREAM_REJECTED", "message": "…",
           "field": "to[1].address", "index": 1,                              // validation errors
           "upstream": {"stage": "rcpt_to", "code": 550, "message": "…"},      // conversation errors
           "message_id": "<…>", "size_bytes": 12345, "duration_ms": 420,
           "recipients": [ … ]}}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | wrong content type, invalid JSON, wrong shape, duplicate recipient, no recipient, neither `text` nor `html` |
| 400 | `INVALID_ADDRESS` | an address outside the grammar |
| 400 | `INVALID_HEADER` | a control character in a header field, a reserved or malformed custom header, a header that cannot be folded |
| 400 | `INVALID_CONTENT` | bad base64, `cid`, content type or file name |
| 400 | `TOO_MANY_RECIPIENTS` | over `MAX_RECIPIENTS` |
| 413 | `REQUEST_TOO_LARGE` | the body over `MAX_REQUEST_BYTES` |
| 413 | `MESSAGE_TOO_LARGE` | the composed message over `MAX_MESSAGE_BYTES` or the server's `SIZE` |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | unknown path / method |
| 500 | `INTERNAL_ERROR` | an unexpected exception |
| 502 | `UPSTREAM_UNREACHABLE` | the name does not resolve, or no connection can be made |
| 504 | `UPSTREAM_TIMEOUT` | `SMTP_TIMEOUT_SECONDS` passed at some stage |
| 502 | `UPSTREAM_TLS` | TLS cannot be used as configured |
| 502 | `UPSTREAM_AUTH` | authentication failed — the service's configuration, not your fault `[D23]` |
| 502 | `UPSTREAM_REJECTED` | a permanent refusal (5xx) |
| 503 | `UPSTREAM_TRANSIENT` | a temporary refusal (4xx); no `Retry-After`, since SMTP gives none `[D24]` |
| 502 | `UPSTREAM_ERROR` | a protocol violation or a broken connection |

A 4xx never opens a connection: everything is validated before the conversation starts, and
the same invalid request always yields the same error `[D36]`.

### What a response settles

`[D22]` Every `ok: false` falls in one of two cases:

- **Handed to nobody** — no `stage`; a `stage` earlier than `data_end`, whatever the code; or
  `data_end` with a 4xx or 5xx from the server, which answered with a refusal. Retrying does
  not duplicate the message.
- **Unknown outcome** — `stage: "data_end"` without a usable reply: `UPSTREAM_TIMEOUT`,
  `UPSTREAM_ERROR` or `INTERNAL_ERROR`. The server may have accepted the message, and a retry
  may duplicate it. Use `message_id` to check whether it arrived.

`data_end` begins when the service issues the final `CRLF.CRLF` of the message, and it is the
only window of uncertainty SMTP leaves. No transport can remove it.

### Timeouts, and clients that go away

`/v1/send` has **no hard upper bound on its duration**: `SMTP_TIMEOUT_SECONDS` is an idle limit
per socket operation, so a large message on a slow link keeps going as long as data flows
`[D21]`. Give your own HTTP client a generous timeout.

If your client gives up anyway `[D47]`:

- **before `data_end`**, the service aborts the conversation at once. Without the end-of-data
  marker the server accepts nothing, so the message went to nobody and a retry is safe;
- **after it**, nothing can be taken back. The service waits for the server's reply and logs
  it under the same `dispatch_id`. For you, this is the unknown outcome above.

### `GET /v1/health`

Always 200 while the service is alive; the body says how the server is `[D25]`:

```jsonc
{"ok": true, "version": "0.1.0", "uptime_seconds": 864,
 "upstream": {"host": "smtp.example.org", "port": 587, "tls_mode": "starttls", "auth_configured": true,
              "status": "ok", "checked_age_seconds": 7,
              "capabilities": {"size_max_bytes": 52428800, "starttls": true, "auth_methods": ["PLAIN", "LOGIN"],
                               "eightbitmime": true, "smtputf8": true, "pipelining": true}}}
```

The probe connects and sends `EHLO` (and `STARTTLS` and `EHLO` again), never `AUTH` or
`MAIL FROM`. It runs on request, at most once per `HEALTH_CACHE_TTL_SECONDS`, under one deadline
of `HEALTH_PROBE_TIMEOUT_SECONDS` `[D37]`. `status` is `ok`, `down`, `timeout` or `tls_failed`.
Any status but `ok` comes with `upstream.error` saying why `[D43]`. A fresh `SIZE` from the
probe lets `/v1/send` refuse an oversized message before connecting `[D19]`.

## Running it

The image runs as an unprivileged user (uid 10001) on a read-only file system: nothing is
written to disk `[D29]`. Configuration comes only from environment variables, and only
`SMTP_HOST` is required. A contradictory or unreadable configuration stops startup with a
message naming the variable `[D28]`.

**Callers are not authenticated `[D3]`**, so do not publish the port. Put the service on the
network of the containers that use it, and reach it by name:

```yaml
services:
  app:
    image: your-app
    environment:
      MAIL_DISPATCH_URL: http://mail-dispatch:8000
  mail-dispatch:
    image: ghcr.io/janwychowaniak/mail-dispatch:0.1.0
    environment:
      SMTP_HOST: smtp.example.org
      SMTP_USERNAME: sender@example.org
      SMTP_PASSWORD_FILE: /run/secrets/smtp_password
    secrets: [smtp_password]
    read_only: true
    stop_grace_period: 75s
    # no "ports:" section — nothing outside this network reaches it
secrets:
  smtp_password:
    file: ./smtp_password
```

- **Stop grace period.** On `SIGTERM` the service stops accepting requests and lets the sends
  in progress finish. Give the container a grace period with a margin over
  `SMTP_TIMEOUT_SECONDS` (`stop_grace_period`, `docker run --stop-timeout`). Otherwise a send
  killed in the middle of `DATA` leaves your client with a broken connection and the same
  uncertainty as `data_end`.
- **Memory.** There is no limit on concurrent sends and no "busy" code `[D48]`: you control
  how many sends run in parallel, and container limits bound the memory. Size from the
  ceilings, not from a typical message. A send in progress holds, at its peak, the request or
  its parsed form (up to `MAX_REQUEST_BYTES`), the decoded attachments, and the composed
  message being built (up to `MAX_MESSAGE_BYTES`). With the defaults (32 MiB request, 25 MiB
  message), one send at the ceilings measured about 80–100 MiB on top of an idle process of
  about 40 MiB. Count roughly **4 × `MAX_MESSAGE_BYTES` per send in progress** plus the idle
  process. A limit that is too tight does not refuse the excess; it loses responses. A
  container killed for memory leaves an **unknown outcome for every send in progress** at that
  moment.
- **Logs** are JSON lines on stdout, one per request: `dispatch_id`, the sender, the number
  and domains of recipients, the size, the stage, the reply codes (with enhanced codes such as
  `5.1.1`) and the counters. Never the content, the subject, the recipient list, the password,
  or the text of a server reply, which often quotes an address `[D30]`.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `SMTP_HOST` | the server: a name or an IP address (IPv6 without brackets) | **required** |
| `SMTP_PORT` | the server's port — **set `465` for `implicit`** | `587` |
| `SMTP_TLS` | `starttls` (TLS required) / `starttls-opportunistic` / `implicit` / `none` | `starttls` |
| `SMTP_TLS_VERIFY` | certificate verification (`true`/`false`/`1`/`0`) | `true` |
| `SMTP_CA_FILE` | an own certificate authority, added to the system's | — |
| `SMTP_USERNAME`, `SMTP_PASSWORD` or `SMTP_PASSWORD_FILE` | authentication; none set means no `AUTH` | — |
| `SMTP_EHLO_NAME` | the name presented in `EHLO` | the container's hostname |
| `SMTP_TIMEOUT_SECONDS` | the idle limit of every socket operation; the deadline of connecting | `60` |
| `MESSAGE_ID_DOMAIN` | the domain in `Message-ID` | the sender's domain |
| `MAX_REQUEST_BYTES` | the request body limit | `33554432` (32 MiB) |
| `MAX_MESSAGE_BYTES` | the composed message limit | `26214400` (25 MiB) |
| `MAX_RECIPIENTS` | `to` + `cc` + `bcc` | `100` |
| `HEALTH_CACHE_TTL_SECONDS` | how long a probe result is reused | `10` |
| `HEALTH_PROBE_TIMEOUT_SECONDS` | the deadline of the whole probe | `5` |
| `PORT` | the HTTP port | `8000` |
| `LOG_LEVEL` | the log level | `INFO` |

[`.env.example`](.env.example) lists them all with comments. Some notes:

- Credentials travel only under TLS: `SMTP_TLS=none` with credentials refuses to start, and a
  failed TLS attempt never falls back to plain text `[D26]`. Authentication uses `PLAIN` when
  the server announces it, otherwise `LOGIN`, in one attempt, with a UTF-8 password `[D27]`.
  From `SMTP_PASSWORD_FILE`, exactly one trailing line ending is removed.
- An `SMTP_EHLO_NAME` without a dot (such as the default container hostname) is accepted by
  the service but refused by some servers that require a fully qualified name. Set it when
  your server is one of them.
- `SMTP_TIMEOUT_SECONDS` is a trade-off `[D41]`: servers may take a long time after `DATA`
  while they scan the content, and a short limit makes an unknown outcome more likely; a long
  one holds your HTTP connection open longer.
- The size limits are not the size of an attachment. An attachment travels as base64 with a
  line break every 76 characters, about 37% larger than the file (× 4/3 × 78/76), so with the
  default 25 MiB message the largest single attachment is about 18 MiB. For attachments of
  N MiB, set `MAX_MESSAGE_BYTES` to about 1.4 × N MiB plus the rest of the message, and
  `MAX_REQUEST_BYTES` at least as high: the request carries the same base64 without the line
  breaks.

## Development

Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest -q --cov --cov-fail-under=90
```

The tests need no network and no real server: a scripted SMTP server runs inside the test
process, and name resolution is substituted. [`docs/spec-coverage.md`](docs/spec-coverage.md)
maps every acceptance case of the specification to its tests. The same suite runs inside the
image's `test` stage on a read-only file system with no network:

```sh
docker build --target test -t mail-dispatch:test .
docker run --rm --read-only --tmpfs /tmp --network none mail-dispatch:test
```

Secret scanning runs as git hooks and in CI. Activate the hooks once per clone (the setting
is local and does not travel with a clone):

```sh
git config core.hooksPath .githooks
```

## Versioning

The `/v1` path is part of the contract. Adding a field is backwards-compatible; changing a
field's meaning or a closed set of values (`status`, `stage`, `code`, …) requires `/v2`
`[D31]`. Images are published as `ghcr.io/janwychowaniak/mail-dispatch:<version>`; `latest` is
not a contract.

## License

[MIT](LICENSE)
