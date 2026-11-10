"""Artek Brain: общий слой памяти (хранение, версии, аудит).

В ядре нет вызовов LLM и сети: только стандартная библиотека Python.
"""

from __future__ import annotations

from . import frontmatter, secrets
from .errors import (
    ArtekBrainError,
    BaseVersionRequiredError,
    ConflictError,
    DeletedError,
    FrontmatterError,
    InvalidVersionError,
    NotDeletedError,
    NotFoundError,
    QuotaError,
    SecretDetectedError,
    StoredDataError,
    ValidationError,
)
from .frontmatter import Frontmatter
from .quota import Quota
from .store import (
    AuditEntry,
    GcResult,
    ReadResult,
    RollbackReport,
    RollbackSkipped,
    SectionBlame,
    Store,
    VersionInfo,
    WriteResult,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Store",
    "Quota",
    "Frontmatter",
    "frontmatter",
    "secrets",
    "VersionInfo",
    "WriteResult",
    "ReadResult",
    "SectionBlame",
    "RollbackReport",
    "RollbackSkipped",
    "AuditEntry",
    "GcResult",
    "ArtekBrainError",
    "ValidationError",
    "NotFoundError",
    "InvalidVersionError",
    "BaseVersionRequiredError",
    "DeletedError",
    "NotDeletedError",
    "StoredDataError",
    "ConflictError",
    "SecretDetectedError",
    "QuotaError",
    "FrontmatterError",
]
