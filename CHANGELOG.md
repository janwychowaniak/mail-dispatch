# Changelog

The release notes are the annotated tag messages; this file carries the same notes where they
can be read without git, together with the image digest and Image Id each version was published
under, and corrections to them.

**A tag is never pushed again, not even to fix its message.** Pushing it runs the release again
and can publish the same version under a different digest, which breaks the one property a
pinned deployment stands on. A wrong line in a tag message is corrected here instead, as an
erratum that says what the tag claims and what is true.

## 0.1.0 — 2026-10-08

The first release of the contract in `docs/SPEC.md`: one email message described in JSON,
composed into an RFC 5322 / MIME message and sent through one configured SMTP server, with a
report of the conversation recipient by recipient. A transport and nothing more: no templates,
no queue, no retry.

All 22 acceptance cases of SPEC §10.2 pass through HTTP against an in-process fake SMTP server,
and every case in which a message reached the server ends with an assertion on the raw message
it received; the robustness requirements of §10.3 pass too. 678 tests, 98% coverage. The suite
also passes inside the image on a read-only file system with no network, and the release image
serves on a read-only file system with no writable mount at all.

Tried end to end against Mailpit v1.31.4 with STARTTLS required and authentication, through the
`compose.yml` of this repository.

The image runs on `python:3.13-slim-trixie` (Debian 13) as uid 10001, for amd64 on baseline
x86-64. Pull it from GHCR, or, on a host that cannot reach the registry, take the archive
attached to this release: check it with `sha256sum -c`, `docker load` it, and compare its Id with
the one recorded in `CHANGELOG.md`.

0.1.0 rather than 1.0.0 on purpose: the `/v1` path already carries the stability promise, and
the contract has not yet been accepted by its consumer on a published image.
