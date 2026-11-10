# BRAIN-9: песочница скриптов и реестр инструментов

## Что сделано
- `sandbox.py`: `Sandbox(config, store, access)`, `run/submit/status/cancel`; bwrap с
  `--unshare-all`, `--die-with-parent`, `--new-session`, `--cap-drop ALL`, `--clearenv`;
  read-only срез `/data`, хелпер `brainlib`, запись только в `/out` и `/tmp`.
- Каждый запуск — transient systemd-scope `brain-sbx-<id>` с `TasksMax`,
  `MemoryMax`, `CPUQuota`; bwrap работает от host-uid 65534, поэтому RLIMIT_NPROC
  действует. Нет systemd-run/bwrap — `SandboxUnavailable`, тихого запуска нет.
- Таймаут/лимит процессов/вывода и отмена убивают scope через `systemctl`,
  плюс `prlimit` на CPU/AS/FSIZE/NPROC.
- Аудит: кто, sha256, скрипт в Store `runs/sandbox/<id>/script.<ext>` (с очисткой
  секретов), срез и версии, код/время/размеры. Кэш по hash+срез+лимиты.
- Состояние job сохраняется в `<root>/sandbox/jobs`, поэтому CLI `status|cancel`
  видят запуск из другого процесса.
- `registry.py`: `sandbox.run` — первый инструмент; права роли, суточный лимит,
  журнал вызовов, общий аудит; секреты инструментов в отдельной таблице.
- CLI `brain sandbox run|status|cancel`, HTTP `/sandbox/run|status|cancel`,
  MCP `brain_help("sandbox")` + `brain_task(action="sandbox_run")`.

## Как проверить
- `uv run pytest -q` — 154 теста зелёные, включая реальные запуски bwrap.
- `test_fork_bomb_hits_process_limit`: max_processes=8, wall=4; завершается за
  0.12 с, load не растёт, cgroup-scope после теста удалён.

## Что не сделано
- `/out` — bind-каталог на хосте (не отдельный tmpfs), размер ограничен
  монитором и `RLIMIT_FSIZE`.
- Сеть выключена полностью; списка разрешённых хостов нет.
