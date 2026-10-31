"""Static file serving for the page: explicit media types and per-path cache headers (ADR 0010).

`StaticAssets` is Starlette's `StaticFiles` with two changes. Media types for the page's file
kinds come from a fixed table rather than the host's `mimetypes` database, which on a slim image
has no `.woff2` entry and would serve fonts as `text/plain`; the global `mimetypes` registry is
left untouched. And every response carries a `Cache-Control`: files under `vendor/` sit in
`<package>@<version>/` paths, so they are cached for a year as immutable; any other file keeps a
stable URL across releases, so browsers revalidate it (`no-cache`, answered by ETag).
"""

import os
from pathlib import PurePath

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

VENDOR_PREFIX = "vendor"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

# Keyed by lower-case suffix; a file with no suffix (a LICENSE) is plain text.
MEDIA_TYPES: dict[str, str] = {
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
    "": "text/plain",
}


def media_type_for(path: str | os.PathLike[str]) -> str | None:
    """Return the media type for a static file, or None to let Starlette guess."""
    return MEDIA_TYPES.get(PurePath(path).suffix.lower())


class StaticAssets(StaticFiles):
    """`StaticFiles` with host-independent media types and a Cache-Control on every file."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """Serve one file with its media type and cache policy, or a 304 that keeps both."""
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            media_type=media_type_for(full_path),
        )
        is_vendored = PurePath(self.get_path(scope)).parts[:1] == (VENDOR_PREFIX,)
        response.headers["Cache-Control"] = IMMUTABLE if is_vendored else REVALIDATE
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
