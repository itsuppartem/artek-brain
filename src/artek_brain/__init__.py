"""Artek Brain: общий слой памяти (хранение, версии, аудит).

В ядре нет вызовов LLM и сети: только стандартная библиотека Python.
"""

from __future__ import annotations

from . import frontmatter, secrets
from .access import Access, IssuedToken, Principal, Scope, TokenRecord
from .brain import Brain
from .errors import (
    ArtekBrainError,
    BaseVersionRequiredError,
    ConflictError,
    DeletedError,
    FrontmatterError,
    InvalidVersionError,
    NotDeletedError,
    NotFoundError,
    PermissionDenied,
    QuotaError,
    SecretDetectedError,
    StoredDataError,
    ValidationError,
)
from .frontmatter import Frontmatter
from .nav import ChangesPage, Link, Nav, Node, ReadmeMap, TocItem
from .quota import Quota
from .search import Search, SearchItem, SearchPage
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
    "Access",
    "Brain",
    "Search",
    "Nav",
    "Scope",
    "Principal",
    "IssuedToken",
    "TokenRecord",
    "SearchItem",
    "SearchPage",
    "Node",
    "TocItem",
    "Link",
    "ChangesPage",
    "ReadmeMap",
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
    "PermissionDenied",
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
