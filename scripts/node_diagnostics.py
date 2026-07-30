#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""801 — диагностика ASR-узла. Отвечает: использует ли узел CUDA и заработает ли
voiceprint enroll. Ничего не меняет — только читает и печатает.

Главное: результат НЕ ТОЛЬКО печатается в консоль, но и ПИШЕТСЯ ФАЙЛОМ прямо в
_meta Hub, помеченный идентификатором машины — чтобы не ловить вывод в терминале.

    {hub_root}/_meta/801-diag-{HOST}.txt

Живёт в репозитории (scripts/) и доставляется на узлы по git pull. Запуск на узле
(любым Python; он сам найдёт venv узла из node.local.json):

    python scripts/node_diagnostics.py

Без аргументов скрипт берёт репозиторий как свой родительский каталог. Если
запускается из копии вне репо (например из Hub/_meta), укажи путь явно:

    python node_diagnostics.py --repo "C:\\work\\speaker-transcribe"

Проверки (torch/faster-whisper/speechbrain/rapidocr) выполняются В ИНТЕРПРЕТАТОРЕ
из поля transcribe_python — именно им движок гоняет ASR.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import sys

LINE = "=" * 62
_BUF: list[str] = []


def emit(text: str = "") -> None:
    """Печать в консоль + накопление для файла отчёта."""
    print(text)
    _BUF.append(text)


def local_ip() -> str:
    """Best-effort основной IPv4 (без внешних запросов)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # ничего не отправляет, только выбирает интерфейс
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "?"


def run_in_venv(venv: str, code: str) -> str:
    # Force UTF-8 in the child so Cyrillic prints don't come back as cp1251 mojibake
    # (a venv Python without PYTHONUTF8 defaults to the OEM code page on Windows).
    import os
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        out = subprocess.run([venv, "-X", "utf8", "-c", code], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"  (не удалось запустить venv: {exc})\n"


def write_report(dest_dir: pathlib.Path, host_tag: str) -> pathlib.Path | None:
    """Пишет накопленный отчёт в {dest_dir}/801-diag-{host_tag}.txt (перезапись)."""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host_tag) or "node"
        path = dest_dir / f"801-diag-{safe}.txt"
        path.write_text("\n".join(_BUF) + "\n", encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"  (не удалось записать отчёт в {dest_dir}: {exc})")
        return None


def default_repo() -> pathlib.Path:
    """Repo root. When run from scripts/ inside the repo, that's the parent dir;
    from a stray copy (e.g. Hub/_meta) fall back to the canonical install path."""
    here = pathlib.Path(__file__).resolve().parent.parent
    if (here / "config" / "node.local.json").is_file() or (here / "src").is_dir():
        return here
    return pathlib.Path(r"C:\work\speaker-transcribe")


def maybe_pause(auto: bool) -> None:
    """Пауза в конце — чтобы человек успел прочитать отчёт в сессии.

    Ждёт нажатия ОДНОЙ клавиши. Пропускается: (1) в авторежиме (--auto); (2) когда ввод
    не интерактивный — scheduled task / пайп / запуск из агента (stdin не TTY), чтобы
    автоматические прогоны не подвисали в ожидании клавиши.
    """
    if auto:
        return
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
        return
    prompt = "\n  [нажмите любую клавишу, чтобы закрыть окно] "
    try:
        if sys.platform == "win32":
            import msvcrt
            print(prompt, end="", flush=True)
            msvcrt.getch()          # читает один символ без Enter
            print()
        else:
            print(prompt, end="", flush=True)
            sys.stdin.read(1)
    except Exception:
        pass


def query_watch_task() -> str:
    """Состояние scheduled task вотчера (Windows). Стабильные поля через PowerShell."""
    if sys.platform != "win32":
        return "(не Windows — проверь launchd/cron вручную)"
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "foreach($n in 'speaker-transcribe-watch','MS-Audio-Inbox-Watch'){"
        "$t=Get-ScheduledTask -TaskName $n;"
        "if($t){$i=Get-ScheduledTaskInfo -TaskName $n;"
        "Write-Output ('{0}|{1}|{2}|{3}' -f $n,$t.State,$i.LastRunTime,$i.LastTaskResult);break}}"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        out = (r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(ошибка запроса: {exc})"
    if not out:
        return "НЕ НАЙДЕНА (ни speaker-transcribe-watch, ни MS-Audio-Inbox-Watch) — зарегистрировать install-watch-task.ps1 (admin)"
    parts = out.split("|")
    name = parts[0] if len(parts) > 0 else "?"
    state = parts[1] if len(parts) > 1 else "?"
    last = parts[2] if len(parts) > 2 else "?"
    res = parts[3] if len(parts) > 3 else "?"
    hint = ""
    if str(res).strip() in ("267011", "267009"):
        hint = " (267011=ни разу не запускалась / 267009=выполняется)"
    return f"'{name}' — {state}, посл.запуск {last}, результат {res}{hint}"


def collect_env_refs(cfg: dict | None) -> list[str]:
    """Все ссылки вида 'env:VARNAME' в конфиге (рекурсивно) — их проверяем в окружении."""
    found: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and v.startswith("env:"):
            name = v[4:].strip()
            if name and name not in found:
                found.append(name)

    walk(cfg or {})
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(default_repo()))
    ap.add_argument("--auto", "--no-pause", dest="auto", action="store_true",
                    help="автоматический режим: не ждать клавишу в конце (для scheduled/agent)")
    args = ap.parse_args()

    cfg_path = pathlib.Path(args.repo) / "config" / "node.local.json"
    hostname = socket.gethostname()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    emit(LINE)
    emit(" 801 ДИАГНОСТИКА УЗЛА")
    emit(LINE)
    emit(f"  время прогона : {now}")
    emit(f"  hostname      : {hostname}")
    emit(f"  IP            : {local_ip()}")
    emit(f"  ОС            : {platform.platform()}")

    # Куда писать отчёт: приоритет — hub_root/_meta из конфига; запас — папка скрипта
    # (если скрипт лежит в _meta, это тот же путь и работает даже при битом конфиге).
    hub_meta: pathlib.Path | None = None
    host_label = hostname

    emit(f"\n[1] Конфиг: {cfg_path}")
    cfg: dict | None = None
    if not cfg_path.is_file():
        emit("  ОШИБКА: файл не найден — укажи --repo")
    else:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            emit("  JSON валиден")
        except Exception as exc:  # noqa: BLE001
            emit(f"  ОШИБКА JSON: {exc}")
            emit("  (узел не стартует, пока это не исправлено)")

    venv = ""
    if cfg is not None:
        node = cfg.get("node") or {}
        rt = cfg.get("runtime") or {}
        venv = cfg.get("transcribe_python") or ""
        host_label = node.get("host_label") or hostname
        hub_root = cfg.get("hub_root")
        if hub_root:
            candidate = pathlib.Path(str(hub_root)).expanduser() / "_meta"
            if candidate.parent.exists():
                hub_meta = candidate
        emit(f"  host_label       : {node.get('host_label')}")
        emit(f"  device / compute : {rt.get('device')} / {rt.get('compute_type')}")
        emit(f"  diar device      : {rt.get('diarization_device')}")
        emit(f"  voiceprint_mode  : {cfg.get('voiceprint_mode')}")
        emit(f"  video_frames     : {(cfg.get('video_frames') or {}).get('mode') or 'выкл'}")
        emit(f"  transcribe_python: {venv}")

    if hub_meta is None:
        # запас: рядом со скриптом (обычно это и есть _meta, раз его оттуда и запускают)
        hub_meta = pathlib.Path(__file__).resolve().parent

    if venv and pathlib.Path(venv).is_file():
        emit("\n[2] CUDA (использует ли узел GPU)")
        emit(run_in_venv(venv, (
            "import importlib.util as u,sys\n"
            "print('  python         :', sys.version.split()[0])\n"
            "if u.find_spec('torch'):\n"
            "    import torch\n"
            "    ok=torch.cuda.is_available()\n"
            "    name=('| '+torch.cuda.get_device_name(0)) if ok else ''\n"
            "    print('  torch.cuda     :', ok, name)\n"
            "else:\n"
            "    print('  torch          : НЕ УСТАНОВЛЕН')\n"
        )).rstrip())
        emit("  faster-whisper на CUDA (главный признак — считает ли ASR на GPU):")
        emit(run_in_venv(venv, (
            "try:\n"
            "    from faster_whisper import WhisperModel\n"
            "    WhisperModel('tiny', device='cuda', compute_type='float16')\n"
            "    print('  faster-whisper CUDA: OK  -> ASR пойдёт на GPU')\n"
            "except Exception as e:\n"
            "    print('  faster-whisper CUDA: НЕТ ->', str(e).splitlines()[0][:110])\n"
            "    print('  (движок молча уйдёт на CPU; для GPU обычно нужно:')\n"
            "    print('     <venv> -m pip install nvidia-cudnn-cu12 nvidia-cublas-cu12 )')\n"
        )).rstrip())

        emit("\n[3] Voiceprint enroll (накопление голосовых отпечатков)")
        emit(run_in_venv(venv, (
            "import importlib.util as u\n"
            "sb=bool(u.find_spec('speechbrain')); ta=bool(u.find_spec('torchaudio'))\n"
            "print('  speechbrain    :', sb)\n"
            "print('  torchaudio     :', ta)\n"
            "if sb and ta:\n"
            "    print('  -> зависимости на месте; enroll должен писать профили')\n"
            "else:\n"
            "    print('  -> enroll не заработает (enabled=false, status=pending).')\n"
            "    print('     поставить: <venv> -m pip install speechbrain torchaudio')\n"
        )).rstrip())

        emit("\n[4] OCR слайдов (rapidocr)")
        emit(run_in_venv(venv, (
            "import importlib.util as u\n"
            "ok=bool(u.find_spec('rapidocr_onnxruntime'))\n"
            "print('  rapidocr_onnxruntime:', ok, '' if ok else "
            "'-> <venv> -m pip install rapidocr_onnxruntime')\n"
        )).rstrip())
    else:
        emit("\n[2-4] Пропущено: интерпретатор venv не найден "
             "(transcribe_python в конфиге неверен или конфиг не читается).")

    emit("\n[5] ffmpeg")
    ff = shutil.which("ffmpeg")
    emit(f"  {ff if ff else 'НЕ НАЙДЕН в PATH (нужен для нарезки клипов и кадров слайдов)'}")

    if venv and pathlib.Path(venv).is_file():
        emit("\n[6b] Возможность CUDA-диаризации (pyannote на GPU)")
        smi = shutil.which("nvidia-smi")
        if smi:
            try:
                q = subprocess.run([smi, "--query-gpu=name,driver_version,memory.total",
                                    "--format=csv,noheader"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30)
                emit(f"  GPU (nvidia-smi): {(q.stdout or q.stderr).strip()}")
            except Exception as exc:  # noqa: BLE001
                emit(f"  nvidia-smi: ошибка запуска: {exc}")
        else:
            emit("  nvidia-smi: НЕ НАЙДЕН — драйвер NVIDIA не установлен или не в PATH")
        emit(run_in_venv(venv, (
            "import importlib.util as u\n"
            "if not u.find_spec('torch'):\n"
            "    print('  torch: НЕ УСТАНОВЛЕН'); raise SystemExit\n"
            "import torch\n"
            "print('  torch версия   :', torch.__version__)\n"
            "print('  torch CUDA-build:', torch.version.cuda or 'НЕТ (CPU-only сборка)')\n"
            "print('  torch.cuda ok  :', torch.cuda.is_available())\n"
            "try:\n"
            "    import torchaudio; print('  torchaudio     :', torchaudio.__version__)\n"
            "except Exception as e: print('  torchaudio     :', e)\n"
            "if torch.version.cuda:\n"
            "    print('  -> torch собран с CUDA; если torch.cuda ok=True, diarization_device: cuda заработает')\n"
            "else:\n"
            "    print('  -> torch CPU-only. Для GPU-диаризации ПЕРЕУСТАНОВИТЬ torch+torchaudio с CUDA')\n"
            "    print('     под драйвер из nvidia-smi (cu121 обычно безопасно):')\n"
            "    print('     <venv> -m pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu121')\n"
            "    print('     ВНИМАНИЕ: версии torch/torchaudio должны совпадать; проверить pyannote после.')\n"
        )).rstrip())

    emit("\n[6] Версия кода на узле (git) — на том ли коммите, что webm/voiceprint фиксы")
    repo = pathlib.Path(args.repo)
    if (repo / ".git").exists():
        def _git(*a: str) -> str:
            try:
                r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=30)
                return (r.stdout or r.stderr or "").strip()
            except Exception as exc:  # noqa: BLE001
                return f"(git error: {exc})"
        emit(f"  ветка       : {_git('rev-parse', '--abbrev-ref', 'HEAD')}")
        emit(f"  коммит      : {_git('log', '-1', '--format=%h %s')}")
        emit(f"  дата коммита: {_git('log', '-1', '--format=%ci')}")
        webm = _git("log", "-1", "--format=%h", "--", "src/media_transcribe.py")
        has_status = "есть" if (repo / "src" / "node_status.py").exists() else "НЕТ (старый код)"
        emit(f"  node_status : {has_status}")
        emit("  Свежий код содержит коммит 49da32b (webm fix). Если коммит на узле старше —")
        emit("  сделать: git pull  (пуш уже в origin). 'нечего обновлять' при коммите 49da32b = всё ок.")
    else:
        emit(f"  (не git-репозиторий: {repo})")

    # -----------------------------------------------------------------------
    emit("\n[7] Вотчер / источники / расписание (почему может быть тихо)")
    emit(f"  scheduled task : {query_watch_task()}")

    if cfg is not None:
        node = cfg.get("node") or {}
        hub_root = cfg.get("hub_root")
        if hub_root:
            hp = pathlib.Path(str(hub_root)).expanduser()
            emit(f"  hub_root       : {hub_root}  ->  " +
                 ("смонтирован" if hp.exists() else "НЕ НАЙДЕН (Google Drive не смонтирован?)"))
        else:
            emit("  hub_root       : не задан в конфиге")
        cache_root = node.get("cache_root")
        if cache_root:
            cp = pathlib.Path(str(cache_root)).expanduser()
            emit(f"  cache_root     : {cache_root}  ->  " + ("ок" if cp.exists() else "НЕ НАЙДЕН"))
        win = str(cfg.get("process_window_local") or "").strip()
        if win:
            emit(f"  process_window : {win}  (ВНЕ окна вотчер молча выходит — обойти: watch.ps1 -ForceWindow)")
        else:
            emit("  process_window : не задано (работает круглосуточно)")
        if cfg.get("respect_cpu_load", False):
            emit(f"  cpu gate       : ON, порог {cfg.get('cpu_threshold_percent', 70)}% (выше — откладывает прогон)")
        else:
            emit("  cpu gate       : off")
        for s in (cfg.get("sources") or []):
            if not isinstance(s, dict):
                continue
            root_tpl = str(s.get("root") or "")
            root = root_tpl.replace("{hub_root}", str(hub_root or ""))
            rp = pathlib.Path(root).expanduser()
            kind = s.get("discover") or s.get("route") or ""
            emit(f"  source         : {root_tpl}  ->  " +
                 ("ок" if rp.exists() else "НЕ НАЙДЕН") + (f" ({kind})" if kind else ""))
    else:
        emit("  (конфиг не прочитан — проверки Hub/окна/источников пропущены)")

    log_path = repo / "logs" / "watch.log"
    if log_path.is_file():
        st = log_path.stat()
        age_min = (dt.datetime.now().timestamp() - st.st_mtime) / 60
        emit(f"  watch.log      : есть, {st.st_size} байт, обновлён {age_min:.0f} мин назад")
    else:
        emit(f"  watch.log      : НЕТ ({log_path}) — задача ещё не запускалась")
    emit("  Напоминание: watch.log пишет только scheduled task; ручной watch.ps1 -Once печатает в консоль.")

    # -----------------------------------------------------------------------
    emit("\n[8] Среда и переменные (Error 103: python не найден в PATH)")
    try:
        import getpass
        cur_user = getpass.getuser()
    except Exception:  # noqa: BLE001
        cur_user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    emit(f"  текущий пользователь : {cur_user}")
    pth = shutil.which("python") or shutil.which("python3")
    emit("  python в PATH        : " +
         (pth or "НЕ НАЙДЕН — bare python не резолвится (это Error 103; задача под другим юзером?)"))
    if venv:
        vp = pathlib.Path(str(venv)).expanduser()
        emit(f"  transcribe_python    : {venv}  ->  " +
             ("ок" if vp.exists() else "НЕ НАЙДЕН (venv отсутствует или другой путь)"))
    else:
        emit("  transcribe_python    : не задан — вотчер возьмёт bare 'python' из PATH (уязвимо к Error 103)")
    refs = collect_env_refs(cfg) if cfg is not None else []
    if not refs:
        emit("  env-переменные       : в конфиге нет ссылок env:VAR")
    missing = False
    for name in refs:
        val = os.environ.get(name)
        if val:
            emit(f"  env:{name:15}: установлен (…{val[-4:]})")
        else:
            missing = True
            emit(f"  env:{name:15}: НЕ УСТАНОВЛЕН в этой сессии")
    if missing:
        emit("    -> ставить на уровне МАШИНЫ (чтобы работало под любым юзером и после ребута), напр.:")
        for name in refs:
            if not os.environ.get(name):
                emit(f'       [Environment]::SetEnvironmentVariable("{name}","<значение>","Machine")')
    emit("  Подсказка: задача с триггером AtLogon наследует PATH/env ТОГО юзера, что залогинен.")

    emit("\n" + LINE)
    emit(" [2]/[3] — GPU и voiceprint; [7] — почему вотчер молчит (задача/Hub/окно/источники/лог);")
    emit(" [8] — среда (пользователь/python-в-PATH/venv/env-переменные, Error 103).")
    emit(LINE)

    written = write_report(hub_meta, host_label)
    if written:
        # печатается ПОСЛЕ записи, поэтому в файле этой строки нет — и хорошо
        print(f"\n>>> отчёт записан: {written}")
        print(">>> он лежит в _meta Hub — можно прочитать с любой машины / из чата")
    maybe_pause(args.auto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
