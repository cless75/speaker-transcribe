#!/bin/bash
# speaker-transcribe watcher wrapper — macOS node M4-MAC (STANDBY / резервный узел).
#
# Роль узла: резерв. Основной узел — LENOVO-AMD (CUDA). Мак подхватывает очередь
# ТОЛЬКО когда основной недоступен: молчит дольше PRIMARY_STALE_MIN минут ЛИБО
# отчитался аварийной фазой. Пока основной здоров — sweep не запускается вовсе.
#
# ВЕТКА: узел живёт на `release` (git checkout release), не на `main` — см.
# docs/deployment.md. Self-update ниже тянет именно ту ветку, на которой стоит клон.
#
# ЗАПУСК: не напрямую из launchd, а через .app-обёртку (scripts/install-mac-node-app.sh).
# Из «голого» launchd-процесса Google Drive не материализует dataless-файлы:
# read() -> EDEADLK. Ротация логов живёт в launcher'е обёртки, не здесь.
#
# Explicit PATH: launchd/non-login shells lack /opt/homebrew/bin & ~/.local/bin.
# Neutralize dead system proxy (127.0.0.1:7890 Clash) so pip/huggingface go direct.
# Load HF token secret from ~/.config (chmod 600), keep it out of the plist/git.
set -u

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= http_proxy= https_proxy= all_proxy=
export NO_PROXY="*" no_proxy="*"
if [ -f "$HOME/.config/speaker-transcribe/secrets.env" ]; then
  set -a; . "$HOME/.config/speaker-transcribe/secrets.env"; set +a
fi

REPO="$HOME/work/speaker-transcribe"
VENV_PY="$HOME/work/venvs/asr/bin/python"
CONFIG="$REPO/config/node.local.json"

PRIMARY_HOST="${PRIMARY_HOST:-LENOVO-AMD}"
PRIMARY_STALE_MIN="${PRIMARY_STALE_MIN:-45}"

# --- self-update: тянем ветку узла (release) ПЕРЕД standby-guard ---
# Обновляться после guard бессмысленно: пока основной узел жив, guard выходит
# раньше — и резерв не обновляется никогда. Так и вышло: мак месяцами работал на
# старом коде. --ff-only + gitignored node.local.json дают чистый fast-forward или
# чистый пропуск; сетевая ошибка логируется и не срывает sweep. SELF_UPDATE=0 —
# отключить (ручная диагностика на конкретной версии).
if [ "${SELF_UPDATE:-1}" = "1" ] && [ -d "$REPO/.git" ]; then
  version_before="$(cat "$REPO/VERSION" 2>/dev/null || echo '?')"
  pull_out="$(git -C "$REPO" pull --ff-only 2>&1)" || true
  version_after="$(cat "$REPO/VERSION" 2>/dev/null || echo '?')"
  branch_now="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  if [ "$version_before" != "$version_after" ]; then
    printf '[%s] engine updated: %s -> %s (%s)\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$version_before" "$version_after" "$branch_now"
  else
    printf '[%s] engine up to date: %s (%s)\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$version_after" "$branch_now"
  fi
  case "$pull_out" in
    *error*|*fatal*|*Aborting*)
      printf '[%s] engine self-update skipped: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$pull_out" ;;
  esac
fi

# --- standby guard: работаем только если основной узел недоступен ---
# SKIP_STANDBY_GUARD=1 — принудительный прогон (ручная диагностика).
if [ "${SKIP_STANDBY_GUARD:-0}" != "1" ]; then
  guard_verdict="$("$VENV_PY" - "$CONFIG" "$PRIMARY_HOST" "$PRIMARY_STALE_MIN" <<'PY'
import datetime as dt, json, pathlib, sys

# Фазы, при которых основной узел считается неработоспособным, даже если он
# продолжает исправно обновлять свой статус-файл. Проверено на живом инциденте:
# LENOVO-AMD падал каждый sweep (WinError 87), статус обновлялся каждые 12 минут,
# и резерв по одной лишь свежести метки считал его здоровым.
DEAD_PHASES = {"crashed", "failed", "error"}

config_path, primary, stale_min = sys.argv[1], sys.argv[2], float(sys.argv[3])
try:
    cfg = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
    hub = pathlib.Path(cfg["hub_root"])
    status = hub / "_status" / f"{primary}.json"
    if not status.is_file():
        print(f"RUN|статус {primary} отсутствует")
        raise SystemExit(0)
    data = json.loads(status.read_text(encoding="utf-8"))
    phase = str(data.get("phase") or "").lower()
    stamp = dt.datetime.fromisoformat(str(data.get("updated_at")))
    age_min = (dt.datetime.now() - stamp).total_seconds() / 60.0
    if phase in DEAD_PHASES:
        print(f"RUN|{primary} в фазе '{phase}' ({age_min:.0f} мин назад)")
    elif age_min > stale_min:
        print(f"RUN|{primary} молчит {age_min:.0f} мин (порог {stale_min:.0f})")
    else:
        print(f"SKIP|{primary} жив ({phase}, {age_min:.0f} мин назад)")
except Exception as exc:
    # Хаб недоступен / статус нечитаем — резерв не должен молчать из-за этого.
    print(f"RUN|проверка статуса не удалась: {type(exc).__name__}: {exc}")
PY
)"
  printf '[%s] standby-guard: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${guard_verdict#*|}"
  case "$guard_verdict" in
    SKIP*) exit 0 ;;
  esac
fi

exec "$VENV_PY" "$REPO/src/audio_inbox_watch.py" --config "$CONFIG" --once "$@"
