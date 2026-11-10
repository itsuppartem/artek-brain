"""Дельта-хранение версий и экономика места (ТЗ 3.12).

Блоб — самодостаточный контейнер: MAGIC + кодек + сжатые данные. Старые
блобы без MAGIC читаются как сырой zlib, поэтому миграция не нужна.

Дельта — построчный патч (difflib/opcodes) в JSON. Версия хранится дельтой
к предыдущей, если она заметно меньше полной копии; каждая K-я версия и
вершина длинной цепочки хранятся полным снимком.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import lzma
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import QuotaError, StoredDataError, ValidationError
from .quota import Quota

MAGIC = b"ABLOB1"
_CODEC_ZLIB = 0
_CODEC_LZMA = 1
DELTA_MAGIC = b"BRAIN-DELTA-1\n"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_METADATA_OVERHEAD = 512

_SCHEMA = """
CREATE TABLE IF NOT EXISTS version_blobs (
    path TEXT NOT NULL,
    n INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    base_n INTEGER,
    depth INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL,
    codec TEXT NOT NULL,
    PRIMARY KEY (path, n)
);
CREATE INDEX IF NOT EXISTS idx_version_blobs_content
    ON version_blobs(content_sha256);
CREATE INDEX IF NOT EXISTS idx_version_blobs_blob
    ON version_blobs(blob_sha256);
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _blob_path(blobs_dir: Path, sha: str) -> Path:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise StoredDataError(
            f"Некорректный sha256 {sha!r} в метаданных.",
            hint="Запустите gc() и проверьте целостность базы.",
        )
    return blobs_dir / sha[:2] / sha[2:]


def encode_container(payload: bytes, codec: str = "zlib") -> bytes:
    """Упаковать данные в самодостаточный блоб (magic + кодек + сжатие)."""
    if codec == "lzma":
        return MAGIC + bytes([_CODEC_LZMA]) + lzma.compress(payload, preset=9)
    return MAGIC + bytes([_CODEC_ZLIB]) + zlib.compress(payload, 6)


def decode_container(raw: bytes) -> bytes:
    """Распаковать блоб; старый формат (сырой zlib) тоже поддерживается."""
    if raw.startswith(MAGIC):
        if len(raw) < len(MAGIC) + 1:
            raise StoredDataError(
                "Блоб повреждён: нет кодека.",
                hint="Восстановите каталог blobs из резервной копии.",
            )
        codec = raw[len(MAGIC)]
        body = raw[len(MAGIC) + 1 :]
        try:
            if codec == _CODEC_ZLIB:
                return zlib.decompress(body)
            if codec == _CODEC_LZMA:
                return lzma.decompress(body)
        except (zlib.error, lzma.LZMAError) as exc:
            raise StoredDataError(
                "Блоб повреждён при распаковке.",
                hint="Восстановите каталог blobs из резервной копии.",
            ) from exc
        raise StoredDataError(
            f"Блоб повреждён: неизвестный кодек {codec}.",
            hint="Восстановите каталог blobs из резервной копии.",
        )
    try:
        return zlib.decompress(raw)
    except zlib.error as exc:
        raise StoredDataError(
            "Блоб повреждён (zlib).",
            hint="Восстановите каталог blobs из резервной копии.",
        ) from exc


def _op_range(op: list[Any], offset: int, limit: int) -> tuple[int, int]:
    if len(op) < offset + 2:
        raise StoredDataError("Дельта повреждена: не хватает границ.",
                              hint="Восстановите блоб из резервной копии.")
    first, second = op[offset], op[offset + 1]
    if (
        not isinstance(first, int)
        or isinstance(first, bool)
        or not isinstance(second, int)
        or isinstance(second, bool)
        or not 0 <= first <= second <= limit
    ):
        raise StoredDataError("Дельта повреждена: некорректные границы.",
                              hint="Восстановите блоб из резервной копии.")
    return first, second


def encode_delta(base: bytes, new: bytes) -> bytes:
    """Построчный детерминированный патч base -> new."""
    old_lines = base.decode("utf-8").splitlines(keepends=True)
    new_lines = new.decode("utf-8").splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    ops: list[list[Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append(["=", i1, i2])
        elif tag == "delete":
            ops.append(["-", i1, i2])
        elif tag == "insert":
            ops.append(["+", new_lines[j1:j2]])
        elif tag == "replace":
            ops.append(["~", i1, i2, new_lines[j1:j2]])
    document = {"format": "brain-delta", "version": 1, "ops": ops}
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return DELTA_MAGIC + body.encode("utf-8")


def decode_delta(base: bytes, payload: bytes) -> bytes:
    """Применить патч к base; любое несоответствие — StoredDataError."""
    if not payload.startswith(DELTA_MAGIC):
        raise StoredDataError("Дельта повреждена: нет заголовка.",
                              hint="Восстановите блоб из резервной копии.")
    try:
        document = json.loads(payload[len(DELTA_MAGIC) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoredDataError("Дельта повреждена: не JSON.",
                              hint="Восстановите блоб из резервной копии.") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != "brain-delta"
        or document.get("version") != 1
        or not isinstance(document.get("ops"), list)
    ):
        raise StoredDataError("Дельта повреждена: неизвестный формат.",
                              hint="Восстановите блоб из резервной копии.")
    old_lines = base.decode("utf-8").splitlines(keepends=True)
    output: list[str] = []
    position = 0
    for op in document["ops"]:
        if not isinstance(op, list) or not op:
            raise StoredDataError("Дельта повреждена: пустая операция.",
                                  hint="Восстановите блоб из резервной копии.")
        tag = op[0]
        if tag == "=":
            first, second = _op_range(op, 1, len(old_lines))
            if first != position:
                raise StoredDataError("Дельта повреждена: разрыв позиции.",
                                      hint="Восстановите блоб из резервной копии.")
            output.extend(old_lines[first:second])
            position = second
        elif tag == "-":
            first, second = _op_range(op, 1, len(old_lines))
            if first != position:
                raise StoredDataError("Дельта повреждена: разрыв позиции.",
                                      hint="Восстановите блоб из резервной копии.")
            position = second
        elif tag == "+":
            if len(op) != 2 or not isinstance(op[1], list):
                raise StoredDataError("Дельта повреждена: операция вставки.",
                                      hint="Восстановите блоб из резервной копии.")
            output.extend(op[1])
        elif tag == "~":
            first, second = _op_range(op, 1, len(old_lines))
            if first != position or len(op) != 4 or not isinstance(op[3], list):
                raise StoredDataError("Дельта повреждена: операция замены.",
                                      hint="Восстановите блоб из резервной копии.")
            output.extend(op[3])
            position = second
        else:
            raise StoredDataError("Дельта повреждена: неизвестная операция.",
                                  hint="Восстановите блоб из резервной копии.")
    if position != len(old_lines):
        raise StoredDataError("Дельта повреждена: не весь базовый текст.",
                              hint="Восстановите блоб из резервной копии.")
    try:
        return "".join(output).encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - защита от порчи JSON
        raise StoredDataError("Дельта повреждена: не текст UTF-8.",
                              hint="Восстановите блоб из резервной копии.") from exc


@dataclass(frozen=True)
class RepackResult:
    """Итог переупаковки версий в дельты."""

    versions: int
    rewritten: int
    deltas: int
    fulls: int
    blob_bytes_before: int
    blob_bytes_after: int

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "versions": self.versions,
            "rewritten": self.rewritten,
            "deltas": self.deltas,
            "fulls": self.fulls,
            "blob_bytes_before": self.blob_bytes_before,
            "blob_bytes_after": self.blob_bytes_after,
        }


@dataclass(frozen=True)
class ArchiveResult:
    """Итог архивации холодных версий."""

    archived: int
    skipped: int
    bytes_before: int
    bytes_after: int
    cutoff: str
    keep_last: int

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "archived": self.archived,
            "skipped": self.skipped,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "cutoff": self.cutoff,
            "keep_last": self.keep_last,
        }


class DeltaStorage:
    """Слой хранения содержимого версий: полные блобы и дельты."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        blobs_dir: Path,
        *,
        root: Path | None = None,
        snapshot_every: int = 20,
        delta_ratio: float = 0.9,
        quota_provider: Callable[[], Quota] | None = None,
    ) -> None:
        self.conn = conn
        self.blobs_dir = blobs_dir
        self.root = root
        self.snapshot_every = self._validate_every(snapshot_every)
        self.delta_ratio = self._validate_ratio(delta_ratio)
        self.quota_provider = quota_provider or Quota
        with self.conn:
            self.conn.executescript(_SCHEMA)

    @staticmethod
    def _validate_every(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValidationError(
                "snapshot_every должен быть целым числом >= 1.",
                hint="Передайте, например, snapshot_every=20.",
            )
        return value

    @staticmethod
    def _validate_ratio(value: float) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 < float(value) <= 1
        ):
            raise ValidationError(
                "delta_ratio должен быть числом в диапазоне (0, 1].",
                hint="Передайте delta_ratio=0.9.",
            )
        return float(value)

    def _quota(self) -> Quota:
        quota = self.quota_provider() if self.quota_provider else Quota()
        return quota or Quota()

    # ------------------------------------------------------------------
    # Физические блобы
    # ------------------------------------------------------------------
    def _path(self, sha: str) -> Path:
        return _blob_path(self.blobs_dir, sha)

    def _read_payload(self, sha: str) -> bytes:
        target = self._path(sha)
        try:
            raw = target.read_bytes()
        except FileNotFoundError as exc:
            raise StoredDataError(
                f"Блоб {sha[:12]}… не найден на диске.",
                hint="Запустите gc() или восстановите каталог blobs из резервной копии.",
            ) from exc
        payload = decode_container(raw)
        if _sha256(payload) != sha:
            raise StoredDataError(
                f"Блоб {sha[:12]}… не совпал по sha256.",
                hint="Данные повреждены; восстановите каталог blobs из резервной копии.",
            )
        return payload

    def _write_payload(self, sha: str, container: bytes) -> bool:
        target = self._path(sha)
        if target.exists():
            return False
        self._overwrite_payload(sha, container)
        return True

    def _overwrite_payload(self, sha: str, container: bytes) -> None:
        target = self._path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.tmp-{uuid4().hex}")
        try:
            temp.write_bytes(container)
            os.replace(temp, target)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    def _decode_text(self, payload: bytes) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoredDataError(
                "Блоб не является UTF-8 текстом.",
                hint="Проверьте, что документ был записан как текст.",
            ) from exc

    # ------------------------------------------------------------------
    # Метаданные хранения
    # ------------------------------------------------------------------
    def get_storage(self, path: str, n: int) -> sqlite3.Row | None:
        """Запись о хранении версии или None для старого полного блоба."""
        return self.conn.execute(
            "SELECT * FROM version_blobs WHERE path=? AND n=?", (path, n)
        ).fetchone()

    def _upsert_row(
        self,
        path: str,
        n: int,
        kind: str,
        content_sha: str,
        blob_sha: str,
        base_n: int | None,
        depth: int,
        archived: int,
        stored_bytes: int,
        codec: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO version_blobs
                (path, n, kind, content_sha256, blob_sha256, base_n,
                 depth, archived, stored_bytes, codec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path, n) DO UPDATE SET
                kind=excluded.kind,
                content_sha256=excluded.content_sha256,
                blob_sha256=excluded.blob_sha256,
                base_n=excluded.base_n,
                depth=excluded.depth,
                archived=excluded.archived,
                stored_bytes=excluded.stored_bytes,
                codec=excluded.codec
            """,
            (
                path,
                n,
                kind,
                content_sha,
                blob_sha,
                base_n,
                depth,
                archived,
                stored_bytes,
                codec,
            ),
        )

    def _depth(self, path: str, n: int | None) -> int:
        if n is None:
            return 0
        row = self.get_storage(path, n)
        return int(row["depth"]) if row is not None else 1

    def _version_sha(self, path: str, n: int) -> str:
        row = self.conn.execute(
            "SELECT sha256 FROM versions WHERE path=? AND n=?", (path, n)
        ).fetchone()
        if row is None:
            raise StoredDataError(
                f"Версия {n} документа «{path}» не найдена в метаданных.",
                hint="Проверьте целостность базы и повторите операцию.",
            )
        return str(row["sha256"])

    # ------------------------------------------------------------------
    # Запись новой версии
    # ------------------------------------------------------------------
    def put_version(
        self,
        path: str,
        n: int,
        parent_n: int | None,
        data: bytes,
        base_data: bytes | None,
        *,
        force_full: bool = False,
    ) -> tuple[str, int]:
        """Записать содержимое версии (дельной или полной копией).

        Возвращает (sha256 содержимого, размер содержимого в байтах).
        """
        content_sha = _sha256(data)
        target = self._path(content_sha)
        if target.exists():
            try:
                self._read_payload(content_sha)
            except StoredDataError:
                pass
            else:
                self._check_disk_quota(_METADATA_OVERHEAD)
                self._upsert_row(
                    path,
                    n,
                    "full",
                    content_sha,
                    content_sha,
                    None,
                    1,
                    0,
                    target.stat().st_size,
                    "legacy",
                )
                return content_sha, len(data)

        full_container = encode_container(data, "zlib")
        full_size = len(full_container)
        choose_full = (
            force_full
            or parent_n is None
            or base_data is None
            or (n - 1) % self.snapshot_every == 0
        )
        base_depth = 0
        if not choose_full:
            base_depth = self._depth(path, parent_n)
            if base_depth >= self.snapshot_every:
                choose_full = True
        delta_payload: bytes | None = None
        delta_container: bytes | None = None
        if not choose_full:
            try:
                delta_payload = encode_delta(base_data, data)
                delta_container = encode_container(delta_payload, "zlib")
            except (UnicodeDecodeError, ValueError):
                choose_full = True
            else:
                if len(delta_container) >= int(full_size * self.delta_ratio):
                    choose_full = True

        if choose_full:
            extra = 0 if target.exists() else full_size
            self._check_disk_quota(extra + _METADATA_OVERHEAD)
            if extra:
                self._write_payload(content_sha, full_container)
            stored = target.stat().st_size if target.exists() else full_size
            self._upsert_row(
                path, n, "full", content_sha, content_sha, None, 1, 0, stored, "zlib"
            )
            return content_sha, len(data)

        assert delta_payload is not None and delta_container is not None
        blob_sha = _sha256(delta_payload)
        delta_target = self._path(blob_sha)
        extra = 0 if delta_target.exists() else len(delta_container)
        self._check_disk_quota(extra + _METADATA_OVERHEAD)
        if extra:
            self._write_payload(blob_sha, delta_container)
        stored = delta_target.stat().st_size if delta_target.exists() else len(delta_container)
        self._upsert_row(
            path,
            n,
            "delta",
            content_sha,
            blob_sha,
            int(parent_n) if parent_n is not None else None,
            base_depth + 1,
            0,
            stored,
            "zlib",
        )
        return content_sha, len(data)

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def _read_version_content(self, path: str, n: int, seen: set[tuple[str, int]]) -> bytes:
        key = (path, n)
        if key in seen:
            raise StoredDataError(
                f"Цикл в цепочке дельт у «{path}» v{n}.",
                hint="Восстановите каталог blobs и метаданные из резервной копии.",
            )
        seen.add(key)
        sha = self._version_sha(path, n)
        row = self.get_storage(path, n)
        if row is None:
            payload = self._read_payload(sha)
            if _sha256(payload) != sha:
                raise StoredDataError(
                    f"Содержимое «{path}» v{n} не совпало по sha256.",
                    hint="Данные повреждены; восстановите из резервной копии.",
                )
            return payload
        kind = row["kind"]
        if kind == "full":
            payload = self._read_payload(str(row["blob_sha256"]))
            if _sha256(payload) != sha:
                raise StoredDataError(
                    f"Содержимое «{path}» v{n} не совпало по sha256.",
                    hint="Данные повреждены; восстановите из резервной копии.",
                )
            return payload
        if kind != "delta":
            raise StoredDataError(
                f"Неизвестный способ хранения {kind!r} у «{path}» v{n}.",
                hint="Проверьте целостность базы.",
            )
        base_n = row["base_n"]
        if base_n is None:
            raise StoredDataError(
                f"У дельты «{path}» v{n} нет базовой версии.",
                hint="Проверьте целостность базы.",
            )
        base = self._read_version_content(path, int(base_n), seen)
        delta_payload = self._read_payload(str(row["blob_sha256"]))
        content = decode_delta(base, delta_payload)
        if _sha256(content) != sha:
            raise StoredDataError(
                f"Восстановленное содержимое «{path}» v{n} не совпало по sha256.",
                hint="Данные повреждены; восстановите из резервной копии.",
            )
        return content

    def read_content(self, sha: str) -> str:
        """Прочитать содержимое по sha256: полный блоб или цепочка дельт."""
        first_error: StoredDataError | None = None
        target = self._path(sha)
        if target.exists():
            try:
                return self._decode_text(self._read_payload(sha))
            except StoredDataError as exc:
                first_error = exc
        rows = self.conn.execute(
            """
            SELECT * FROM version_blobs
            WHERE content_sha256=?
            ORDER BY depth ASC, path ASC, n ASC
            """,
            (sha,),
        ).fetchall()
        last_error: StoredDataError | None = None
        for row in rows:
            try:
                content = self._read_version_content(
                    str(row["path"]), int(row["n"]), set()
                )
            except StoredDataError as exc:
                last_error = exc
                continue
            if _sha256(content) == sha:
                return self._decode_text(content)
        if last_error is not None:
            raise last_error
        if first_error is not None:
            raise first_error
        raise StoredDataError(
            f"Блоб {sha[:12]}… не найден на диске.",
            hint="Запустите gc() или восстановите каталог blobs из резервной копии.",
        )

    def referenced_blobs(self) -> set[str]:
        """Физические блобы, нужные версиям и цепочкам дельт."""
        rows = self.conn.execute(
            """
            SELECT v.sha256 AS sha
            FROM versions v
            LEFT JOIN version_blobs b ON b.path=v.path AND b.n=v.n
            WHERE b.path IS NULL
            UNION
            SELECT blob_sha256 AS sha FROM version_blobs
            """
        ).fetchall()
        return {str(row["sha"]) for row in rows}

    # ------------------------------------------------------------------
    # Переупаковка, архив, отчёт
    # ------------------------------------------------------------------
    def _plan(
        self,
        path: str,
        n: int,
        content: bytes,
        base_content: bytes | None,
        every: int,
        codec: str,
    ) -> tuple[str, int | None, str, bytes, bytes, int]:
        content_sha = _sha256(content)
        base_n = n - 1 if base_content is not None else None
        choose_full = (
            base_content is None
            or base_n is None
            or (n - 1) % every == 0
        )
        base_depth = 0
        if not choose_full:
            base_depth = self._depth(path, base_n)
            if base_depth >= every:
                choose_full = True
        full_container = encode_container(content, codec)
        if not choose_full:
            assert base_content is not None
            delta_payload = encode_delta(base_content, content)
            delta_container = encode_container(delta_payload, codec)
            if len(delta_container) < int(len(full_container) * self.delta_ratio):
                return (
                    "delta",
                    base_n,
                    _sha256(delta_payload),
                    delta_payload,
                    delta_container,
                    base_depth + 1,
                )
        return "full", None, content_sha, content, full_container, 1

    def repack(self, *, snapshot_every: int | None = None) -> RepackResult:
        """Переупаковать версии в дельты; атомарно и идемпотентно."""
        every = self._validate_every(
            self.snapshot_every if snapshot_every is None else snapshot_every
        )
        before = self._blob_bytes()
        rewritten = 0
        total = 0
        with self.conn:
            rows = self.conn.execute(
                "SELECT path, n FROM versions ORDER BY path ASC, n ASC"
            ).fetchall()
            cache_path: str | None = None
            cache_n: int | None = None
            cache_content: bytes | None = None
            for row in rows:
                path = str(row["path"])
                n = int(row["n"])
                total += 1
                content = self._read_version_content(path, n, set())
                if cache_path == path and cache_n == n - 1:
                    base_content = cache_content
                elif n > 1:
                    base_content = self._read_version_content(path, n - 1, set())
                else:
                    base_content = None
                existing = self.get_storage(path, n)
                archived = int(existing["archived"]) if existing is not None else 0
                codec = "lzma" if archived else "zlib"
                kind, base_n, blob_sha, _payload, container, depth = self._plan(
                    path, n, content, base_content, every, codec
                )
                if (
                    existing is not None
                    and existing["kind"] == kind
                    and existing["base_n"] == base_n
                    and self._path(str(existing["blob_sha256"])).exists()
                ):
                    try:
                        check = self._read_version_content(path, n, set())
                    except StoredDataError:
                        check = b""
                    if _sha256(check) == _sha256(content):
                        cache_path, cache_n, cache_content = path, n, content
                        continue
                if archived:
                    self._overwrite_payload(blob_sha, container)
                else:
                    self._write_payload(blob_sha, container)
                stored = self._path(blob_sha).stat().st_size
                self._upsert_row(
                    path,
                    n,
                    kind,
                    _sha256(content),
                    blob_sha,
                    base_n,
                    depth,
                    archived,
                    stored,
                    codec,
                )
                check = self._read_version_content(path, n, set())
                if _sha256(check) != _sha256(content):
                    raise StoredDataError(
                        f"Переупаковка «{path}» v{n} не прошла проверку sha256.",
                        hint="Данные не изменены; проверьте каталог blobs.",
                    )
                rewritten += 1
                cache_path, cache_n, cache_content = path, n, content
        counts = self.conn.execute(
            "SELECT kind, COUNT(*) AS c FROM version_blobs GROUP BY kind"
        ).fetchall()
        kinds = {str(item["kind"]): int(item["c"]) for item in counts}
        return RepackResult(
            versions=total,
            rewritten=rewritten,
            deltas=kinds.get("delta", 0),
            fulls=kinds.get("full", 0),
            blob_bytes_before=before,
            blob_bytes_after=self._blob_bytes(),
        )

    def _cutoff(self, older_than: datetime | str | timedelta | float) -> str:
        if isinstance(older_than, datetime):
            moment = older_than
        elif isinstance(older_than, timedelta):
            moment = _utc_now() - older_than
        elif isinstance(older_than, str):
            raw = older_than.strip()
            if not raw:
                raise ValidationError(
                    "older_than не может быть пустым.",
                    hint="Передайте дату ISO или timedelta.",
                )
            try:
                moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError(
                    f"Не удалось разобрать дату older_than={raw!r}.",
                    hint="Передайте ISO-дату, например 2026-01-31T00:00:00Z.",
                ) from exc
        elif isinstance(older_than, (int, float)) and not isinstance(older_than, bool):
            moment = _utc_now() - timedelta(days=float(older_than))
        else:
            raise ValidationError(
                "older_than должен быть датой, строкой ISO, timedelta или числом дней.",
                hint="Передайте, например, older_than=timedelta(days=30).",
            )
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return _format_ts(moment)

    def archive_old_versions(
        self,
        older_than: datetime | str | timedelta | float,
        keep_last: int = 0,
    ) -> ArchiveResult:
        """Максимально сжать холодные версии, сохранив последние keep_last."""
        if not isinstance(keep_last, int) or isinstance(keep_last, bool) or keep_last < 0:
            raise ValidationError(
                "keep_last должен быть целым числом >= 0.",
                hint="Передайте keep_last=10.",
            )
        cutoff = self._cutoff(older_than)
        before = self._blob_bytes()
        archived = 0
        skipped = 0
        with self.conn:
            protected: set[tuple[str, int]] = set()
            if keep_last:
                protected_rows = self.conn.execute(
                    """
                    SELECT v.path, v.n FROM versions v
                    WHERE (
                        SELECT COUNT(*) FROM versions w
                        WHERE w.path=v.path AND w.n>v.n
                    ) < ?
                    """,
                    (keep_last,),
                ).fetchall()
                protected = {
                    (str(item["path"]), int(item["n"])) for item in protected_rows
                }
            rows = self.conn.execute(
                "SELECT path, n, sha256 FROM versions WHERE ts < ? ORDER BY path ASC, n ASC",
                (cutoff,),
            ).fetchall()
            for row in rows:
                path = str(row["path"])
                n = int(row["n"])
                if (path, n) in protected:
                    skipped += 1
                    continue
                existing = self.get_storage(path, n)
                if existing is not None and int(existing["archived"]):
                    skipped += 1
                    continue
                if existing is not None:
                    kind = str(existing["kind"])
                    content_sha = str(existing["content_sha256"])
                    blob_sha = str(existing["blob_sha256"])
                    base_n = existing["base_n"]
                    depth = int(existing["depth"])
                    payload = self._read_payload(blob_sha)
                else:
                    kind = "full"
                    content_sha = str(row["sha256"])
                    blob_sha = content_sha
                    base_n = None
                    depth = 1
                    payload = self._read_payload(blob_sha)
                if _sha256(payload) != blob_sha:
                    raise StoredDataError(
                        f"Архивация «{path}» v{n}: блоб не совпал по sha256.",
                        hint="Данные не изменены; восстановите из резервной копии.",
                    )
                container = encode_container(payload, "lzma")
                if decode_container(container) != payload:
                    raise StoredDataError(
                        f"Архивация «{path}» v{n}: сжатие не прошло проверку.",
                        hint="Данные не изменены; повторите операцию.",
                    )
                self._overwrite_payload(blob_sha, container)
                self._upsert_row(
                    path,
                    n,
                    kind,
                    content_sha,
                    blob_sha,
                    base_n,
                    depth,
                    1,
                    len(container),
                    "lzma",
                )
                archived += 1
        return ArchiveResult(
            archived=archived,
            skipped=skipped,
            bytes_before=before,
            bytes_after=self._blob_bytes(),
            cutoff=cutoff,
            keep_last=keep_last,
        )

    # ------------------------------------------------------------------
    # Место на диске
    # ------------------------------------------------------------------
    def _blob_bytes(self) -> int:
        if not self.blobs_dir.exists():
            return 0
        return sum(
            item.stat().st_size for item in self.blobs_dir.rglob("*") if item.is_file()
        )

    def _disk_usage_bytes(self) -> int:
        if self.root is None:
            return self._blob_bytes()
        total = 0
        for item in self.root.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
        return total

    def _check_disk_quota(self, extra: int) -> None:
        quota = self._quota()
        limit = quota.max_disk_bytes
        if limit is None:
            return
        used = self._disk_usage_bytes()
        if used + extra > limit:
            raise QuotaError(
                "диск",
                limit,
                used + extra,
                hint=(
                    "Удалите лишние версии/блобы через gc(), включите архив "
                    "archive_old_versions() или увеличьте max_disk_bytes."
                ),
            )

    def disk_report(self, *, days: int = 30, forecast_days: int = 30) -> dict[str, Any]:
        """Размеры, байты на версию, прогноз роста и процент квоты."""
        for name, value in (("days", days), ("forecast_days", forecast_days)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValidationError(
                    f"{name} должен быть целым числом >= 1.",
                    hint=f"Передайте {name}=30.",
                )
        blob_bytes = self._blob_bytes()
        blob_files = (
            sum(1 for item in self.blobs_dir.rglob("*") if item.is_file())
            if self.blobs_dir.exists()
            else 0
        )
        disk_bytes = self._disk_usage_bytes()
        db_bytes = max(0, disk_bytes - blob_bytes)
        documents = int(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        )
        versions = int(
            self.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        )
        content_bytes = int(
            self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM versions").fetchone()[0]
        )
        cutoff = _format_ts(_utc_now() - timedelta(days=days))
        recent = self.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS b FROM versions WHERE ts>=?",
            (cutoff,),
        ).fetchone()
        recent_versions = int(recent["c"])
        recent_content = int(recent["b"])
        recent_stored = int(
            self.conn.execute(
                """
                SELECT COALESCE(SUM(b.stored_bytes), 0)
                FROM versions v
                JOIN version_blobs b ON b.path=v.path AND b.n=v.n
                WHERE v.ts>=?
                """,
                (cutoff,),
            ).fetchone()[0]
        )
        versions_per_day = recent_versions / days
        content_per_day = recent_content / days
        stored_per_day = recent_stored / days
        quota = self._quota()
        limit = quota.max_disk_bytes
        return {
            "disk_bytes": disk_bytes,
            "db_bytes": db_bytes,
            "blob_bytes": blob_bytes,
            "blob_files": blob_files,
            "documents": documents,
            "versions": versions,
            "content_bytes": content_bytes,
            "bytes_per_version": round(disk_bytes / versions, 2) if versions else 0.0,
            "quota_bytes": limit,
            "quota_percent": (
                round(disk_bytes / limit * 100, 2) if limit else None
            ),
            "growth": {
                "window_days": days,
                "versions": recent_versions,
                "content_bytes": recent_content,
                "stored_bytes": recent_stored,
                "versions_per_day": round(versions_per_day, 2),
                "content_bytes_per_day": round(content_per_day, 2),
                "stored_bytes_per_day": round(stored_per_day, 2),
                "forecast_days": forecast_days,
                "forecast_versions": round(versions + versions_per_day * forecast_days, 2),
                "forecast_content_bytes": round(
                    content_bytes + content_per_day * forecast_days, 2
                ),
                "forecast_disk_bytes": round(
                    disk_bytes + stored_per_day * forecast_days, 2
                ),
            },
        }
