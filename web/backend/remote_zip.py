# -*- coding: utf-8 -*-
"""Read selected members of a remote ZIP over HTTP range requests.

The ONSPD ships as one 246 MB zip of per-postcode-area CSVs.  A design needs
one area, and a 246 MB download to read 1.8 MB of it is the wrong shape --
especially for a query-time path, where the whole point of the module is that
nothing is downloaded until an operator asks.  The object store behind the ONS
publish serves ``Accept-Ranges: bytes``, so the zip's central directory is
enough to find a member's offset and the member is then pulled on its own.

Deliberately tiny and dependency-free (stdlib ``urllib`` only, no
``remotezip``/``requests``): it is 200 lines of a spec, it has no failure modes
of its own beyond the ones the caller has to handle anyway, and it is
testable against a zip built in a ``tmp_path`` with a ``file://`` opener.

NOT a general zip reader.  Stored members (method 0) and deflated members
(method 8) are handled, which is all a published data archive uses; encryption,
ZIP64 record fields and multi-disk archives are refused rather than
half-supported, and each refusal says so.
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["ZipEntry", "zip_entries", "read_member", "RemoteZipError"]

# Fetch window used to locate the End Of Central Directory record. 22 bytes is
# the record; the comment may push it back by up to 64 KiB, and a leading
# archive comment longer than that makes the file unreadable by any reader.
_EOCD_WINDOW = 66_000
_EOCD = b"PK\x05\x06"
_CENTRAL = b"PK\x01\x02"
_LOCAL = b"PK\x03\x04"

# Central directory file header, after its 4-byte signature: 6 H, 3 I, 5 H, 2 I.
_CENTRAL_FMT = "<HHHHHHIIIHHHHHII"
_CENTRAL_LEN = struct.calcsize(_CENTRAL_FMT)          # 42
# Local file header, after its 4-byte signature. A DIFFERENT and shorter struct
# than the central one -- same fields, fewer of them (no version-made-by, no
# disk/attribute/offset) -- so it gets its own format rather than reusing the
# central header's by accident.
_LOCAL_FMT = "<HHHHHIIIHH"
_LOCAL_LEN = 4 + struct.calcsize(_LOCAL_FMT)          # 30, signature included
_EOCD_FMT = "<IHHHHIIH"
_EOCD_LEN = struct.calcsize(_EOCD_FMT)                 # 22


class RemoteZipError(RuntimeError):
    """The remote file is not a zip this reader can handle."""


class ZipEntry(object):
    """One member of a remote zip, located but not yet read."""

    __slots__ = ("name", "method", "compress_size", "file_size", "header_offset")

    def __init__(self, name: str, method: int, compress_size: int,
                 file_size: int, header_offset: int) -> None:
        self.name = name
        self.method = method
        self.compress_size = compress_size
        self.file_size = file_size
        self.header_offset = header_offset

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return ("ZipEntry(%r, method=%d, %d -> %d bytes)"
                % (self.name, self.method, self.compress_size, self.file_size))


def _default_open(url: str, start: int, end: Optional[int]) -> bytes:
    """GET `url` byte range [start, end] (inclusive). Overridable in tests."""
    import urllib.request

    last = "" if end is None else str(end)
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{last}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _content_length(url: str, opener: Callable) -> int:
    """Total size, from a Range request's own ``Content-Range`` when offered.

    A HEAD is avoided on purpose: the publishing host redirects to a
    pre-signed object URL, and following that redirect for a HEAD and then
    again for the body wastes the signature for no gain.  A ``Range: bytes=0-0``
    answers with the total in ``Content-Range`` on the same redirect chain the
    real reads will use.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            cr = resp.headers.get("Content-Range") or ""
            resp.read(1)
    except urllib.error.HTTPError as exc:              # 200 == ranges ignored
        cr = exc.headers.get("Content-Range") or "" if exc.headers else ""
        exc.close()
    except Exception as exc:  # noqa: BLE001
        raise RemoteZipError(f"cannot reach {url}: {exc}") from exc
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1].strip())
        except ValueError:
            pass
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            n = resp.headers.get("Content-Length")
        return int(n) if n else 0
    except Exception as exc:  # noqa: BLE001
        raise RemoteZipError(f"cannot size {url}: {exc}") from exc


def _parse_central(blob: bytes) -> List[ZipEntry]:
    """Central directory entries, given the bytes of the directory itself.

    The "local header offset" each entry carries is already relative to the
    start of the archive, not to the directory, so it is used as-is.
    """
    out: List[ZipEntry] = []
    j = 0
    while j + _CENTRAL_LEN + 4 <= len(blob) and blob[j:j + 4] == _CENTRAL:
        f = struct.unpack(_CENTRAL_FMT, blob[j + 4:j + 4 + _CENTRAL_LEN])
        method, csize, usize = f[3], f[7], f[8]
        name_len, extra_len, comment_len = f[9], f[10], f[11]
        offset = f[15]
        name = blob[j + 4 + _CENTRAL_LEN: j + 4 + _CENTRAL_LEN + name_len]
        entry_name = name.decode("utf-8", "replace")
        if method not in (0, 8):
            raise RemoteZipError(
                f"{entry_name}: compression method {method} is not supported "
                "(only stored 0 and deflate 8)")
        if 0xFFFFFFFF in (csize, usize, offset):
            raise RemoteZipError(f"{entry_name}: ZIP64 archive is not supported")
        out.append(ZipEntry(entry_name, method, csize, usize, offset))
        j += 4 + _CENTRAL_LEN + name_len + extra_len + comment_len
    return out


def zip_entries(url: str, opener: Optional[Callable] = None) -> List[ZipEntry]:
    """Every member of the remote zip, located but not read.

    Two requests whatever the archive holds: the tail, to find the directory's
    offset, then the directory itself.
    """
    open_ = opener or _default_open
    total = _content_length(url, open_)
    if total <= 0:
        raise RemoteZipError(f"{url}: server did not report a size")
    tail_start = max(0, total - _EOCD_WINDOW)
    tail = open_(url, tail_start, total - 1)
    pos = tail.rfind(_EOCD)
    if pos < 0:
        raise RemoteZipError(
            f"{url}: no end-of-central-directory record in the last "
            f"{len(tail)} bytes (encrypted, multi-disk, or not a zip)")
    _sig, _d1, _d2, _n_disk, _n_total, cd_size, cd_offset, _clen = struct.unpack(
        _EOCD_FMT, tail[pos:pos + _EOCD_LEN])
    if 0xFFFFFFFF in (cd_size, cd_offset):
        raise RemoteZipError(f"{url}: ZIP64 archive is not supported")
    if cd_offset + cd_size > total:
        raise RemoteZipError(f"{url}: central directory runs past the end of the file")
    cd = open_(url, cd_offset, cd_offset + cd_size - 1)
    return _parse_central(cd)


def read_member(url: str, entry: ZipEntry,
                opener: Optional[Callable] = None) -> bytes:
    """The decompressed bytes of one member."""
    open_ = opener or _default_open
    head = open_(url, entry.header_offset,
                 entry.header_offset + _LOCAL_LEN - 1)
    if head[:4] != _LOCAL:
        raise RemoteZipError(
            f"{entry.name}: no local file header at offset {entry.header_offset}")
    f = struct.unpack(_LOCAL_FMT, head[4:_LOCAL_LEN])
    name_len, extra_len = f[8], f[9]
    data_start = entry.header_offset + _LOCAL_LEN + name_len + extra_len
    raw = open_(url, data_start, data_start + entry.compress_size - 1)
    if entry.method == 0:
        return raw
    return zlib.decompress(raw, -zlib.MAX_WBITS)


def select(entries: List[ZipEntry], patterns: List[str],
           exclude_dirs: bool = True) -> List[ZipEntry]:
    """Members whose name matches any of `patterns` (a case-insensitive fnmatch).

    Directory entries have a trailing "/" and zero bytes; they are dropped by
    default because a caller naming a data pattern never wants them.
    """
    import fnmatch

    out: List[ZipEntry] = []
    for e in entries:
        if exclude_dirs and e.name.endswith("/"):
            continue
        low = e.name.lower()
        if any(fnmatch.fnmatch(low, p.lower()) for p in patterns):
            out.append(e)
    return out
