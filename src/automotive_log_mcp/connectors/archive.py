"""Archives: zip, tar (plain, gz, bz2, xz) and single-file gzip.

Each archive member becomes a member to sniff; nested archives are expanded up to a depth of 4.
Members are read in archive order and streamed, never extracted to disk.
"""

from __future__ import annotations

import gzip
import posixpath
import tarfile
import zipfile
from collections.abc import Iterator
from datetime import datetime
from typing import BinaryIO

from .base import ContainerContext, Member

MAX_DEPTH = 4


class _OwningGzip(gzip.GzipFile):
    """A GzipFile that also closes the stream it reads from."""

    def __init__(self, raw: BinaryIO) -> None:
        super().__init__(fileobj=raw, mode="rb")
        self._raw = raw

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._raw.close()


def _is_tar(head: bytes) -> bool:
    return len(head) >= 262 and head[257:262] == b"ustar"


class ArchiveContainer:
    name = "archive"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            return 0.6
        if head.startswith(b"\x1f\x8b") or head.startswith(b"BZh") or head.startswith(b"\xfd7zXZ\x00"):
            return 0.6
        if _is_tar(head):
            return 0.6
        return 0.0

    def expand(self, member: Member, ctx: ContainerContext) -> Iterator[Member]:
        assert member.open is not None
        depth = int(member.meta.get("depth", 0)) + 1
        if depth > MAX_DEPTH:
            ctx.notes.append(f"{member.name}: archive nested deeper than {MAX_DEPTH}, skipped")
            return
        with member.open() as f:
            head = f.read(512)
        if head.startswith(b"PK"):
            yield from self._zip(member, depth)
        elif _is_tar(head) or self._compressed_tar(member):
            yield from self._tar(member, depth)
        elif head.startswith(b"\x1f\x8b"):
            yield self._gzip(member, depth)
        else:
            ctx.notes.append(f"{member.name}: unsupported compression, skipped")

    @staticmethod
    def _compressed_tar(member: Member) -> bool:
        assert member.open is not None
        try:
            with member.open() as f, tarfile.open(fileobj=f, mode="r:*") as tf:
                return tf.next() is not None
        except (tarfile.TarError, OSError, EOFError):
            return False

    @staticmethod
    def _zip(member: Member, depth: int) -> Iterator[Member]:
        assert member.open is not None
        with member.open() as f, zipfile.ZipFile(f) as zf:
            infos = sorted((i for i in zf.infolist() if not i.is_dir()), key=lambda i: i.filename)
            for info in infos:
                year, month = info.date_time[0], info.date_time[1]

                def opener(info: zipfile.ZipInfo = info) -> BinaryIO:
                    return zf.open(info)  # type: ignore[return-value]

                yield Member(
                    name=f"{member.name}!{info.filename}",
                    open=opener,
                    meta={"depth": depth},
                    ref_year=year if year > 1980 else None,
                    ref_month=month if year > 1980 else None,
                )

    @staticmethod
    def _tar(member: Member, depth: int) -> Iterator[Member]:
        assert member.open is not None
        with member.open() as f, tarfile.open(fileobj=f, mode="r:*") as tf:
            for info in tf:
                if not info.isfile():
                    continue
                when = datetime.fromtimestamp(info.mtime) if info.mtime else None

                def opener(info: tarfile.TarInfo = info) -> BinaryIO:
                    stream = tf.extractfile(info)
                    assert stream is not None
                    return stream  # type: ignore[return-value]

                yield Member(
                    name=f"{member.name}!{info.name}",
                    open=opener,
                    meta={"depth": depth},
                    ref_year=when.year if when else None,
                    ref_month=when.month if when else None,
                )

    @staticmethod
    def _gzip(member: Member, depth: int) -> Member:
        assert member.open is not None
        parent = member.open
        inner = posixpath.basename(member.name.rsplit("!", 1)[-1])
        if inner.endswith(".gz"):
            inner = inner[:-3]

        def opener() -> BinaryIO:
            return _OwningGzip(parent())  # type: ignore[return-value]

        return Member(
            name=f"{member.name}!{inner}",
            open=opener,
            meta={"depth": depth},
            ref_year=member.ref_year,
            ref_month=member.ref_month,
        )
