# Changelog

The release notes are the annotated tag messages; this file carries the same notes where they
can be read without git, together with the image digest and Image Id each version was published
under, and corrections to them.

**A tag is never pushed again, not even to fix its message.** Pushing it runs the release again
and can publish the same version under a different digest, which breaks the one property a
pinned deployment stands on. A wrong line in a tag message is corrected here instead, as an
erratum that says what the tag claims and what is true.

## 0.1.0 — 2026-10-08

`ghcr.io/janwychowaniak/mail-dispatch@sha256:97885c180a3b7fb3ea0d40b2fd4e11bf0613f4c5502a94490a7c36a512338b9d`

Image Id `sha256:fc22e0f5c7022e3436d8745331173b8c030ef3940bee6774b43be7c7b9b3835a`, after a pull
and after `docker load` of the release file alike. Python 3.13.16. The digest was read from the
workflow's push and from a pull of the tag; the image can be pulled without logging in. The
release workflow ran for the first time with this tag, and every step of both jobs passed.

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

The image is built on `python:3.13-slim-trixie` (Debian 13) for amd64 on baseline x86-64, and
runs as uid 10001. Pull it from GHCR, or, on a host that cannot reach the registry, take the
archive attached to this release: check it with `sha256sum -c`, `docker load` it, and compare
its Id with the one recorded in `CHANGELOG.md`.

0.1.0 rather than 1.0.0 on purpose: the `/v1` path already carries the stability promise, and
the contract has not yet been accepted by its consumer on a published image.
