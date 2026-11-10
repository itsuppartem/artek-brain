"""Ошибки ядра Artek Brain: понятный текст «что не так / что передать»."""

from __future__ import annotations


class ArtekBrainError(Exception):
    """Базовая ошибка хранилища."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message)

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message} Что сделать: {self.hint}"
        return self.message


class ValidationError(ArtekBrainError):
    """Некорректные аргументы вызова."""


class NotFoundError(ArtekBrainError):
    """Документ или версия не найдены."""


class InvalidVersionError(NotFoundError):
    """Запрошена несуществующая версия документа."""


class BaseVersionRequiredError(ValidationError):
    """Для существующего документа не передан base_version."""


class DeletedError(ArtekBrainError):
    """Документ удалён; запись и чтение заблокированы до restore."""


class NotDeletedError(ValidationError):
    """Документ не удалён, восстанавливать нечего."""


class StoredDataError(ArtekBrainError):
    """Повреждены данные в хранилище (блоб не совпал по хешу)."""


class ConflictError(ArtekBrainError):
    """Версия документа изменилась, запись конфликтует с текущей."""

    def __init__(
        self,
        path: str,
        current_version: int,
        diff: str = "",
        conflicting_sections: list[str] | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self.path = path
        self.current_version = current_version
        self.diff = diff
        self.conflicting_sections = list(conflicting_sections or [])
        message = (
            f"Конфликт в документе «{path}»: текущая версия {current_version}, "
            "а переданная запись основана на другой версии."
        )
        if self.conflicting_sections:
            message += " Пересекаются разделы: " + ", ".join(self.conflicting_sections) + "."
        super().__init__(
            message,
            hint=hint
            or (
                "Прочитайте документ заново через read(path) и передайте "
                f"base_version={current_version} (и expected_sha256 текущей версии)."
            ),
        )


class SecretDetectedError(ArtekBrainError):
    """В тексте найден секрет; сам секрет в сообщение не попадает."""

    def __init__(self, secret_type: str, line: int, *, path: str | None = None) -> None:
        self.secret_type = secret_type
        self.kind = secret_type
        self.line = line
        where = f" в документе «{path}»" if path else ""
        super().__init__(
            f"Найден секрет (тип: {secret_type}, строка {line}){where}. Запись отклонена.",
            hint=(
                "Уберите секрет из текста и передайте ссылку на хранилище секретов. "
                "Запись с секретом в память не сохраняется."
            ),
        )


class QuotaError(ArtekBrainError):
    """Превышена квота хранилища."""

    def __init__(
        self,
        kind: str,
        limit: int,
        used: int,
        *,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.kind = kind
        self.limit = limit
        self.used = used
        where = f" для «{path}»" if path else ""
        message = f"Превышена квота «{kind}»{where}: использовано {used} из {limit}."
        super().__init__(
            message,
            hint=hint
            or (
                "Уменьшите объём данных, удалите лишние версии/блобы через gc() "
                "или увеличьте лимит в Quota при создании Store."
            ),
        )


class FrontmatterError(ValidationError):
    """Невалидный YAML-подобный frontmatter."""

    def __init__(self, message: str, *, line: int | None = None, hint: str | None = None) -> None:
        self.line = line
        prefix = f"Строка {line}: " if line is not None else ""
        super().__init__(
            f"Невалидный frontmatter: {prefix}{message}",
            hint=hint
            or (
                "Ожидается блок между строками --- в начале файла: "
                "строки key: value и списки вида [a, b]."
            ),
        )


class PermissionDenied(ArtekBrainError):
    """Недостаточно прав на действие или путь."""

    def __init__(
        self,
        action: str,
        path: str | None,
        required: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.action = action
        self.path = path
        self.required = required
        self.actor = actor
        self.reason = reason
        where = f" на путь «{path}»" if path else ""
        who = f" у «{actor}»" if actor else ""
        message = (
            f"Отказ: для действия «{action}»{where} нужно право «{required}»{who}."
        )
        if reason:
            message += f" Причина: {reason}."
        super().__init__(
            message,
            hint=hint
            or (
                "Запросите у владельца токен с нужным scope (префикс пути + "
                "read/write/admin) или выполните действие другим токеном."
            ),
        )
