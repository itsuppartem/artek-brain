"""CLI Artek Brain: тонкая оболочка над фасадом Brain и координацией агентов."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import ArtekBrainError, PermissionDenied
from .service import Service, resolve_root

PROG = "brain"


def _add_common(parser: argparse.ArgumentParser, *, top: bool = False) -> None:
    """Добавить общие флаги. В подкомандах значения не затирают верхние."""
    default = None if top else argparse.SUPPRESS
    parser.add_argument(
        "--root",
        default=default,
        help="Каталог хранилища (по умолчанию BRAIN_HOME или ./.brain).",
    )
    parser.add_argument(
        "--token",
        default=default,
        help="Токен доступа (по умолчанию BRAIN_TOKEN).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False if top else argparse.SUPPRESS,
        help="Машинный вывод JSON.",
    )


def _token(args: argparse.Namespace, root: Path | None = None) -> str | None:
    """Токен: --token, BRAIN_TOKEN или файл agent.token в хранилище."""
    explicit = getattr(args, "token", None) or os.environ.get("BRAIN_TOKEN")
    if explicit:
        return explicit
    if root is not None:
        from .project import read_token_file

        return read_token_file(root)
    return None


def _hook(args: argparse.Namespace, root: Path) -> None:
    """Напечатать бриф памяти для хука SessionStart; никогда не падать.

    Хук Claude Code кладёт stdout в контекст агента, в том числе после
    сжатия контекста. Нет хранилища или токена — молча ничего не печатаем,
    чтобы не мешать сессии.
    """
    try:
        if not root.exists():
            return
        token = _token(args, root)
        if not token:
            return
        service = Service(root)
        try:
            data = service.coord_action(token, "brief")
        finally:
            service.close()
        text = data.get("brief") if isinstance(data, dict) else str(data)
        if not text:
            return
        print(
            "Память проекта (artek-brain, MCP-сервер brain). Бриф на сейчас:\n"
            + text
            + "\nДальше: brain_read нужных документов из брифа, "
            "brain_search перед правкой незнакомой части."
        )
    except Exception:  # pragma: no cover - хук не должен ломать старт сессии
        return


def _read_content(args: argparse.Namespace) -> str:
    if getattr(args, "content", None) is not None:
        return args.content
    filename = getattr(args, "file", None)
    if filename and filename != "-":
        return Path(filename).read_text(encoding="utf-8")
    return sys.stdin.read()


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _print_text(command: str, data: Any) -> None:
    if data is None:
        print("нет данных")
        return
    if isinstance(data, str):
        print(data)
        return
    if command == "quality" and isinstance(data, dict):
        action = data.get("action")
        if action == "report":
            print(f"документов: {data.get('documents', 0)}")
            print(
                f"покрытие refs: {data.get('with_refs', 0)} "
                f"({data.get('coverage', 0):.2%})"
            )
            print(
                f"просрочено review_by: {data.get('overdue', 0)} "
                f"({data.get('freshness_overdue', 0):.2%})"
            )
            print(
                f"просрочено по сроку свежести (без review_by): "
                f"{data.get('overdue_by_ttl', 0)}"
            )
            for path in data.get("overdue_by_ttl_paths", [])[:10]:
                print(f"  - {path}")
            print(
                f"свежих: {data.get('fresh_ratio', 1.0):.0%} "
                f"(по {data.get('freshness_source', 'review_by')})"
            )
            lagging = data.get("docs_lagging", [])
            print(f"отстали от заметок: {len(lagging)}")
            for item in lagging[:10]:
                print(f"  - {item['path']} (+{item['notes_after']} заметок после правки)")
            print(f"stale: {data.get('stale', 0)}")
            print(f"открытая очередь ревью: {data.get('review_queue_open', 0)}")
            labels = (
                ("without_frontmatter", "без frontmatter"),
                ("without_owner", "без owner"),
                ("without_review_by", "без review_by"),
                ("without_refs", "без refs"),
                ("overdue_paths", "просрочен review_by"),
            )
            for key, title in labels:
                if key not in data:
                    continue
                paths = data.get(key) or []
                print(f"{title}: {len(paths)}")
                for path in paths:
                    print(f"  - {path}")
            return
        if action == "links":
            items = data.get("items", [])
            print(f"битых ссылок: {len(items)}")
            for item in items:
                print(f"- {item['source']}: {item['target']}")
            return
        if action == "drift":
            items = data.get("items", [])
            print(f"дрифт: {len(items)}")
            for item in items:
                print(
                    f"- {item['doc']} v{item['version']}: {item['ref']} "
                    f"({item['reason']}, было {item['expected']}, "
                    f"стало {item.get('actual') or 'нет'})"
                )
            for item in data.get("marked", []):
                print(f"  stale: {item['path']} v{item['version']}")
            return
        if action == "dups":
            print(f"похожих пар: {len(data.get('pairs', []))}")
            for pair in data.get("pairs", []):
                print(
                    f"- {pair['path_a']} ~ {pair['path_b']} "
                    f"({pair['score']:.3f})"
                )
            return
        if action == "contradictions":
            items = data.get("items", [])
            print(f"противоречий: {len(items)}")
            for item in items:
                where_a = item["path_a"] + (
                    f":{item['line_a']}" if item.get("line_a") else ""
                )
                where_b = item["path_b"] + (
                    f":{item['line_b']}" if item.get("line_b") else ""
                )
                print(
                    f"- {item['key']}: {item['value_a']} ({where_a}) "
                    f"!= {item['value_b']} ({where_b})"
                )
            return
        if action == "queue":
            if "item" in data:
                item = data["item"]
                print(
                    f"#{item['id']} {item['status']}: {item['ref']} "
                    f"({item['reason']})"
                )
                return
            items = data.get("items", [])
            print(f"записей в очереди: {len(items)}")
            for item in items:
                print(
                    f"#{item['id']} [{item['status']}] {item['ref']} "
                    f"{item['reason']}: {item['detail']}"
                )
            return
        _print_json(data)
        return
    if command == "read" and isinstance(data, dict):
        print(data.get("content", ""), end="")
        return
    if command == "export" and isinstance(data, dict):
        print(data.get("content", ""), end="")
        return
    if command == "diff" and isinstance(data, dict):
        print(data.get("diff", ""), end="")
        return
    if command == "sandbox" and isinstance(data, dict):
        if "stdout" in data:
            print(data.get("stdout", ""), end="")
            if data.get("stderr"):
                print(data["stderr"], file=sys.stderr, end="")
            print(
                f"\n[exit={data.get('exit_code')} "
                f"time={data.get('duration_ms')}ms "
                f"mem={data.get('peak_memory_bytes')}b "
                f"out={len(data.get('out') or [])}]",
                file=sys.stderr,
            )
            return
        _print_json(data)
        return
    if command == "search" and isinstance(data, dict):
        items = data.get("items", [])
        print(f"Найдено: {data.get('total', len(items))}")
        for item in items:
            print(f"- {item['path']} v{item['version']} ({item['author']}) {item['title']}")
            snippet = item.get("snippet") or ""
            if snippet:
                print(f"  {snippet}")
        return
    if command in ("ls", "tree") and isinstance(data, dict):
        for node in data.get("nodes", []):
            mark = "/" if node.get("kind") == "dir" else ""
            print(f"{node['path']}{mark}  v{node.get('versions', 0)}  {node.get('size', 0)}b")
        return
    if command == "history" and isinstance(data, list):
        for item in data:
            print(
                f"v{item['n']}  {item['ts']}  {item['author']}  {item.get('op')}  "
                f"{item.get('reason') or ''}"
            )
        return
    if command == "blame" and isinstance(data, list):
        for item in data:
            print(f"{'#' * item['level']} {item['title']}  v{item['version']}  {item['author']}")
        return
    if command == "tokens" and isinstance(data, list):
        for item in data:
            state = "revoked" if item.get("revoked_at") else "active"
            scopes = ", ".join(
                f"{scope['prefix']}:{scope['level']}" for scope in item.get("scopes", [])
            )
            print(f"#{item['id']}  {item['name']}  {item['role']}  {state}  {scopes}")
        return
    if command == "changes" and isinstance(data, dict):
        for item in data.get("items", []):
            print(f"{item['ts']}  {item['actor']}  {item['action']}  {item.get('path') or ''}")
        print(f"next_cursor={data.get('next_cursor')}")
        return
    if command == "audit" and isinstance(data, list):
        for item in data:
            print(f"{item['ts']}  {item['actor']}  {item['action']}  {item.get('path') or ''}")
        return
    if command == "inbox" and isinstance(data, list):
        for item in data:
            mark = " " if item.get("read") else "*"
            print(f"{mark}#{item['id']}  {item['ts']}  от {item['sender']}: {item['subject']}")
        return
    if command == "brief" and isinstance(data, dict):
        print(data.get("brief", ""))
        return
    if command == "token-create" and isinstance(data, dict):
        print(data.get("token", ""))
        return
    _print_json(data)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Artek Brain: общая память, поиск и доска задач.",
    )
    _add_common(parser, top=True)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        _add_common(item)
        return item

    setup = add("setup", "развернуть мозг в проекте: .brain, токены, .mcp.json, AGENTS.md")
    setup.add_argument("project", nargs="?", default=".", help="Каталог проекта (по умолчанию текущий).")
    setup.add_argument("--name", default=None, help="Название проекта для README памяти.")

    hook = add(
        "hook",
        "вывод для хуков агентов: бриф памяти на старте сессии и после сжатия",
    )
    hook.add_argument(
        "event",
        nargs="?",
        default="session-start",
        choices=["session-start"],
        help="Событие хука (пока только session-start).",
    )

    init = add("init", "инициализировать хранилище и выдать owner-токен")
    init.add_argument("--owner-name", default="owner", help="Имя owner-токена.")
    init.add_argument("--force", action="store_true", help="Создать токен даже если owner уже есть.")

    token = add("token", "управление токенами")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="создать токен")
    _add_common(create)
    create.add_argument("--name", required=True, help="Имя агента/токена.")
    create.add_argument("--role", default="agent", choices=["owner", "human", "agent", "auditor"])
    create.add_argument(
        "--scope",
        action="append",
        default=None,
        help="Право вида docs:write или docs:read; можно повторять.",
    )
    create.add_argument("--ttl", type=float, default=None, help="Срок жизни в секундах.")
    revoke = token_sub.add_parser("revoke", help="отозвать токен")
    _add_common(revoke)
    revoke.add_argument("--id", type=int, default=None, help="id токена.")
    revoke.add_argument("--value", default=None, help="Значение токена.")
    revoke.add_argument("--name", default=None, help="Отозвать все токены с именем.")
    for action, text in (
        ("suspend", "приостановить токен"),
        ("resume", "снять приостановку токена"),
    ):
        item = token_sub.add_parser(action, help=text)
        _add_common(item)
        item.add_argument("--id", type=int, default=None, help="id токена.")
        item.add_argument("--value", default=None, help="Значение токена.")
        item.add_argument("--name", default=None, help="Имя токена.")
    token_list = token_sub.add_parser("list", help="список токенов")
    _add_common(token_list)

    ls = add("ls", "список каталога")
    ls.add_argument("prefix", nargs="?", default="")
    ls.add_argument("--tree", action="store_true", help="Показать дерево.")
    ls.add_argument("--max-depth", type=int, default=None)

    tree = add("tree", "дерево памяти")
    tree.add_argument("prefix", nargs="?", default="")
    tree.add_argument("--max-depth", type=int, default=None)

    read = add("read", "прочитать документ")
    read.add_argument("path")
    read.add_argument("--version", type=int, default=None)
    read.add_argument("--section", default=None)
    read.add_argument("--lines", default=None, help="Диапазон строк START:END.")
    read.add_argument("--max-chars", type=int, default=None)
    read.add_argument("--session", default=None)
    read.add_argument("--model", default=None)
    read.add_argument("--output", default=None, help="Записать содержимое в файл.")

    write = add("write", "записать документ из файла или stdin")
    write.add_argument("path")
    write.add_argument("--file", default=None, help="Файл с содержимым; - значит stdin.")
    write.add_argument("--content", default=None, help="Содержимое строкой.")
    write.add_argument("--reason", required=True)
    write.add_argument("--base-version", type=int, default=None)
    write.add_argument("--expected-sha256", default=None)
    write.add_argument("--batch", dest="batch_id", default=None)
    write.add_argument("--session", default=None)
    write.add_argument("--model", default=None)

    diff = add("diff", "сравнить версии")
    diff.add_argument("path")
    diff.add_argument("--from", dest="a", type=int, default=None)
    diff.add_argument("--to", dest="b", type=int, default=None)

    history = add("history", "история версий")
    history.add_argument("path")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--offset", type=int, default=0)

    blame = add("blame", "авторы разделов")
    blame.add_argument("path")

    rollback = add("rollback", "откат версии, автора или пачки")
    rollback.add_argument("path", nargs="?", default=None)
    rollback.add_argument("--path", dest="path_opt", default=None)
    rollback.add_argument("--to", dest="to_version", type=int, default=None)
    rollback.add_argument("--author", default=None)
    rollback.add_argument("--since", default=None)
    rollback.add_argument("--until", default=None)
    rollback.add_argument("--batch", dest="batch_id", default=None)
    rollback.add_argument("--reason", required=True)

    delete = add("delete", "мягко удалить документ")
    _add_delete_flags(delete)

    restore = add("restore", "восстановить документ")
    _add_delete_flags(restore)

    search = add("search", "полнотекстовый поиск")
    search.add_argument("query")
    search.add_argument("--prefix", dest="path_prefix", default=None)
    search.add_argument("--type", dest="doc_type", default=None)
    search.add_argument("--status", default=None)
    search.add_argument("--tag", dest="tags", action="append", default=None)
    search.add_argument("--author", default=None)
    search.add_argument("--since", default=None)
    search.add_argument("--until", default=None)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--history", action="store_true", help="Искать по всем версиям.")
    search.add_argument(
        "--semantic",
        action="store_true",
        help="Гибридный поиск: FTS + семантика (RRF).",
    )
    search.add_argument(
        "--mode",
        choices=["fts", "semantic", "hybrid"],
        default=None,
        help="Режим поиска: fts, semantic или hybrid.",
    )

    semantic = add("semantic", "семантический поиск: состояние и настройка")
    semantic_sub = semantic.add_subparsers(dest="semantic_command", required=True)
    s_status = semantic_sub.add_parser("status", help="включена ли семантика и состояние индекса")
    _add_common(s_status)
    s_enable = semantic_sub.add_parser("enable", help="включить семантику (только owner)")
    _add_common(s_enable)
    s_enable.add_argument(
        "--embedder", choices=["hash", "ollama"], default="hash", help="Тип эмбеддера."
    )
    s_enable.add_argument("--dim", type=int, default=None, help="Размерность (hash).")
    s_enable.add_argument("--model", default=None, help="Модель Ollama.")
    s_enable.add_argument("--url", default=None, help="Адрес Ollama.")
    s_disable = semantic_sub.add_parser("disable", help="выключить семантику (только owner)")
    _add_common(s_disable)
    s_reindex = semantic_sub.add_parser("reindex", help="перестроить индекс (только owner)")
    _add_common(s_reindex)

    links = add("links", "исходящие ссылки")
    links.add_argument("path")
    links.add_argument("--version", type=int, default=None)

    backlinks = add("backlinks", "обратные ссылки")
    backlinks.add_argument("path")

    changes = add("changes", "лента изменений")
    changes.add_argument("--cursor", type=int, default=None)
    changes.add_argument("--limit", type=int, default=100)

    audit = add("audit", "журнал аудита")
    audit.add_argument("--limit", type=int, default=100)
    audit.add_argument("--actor", default=None)
    audit.add_argument("--action", default=None)
    audit.add_argument("--since", default=None)

    add("stats", "статистика хранилища")

    repack = add("repack", "переупаковать версии в дельты")
    repack.add_argument(
        "--snapshot-every",
        type=int,
        default=None,
        help="Полный снимок каждые N версий (по умолчанию из хранилища).",
    )

    disk_report = add("disk-report", "отчёт о месте на диске")
    disk_report.add_argument("--days", type=int, default=30)
    disk_report.add_argument("--forecast-days", dest="forecast_days", type=int, default=30)

    backup = add("backup", "резервные копии хранилища")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_sub.add_parser("create", help="создать архив хранилища")
    _add_common(backup_create)
    backup_create.add_argument(
        "--output", required=True, help="Каталог для архивов и манифестов."
    )
    backup_create.add_argument(
        "--keep", type=int, default=None, help="Хранить N последних архивов."
    )
    backup_verify = backup_sub.add_parser("verify", help="проверить архив")
    _add_common(backup_verify)
    backup_verify.add_argument("archive", help="Путь к brain-backup-*.tar.gz.")

    add("maintain", "обслуживание: WAL checkpoint, gc, repack, отчёт")

    quality = add("quality", "качество знаний: отчёты, drift, дубли, противоречия, очередь")
    quality_sub = quality.add_subparsers(dest="quality_command", required=True)
    q_report = quality_sub.add_parser("report", help="метрики качества")
    _add_common(q_report)
    q_report.add_argument("--prefix", default="", help="Ограничить префиксом пути.")
    q_report.add_argument(
        "--details", action="store_true", help="Показать пути документов для исправления."
    )
    q_links = quality_sub.add_parser("links", help="битые внутренние ссылки")
    _add_common(q_links)
    q_links.add_argument("--prefix", default="", help="Ограничить префиксом пути.")
    q_drift = quality_sub.add_parser("drift", help="привязки refs к файлам репозитория")
    _add_common(q_drift)
    q_drift.add_argument("--repo", default=".", help="Корень git-репозитория с кодом.")
    q_drift.add_argument("--prefix", default="", help="Ограничить префиксом пути.")
    q_drift.add_argument("--mark-stale", action="store_true", help="Пометить документы stale.")
    q_dups = quality_sub.add_parser("dups", help="похожие документы (Jaccard)")
    _add_common(q_dups)
    q_dups.add_argument("--prefix", default="", help="Ограничить префиксом пути.")
    q_dups.add_argument("--threshold", type=float, default=None)
    q_dups.add_argument("--limit", type=int, default=100)
    q_contra = quality_sub.add_parser("contradictions", help="противоречия в знаниях")
    _add_common(q_contra)
    q_contra.add_argument("--prefix", default="", help="Ограничить префиксом пути.")
    q_contra.add_argument("--limit", type=int, default=200)
    q_queue = quality_sub.add_parser("queue", help="очередь ревью")
    _add_common(q_queue)
    q_queue.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "sync", "approve", "reject", "defer"],
    )
    q_queue.add_argument("--id", dest="item_id", type=int, default=None)
    q_queue.add_argument("--note", default="")
    q_queue.add_argument("--until", default=None, help="Дата ISO для defer.")
    q_queue.add_argument("--status", default="open")
    q_queue.add_argument("--prefix", default="")
    q_queue.add_argument("--limit", type=int, default=100)

    coord = add("coord", "присутствие агентов и кто менял путь")
    coord.add_argument("action", help="presence, agents, who-knows.")
    coord.add_argument("--activity", default=None)
    coord.add_argument("--ttl", dest="ttl_seconds", type=int, default=None)
    coord.add_argument("--path", default=None)

    inbox = add("inbox", "входящие: list, send, read")
    inbox.add_argument("action", nargs="?", default="list")
    inbox.add_argument("--unread", action="store_true")
    inbox.add_argument("--to", default=None)
    inbox.add_argument("--subject", default=None)
    inbox.add_argument("--body", default=None)
    inbox.add_argument("--message-id", dest="message_id", action="append", type=int, default=None)

    brief = add("brief", "бриф для агента")
    brief.add_argument("--limit", type=int, default=2000)

    export = add("export", "экспорт Markdown")
    export.add_argument("path")
    export.add_argument("--version", type=int, default=None)
    export.add_argument("--output", default=None)
    export.add_argument("--session", default=None)
    export.add_argument("--model", default=None)

    proposal = add("proposal", "предложения агентов и утверждение")
    proposal_sub = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_list = proposal_sub.add_parser("list", help="список предложений")
    _add_common(proposal_list)
    proposal_list.add_argument("--status", default=None, choices=["pending", "approved", "rejected"])
    proposal_show = proposal_sub.add_parser("show", help="показать предложение")
    _add_common(proposal_show)
    proposal_show.add_argument("path")
    proposal_approve = proposal_sub.add_parser("approve", help="утвердить предложение")
    _add_common(proposal_approve)
    proposal_approve.add_argument("path")
    proposal_approve.add_argument("--to", dest="to", default=None, help="Целевой защищённый путь.")
    proposal_approve.add_argument("--reason", default=None)
    proposal_reject = proposal_sub.add_parser("reject", help="отклонить предложение")
    _add_common(proposal_reject)
    proposal_reject.add_argument("path")
    proposal_reject.add_argument("--reason", default=None)
    proposal_verify = proposal_sub.add_parser("verify", help="проверить дрейф approved/")
    _add_common(proposal_verify)

    anomaly = add("anomaly", "детекторы аномалий")
    anomaly_sub = anomaly.add_subparsers(dest="anomaly_command", required=True)
    anomaly_scan = anomaly_sub.add_parser("scan", help="найти аномалии")
    _add_common(anomaly_scan)
    anomaly_scan.add_argument("--auto-suspend", dest="auto_suspend", action="store_true", default=None)
    anomaly_scan.add_argument("--no-auto-suspend", dest="auto_suspend", action="store_false")
    anomaly_list = anomaly_sub.add_parser("list", help="список инцидентов")
    _add_common(anomaly_list)
    anomaly_list.add_argument("--actor", default=None)
    anomaly_list.add_argument("--kind", default=None)
    anomaly_list.add_argument("--limit", type=int, default=100)
    sandbox = add("sandbox", "песочница скриптов рядом с данными")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    sandbox_run = sandbox_sub.add_parser("run", help="выполнить скрипт")
    _add_common(sandbox_run)
    sandbox_run.add_argument(
        "script",
        nargs="?",
        default="-",
        help="Файл скрипта; - или пусто — читать stdin.",
    )
    sandbox_run.add_argument(
        "--lang",
        "--language",
        dest="language",
        default="python",
        choices=["python", "shell", "sh", "bash"],
        help="Язык скрипта (python или shell).",
    )
    sandbox_run.add_argument(
        "--paths",
        action="append",
        default=None,
        help="Путь или префикс среза (docs/ — префикс); можно повторять.",
    )
    sandbox_run.add_argument(
        "--prefix", action="append", default=None, help="Префикс среза."
    )
    sandbox_run.add_argument("--max-documents", type=int, default=None)
    sandbox_run.add_argument("--max-bytes", type=int, default=None)
    sandbox_run.add_argument("--cpu", type=int, default=None, help="Лимит CPU, секунд.")
    sandbox_run.add_argument(
        "--wall", type=float, default=None, help="Лимит wall-time, секунд."
    )
    sandbox_run.add_argument(
        "--memory", type=int, default=None, help="Лимит памяти, байт."
    )
    sandbox_run.add_argument(
        "--processes", type=int, default=None, help="Лимит числа процессов."
    )
    sandbox_run.add_argument(
        "--max-output", type=int, default=None, help="Лимит stdout/stderr, байт."
    )
    sandbox_run.add_argument("--cache", action="store_true", help="Вернуть кэш.")

    sandbox_status = sandbox_sub.add_parser("status", help="статус запуска")
    _add_common(sandbox_status)
    sandbox_status.add_argument("id")

    sandbox_cancel = sandbox_sub.add_parser("cancel", help="отменить запуск")
    _add_common(sandbox_cancel)
    sandbox_cancel.add_argument("id")

    serve = add("serve", "запустить HTTP JSON API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--max-body", type=int, default=1024 * 1024)

    mcp = add("mcp", "запустить MCP stdio сервер")
    mcp.add_argument("--max-chars", type=int, default=8000)

    return parser


def _add_delete_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--base-version", type=int, default=None)
    parser.add_argument("--batch", dest="batch_id", default=None)


def _parse_lines(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    parts = value.replace("-", ":").split(":")
    if len(parts) != 2:
        raise ValueError("Диапазон строк задаётся как START:END, например 10:20.")
    return int(parts[0]), int(parts[1])


def _read_script(name: str | None) -> str:
    if not name or name == "-":
        return sys.stdin.read()
    try:
        return Path(name).read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtekBrainError(
            f"Не удалось прочитать скрипт {name!r}: {exc}.",
            hint="Передайте путь к файлу или - для stdin.",
        ) from exc


def _sandbox_inputs(args: argparse.Namespace) -> dict[str, Any] | None:
    inputs: dict[str, Any] = {}
    if getattr(args, "paths", None):
        inputs["paths"] = list(args.paths)
    if getattr(args, "prefix", None):
        inputs["prefix"] = list(args.prefix)
    if getattr(args, "max_documents", None) is not None:
        inputs["max_documents"] = args.max_documents
    if getattr(args, "max_bytes", None) is not None:
        inputs["max_bytes"] = args.max_bytes
    return inputs or None


def _sandbox_limits(args: argparse.Namespace) -> dict[str, Any] | None:
    mapping = {
        "cpu_seconds": getattr(args, "cpu", None),
        "wall_seconds": getattr(args, "wall", None),
        "memory_bytes": getattr(args, "memory", None),
        "max_processes": getattr(args, "processes", None),
        "max_output_bytes": getattr(args, "max_output", None),
    }
    limits = {key: value for key, value in mapping.items() if value is not None}
    return limits or None


def _run(args: argparse.Namespace) -> Any:
    root = resolve_root(getattr(args, "root", None))
    command = args.command
    if command == "setup":
        from .project import setup_project

        return setup_project(args.project, name=args.name)

    if command == "hook":
        _hook(args, root)
        return None

    if command != "init" and not root.exists():
        raise ArtekBrainError(
            f"Хранилище памяти не найдено: {root}.",
            hint=(
                "Разверните мозг в проекте (brain setup <каталог>) или brain init, "
                "либо укажите BRAIN_HOME / --root. Пустое хранилище само не создаётся."
            ),
        )

    token = _token(args, root)

    if command == "serve":
        from .http_api import serve

        service = Service(root)
        try:
            serve(
                service,
                host=args.host,
                port=args.port,
                max_body=args.max_body,
            )
        finally:
            service.close()
        return None

    if command == "mcp":
        from .mcp_server import run_stdio

        service = Service(root)
        try:
            run_stdio(service, token, max_response_chars=args.max_chars)
        finally:
            service.close()
        return None

    service = Service(root)
    try:
        if command == "init":
            tokens = [
                item for item in service.access.list_tokens() if item.role == "owner"
            ]
            if tokens and not args.force:
                raise ArtekBrainError(
                    f"Хранилище уже инициализировано: {root}.",
                    hint="Создайте токен через brain token create или передайте --force.",
                )
            issued = service.access.create_token(args.owner_name, "owner")
            return {"root": str(root), **_json_issued(issued)}
        if command == "token":
            if args.token_command == "create":
                return service.token_create(
                    token,
                    args.name,
                    role=args.role,
                    scopes=args.scope,
                    ttl=args.ttl,
                )
            if args.token_command == "revoke":
                return service.token_revoke(
                    token,
                    token_id=args.id,
                    value=args.value,
                    name=args.name,
                )
            if args.token_command == "suspend":
                return service.token_suspend(
                    token,
                    token_id=args.id,
                    value=args.value,
                    name=args.name,
                )
            if args.token_command == "resume":
                return service.token_resume(
                    token,
                    token_id=args.id,
                    value=args.value,
                    name=args.name,
                )
            return service.token_list(token)
        if command in ("ls", "tree"):
            return service.ls(
                token,
                args.prefix,
                tree=(command == "tree" or args.tree),
                max_depth=args.max_depth,
            )
        if command == "read":
            start, end = _parse_lines(args.lines)
            data = service.read(
                token,
                args.path,
                version=args.version,
                section=args.section,
                line_start=start,
                line_end=end,
                max_chars=args.max_chars,
                session=args.session,
                model=args.model,
            )
            if args.output:
                Path(args.output).write_text(data["content"], encoding="utf-8")
                return {"path": args.path, "output": args.output}
            return data
        if command == "write":
            content = _read_content(args)
            return service.write(
                token,
                args.path,
                content,
                args.reason,
                base_version=args.base_version,
                expected_sha256=args.expected_sha256,
                batch_id=args.batch_id,
                session=args.session,
                model=args.model,
            )
        if command == "diff":
            return service.diff(token, args.path, a=args.a, b=args.b)
        if command == "history":
            return service.history(
                token, args.path, limit=args.limit, offset=args.offset
            )
        if command == "blame":
            return service.blame(token, args.path)
        if command == "rollback":
            path = args.path or args.path_opt
            if args.batch_id:
                return service.rollback_batch(
                    token, args.batch_id, args.reason
                )
            if args.author:
                return service.rollback_author(
                    token,
                    args.author,
                    since=args.since,
                    until=args.until,
                    reason=args.reason,
                )
            if path is None or args.to_version is None:
                raise ArtekBrainError(
                    "Не указан способ отката.",
                    hint="Передайте PATH и --to, либо --author, либо --batch.",
                )
            return service.rollback_version(
                token, path, args.to_version, args.reason
            )
        if command == "delete":
            return service.delete(
                token,
                args.path,
                args.reason,
                base_version=args.base_version,
                batch_id=args.batch_id,
            )
        if command == "restore":
            return service.restore(
                token,
                args.path,
                args.reason,
                base_version=args.base_version,
                batch_id=args.batch_id,
            )
        if command == "search":
            mode = args.mode or ("hybrid" if args.semantic else "fts")
            return service.search(
                token,
                args.query,
                path_prefix=args.path_prefix,
                doc_type=args.doc_type,
                status=args.status,
                tags=args.tags,
                author=args.author,
                since=args.since,
                until=args.until,
                limit=args.limit,
                offset=args.offset,
                history=args.history,
                mode=mode,
            )
        if command == "semantic":
            if args.semantic_command == "status":
                return service.semantic_status(token)
            if args.semantic_command == "enable":
                return service.semantic_enable(
                    token,
                    embedder=args.embedder,
                    dim=args.dim,
                    model=args.model,
                    url=args.url,
                )
            if args.semantic_command == "disable":
                return service.semantic_disable(token)
            return service.semantic_reindex(token)
        if command == "links":
            return service.links(token, args.path, version=args.version)
        if command == "backlinks":
            return service.backlinks(token, args.path)
        if command == "changes":
            return service.changes(token, cursor=args.cursor, limit=args.limit)
        if command == "audit":
            return service.audit(
                token,
                limit=args.limit,
                actor=args.actor,
                action=args.action,
                since=args.since,
            )
        if command == "stats":
            return service.stats(token)
        if command == "repack":
            return service.repack(
                token, snapshot_every=args.snapshot_every
            )
        if command == "disk-report":
            return service.disk_report(
                token, days=args.days, forecast_days=args.forecast_days
            )
        if command == "backup":
            if args.backup_command == "create":
                return service.backup_create(
                    token, args.output, keep=args.keep
                )
            return service.backup_verify(token, args.archive)
        if command == "maintain":
            return service.maintain(token)
        if command == "quality":
            return _quality(service, token, args)
        if command == "coord":
            params = {
                key: getattr(args, key)
                for key in ("activity", "ttl_seconds", "path")
                if getattr(args, key) is not None
            }
            return service.coord_action(token, args.action, **params)
        if command == "inbox":
            params = {
                key: value
                for key, value in (
                    ("unread", args.unread or None),
                    ("to", args.to),
                    ("subject", args.subject),
                    ("body", args.body),
                    ("message_id", args.message_id),
                )
                if value is not None
            }
            return service.inbox(token, args.action, **params)
        if command == "brief":
            return service.brief(token, limit=args.limit)
        if command == "sandbox":
            if args.sandbox_command == "run":
                return service.sandbox_run(
                    token,
                    _read_script(args.script),
                    args.language,
                    inputs=_sandbox_inputs(args),
                    limits=_sandbox_limits(args),
                    cache=args.cache,
                )
            if args.sandbox_command == "status":
                return service.sandbox_status(token, args.id)
            return service.sandbox_cancel(token, args.id)
        if command == "export":
            data = service.export(
                token,
                args.path,
                version=args.version,
                session=args.session,
                model=args.model,
            )
            if args.output:
                Path(args.output).write_text(data["content"], encoding="utf-8")
                return {"path": args.path, "output": args.output}
            return data
        if command == "proposal":
            return service.proposal(
                token,
                args.proposal_command,
                path=getattr(args, "path", None),
                status=getattr(args, "status", None),
                to=getattr(args, "to", None),
                reason=getattr(args, "reason", None),
            )
        if command == "anomaly":
            if args.anomaly_command == "scan":
                return service.anomaly_scan(token, auto_suspend=args.auto_suspend)
            return service.anomaly_list(
                token,
                actor=args.actor,
                kind=args.kind,
                limit=args.limit,
            )
        raise ArtekBrainError(f"Неизвестная команда «{command}».")
    finally:
        service.close()


def _quality(service: Service, token: str | None, args: argparse.Namespace) -> Any:
    """Выполнить подкоманду quality и вернуть JSON-совместимые данные."""
    from .drift import Drift
    from .quality import Quality
    from .review_queue import ReviewQueue

    quality = Quality(service.brain)
    action = args.quality_command
    if action == "report":
        return {
            "action": "report",
            **quality.report(
                token or "", args.prefix, details=bool(getattr(args, "details", False))
            ),
        }
    if action == "links":
        links = quality.broken_links(token or "", args.prefix)
        return {
            "action": "links",
            "items": [item.as_dict() for item in links],
        }
    if action == "drift":
        drift = Drift(service.brain, args.repo)
        items = drift.check(token or "", prefix=args.prefix)
        marked = (
            drift.mark_stale(token or "", items=items)
            if args.mark_stale
            else []
        )
        return {
            "action": "drift",
            "items": [item.as_dict() for item in items],
            "marked": [item.as_dict() for item in marked],
        }
    if action == "dups":
        pairs = quality.duplicates(
            token or "",
            prefix=args.prefix,
            threshold=args.threshold,
            limit=args.limit,
        )
        return {
            "action": "dups",
            "threshold": quality.threshold if args.threshold is None else args.threshold,
            "pairs": [item.as_dict() for item in pairs],
        }
    if action == "contradictions":
        items = quality.contradictions(
            token or "", prefix=args.prefix, limit=args.limit
        )
        return {
            "action": "contradictions",
            "items": [item.as_dict() for item in items],
        }
    queue = ReviewQueue(service.store, service.access)
    if args.action in ("list", "sync"):
        added = queue.sync(service.brain, token or "", prefix=args.prefix)
        if args.action == "sync":
            return {"action": "queue", "added": [item.as_dict() for item in added]}
        items = queue.list(token, status=args.status, limit=args.limit)
        return {"action": "queue", "items": [item.as_dict() for item in items]}
    if args.item_id is None:
        raise ArtekBrainError(
            "Не указан id записи очереди.",
            hint="Передайте --id из brain quality queue list.",
        )
    if args.action == "approve":
        item = queue.approve(token or "", args.item_id, args.note)
    elif args.action == "reject":
        item = queue.reject(token or "", args.item_id, args.note)
    else:
        item = queue.defer(token or "", args.item_id, args.note, args.until)
    return {"action": "queue", "item": item.as_dict()}


def _json_issued(issued: Any) -> dict[str, Any]:
    return json.loads(json.dumps(issued.as_dict(), ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код возврата."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        data = _run(args)
    except PermissionDenied as exc:
        _error(args, exc)
        return 3 if exc.action == "authenticate" else 1
    except ArtekBrainError as exc:
        _error(args, exc)
        return 1
    except (OSError, ValueError) as exc:
        _error(args, exc)
        return 1
    if data is None:
        return 0
    if getattr(args, "json", False):
        _print_json(data)
    else:
        _print_text(_output_command(args), data)
    return 0


def _output_command(args: argparse.Namespace) -> str:
    if args.command == "token":
        return "tokens" if args.token_command == "list" else "token-create"
    return args.command


def _error(args: argparse.Namespace, exc: Exception) -> None:
    message = str(exc)
    if getattr(args, "json", False):
        payload = {
            "ok": False,
            "error": type(exc).__name__,
            "message": message,
            "hint": getattr(exc, "hint", None),
        }
        _print_json(payload)
    else:
        print(f"Ошибка: {message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
