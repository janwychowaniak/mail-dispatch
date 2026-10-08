# CLAUDE.md — mail-dispatch

## Hard constraints (never violate)

- **A transport, nothing more.** One instance, one SMTP server `[D1]`. No templates, no
  per-sender rules, no quotas, no queue, no retry, no schedule. Content goes out as it came in;
  the only transformation is line endings normalised to `CRLF` `[D8]`. See `docs/SPEC.md` §2.
- **Stateless.** Request, conversation, response, and nothing remains. No idempotency key `[D2]`.
- **No traffic other than the configured server.** Resolving its name is the only lookup: no MX,
  no fetching, no mailbox verification. The test suite runs offline, with name resolution
  substituted (§10.1).
- **Nothing is written to disk** `[D29]`. The message is composed in memory; the container runs
  as uid 10001 on a read-only file system.
- **Logs never carry** the content, the subject, attachments, full recipient lists, the
  password, or **the text of a server reply** — replies quote recipients' addresses `[D30]`.
  Stage, reply codes, counters and `dispatch_id` may be logged.
- **The password never appears anywhere** — not in a response, not in health, not in a log, in
  no form. Exception messages from TLS and authentication code are never passed through as they
  are; the envelope carries a fixed descriptive message instead.
- **Every error leaves through the envelope of §5.3**, with a fresh `dispatch_id` `[D33]` —
  unknown paths, wrong methods and unexpected exceptions included.
- **Closed value sets stay closed** `[D31]`: `recipients[].status`, `recipients[].field`,
  `stage`, `code`, `tls_mode`, health's `upstream.status`, `limit_source`. Extending one is a
  `/v2` change, not a commit.
- **The SMTP client is our own, on asyncio, never `smtplib`** `[D44]`.
- **Test material is synthetic only**: RFC 2606 domains (`example.org`), generated certificates,
  the in-process fake server. No real mailboxes, no real servers.

## Source of truth

`docs/SPEC.md` is the specification, **APPROVED** on 2026-10-08. Decisions are numbered `[D#]`
there and cited from code comments and test docstrings; acceptance cases are numbered in §10.2.
Defects are reported against those numbers.

The contract changes only by the maintainer's decision, recorded in SPEC: an existing `[D#]` is
extended or a new one is added; decision numbers are never reused, and **acceptance-case numbers
never shift** — new cases or variants are appended. After every change to SPEC, check it
mechanically: every `[D#]` that is referenced is defined, and no `§` reference dangles.

`docs/spec-coverage.md` maps every acceptance case to its test (§12); a case without a test is a
promise nobody checks.

## Language

All repository content is **English**: code, comments, docstrings, documentation, commit
messages, PR descriptions. Conversation with the maintainer may be in Polish; that never changes
the English-only rule for the repository.

## Toolchain

Python 3.13, `uv` with a committed `uv.lock`, hatchling, `src/` layout. FastAPI on plain
`uvicorn` (no `[standard]` extras), pydantic v2 and pydantic-settings, ruff at line length 100,
`mypy --strict`, pytest with coverage of at least 90% `[D44]`.

## Development

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest -q --cov --cov-fail-under=90   # what CI gates on
```

Activate the secret-scanning hooks once per clone: `git config core.hooksPath .githooks`.
Scanning runs in four layers — the three hooks in `.githooks/` and
`.github/workflows/gitleaks.yml`, the only one that cannot be bypassed.

**Tests.** The fake SMTP server lives inside the test process and can answer anything at any
stage, or stay silent (§10.1). Every case in which a message reached the fake ends with an
assertion on the raw message, parsed independently with the standard library — not only on the
HTTP response. **Every assertion must be able to fail**: break the behaviour a test describes
and confirm it goes red.

## Release

Rules from §12, in force from the first tag (`0.1.0`); the release workflow lands with the
image:

- an **annotated** `v*` tag equal to `pyproject.version` (and `mail_dispatch.__version__`, which
  a test keeps in step);
- the workflow publishes `ghcr.io/janwychowaniak/mail-dispatch:<version>` for amd64; `latest`
  is not a contract;
- a separate job attaches the image archive (`docker save` by the full tag, `.tar.gz` +
  `.sha256`) to the release, for hosts without access to the registry;
- `CHANGELOG.md` records the image digest and its Id;
- **a tag is never pushed again**, not even to fix its message — that can publish the same
  version under another digest. A wrong line is corrected in `CHANGELOG.md` as an erratum.

## Key decisions

The full record is `docs/SPEC.md` §13. The ones most likely to be "improved" by accident:

- **[D22] / [D47]** the issuing of the final `CRLF.CRLF` write is the one boundary that matters.
  The stage becomes `data_end` when that write is issued, not when a reply arrives, and stays
  there until the HTTP response is written. Before it, a client that went away aborts the
  conversation; after it, the service waits for the reply and logs it.
- **[D11]** the transfer encoding is a rule, not a heuristic: `text`/`html` are
  `quoted-printable` up to one third of bytes outside 32–126, `base64` above; parts are always
  `base64`; `7bit`/`8bit` are never used.
- **[D13]** composition is deterministic — fixed header order, fixed part order; only `Date`,
  `Message-ID` and boundaries vary.
- **[D16]** control characters are checked before any grammar and always give `INVALID_HEADER`.
- **[D36]** the first error in the fixed schema order of §4.7 wins; the same invalid request
  always gets the same error.
- **[D40]** `SMTP_EHLO_NAME` is a `Domain` without the all-digits rule; a default container
  hostname must never fail startup at random.
