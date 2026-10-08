"""The built-in table that names a missing attachment type (SPEC §4.3) [D10].

Built in rather than read from the host, so the result never depends on where the service
runs. Only the last extension counts, compared case-insensitively; nothing here is
`message/*` or `multipart/*`, so `.eml`, `.msg` and `.mht` fall through to
`application/octet-stream`. No parameter is ever added.
"""

from __future__ import annotations

from typing import Final

FALLBACK: Final = "application/octet-stream"

_TYPES: Final[dict[str, str]] = {
    # text
    "txt": "text/plain",
    "log": "text/plain",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "htm": "text/html",
    "html": "text/html",
    "css": "text/css",
    "md": "text/markdown",
    "ics": "text/calendar",
    "vcf": "text/vcard",
    # documents
    "pdf": "application/pdf",
    "rtf": "application/rtf",
    "json": "application/json",
    "xml": "application/xml",
    "js": "text/javascript",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "epub": "application/epub+zip",
    # archives
    "zip": "application/zip",
    "gz": "application/gzip",
    "tgz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar",
    # images
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "ico": "image/vnd.microsoft.icon",
    "heic": "image/heic",
    "avif": "image/avif",
    # audio and video
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
}


def guess(filename: str) -> str:
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem:
        return FALLBACK
    return _TYPES.get(extension.lower(), FALLBACK)
