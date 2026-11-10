"""Привязка знания к коду: refs, drift, pin, отметка stale.

Записи refs в frontmatter: ``path/to/file.py@<sha256|git-sha>`` или
``path#symbol@<sha>``. Для git-хешей используется ``git hash-object`` через
subprocess; сеть и LLM не используются.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import frontmatter
from .errors import NotFoundError, ValidationError
from .knowledge import as_list, iter_documents
from .review_queue import DRIFT, ReviewQueue

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Ref:
    """Разобранная привязка к файлу: путь, символ и записанный хеш."""

    path: str
    hash: str
    symbol: str | None = None

    @property
    def raw(self) -> str:
        """Собрать запись обратно в строку frontmatter."""
        left = f"{self.path}#{self.symbol}" if self.symbol else self.path
        return f"{left}@{self.hash}"

    def as_dict(self) -> dict[str, Any]:
        """Представить привязку словарём."""
        return {"path": self.path, "symbol": self.symbol, "hash": self.hash}


@dataclass(frozen=True)
class DriftItem:
    """Устаревшая привязка: какой документ, какой ref, было/стало."""

    doc: str
    version: int
    ref: Ref
    expected: str
    actual: str | None
    reason: str

    @property
    def doc_ref(self) -> str:
        """Ссылка на версию документа."""
        return f"{self.doc}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        """Представить дрифт словарём."""
        return {
            "doc": self.doc,
            "version": self.version,
            "doc_ref": self.doc_ref,
            "ref": self.ref.raw,
            "path": self.ref.path,
            "symbol": self.ref.symbol,
            "expected": self.expected,
            "actual": self.actual,
            "reason": self.reason,
        }


def parse_ref(raw: str) -> Ref:
    """Разобрать запись refs вида путь[#символ]@хеш."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(
            "Пустая ссылка refs.",
            hint="Ожидается path/to/file.py@<sha256|git-sha>.",
        )
    value = raw.strip()
    if "@" not in value:
        raise ValidationError(
            f"В ссылке «{value}» нет @хеша.",
            hint="Запишите refs как path/to/file.py@<sha256|git-sha>.",
        )
    left, recorded = value.rsplit("@", 1)
    recorded = recorded.strip().lower()
    if not recorded:
        raise ValidationError(
            f"В ссылке «{value}» пустой хеш.",
            hint="Укажите sha256 (64 hex) или git-хеш (7-40 hex).",
        )
    if not (_SHA256_RE.match(recorded) or _GIT_RE.match(recorded)):
        raise ValidationError(
            f"Некорректный хеш «{recorded}» в ссылке «{value}».",
            hint="Укажите sha256 (64 hex) или git-хеш (7-40 hex).",
        )
    path, symbol = left, None
    if "#" in left:
        path, symbol = left.split("#", 1)
        symbol = symbol.strip() or None
    path = path.strip().strip("/")
    if not path:
        raise ValidationError(
            f"В ссылке «{value}» пустой путь.",
            hint="Укажите путь к файлу от корня репозитория.",
        )
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise ValidationError(
            f"Недопустимый путь в ссылке «{value}».",
            hint="Путь должен быть относительным и без ..",
        )
    return Ref(path=path, hash=recorded, symbol=symbol)


def refs_from_data(data: dict[str, Any]) -> list[Ref]:
    """Достать и разобрать поле refs; битые записи пропускаются."""
    refs: list[Ref] = []
    for raw in as_list(data.get("refs")):
        try:
            refs.append(parse_ref(raw))
        except ValidationError:
            continue
    return refs


def _sha256_file(target: Path) -> str:
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_hash(repo_root: Path, relative: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "hash-object", "--", relative],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().lower()
    try:
        fallback = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{relative}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if fallback.returncode == 0 and fallback.stdout.strip():
        return fallback.stdout.strip().lower()
    return None


def _safe_target(repo_root: Path, relative: str) -> Path:
    root = repo_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            f"Путь «{relative}» выходит за пределы репозитория.",
            hint="Ссылка должна указывать внутрь repo_root.",
        ) from exc
    return target


def current_hash(repo_root: str | Path, ref: Ref) -> tuple[str | None, str | None]:
    """Текущий хеш файла и причина отказа (None, None при успехе)."""
    root = Path(repo_root)
    target = _safe_target(root, ref.path)
    if not target.is_file():
        return None, "missing"
    if _SHA256_RE.match(ref.hash):
        return _sha256_file(target), None
    actual = _git_hash(root, ref.path)
    if actual is None:
        return None, "git-error"
    return actual, None


def _matches(recorded: str, actual: str) -> bool:
    if _SHA256_RE.match(recorded):
        return recorded == actual
    return actual.startswith(recorded)


def check_drift(
    brain: Any,
    token: str,
    repo_root: str | Path,
    *,
    prefix: str = "",
) -> list[DriftItem]:
    """Сравнить refs документов с текущими файлами репозитория."""
    return Drift(brain, repo_root).check(token, prefix=prefix)


def mark_stale(
    brain: Any,
    token: str,
    repo_root: str | Path,
    *,
    items: Sequence[DriftItem] | None = None,
    prefix: str = "",
    queue: ReviewQueue | None = None,
) -> list[Any]:
    """Пометить документы с drift статусом stale от имени system."""
    return Drift(brain, repo_root).mark_stale(
        token, items=items, prefix=prefix, queue=queue
    )


def pin(
    brain: Any,
    token: str,
    repo_root: str | Path,
    doc: str,
    ref: str | Ref,
) -> Any:
    """Зафиксировать текущий хеш файла в refs документа."""
    return Drift(brain, repo_root).pin(token, doc, ref)


class Drift:
    """Проверка и обслуживание привязок документов к файлам репозитория."""

    def __init__(
        self, brain: Any, repo_root: str | Path, token: str | None = None
    ) -> None:
        self.brain = brain
        self.repo_root = Path(repo_root)
        self.token = token

    def _token(self, token: str | None) -> str:
        value = token or self.token
        if not value:
            raise ValidationError(
                "Не передан токен для проверки drift.",
                hint="Передайте token в Drift(...) или первым аргументом метода.",
            )
        return value

    def check(self, token: str | None = None, *, prefix: str = "") -> list[DriftItem]:
        """Найти документы, у которых refs разошлись с файлами."""
        items: list[DriftItem] = []
        for doc in iter_documents(self.brain, self._token(token), prefix):
            for ref in refs_from_data(doc.data):
                actual, reason = current_hash(self.repo_root, ref)
                if reason is None and actual is not None and _matches(ref.hash, actual):
                    continue
                items.append(
                    DriftItem(
                        doc=doc.path,
                        version=doc.version,
                        ref=ref,
                        expected=ref.hash,
                        actual=actual,
                        reason=reason or "changed",
                    )
                )
        return items

    def mark_stale(
        self,
        token: str | None = None,
        *,
        items: Sequence[DriftItem] | None = None,
        prefix: str = "",
        queue: ReviewQueue | None = None,
    ) -> list[Any]:
        """Создать версию со status=stale и положить запись в очередь ревью."""
        value = self._token(token)
        found = list(items) if items is not None else self.check(value, prefix=prefix)
        if not found:
            return []
        queue = queue or ReviewQueue(
            self.brain.store, getattr(self.brain, "access", None)
        )
        by_doc: dict[str, list[DriftItem]] = {}
        for item in found:
            by_doc.setdefault(item.doc, []).append(item)
        results: list[Any] = []
        for path, doc_items in by_doc.items():
            try:
                read = self.brain.store.read(path)
            except NotFoundError:
                continue
            parsed = frontmatter.parse(read.content)
            if str(parsed.data.get("status") or "").strip().lower() == "stale":
                queue.add(
                    path,
                    DRIFT,
                    version=read.version,
                    detail="; ".join(
                        f"{item.ref.raw}: было {item.expected}, "
                        f"стало {item.actual or 'нет'}"
                        for item in doc_items
                    ),
                    actor="system",
                )
                continue
            detail = "; ".join(
                f"{item.ref.raw}: было {item.expected}, стало {item.actual or 'нет'}"
                for item in doc_items
            )
            reason = f"drift: {detail}"[:1900]
            new_text = frontmatter.update_text(read.content, status="stale")
            result = self.brain.store.write(
                path,
                new_text,
                "system",
                reason,
                base_version=read.version,
            )
            results.append(result)
            queue.add(
                path,
                DRIFT,
                version=result.version,
                detail=detail,
                actor="system",
            )
        return results

    def pin(self, *args: Any, token: str | None = None) -> Any:
        """Заменить/добавить ref текущим хешем файла и записать версию.

        Поддерживаются вызовы pin(token, doc, ref) и pin(doc, ref), если токен
        передан в конструктор Drift.
        """
        if len(args) == 3:
            token_value, doc, ref = args
        elif len(args) == 2:
            doc, ref = args
            token_value = token
        else:
            raise ValidationError(
                "pin ожидает (doc, ref) или (token, doc, ref).",
                hint="Например: pin(\"docs/a.md\", \"app.py@<sha256>\").",
            )
        token_value = self._token(token_value)
        target = parse_ref(ref) if isinstance(ref, str) else ref
        read = self.brain.read(token_value, doc)
        parsed = frontmatter.parse(read.content)
        data = dict(parsed.data)
        actual, reason = current_hash(self.repo_root, target)
        if actual is None:
            raise ValidationError(
                f"Не удалось вычислить хеш «{target.path}»: {reason}.",
                hint="Проверьте путь и что каталог является git-репозиторием.",
            )
        pinned = Ref(path=target.path, hash=actual, symbol=target.symbol)
        raw_refs = as_list(data.get("refs"))
        replaced = False
        updated: list[str] = []
        for raw in raw_refs:
            try:
                existing = parse_ref(raw)
            except ValidationError:
                updated.append(raw)
                continue
            if existing.path == pinned.path and existing.symbol == pinned.symbol:
                updated.append(pinned.raw)
                replaced = True
            else:
                updated.append(raw)
        if not replaced:
            updated.append(pinned.raw)
        data["refs"] = updated
        new_text = frontmatter.dump(data, parsed.body)
        return self.brain.write(
            token_value,
            doc,
            new_text,
            f"pin {pinned.raw}",
            base_version=read.version,
        )
