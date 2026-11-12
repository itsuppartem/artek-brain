"""HTTP JSON API Artek Brain на стандартном http.server.

Bearer-токен, те же операции, что в CLI, лимит тела запроса и OpenAPI на
/openapi.json. По умолчанию сервер слушает только 127.0.0.1.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .errors import (
    ArtekBrainError,
    ConflictError,
    DeletedError,
    NotFoundError,
    PermissionDenied,
    QuotaError,
    SecretDetectedError,
    ValidationError,
)
from .service import Service

MAX_BODY = 1024 * 1024


class HttpError(Exception):
    """Ошибка HTTP-слоя с кодом и понятным текстом."""

    def __init__(self, status: int, message: str, *, hint: str | None = None) -> None:
        self.status = status
        self.message = message
        self.hint = hint
        super().__init__(message)


def _error_status(exc: ArtekBrainError) -> int:
    if isinstance(exc, PermissionDenied):
        return 401 if exc.action == "authenticate" else 403
    if isinstance(exc, (DeletedError,)):
        return 410
    if isinstance(exc, (NotFoundError,)):
        return 404
    if isinstance(exc, (ConflictError, QuotaError)):
        return 409
    if isinstance(exc, (SecretDetectedError, ValidationError)):
        return 400
    return 400


def _q1(query: dict[str, list[str]], name: str, default: Any = None) -> Any:
    values = query.get(name)
    if not values:
        return default
    return values[-1]


def _qbool(query: dict[str, list[str]], name: str) -> bool:
    value = _q1(query, name, "")
    return str(value).lower() in ("1", "true", "yes", "on")


def _qint(query: dict[str, list[str]], name: str) -> int | None:
    value = _q1(query, name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _body_value(body: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in body and body[name] is not None:
            return body[name]
    return default


class BrainHTTPServer(ThreadingHTTPServer):
    """HTTP-сервер с общим Service и лимитом тела."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: Service,
        *,
        max_body: int = MAX_BODY,
    ) -> None:
        self.service = service
        self.max_body = max_body
        super().__init__(address, BrainRequestHandler)


class BrainRequestHandler(BaseHTTPRequestHandler):
    """Обработчик JSON-запросов."""

    server_version = "ArtekBrain/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> BrainHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if getattr(self.app, "quiet", False):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------
    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise HttpError(400, "Некорректный Content-Length.") from exc
        if length < 0:
            raise HttpError(400, "Некорректный Content-Length.")
        if length > self.app.max_body:
            raise HttpError(
                413,
                f"Тело запроса больше лимита {self.app.max_body} байт.",
                hint="Сократите тело или увеличьте --max-body.",
            )
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "Тело запроса должно быть JSON в UTF-8.") from exc
        if not isinstance(data, dict):
            raise HttpError(400, "Тело запроса должно быть JSON-объектом.")
        return data

    def _token(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise HttpError(
                401,
                "Нужен Bearer-токен.",
                hint="Передайте заголовок Authorization: Bearer <токен>.",
            )
        token = header[7:].strip()
        if not token:
            raise HttpError(401, "Пустой Bearer-токен.")
        return token

    def _send(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="artek-brain"')
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, HttpError):
            status = exc.status
            payload = {
                "error": {
                    "type": "HttpError",
                    "message": exc.message,
                    "hint": exc.hint,
                }
            }
        elif isinstance(exc, ArtekBrainError):
            status = _error_status(exc)
            payload = {
                "error": {
                    "type": type(exc).__name__,
                    "message": exc.message,
                    "hint": exc.hint,
                }
            }
        else:
            status = 500
            payload = {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            }
        if status >= 400:
            self.close_connection = True
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            raw_segments = [item for item in parsed.path.split("/") if item]
            segments = [unquote(item) for item in raw_segments]
            query = parse_qs(parsed.query, keep_blank_values=True)
            body = self._read_json() if method in ("POST", "DELETE") else {}
            data = self._route(method, segments, query, body)
            self._send(200, data)
        except (HttpError, ArtekBrainError) as exc:
            self._error(exc)
        except Exception as exc:  # pragma: no cover - защита от неожиданного
            self._error(exc)

    # ------------------------------------------------------------------
    # Маршруты
    # ------------------------------------------------------------------
    def _route(
        self,
        method: str,
        segments: Sequence[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> Any:
        service = self.app.service
        head = segments[0] if segments else ""

        if method == "GET" and head in ("", "health"):
            return {"name": "artek-brain", "status": "ok"}
        if method == "GET" and head == "openapi.json":
            return OPENAPI

        token = self._token()

        if head == "documents":
            return self._documents(method, list(segments[1:]), query, body, token)
        if head == "read" and method in ("GET", "POST"):
            path = _body_value(body, "path") or _q1(query, "path")
            return service.read(
                token,
                path,
                version=_body_value(body, "version") or _qint(query, "version"),
                section=_body_value(body, "section") or _q1(query, "section"),
                line_start=_body_value(body, "line_start") or _qint(query, "line_start"),
                line_end=_body_value(body, "line_end") or _qint(query, "line_end"),
                max_chars=_body_value(body, "max_chars") or _qint(query, "max_chars"),
                session=_body_value(body, "session") or _q1(query, "session"),
                model=_body_value(body, "model") or _q1(query, "model"),
            )
        if head == "write" and method == "POST":
            return service.write(
                token,
                _body_value(body, "path") or _q1(query, "path"),
                _body_value(body, "content", default=""),
                _body_value(body, "reason", default=""),
                base_version=_body_value(body, "base_version"),
                expected_sha256=_body_value(body, "expected_sha256"),
                batch_id=_body_value(body, "batch_id", "batch"),
                session=_body_value(body, "session"),
                model=_body_value(body, "model"),
            )
        if head == "overwrite" and method == "POST":
            return service.overwrite(
                token,
                _body_value(body, "path") or _q1(query, "path"),
                _body_value(body, "content", default=""),
                session=_body_value(body, "session"),
                model=_body_value(body, "model"),
            )
        if head == "history" and method == "GET":
            return service.history(
                token,
                _q1(query, "path"),
                limit=_qint(query, "limit") or 50,
                offset=_qint(query, "offset") or 0,
            )
        if head == "diff" and method == "GET":
            return service.diff(
                token,
                _q1(query, "path"),
                a=_qint(query, "a") if _qint(query, "a") is not None else _qint(query, "from"),
                b=_qint(query, "b") if _qint(query, "b") is not None else _qint(query, "to"),
            )
        if head == "blame" and method == "GET":
            return service.blame(token, _q1(query, "path"))
        if head == "toc" and method == "GET":
            return service.toc(
                token, _q1(query, "path"), version=_qint(query, "version")
            )
        if head == "links" and method == "GET":
            return service.links(
                token, _q1(query, "path"), version=_qint(query, "version")
            )
        if head == "backlinks" and method == "GET":
            return service.backlinks(token, _q1(query, "path"))
        if head == "broken-links" and method == "GET":
            return service.broken_links(token, _q1(query, "prefix", ""))
        if head == "export" and method == "GET":
            return service.export(
                token, _q1(query, "path"), version=_qint(query, "version")
            )
        if head in ("delete", "restore") and method == "POST":
            path = _body_value(body, "path") or _q1(query, "path")
            if head == "delete":
                return service.delete(
                    token,
                    path,
                    _body_value(body, "reason", default=""),
                    base_version=_body_value(body, "base_version"),
                    batch_id=_body_value(body, "batch_id", "batch"),
                )
            return service.restore(
                token,
                path,
                _body_value(body, "reason", default=""),
                base_version=_body_value(body, "base_version"),
                batch_id=_body_value(body, "batch_id", "batch"),
            )
        if head == "rollback":
            return self._rollback(method, list(segments[1:]), query, body, token)
        if head == "rollback-author" and method == "POST":
            return service.rollback_author(
                token,
                _body_value(body, "author", default=""),
                since=_body_value(body, "since"),
                until=_body_value(body, "until"),
                reason=_body_value(body, "reason", default=""),
            )
        if head == "rollback-batch" and method == "POST":
            return service.rollback_batch(
                token,
                _body_value(body, "batch_id", "batch", default=""),
                _body_value(body, "reason", default=""),
            )
        if head == "search" and method == "GET":
            return service.search(
                token,
                _q1(query, "q") or _q1(query, "query", ""),
                path_prefix=_q1(query, "path_prefix") or _q1(query, "prefix"),
                doc_type=_q1(query, "type") or _q1(query, "doc_type"),
                status=_q1(query, "status"),
                tags=query.get("tag") or query.get("tags"),
                author=_q1(query, "author"),
                since=_q1(query, "since"),
                until=_q1(query, "until"),
                limit=_qint(query, "limit") or 20,
                offset=_qint(query, "offset") or 0,
                history=_qbool(query, "history"),
                mode=_q1(query, "mode") or "fts",
            )
        if head in ("ls", "tree") and method == "GET":
            return service.ls(
                token,
                _q1(query, "prefix", ""),
                tree=(head == "tree") or _qbool(query, "tree"),
                max_depth=_qint(query, "max_depth"),
            )
        if head == "changes" and method == "GET":
            return service.changes(
                token,
                cursor=_qint(query, "cursor"),
                limit=_qint(query, "limit") or 100,
            )
        if head == "audit" and method == "GET":
            return service.audit(
                token,
                limit=_qint(query, "limit") or 100,
                actor=_q1(query, "actor"),
                action=_q1(query, "action"),
                since=_q1(query, "since"),
            )
        if head == "stats" and method == "GET":
            return service.stats(token)
        if head == "readme" and method == "GET":
            return service.readme(token)
        if head in ("who-knows", "who_knows") and method == "GET":
            return service.who_knows(token, _q1(query, "path", ""))
        if head == "tokens":
            return self._tokens(method, list(segments[1:]), query, body, token)
        if head in ("tasks", "task"):
            return self._tasks(method, list(segments[1:]), query, body, token)
        if head == "inbox":
            return self._inbox(method, list(segments[1:]), query, body, token)
        if head == "brief" and method == "GET":
            return service.brief(token, limit=_qint(query, "limit") or 2000)
        if head == "sandbox":
            return self._sandbox(
                method, list(segments[1:]), query, body, token
            )
        if head == "rpc" and method == "POST":
            return self._rpc(body, token)

        raise HttpError(404, f"Маршрут не найден: {method} /{ '/'.join(segments) }")

    # ------------------------------------------------------------------
    # Группы маршрутов
    # ------------------------------------------------------------------
    def _documents(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        if not rest:
            if method == "POST":
                return service.write(
                    token,
                    _body_value(body, "path", default=""),
                    _body_value(body, "content", default=""),
                    _body_value(body, "reason", default=""),
                    base_version=_body_value(body, "base_version"),
                    expected_sha256=_body_value(body, "expected_sha256"),
                    batch_id=_body_value(body, "batch_id", "batch"),
                    session=_body_value(body, "session"),
                    model=_body_value(body, "model"),
                )
            raise HttpError(400, "Укажите путь: /documents/<path>.")
        actions = {
            "history", "diff", "blame", "links", "backlinks", "toc",
            "export", "delete", "restore", "rollback",
        }
        action = rest[-1] if rest[-1] in actions else None
        path = "/".join(rest[:-1] if action else rest)

        if method == "GET":
            if action == "history":
                return service.history(
                    token,
                    path,
                    limit=_qint(query, "limit") or 50,
                    offset=_qint(query, "offset") or 0,
                )
            if action == "diff":
                return service.diff(
                    token,
                    path,
                    a=_qint(query, "a") if _qint(query, "a") is not None else _qint(query, "from"),
                    b=_qint(query, "b") if _qint(query, "b") is not None else _qint(query, "to"),
                )
            if action == "blame":
                return service.blame(token, path)
            if action == "links":
                return service.links(token, path, version=_qint(query, "version"))
            if action == "backlinks":
                return service.backlinks(token, path)
            if action == "toc":
                return service.toc(token, path, version=_qint(query, "version"))
            if action == "export":
                return service.export(token, path, version=_qint(query, "version"))
            if action is None:
                return service.read(
                    token,
                    path,
                    version=_qint(query, "version"),
                    section=_q1(query, "section"),
                    line_start=_qint(query, "line_start"),
                    line_end=_qint(query, "line_end"),
                    max_chars=_qint(query, "max_chars"),
                    session=_q1(query, "session"),
                    model=_q1(query, "model"),
                )
        if method == "POST":
            if action == "delete":
                return service.delete(
                    token,
                    path,
                    _body_value(body, "reason", default=""),
                    base_version=_body_value(body, "base_version"),
                    batch_id=_body_value(body, "batch_id", "batch"),
                )
            if action == "restore":
                return service.restore(
                    token,
                    path,
                    _body_value(body, "reason", default=""),
                    base_version=_body_value(body, "base_version"),
                    batch_id=_body_value(body, "batch_id", "batch"),
                )
            if action == "rollback":
                return service.rollback_version(
                    token,
                    path,
                    int(_body_value(body, "to_version", "to", default=0)),
                    _body_value(body, "reason", default=""),
                )
            if action is None:
                return service.write(
                    token,
                    path,
                    _body_value(body, "content", default=""),
                    _body_value(body, "reason", default=""),
                    base_version=_body_value(body, "base_version"),
                    expected_sha256=_body_value(body, "expected_sha256"),
                    batch_id=_body_value(body, "batch_id", "batch"),
                    session=_body_value(body, "session"),
                    model=_body_value(body, "model"),
                )
        if method == "DELETE" and action is None:
            return service.delete(
                token,
                path,
                _body_value(body, "reason", default=""),
                base_version=_body_value(body, "base_version"),
            )
        raise HttpError(404, "Действие для документа не найдено.")

    def _rollback(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        kind = rest[0] if rest else ""
        if method == "POST" and kind in ("author", "by-author"):
            return service.rollback_author(
                token,
                _body_value(body, "author", default=""),
                since=_body_value(body, "since"),
                until=_body_value(body, "until"),
                reason=_body_value(body, "reason", default=""),
            )
        if method == "POST" and kind in ("batch", "by-batch"):
            return service.rollback_batch(
                token,
                _body_value(body, "batch_id", "batch", default=""),
                _body_value(body, "reason", default=""),
            )
        if method == "POST":
            path = _body_value(body, "path") or _q1(query, "path")
            return service.rollback_version(
                token,
                path,
                int(_body_value(body, "to_version", "to", default=0)),
                _body_value(body, "reason", default=""),
            )
        raise HttpError(404, "Откат: POST /rollback или /rollback/author|batch.")

    def _tokens(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        if method == "GET" and not rest:
            return service.token_list(token)
        if method == "POST" and not rest:
            if _body_value(body, "action") == "revoke":
                return service.token_revoke(
                    token,
                    token_id=_body_value(body, "token_id", "id"),
                    value=_body_value(body, "value", "token_value"),
                    name=_body_value(body, "name"),
                )
            return service.token_create(
                token,
                _body_value(body, "name", default=""),
                role=_body_value(body, "role", default="agent"),
                scopes=_body_value(body, "scopes", "scope"),
                ttl=_body_value(body, "ttl"),
            )
        if method == "POST" and rest and rest[0] == "revoke":
            return service.token_revoke(
                token,
                token_id=_body_value(body, "token_id", "id"),
                value=_body_value(body, "value", "token_value"),
                name=_body_value(body, "name"),
            )
        if method == "DELETE" and rest:
            return service.token_revoke(token, token_id=int(rest[0]))
        raise HttpError(404, "Токены: GET/POST /tokens, DELETE /tokens/<id>.")

    def _tasks(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        if method == "GET":
            if rest:
                return service.task(token, "show", task_id=rest[0])
            return service.task(
                token,
                "list",
                status=_q1(query, "status"),
                assignee=_q1(query, "assignee"),
                area=_q1(query, "area"),
                priority=_qint(query, "priority"),
                limit=_qint(query, "limit"),
                offset=_qint(query, "offset") or 0,
            )
        if method == "POST":
            if rest and len(rest) >= 2:
                action = rest[1]
                return service.task(token, action, task_id=rest[0], **body)
            if rest:
                return service.task(token, "show", task_id=rest[0])
            action = _body_value(body, "action")
            if action:
                params = dict(body)
                params.pop("action", None)
                return service.task(token, action, **params)
            return service.task(token, "create", **body)
        raise HttpError(404, "Задачи: GET/POST /tasks, GET/POST /tasks/<id>/<action>.")

    def _inbox(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        if method == "GET" and not rest:
            return service.inbox(token, "list", unread=_qbool(query, "unread"))
        if method == "POST":
            action = rest[0] if rest else _body_value(body, "action")
            if action is None:
                action = "send" if "to" in body else "list"
            params = dict(body)
            params.pop("action", None)
            return service.inbox(token, action, **params)
        raise HttpError(404, "Inbox: GET /inbox, POST /inbox/{send,read}.")

    def _sandbox(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
        token: str,
    ) -> Any:
        service = self.app.service
        action = rest[0] if rest else "run"
        if action == "run" and method == "POST":
            return service.sandbox_run(
                token,
                _body_value(body, "script", default=""),
                _body_value(body, "language", "lang", default="python"),
                inputs=_body_value(body, "inputs"),
                limits=_body_value(body, "limits"),
                cache=bool(_body_value(body, "cache", default=False)),
            )
        job_id = (rest[1] if len(rest) > 1 else None) or _body_value(
            body, "id", "job_id"
        ) or _q1(query, "id")
        if action == "status" and method in ("GET", "POST"):
            return service.sandbox_status(token, job_id or "")
        if action == "cancel" and method in ("POST", "DELETE"):
            return service.sandbox_cancel(token, job_id or "")
        raise HttpError(
            404,
            "Песочница: POST /sandbox/run, GET /sandbox/status/<id>, "
            "POST /sandbox/cancel/<id>.",
        )

    def _rpc(self, body: dict[str, Any], token: str) -> Any:
        service = self.app.service
        op = _body_value(body, "op", "method")
        if not op:
            raise HttpError(400, "Не указан op.")
        params = dict(body)
        params.pop("op", None)
        params.pop("method", None)
        allowed = {
            "read", "write", "delete", "restore", "history", "diff", "blame",
            "rollback_version", "rollback_author", "rollback_batch", "export",
            "search", "ls", "toc", "links", "backlinks", "broken_links",
            "changes", "readme", "audit", "stats", "who_knows", "task", "inbox",
            "brief", "token_create", "token_list", "token_revoke",
            "sandbox_run", "sandbox_status", "sandbox_cancel",
        }
        if op not in allowed:
            raise HttpError(400, f"Неизвестная операция RPC: {op}.")
        method = getattr(service, op)
        return method(token, **params)


OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "Artek Brain HTTP API",
        "version": "0.1.0",
        "description": (
            "JSON API общей памяти и доски задач. Все операции кроме "
            "/health и /openapi.json требуют Bearer-токен."
        ),
    },
    "servers": [{"url": "http://127.0.0.1:8765"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"}
        }
    },
    "security": [{"bearerAuth": []}],
    "paths": {
        "/health": {"get": {"summary": "Проверка сервера", "security": []}},
        "/openapi.json": {"get": {"summary": "Это описание", "security": []}},
        "/documents/{path}": {
            "get": {"summary": "Прочитать документ"},
            "post": {"summary": "Записать новую версию"},
            "delete": {"summary": "Мягко удалить документ"},
        },
        "/documents/{path}/history": {"get": {"summary": "История версий"}},
        "/documents/{path}/diff": {"get": {"summary": "Diff версий"}},
        "/documents/{path}/blame": {"get": {"summary": "Авторы разделов"}},
        "/documents/{path}/links": {"get": {"summary": "Исходящие ссылки"}},
        "/documents/{path}/backlinks": {"get": {"summary": "Обратные ссылки"}},
        "/documents/{path}/rollback": {"post": {"summary": "Откат к версии"}},
        "/documents/{path}/delete": {"post": {"summary": "Удалить документ"}},
        "/documents/{path}/restore": {"post": {"summary": "Восстановить документ"}},
        "/search": {"get": {"summary": "Поиск", "parameters": [
            {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "mode", "in": "query", "schema": {
                "type": "string", "enum": ["fts", "semantic", "hybrid"]
            }},
        ]}},
        "/ls": {"get": {"summary": "Список каталога"}},
        "/tree": {"get": {"summary": "Дерево памяти"}},
        "/changes": {"get": {"summary": "Лента изменений"}},
        "/audit": {"get": {"summary": "Журнал аудита"}},
        "/stats": {"get": {"summary": "Статистика"}},
        "/tokens": {
            "get": {"summary": "Список токенов"},
            "post": {"summary": "Создать токен"},
        },
        "/tokens/{id}": {"delete": {"summary": "Отозвать токен по id"}},
        "/tasks": {
            "get": {"summary": "Список задач"},
            "post": {"summary": "Создать задачу"},
        },
        "/tasks/{id}/{action}": {"post": {"summary": "Действие с задачей"}},
        "/inbox": {
            "get": {"summary": "Входящие"},
            "post": {"summary": "Отправить письмо"},
        },
        "/brief": {"get": {"summary": "Бриф агента"}},
        "/sandbox/run": {
            "post": {
                "summary": "Выполнить скрипт в песочнице",
                "description": (
                    "Сеть выключена; данные — только read-only срез по inputs. "
                    "Результат: stdout/stderr, /out, код, время, память."
                ),
            }
        },
        "/sandbox/status/{id}": {"get": {"summary": "Статус запуска"}},
        "/sandbox/cancel/{id}": {"post": {"summary": "Отменить запуск"}},
        "/rpc": {"post": {"summary": "Вызов любой операции по имени"}},
    },
}


def create_server(
    service: Service,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    max_body: int = MAX_BODY,
    quiet: bool = False,
) -> BrainHTTPServer:
    """Создать HTTP-сервер, не запуская его."""
    server = BrainHTTPServer((host, int(port)), service, max_body=max_body)
    server.quiet = quiet  # type: ignore[attr-defined]
    return server


def serve(
    service: Service,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    max_body: int = MAX_BODY,
) -> None:
    """Запустить сервер до Ctrl+C."""
    server = create_server(service, host, port, max_body=max_body)
    bound_host, bound_port = server.server_address[:2]
    print(
        f"Artek Brain HTTP: http://{bound_host}:{bound_port}/  (OpenAPI: /openapi.json)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
