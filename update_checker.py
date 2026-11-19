"""
Update Checker — проверка и установка обновлений программ в Windows 11.

Источники: winget, Microsoft Store, Chocolatey, Scoop.
Портативный режим: все данные лежат в settings.json рядом с программой.
"""
import base64
import csv
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

try:
    import winreg
except ImportError:  # запуск не на Windows (только для разработки)
    winreg = None

VERSION = "1.3.0"
APP_NAME = "Update Checker"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEVNULL = subprocess.DEVNULL
WG_FLAGS = ["--accept-source-agreements", "--disable-interactivity"]
WG_PKG = ["--accept-package-agreements", "--accept-source-agreements",
          "--disable-interactivity", "--silent"]

SOURCE_LABEL = {"winget": "winget", "msstore": "Microsoft Store", "choco": "Chocolatey",
                "scoop": "Scoop", "": "—"}
STATUS_LABEL = {"update": "⬆ Доступно обновление", "ok": "✔ Актуальна",
                "unknown": "· Нет в базах", "pinned": "📌 Закреплена"}
STATUS_ORDER = {"update": 0, "pinned": 1, "ok": 2, "unknown": 3}
ERR_MARKS = ("⚠", "❌", "✖", "ERROR", "Ошибка")
WINGET_HINTS = {0x8A15002B: "обновление не применимо (программа уже актуальна или требуется другой источник)"}

UNINSTALL_KEYS = [
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", 0x0100),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", 0),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU", 0),
]

HELP_TEXT = """КАК ПОЛЬЗОВАТЬСЯ

1. Проверка запускается сама при старте (и по кнопке «Проверить» / F5).
   Программы с доступными обновлениями всегда стоят в начале списка.
2. Поставьте галочки ☑ слева от программ (клик по ☑ в заголовке отмечает
   все обновления) и нажмите «Обновить отмеченные».
   Успешно обновлённая программа сразу исчезает из списка обновлений.
3. Если программа запущена, вас спросят, можно ли её закрыть. Сначала
   отправляется обычная команда закрытия окна, затем — принудительная.
4. Если установка «зависла» (например, на «Starting package install…»),
   нажмите «⏹ Остановить». Через 90 секунд без вывода программа сама
   предупредит об этом в журнале.

📌 ЗАКРЕПИТЬ ВЕРСИЮ — программа не обновляется ни кнопкой «Обновить все»,
   ни отметкой.

↩ ОТКАТ — установит выбранную старую версию (winget, Chocolatey, Scoop;
   Microsoft Store откат не поддерживает). Вкладка «История и откат»
   показывает все обновления: выберите запись и нажмите «Откатить».

ДРАЙВЕРА — вкладка проверяет Центр обновления Windows (только подписанные
   драйверы WHQL; сторонних баз нет, поэтому «чужой» драйвер не подставится).
   Перед установкой создаётся точка восстановления и копия текущего драйвера.
   Риск: 🟢 низкий, 🟡 средний, 🔴 критичный (чипсет, система, накопители,
   прошивки). «Обновить рекомендуемые» критичные не трогает. «📌 Не обновлять»
   исключает драйвер, «↩ Откат» возвращает прежнюю версию. Видеодрайверы
   обычно свежее на сайте производителя — есть кнопка «🌐 Сайт производителя».
   Обновляйте драйверы, только если есть проблема или нужное исправление.

ЖУРНАЛ — потяните за разделитель над журналом, чтобы изменить его высоту.
   На вкладке «История и откат» клик по записи показывает её строки в журнале
   и подсвечивает ошибку, даже если журнал был очищен или программа перезапущена.

ПРАВА АДМИНИСТРАТОРА — программа запрашивает их при запуске: они нужны
   установщикам, чтобы обновления не зависали на запросе UAC.
"""


# ------------------------------------------------------------ portable I/O ---
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_PATH = os.path.join(app_dir(), "settings.json")
DEFAULTS = {
    "geometry": "1180x760", "close_mode": "ask",
    "sources": {"winget": True, "msstore": True, "choco": True, "scoop": True},
    "drivers": {"restore_point": True, "backup": True, "skip_high_risk": True,
                "hide_inbox": True, "scan_on_start": True},
    "pins": {}, "history": [], "cache": [], "cache_time": "",
    "drv_pins": {}, "drv_cache": [], "drv_cache_time": "",
}


def load_settings():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in ("sources", "drivers") and isinstance(v, dict):
                cfg[k].update(v)
            elif k in cfg and type(v) is type(cfg[k]):
                cfg[k] = v
    except (OSError, ValueError):
        pass
    cfg["cache"] = [c for c in cfg["cache"] if isinstance(c, dict) and "name" in c]
    cfg["drv_cache"] = [c for c in cfg["drv_cache"] if isinstance(c, dict) and "name" in c and "risk" in c]
    return cfg


def save_settings(cfg):
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass  # например, носитель защищён от записи


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Перезапуск с запросом UAC. True — запрос принят (текущий процесс нужно закрыть)."""
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe, params = sys.executable, f'"{os.path.abspath(__file__)}"'
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32
    except Exception:
        return False


# ------------------------------------------------------------------ утилиты ---
def norm(s):
    return re.sub(r"[^0-9a-zа-я]+", "", (s or "").lower())


def key_of(item):
    return norm(item["name"])


def has_digit(s):
    return bool(re.search(r"\d", s or ""))


ERRS = []   # ошибки запуска внешних команд (для диагностики в журнале)
RAW = {}    # последний «сырой» вывод команд


def run_text(cmd, timeout=240, tag=None):
    """Запуск команды с захватом вывода. stdin отключён: иначе в оконном exe процессы виснут/падают."""
    try:
        out = subprocess.run(cmd, stdin=DEVNULL, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", creationflags=NO_WINDOW, timeout=timeout).stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        ERRS.append(f"{os.path.basename(str(cmd[0]))}: {e}")
        out = ""
    out = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", out).lstrip("\ufeff")
    if tag:
        RAW[tag] = out
    return out


def ps_cmd(command):
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]


# --- поиск установленных менеджеров пакетов (в т.ч. когда их нет в PATH) ---
def find_winget():
    return shutil.which("winget")


def find_choco():
    p = shutil.which("choco")
    if p:
        return p
    root = os.environ.get("ChocolateyInstall") or os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey")
    c = os.path.join(root, "bin", "choco.exe")
    return c if os.path.isfile(c) else None


def find_scoop():
    for root in (os.environ.get("SCOOP"), os.path.join(os.path.expanduser("~"), "scoop")):
        if root:
            for n in ("scoop.cmd", "scoop.ps1"):
                p = os.path.join(root, "shims", n)
                if os.path.isfile(p):
                    return p
    return shutil.which("scoop")


def have_winget():
    return bool(find_winget())


def have_choco():
    return bool(find_choco())


def have_scoop():
    return bool(find_scoop())


def choco_cmd(*args):
    return [find_choco() or "choco", *args]


def scoop_prefix():
    p = find_scoop() or "scoop"
    return "scoop" if p == "scoop" else f"& '{p}'"


def scoop_ps(args):
    return ps_cmd(f"{scoop_prefix()} {args}")


def table_rows(text, valid):
    """Разбор таблиц winget/scoop. Для каждой строки пробуем два способа:
    нарезка по позициям колонок и разделение по 2+ пробелам (устойчиво к широким символам)."""
    rows, starts = [], None
    lines = [l.rstrip("\r") for l in text.splitlines()]
    for i, line in enumerate(lines):
        s = line.strip()
        if i > 0 and lines[i - 1].strip() and re.fullmatch(r"-[- ]{4,}", s):
            if " " in s:  # scoop/PowerShell: группы дефисов под колонками
                starts = [m.start() for m in re.finditer(r"-+", line)]
            else:         # winget: сплошная линия, колонки берём из заголовка
                starts = [m.start() for m in re.finditer(r"\S+(?: \S+)*", lines[i - 1])]
            continue
        if not starts or not s:
            continue
        pos = [line[a:b].strip() for a, b in zip(starts, starts[1:] + [None])]
        gap = re.split(r"\s{2,}", s)
        for cols in (pos, gap):
            if valid(cols):
                rows.append(cols)
                break
    return rows


# -------------------------------------------------------- чтение инвентаря ---
def read_installed():
    """Программы из реестра: {name: {version, publisher, location}}."""
    result = {}
    if winreg is None:
        return result
    for path, hive, flag in UNINSTALL_KEYS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
            except OSError:
                continue

            def val(n):
                try:
                    return str(winreg.QueryValueEx(sub, n)[0]).strip()
                except OSError:
                    return ""

            name = val("DisplayName")
            if not name or val("SystemComponent") == "1" or val("ParentKeyName"):
                continue
            if re.match(r"^(KB\d+|Update for|Security Update|Hotfix)", name, re.I):
                continue
            loc = val("InstallLocation").strip('"')
            if not loc:
                icon = val("DisplayIcon").split(",")[0].strip('"')
                if icon.lower().endswith(".exe"):
                    loc = os.path.dirname(icon)
            result.setdefault(name, {"version": val("DisplayVersion"),
                                     "publisher": val("Publisher"), "location": loc})
    return result


def winget_installed():
    out = run_text(["winget", "list"] + WG_FLAGS, tag="winget list")
    valid = lambda c: (len(c) >= 4 and c[1] and " " not in c[1] and has_digit(c[2])
                       and c[-1].lower() in ("winget", "msstore"))
    return [{"name": c[0], "id": c[1], "version": c[2].lstrip("<> "), "source": c[-1].lower()}
            for c in table_rows(out, valid)]


def winget_outdated():
    out = run_text(["winget", "upgrade"] + WG_FLAGS, tag="winget upgrade")
    valid = lambda c: len(c) >= 4 and c[1] and " " not in c[1] and c[2] and has_digit(c[3])
    rows = []
    for c in table_rows(out, valid):
        src = c[4].lower() if len(c) > 4 and c[4] else "winget"
        rows.append({"name": c[0], "id": c[1], "version": c[2].lstrip("<> "),
                     "available": c[3].lstrip("<> "),
                     "source": "msstore" if src.startswith("msstore") else "winget"})
    return rows


def choco_installed():
    out = []
    for line in run_text(choco_cmd("list", "--local-only", "-r", "--no-color"), tag="choco list").splitlines():
        p = line.strip().split("|")
        if len(p) == 2 and has_digit(p[1]):
            out.append({"name": p[0], "id": p[0], "version": p[1], "source": "choco"})
    return out


def choco_outdated():
    out = []
    for line in run_text(choco_cmd("outdated", "-r", "--no-color"), tag="choco outdated").splitlines():
        p = line.strip().split("|")
        if len(p) >= 3 and has_digit(p[1]) and has_digit(p[2]):
            out.append({"name": p[0], "id": p[0], "version": p[1], "available": p[2],
                        "source": "choco"})
    return out


def scoop_installed():
    valid = lambda c: len(c) >= 2 and c[0] and " " not in c[0] and has_digit(c[1])
    return [{"name": c[0], "id": c[0], "version": c[1], "source": "scoop"}
            for c in table_rows(run_text(scoop_ps("list"), tag="scoop list"), valid)]


def scoop_outdated():
    valid = lambda c: len(c) >= 3 and c[0] and " " not in c[0] and has_digit(c[1]) and has_digit(c[2])
    return [{"name": c[0], "id": c[0], "version": c[1], "available": c[2], "source": "scoop"}
            for c in table_rows(run_text(scoop_ps("status"), tag="scoop status"), valid)]


def match(nm, table):
    """Сопоставление названий: точное, затем по вхождению."""
    if nm in table:
        return table[nm]
    if len(nm) >= 4:
        for k, v in table.items():
            if len(k) >= 4 and (k in nm or nm in k):
                return v
    return None


def head(tag):
    raw = [l.strip() for l in (RAW.get(tag) or "").splitlines() if l.strip()]
    return " | ".join(raw[:4])[:300] or "(пусто)"


def scan(sources, log=lambda t: None):
    """Возвращает список программ (dict) с источником, id и статусом."""
    ERRS.clear()
    RAW.clear()
    reg = read_installed()
    log(f"   Реестр Windows: программ найдено — {len(reg)}")
    use_w = bool(sources.get("winget") or sources.get("msstore")) and have_winget()
    use_c = bool(sources.get("choco")) and have_choco()
    use_s = bool(sources.get("scoop")) and have_scoop()
    tasks = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        if use_w:
            tasks["w_inv"], tasks["w_up"] = ex.submit(winget_installed), ex.submit(winget_outdated)
        if use_c:
            tasks["c_inv"], tasks["c_up"] = ex.submit(choco_installed), ex.submit(choco_outdated)
        if use_s:
            tasks["s_inv"], tasks["s_up"] = ex.submit(scoop_installed), ex.submit(scoop_outdated)
        res = {k: f.result() for k, f in tasks.items()}

    for e in ERRS:
        log(f"   ⚠ {e}")
    if use_w:
        n_i, n_u = len(res["w_inv"]), len(res["w_up"])
        log(f"   winget: известных программ {n_i}, обновлений {n_u}")
        if not n_i and not n_u:
            log("   ⚠ winget не вернул таблицу. Начало вывода: " + head("winget list"))
    elif sources.get("winget") or sources.get("msstore"):
        log("   ⚠ winget не найден (установите «Установщик приложений» из Microsoft Store)")
    if use_c:
        log(f"   Chocolatey: пакетов {len(res['c_inv'])}, обновлений {len(res['c_up'])}")
    if use_s:
        log(f"   Scoop: пакетов {len(res['s_inv'])}, обновлений {len(res['s_up'])}")

    def tab(rows):
        return {norm(r["name"]): r for r in rows if sources.get(r["source"], True)}

    inv = {"winget": tab(res.get("w_inv", [])), "choco": tab(res.get("c_inv", [])),
           "scoop": tab(res.get("s_inv", []))}
    ups = {"winget": tab(res.get("w_up", [])), "choco": tab(res.get("c_up", [])),
           "scoop": tab(res.get("s_up", []))}
    items, used = [], set()

    for name, r in sorted(reg.items(), key=lambda x: x[0].lower()):
        nm = norm(name)
        it = {"name": name, "version": r["version"], "available": "", "publisher": r["publisher"],
              "id": "", "source": "", "status": "unknown", "location": r["location"]}
        for grp in ("winget", "choco", "scoop"):
            known, up = match(nm, inv[grp]), match(nm, ups[grp])
            if up:
                it.update(id=up["id"], source=up["source"], available=up["available"],
                          status="update", version=it["version"] or up["version"])
                used.add((up["source"], up["id"]))
                break
            if known:
                it.update(id=known["id"], source=known["source"], status="ok",
                          version=it["version"] or known["version"])
                used.add((known["source"], known["id"]))
                break
        items.append(it)

    seen = {key_of(i) for i in items}
    for grp in ("winget", "choco", "scoop"):   # то, чего нет в реестре (Store, Scoop…)
        for r in list(ups[grp].values()) + list(inv[grp].values()):
            if (r["source"], r["id"]) in used or norm(r["name"]) in seen:
                continue
            if r["source"] == "msstore" and "available" not in r:
                continue  # не засоряем список всеми Store-приложениями
            seen.add(norm(r["name"]))
            used.add((r["source"], r["id"]))
            items.append({"name": r["name"], "version": r["version"],
                          "available": r.get("available", ""), "publisher": "",
                          "id": r["id"], "source": r["source"], "location": "",
                          "status": "update" if r.get("available") else "ok"})
    return items


# ------------------------------------------------------- команды обновления ---
def upgrade_cmd(it):
    s, i = it["source"], it["id"]
    if s in ("winget", "msstore"):
        cmd = ["winget", "upgrade", "--id", i, "--exact"] + WG_PKG
        return cmd + (["--source", "msstore"] if s == "msstore" else [])
    if s == "choco":
        return choco_cmd("upgrade", i, "-y", "--no-progress")
    if s == "scoop":
        return scoop_ps(f"update {i}")
    return None


def rollback_cmd(it, ver):
    s, i = it["source"], it["id"]
    if s == "winget":
        return ["winget", "install", "--id", i, "--exact", "--version", ver, "--force"] + WG_PKG
    if s == "choco":
        return choco_cmd("install", i, "--version", ver, "-y", "--force", "--allow-downgrade", "--no-progress")
    if s == "scoop":
        p = scoop_prefix()
        return ps_cmd(f"{p} uninstall {i}; {p} install {i}@{ver}")
    return None  # msstore и неизвестные — откат невозможен


def list_versions(it):
    s, i = it["source"], it["id"]
    vers = []
    if s == "winget":
        out = run_text(["winget", "show", "--id", i, "--exact", "--versions"] + WG_FLAGS)
        seen_sep = False
        for line in out.splitlines():
            if re.fullmatch(r"-{3,}", line.strip()):
                seen_sep = True
            elif seen_sep and line.strip() and has_digit(line):
                vers.append(line.strip())
    elif s == "choco":
        for line in run_text(choco_cmd("list", i, "--exact", "--all-versions", "-r")).splitlines():
            p = line.strip().split("|")
            if len(p) == 2 and has_digit(p[1]):
                vers.append(p[1])
    return vers


def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=DEVNULL,
                       capture_output=True, creationflags=NO_WINDOW)
    except OSError:
        pass


def stream_cmd(cmd, log, cancel, idle_warn=90, hard_timeout=3600):
    """Запуск с потоковым выводом в журнал. Возвращает код возврата или None (прервано).
    Умеет отмену, предупреждение о «зависании» и жёсткий таймаут."""
    p = subprocess.Popen(cmd, stdin=DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    q = queue.Queue()

    def reader():
        for line in p.stdout:
            q.put(line)
        q.put(None)
    threading.Thread(target=reader, daemon=True).start()

    start = last = time.time()
    warned, prev = False, ""
    while True:
        if cancel.is_set():
            kill_tree(p.pid)
            p.kill()
            log("   ⏹ остановлено пользователем")
            return None
        try:
            line = q.get(timeout=1)
        except queue.Empty:
            now = time.time()
            if p.poll() is not None and now - last > 3:
                break  # процесс завершился, но «хвосты» удерживают канал
            if not warned and now - last > idle_warn:
                warned = True
                log(f"   ⏳ Нет вывода уже {int(now - last)} сек. Установщик, возможно, ждёт закрытия "
                    "программы или показывает окно. Если завис — нажмите «⏹ Остановить».")
            if now - start > hard_timeout:
                kill_tree(p.pid)
                p.kill()
                log("   ⏱ превышено время ожидания, установка прервана")
                return None
            continue
        if line is None:
            break
        last, warned = time.time(), False
        for part in re.split(r"[\r\n]+", line):
            part = part.strip()
            if not part or "█" in part or "▒" in part or re.fullmatch(r"[-\\|/]", part) or part == prev:
                continue
            prev = part
            log("   " + part)
    p.wait()
    return p.returncode & 0xFFFFFFFF


# ----------------------------------------------------------------- процессы ---
CRITICAL = {"explorer.exe", "winget.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe",
            "svchost.exe", "python.exe", "pythonw.exe", "windowsterminal.exe",
            "applicationframehost.exe", "choco.exe", "msiexec.exe", "taskmgr.exe"}


def list_processes():
    out = run_text(ps_cmd(
        'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath } | '
        'ForEach-Object { "$($_.ProcessId)|$($_.Name)|$($_.ExecutablePath)" }'))
    procs = []
    for line in out.splitlines():
        p = line.strip().split("|", 2)
        if len(p) == 3 and p[0].isdigit():
            procs.append((int(p[0]), p[1], p[2]))
    return procs


def processes_for(item, procs):
    loc = (item.get("location") or "").lower().rstrip("\\")
    tokens = {t for t in re.split(r"[.\-_ ]", (item.get("id") or "").lower()) if len(t) >= 4}
    nm = norm(item["name"])
    me = os.path.basename(sys.executable).lower()
    win = os.environ.get("SystemRoot", r"C:\Windows").lower()
    found = []
    for pid, name, path in procs:
        low = name.lower()
        if pid == os.getpid() or low in CRITICAL or low == me or path.lower().startswith(win):
            continue
        base = norm(os.path.splitext(name)[0])
        if (len(loc) > 6 and path.lower().startswith(loc + "\\")) or \
                (len(base) >= 4 and (base in tokens or base in nm)):
            found.append((pid, name))
    return found


def pid_alive(pid):
    out = run_text(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=15)
    return f'"{pid}"' in out


def close_processes(pids, log):
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], stdin=DEVNULL, capture_output=True,
                       creationflags=NO_WINDOW)
    deadline = time.time() + 8
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.5)
    for pid in pids:
        if pid_alive(pid):
            log(f"   принудительное закрытие PID {pid}")
            kill_tree(pid)


# ------------------------------------------------------------------ ДРАЙВЕРЫ ---
# Источник обновлений — только Центр обновления Windows (подписанные драйверы WHQL).
# Сторонние «базы драйверов» намеренно не используются: они могут подсунуть неподходящий драйвер.
RISK_LABEL = {"low": "🟢 Низкий", "mid": "🟡 Средний", "high": "🔴 Высокий"}
RISK_ORDER = {"high": 0, "mid": 1, "low": 2}
HIGH_RISK = {"firmware", "system", "scsiadapter", "hdc", "processor", "securitydevices",
             "computer", "smrdevice"}
MID_RISK = {"display", "net", "bluetooth", "usb", "media", "wpd"}
DRV_STATUS = {"update": "⬆ Есть обновление", "ok": "✔ Обновлений нет", "pinned": "📌 Не обновлять"}

CLASS_ADVICE = {
    "display": "Видеодрайвер. Центр обновления ставит проверенные, но часто более старые версии; "
               "самые свежие (для игр и приложений) выходят у NVIDIA / AMD / Intel — загляните на сайт производителя. "
               "Если всё работает, обновлять необязательно.",
    "net": "Сетевой драйвер. Перед обновлением убедитесь, что есть запасной способ выйти в интернет. "
           "Если сеть работает стабильно — лучше не трогать.",
    "media": "Аудиодрайвер. Обновляйте, только если есть проблемы со звуком.",
    "bluetooth": "Bluetooth. Обновление помогает при обрывах связи; после него может понадобиться заново подключить устройства.",
    "firmware": "Прошивка (UEFI/BIOS/ME/контроллеры) — самое рискованное обновление. Подключите питание и не выключайте ПК во время установки.",
    "system": "Системный драйвер (чипсет, управление питанием). Ставьте по одному, перезагружайтесь после каждого, держите свежую точку восстановления.",
    "scsiadapter": "Контроллер накопителя. Ошибка может помешать загрузке Windows — только с точкой восстановления и копией драйвера.",
    "hdc": "Контроллер накопителя. Ошибка может помешать загрузке Windows — только с точкой восстановления и копией драйвера.",
    "processor": "Драйвер процессора/питания. Обновляйте только при рекомендации производителя.",
}
DEFAULT_ADVICE = "Обновляйте, если у устройства есть проблема или в обновлении исправлена нужная ошибка; иначе можно подождать."

VENDOR_LINKS = [
    (r"nvidia", "NVIDIA", "https://www.nvidia.com/Download/index.aspx"),
    (r"advanced micro devices|\bamd\b|\bati\b", "AMD", "https://www.amd.com/en/support"),
    (r"intel", "Intel (Driver & Support Assistant)", "https://www.intel.com/content/www/us/en/support/detect.html"),
    (r"realtek", "Realtek", "https://www.realtek.com/Download"),
    (r"qualcomm|atheros", "Qualcomm", "https://www.qualcomm.com/support"),
    (r"\bdell\b", "Dell", "https://www.dell.com/support/home"),
    (r"hewlett|\bhp\b", "HP", "https://support.hp.com/us-en/drivers"),
    (r"lenovo", "Lenovo", "https://support.lenovo.com/"),
    (r"\basus", "ASUS", "https://www.asus.com/support/"),
]

INVENTORY_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$list = @()
foreach ($d in (Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName })) {
  $date = ''
  if ($d.DriverDate) { $date = $d.DriverDate.ToString('yyyy-MM-dd') }
  $list += [pscustomobject]@{
    name = [string]$d.DeviceName; manufacturer = [string]$d.Manufacturer; cls = [string]$d.DeviceClass
    version = [string]$d.DriverVersion; date = $date; inf = [string]$d.InfName
    hwid = [string]$d.HardWareID; device_id = [string]$d.DeviceID; provider = [string]$d.DriverProviderName
  }
}
ConvertTo-Json -InputObject @($list) -Compress -Depth 3
"""

UPDATES_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result = $searcher.Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
  $list = @()
  foreach ($u in $result.Updates) {
    $date = ''
    try { $date = ([datetime]$u.DriverVerDate).ToString('yyyy-MM-dd') } catch {}
    $ver = ''
    try { $ver = [string]$u.DriverVerVersion } catch {}
    $list += [pscustomobject]@{
      uid = [string]$u.Identity.UpdateID; title = [string]$u.Title; model = [string]$u.DriverModel
      manufacturer = [string]$u.DriverManufacturer; provider = [string]$u.DriverProvider
      cls = [string]$u.DriverClass; hwid = [string]$u.DriverHardwareID; date = $date; ver = $ver
      size = [int64]$u.MaxDownloadSize; reboot = ([int]$u.InstallationBehavior.RebootBehavior -ne 0)
    }
  }
  ConvertTo-Json -InputObject @($list) -Compress -Depth 3
} catch {
  Write-Output ('ERROR:' + $_.Exception.Message)
  exit 1
}
"""

RESTORE_PS = r"""
try {
  $r = Checkpoint-Computer -Description 'Update Checker: драйверы' -RestorePointType MODIFY_SETTINGS -ErrorAction Stop 3>&1
  if ($r) { ($r | Out-String).Trim() }
  Write-Output 'RESULT:OK'
} catch { Write-Output ('WARN:' + $_.Exception.Message) }
"""

_GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def ps_enc(script):
    """PowerShell через -EncodedCommand: без проблем с кавычками и кодировкой."""
    prefix = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
    b64 = base64.b64encode((prefix + script).encode("utf-16-le")).decode()
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", b64]


def parse_json_list(text):
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return data if isinstance(data, list) else [data]


def driver_risk(cls):
    c = (cls or "").lower()
    return "high" if c in HIGH_RISK else "mid" if c in MID_RISK else "low"


def extract_version(title):
    found = re.findall(r"\d+(?:\.\d+){1,}", title or "")
    return found[-1] if found else ""


def drv_key(d):
    return norm(d.get("device_id") or ("u" + (d.get("uid") or d.get("name", ""))))


def is_inbox(d):
    """Встроенные драйверы Windows (HID, хабы и т. п.) — шум, который по умолчанию скрываем."""
    return (d["manufacturer"].lower().startswith("microsoft")
            and not re.fullmatch(r"oem\d+\.inf", d["inf"].lower())
            and d["cls"].lower() not in ("display", "net", "media", "bluetooth"))


def wu_drivers_blocked():
    if winreg is None:
        return False
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate")
        return winreg.QueryValueEx(k, "ExcludeWUDriversInQualityUpdate")[0] == 1
    except OSError:
        return False


def driver_inventory():
    out = run_text(ps_enc(INVENTORY_PS), timeout=180, tag="drv inventory")
    data = parse_json_list(out)
    if data is None:
        ERRS.append("не удалось прочитать список драйверов: " + head("drv inventory"))
        return []
    keys = ("name", "manufacturer", "cls", "version", "date", "inf", "hwid", "device_id", "provider")
    return [{k: str(d.get(k) or "").strip() for k in keys}
            for d in data if isinstance(d, dict) and d.get("name")]


def driver_updates(timeout=420):
    """Список драйверов из Центра обновления Windows. None — источник недоступен."""
    out = run_text(ps_enc(UPDATES_PS), timeout=timeout, tag="drv updates")
    m = re.search(r"ERROR:(.*)", out)
    if m:
        ERRS.append("Центр обновления Windows: " + m.group(1).strip())
        return None
    data = parse_json_list(out)
    if data is None:
        ERRS.append("Центр обновления Windows не ответил: " + head("drv updates"))
        return None
    keys = ("uid", "title", "model", "manufacturer", "provider", "cls", "hwid", "date", "ver")
    res = []
    for d in data:
        if isinstance(d, dict) and d.get("uid"):
            u = {k: str(d.get(k) or "").strip() for k in keys}
            u["size"], u["reboot"] = int(d.get("size") or 0), bool(d.get("reboot"))
            res.append(u)
    return res


def scan_drivers(log=lambda t: None, hide_inbox=True, wu_timeout=420):
    """Возвращает (список драйверов, доступен_ли_Центр_обновления)."""
    ERRS.clear()
    RAW.clear()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fi, fu = ex.submit(driver_inventory), ex.submit(driver_updates, wu_timeout)
        inv, ups = fi.result(), fu.result()
    for e in ERRS:
        log(f"   ⚠ {e}")
    wu_ok = ups is not None
    if wu_drivers_blocked():
        log("   ⚠ Политика Windows отключает драйверы в Центре обновления (ExcludeWUDriversInQualityUpdate): "
            "обновления драйверов могут не находиться")
    log(f"   Драйверов в системе: {len(inv)}; обновлений в Центре обновления: "
        f"{len(ups) if wu_ok else 'нет данных'}")

    by_hw, by_name = {}, {}
    for d in inv:
        if d["hwid"]:
            by_hw.setdefault(d["hwid"].lower(), d)
        by_name.setdefault(norm(d["name"]), d)

    best, matched = {}, set()
    for u in ups or []:
        hw, dev = u["hwid"].lower(), None
        dev = by_hw.get(hw)
        if not dev and hw:
            dev = next((v for k, v in by_hw.items() if k.startswith(hw) or hw.startswith(k)), None)
        if not dev and u["model"]:
            dev = by_name.get(norm(u["model"]))
        base = dict(dev) if dev else {"name": u["model"] or u["title"], "manufacturer": u["manufacturer"],
                                      "cls": u["cls"], "version": "", "date": "", "inf": "", "hwid": u["hwid"],
                                      "device_id": "", "provider": u["provider"]}
        if not base["cls"]:
            base["cls"] = u["cls"]
        base.update(available=u["ver"] or extract_version(u["title"]) or "новая версия",
                    avail_date=u["date"], uid=u["uid"], title=u["title"], size=u["size"],
                    reboot=u["reboot"], status="update", risk=driver_risk(base["cls"]))
        k = drv_key(base)
        if k not in best or best[k]["avail_date"] < base["avail_date"]:
            best[k] = base
        if dev:
            matched.add(drv_key(dev))
    items = sorted(best.values(), key=lambda x: x["name"].lower())
    for d in inv:
        if drv_key(d) in matched:
            continue
        if hide_inbox and is_inbox(d):
            continue
        d.update(available="", avail_date="", uid="", title="", size=0, reboot=False,
                 status="ok", risk=driver_risk(d["cls"]))
        items.append(d)
    return items, wu_ok


def driver_advice(d):
    tips = [CLASS_ADVICE.get(d["cls"].lower(), DEFAULT_ADVICE)]
    if d["risk"] == "high":
        tips.append("🔴 Критичный драйвер: в «Обновить рекомендуемые» не включается — отмечайте вручную.")
    if d.get("reboot"):
        tips.append("После установки потребуется перезагрузка.")
    return " ".join(tips)


def vendor_link(d):
    hay = " ".join([d.get("manufacturer", ""), d.get("provider", ""), d.get("name", "")]).lower()
    for pat, label, url in VENDOR_LINKS:
        if re.search(pat, hay):
            return label, url
    return "Поиск драйвера", "https://www.bing.com/search?q=" + quote(d.get("name", "") + " driver")


def install_script(uid):
    if not _GUID.fullmatch(uid or ""):
        raise ValueError("некорректный идентификатор обновления")
    return r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $r = $session.CreateUpdateSearcher().Search("UpdateID='__UID__'")
  if ($r.Updates.Count -eq 0) { Write-Output 'ERROR: обновление больше не доступно в Центре обновления'; exit 2 }
  $u = $r.Updates.Item(0)
  Write-Output ('Пакет: ' + $u.Title)
  if (-not $u.EulaAccepted) { $u.AcceptEula() }
  $c = New-Object -ComObject Microsoft.Update.UpdateColl
  [void]$c.Add($u)
  Write-Output 'Загрузка драйвера...'
  $d = $session.CreateUpdateDownloader(); $d.Updates = $c
  $dr = $d.Download()
  if ($dr.ResultCode -ne 2) { Write-Output ('ERROR: загрузка не удалась, код ' + $dr.ResultCode); exit 3 }
  Write-Output 'Установка драйвера...'
  $i = $session.CreateUpdateInstaller(); $i.Updates = $c
  $ir = $i.Install()
  Write-Output ('RESULT:' + $ir.ResultCode + ' REBOOT:' + $ir.RebootRequired + ' HRESULT:0x' + ('{0:X8}' -f $ir.HResult))
  if ($ir.ResultCode -eq 2 -or $ir.ResultCode -eq 3) { exit 0 } else { exit 4 }
} catch { Write-Output ('ERROR:' + $_.Exception.Message); exit 1 }
""".replace("__UID__", uid)


def rollback_script(device_id, old_inf, backup):
    if not re.fullmatch(r"[\w\\&.\-{}#@() ]+", device_id or ""):
        raise ValueError("некорректный идентификатор устройства")
    if old_inf and not re.fullmatch(r"oem\d+\.inf", old_inf, re.I):
        old_inf = ""
    if backup and (not os.path.isdir(backup) or "'" in backup):
        backup = ""
    add = (f"& pnputil.exe /add-driver '{backup}\\*.inf' /install /subdirs" if backup
           else "Write-Output 'Копии нет — Windows выберет предыдущий или встроенный драйвер'")
    return r"""
$dev = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -eq '__DEV__' } | Select-Object -First 1
$cur = $dev.InfName
Write-Output ('Текущий драйвер: ' + $dev.DriverVersion + ' (' + $cur + ')')
if ($cur -and $cur -match '^oem\d+\.inf$' -and $cur -ne '__OLD__') {
  & pnputil.exe /delete-driver $cur /uninstall /force
} else { Write-Output 'Удалять нечего' }
__ADD__
& pnputil.exe /scan-devices
$new = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -eq '__DEV__' } | Select-Object -First 1
Write-Output ('Теперь установлен: ' + $new.DriverVersion + ' (' + $new.InfName + ')')
Write-Output 'ROLLBACK:DONE'
""".replace("__DEV__", device_id).replace("__OLD__", old_inf or "none").replace("__ADD__", add)


def backup_driver(d, log, cancel):
    """Экспорт текущего драйвера (только сторонние пакеты oem*.inf). Возвращает папку копии или ''."""
    inf = d.get("inf", "")
    if not re.fullmatch(r"oem\d+\.inf", inf, re.I):
        log("   ℹ Встроенный драйвер Windows — копия не нужна (откат вернёт встроенный)")
        return ""
    safe = lambda s: re.sub(r"[^\w.\-]+", "_", s)[:40]
    dest = os.path.join(app_dir(), "driver_backups",
                        f"{time.strftime('%Y%m%d_%H%M%S')}_{safe(d['name'])}_{safe(d['version'])}")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        log(f"   ⚠ Не удалось создать папку резервной копии: {e}")
        return ""
    log(f"💾 Резервная копия текущего драйвера → {dest}")
    rc = stream_cmd(["pnputil.exe", "/export-driver", inf, dest], log, cancel, idle_warn=120, hard_timeout=300)
    if rc == 0:
        return dest
    log("   ⚠ Резервную копию создать не удалось")
    return ""



# --------------------------------------------------------- диалог отката ---
class RollbackDialog(tk.Toplevel):
    def __init__(self, parent, item, prefill=""):
        super().__init__(parent)
        self.title("Откат на предыдущую версию")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.q = queue.Queue()
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both")
        ttk.Label(f, text=item["name"], font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(f, text=f"Сейчас установлена: {item['version'] or '?'}   ·   "
                          f"источник: {SOURCE_LABEL.get(item['source'], '—')}").pack(anchor="w", pady=(0, 10))
        ttk.Label(f, text="Версия, на которую нужно откатиться:").pack(anchor="w")
        self.var = tk.StringVar(value=prefill)
        self.combo = ttk.Combobox(f, textvariable=self.var, width=36)
        self.combo.pack(anchor="w", pady=4)
        self.hint = ttk.Label(f, text="Загружаю список доступных версий…", foreground="#666")
        self.hint.pack(anchor="w")
        ttk.Label(f, text="Программа будет переустановлена указанной версией.\n"
                          "Закройте её перед откатом. Настройки обычно сохраняются.",
                  foreground="#666").pack(anchor="w", pady=(8, 10))
        bt = ttk.Frame(f)
        bt.pack(fill="x")
        ttk.Button(bt, text="↩ Откатить", command=self.ok).pack(side="right")
        ttk.Button(bt, text="Отмена", command=self.destroy).pack(side="right", padx=6)
        threading.Thread(target=lambda: self.q.put(list_versions(item)), daemon=True).start()
        self.after(150, self.poll)
        self.grab_set()
        self.combo.focus_set()

    def poll(self):
        try:
            vers = self.q.get_nowait()
        except queue.Empty:
            self.after(150, self.poll)
            return
        self.combo["values"] = vers
        if vers:
            self.hint.configure(text=f"Найдено версий: {len(vers)} — выберите из списка или введите вручную")
            if not self.var.get():
                self.var.set(vers[1] if len(vers) > 1 else vers[0])
        else:
            self.hint.configure(text="Список версий недоступен — введите номер вручную")

    def ok(self):
        v = self.var.get().strip()
        if v:
            self.result = v
            self.destroy()


# --------------------------------------------------------------------- GUI ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_settings()
        self.items = self.cfg["cache"]
        self.drivers = self.cfg["drv_cache"]
        self.checked, self.drv_checked = set(), set()
        self.q = queue.Queue()
        self.busy = False
        self.running_jobs = False
        self.cancel = threading.Event()
        self.sort_col, self.sort_desc = None, False
        self.drv_sort_col, self.drv_sort_desc = None, False
        self.drv_auto_done = False
        self.drv_wu_ok = True
        self._hist_refreshing = False
        self.title(f"{APP_NAME} {VERSION}" + ("  —  Администратор" if is_admin() else ""))
        self.geometry(self.cfg["geometry"])
        self.minsize(1000, 640)
        self.build_ui()
        self.bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_table()
        self.drv_refresh_table()
        self.refresh_history()
        if self.items:
            self.status.set(f"Показан результат прошлой проверки ({self.cfg['cache_time']}). Идёт новая проверка…")
        if winreg is None:
            messagebox.showwarning("Только Windows", "Программа работает только в Windows.")
        else:
            self.after(300, self.start_scan)   # проверка при каждом запуске; затем — драйверы
        self.after(100, self.poll)
        self.after(250, self.init_sash)

    def build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Card.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("CardCap.TLabel", foreground="#666")
        style.configure("Treeview", rowheight=26)

        cards = ttk.Frame(self, padding=(10, 8, 10, 0))
        cards.pack(fill="x")
        self.cnt = {}
        for k, cap in (("total", "Программ"), ("update", "Есть обновления"), ("pinned", "Закреплено")):
            box = ttk.Frame(cards, padding=(0, 0, 28, 0))
            box.pack(side="left")
            self.cnt[k] = ttk.Label(box, text="0", style="Card.TLabel")
            self.cnt[k].pack(anchor="w")
            ttk.Label(box, text=cap, style="CardCap.TLabel").pack(anchor="w")
        self.cnt["update"].configure(foreground="#b45f06")

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        self.btn_scan = ttk.Button(bar, text="🔍 Проверить", command=self.start_scan)
        self.btn_upd = ttk.Button(bar, text="⬆ Обновить отмеченные", command=self.update_checked)
        self.btn_all = ttk.Button(bar, text="⬆⬆ Обновить все", command=self.update_all)
        self.btn_pin = ttk.Button(bar, text="📌 Закрепить версию", command=self.toggle_pin)
        self.btn_rb = ttk.Button(bar, text="↩ Откат…", command=self.rollback_selected)
        self.btn_stop = ttk.Button(bar, text="⏹ Остановить", command=self.stop_jobs, state="disabled")
        for b in (self.btn_scan, self.btn_upd, self.btn_all, self.btn_pin, self.btn_rb, self.btn_stop):
            b.pack(side="left", padx=(0, 6))
        mb = ttk.Menubutton(bar, text="⚙ Настройки")
        self.build_menu(mb)
        mb.pack(side="right")
        if not is_admin() and winreg:
            ttk.Button(bar, text="🛡 От администратора", command=self.admin).pack(side="right", padx=6)

        # строка состояния и прогресс — снизу (упаковываем до растягиваемой части)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(10, 6)).pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(side="bottom", fill="x", padx=10, pady=(6, 0))

        # растягиваемая область: вкладки сверху, журнал снизу (граница перетаскивается)
        self.pw = ttk.PanedWindow(self, orient="vertical")
        self.pw.pack(fill="both", expand=True, padx=10)
        self.nb = ttk.Notebook(self.pw)
        self.pw.add(self.nb, weight=4)
        logf = ttk.Frame(self.pw)
        self.pw.add(logf, weight=1)

        tab1, tab_drv, tab2 = ttk.Frame(self.nb), ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.nb.add(tab1, text="  Программы  ")
        self.nb.add(tab_drv, text="  Драйвера  ")
        self.nb.add(tab2, text="  История и откат  ")
        self.build_drivers_tab(tab_drv)

        flt = ttk.Frame(tab1, padding=(0, 8))
        flt.pack(fill="x")
        ttk.Label(flt, text="Поиск:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh_table())
        self.search_entry = ttk.Entry(flt, textvariable=self.search, width=24)
        self.search_entry.pack(side="left", padx=(4, 14))
        ttk.Label(flt, text="Показывать:").pack(side="left")
        self.view = tk.StringVar(value="Все")
        c1 = ttk.Combobox(flt, textvariable=self.view, state="readonly", width=20,
                          values=["Все", "Только с обновлениями", "Актуальные", "Закреплённые", "Нет в базах"])
        c1.pack(side="left", padx=(4, 14))
        ttk.Label(flt, text="Источник:").pack(side="left")
        self.srcf = tk.StringVar(value="Все")
        c2 = ttk.Combobox(flt, textvariable=self.srcf, state="readonly", width=16,
                          values=["Все", "winget", "Microsoft Store", "Chocolatey", "Scoop"])
        c2.pack(side="left", padx=4)
        for c in (c1, c2):
            c.bind("<<ComboboxSelected>>", lambda e: self.refresh_table())

        wrap = ttk.Frame(tab1)
        wrap.pack(fill="both", expand=True)
        cols = ("chk", "name", "cur", "new", "src", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        spec = (("chk", "☑", 40, "center"), ("name", "Программа", 330, "w"),
                ("cur", "Установлена", 120, "w"), ("new", "Доступна", 120, "w"),
                ("src", "Источник", 130, "w"), ("status", "Статус", 210, "w"))
        for c, h, w, a in spec:
            self.tree.heading(c, text=h, command=(self.toggle_all_updates if c == "chk"
                                                  else (lambda c=c: self.sort_by(c))))
            self.tree.column(c, width=w, anchor=a, stretch=(c == "name"))
        sb = ttk.Scrollbar(wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("update", background="#fff3cd")
        self.tree.tag_configure("pinned", background="#e3f0ff")
        self.tree.tag_configure("unknown", foreground="#8a8a8a")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.context_menu)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="⬆ Обновить", command=self.update_selected)
        self.menu.add_command(label="↩ Откат…", command=self.rollback_selected)
        self.menu.add_command(label="📌 Закрепить / открепить версию", command=self.toggle_pin)
        self.menu.add_separator()
        self.menu.add_command(label="Копировать название", command=self.copy_name)
        self.menu.add_command(label="Открыть папку программы", command=self.open_folder)

        hb = ttk.Frame(tab2, padding=(0, 8))
        hb.pack(fill="x")
        ttk.Button(hb, text="↩ Откатить выбранное обновление", command=self.rollback_history).pack(side="left")
        ttk.Button(hb, text="Очистить историю", command=self.clear_history).pack(side="left", padx=6)
        ttk.Label(hb, text="Клик по записи показывает её лог в журнале (ошибка подсвечивается). «Откатить» возвращает прежнюю версию.",
                  foreground="#666").pack(side="left", padx=10)
        hw = ttk.Frame(tab2)
        hw.pack(fill="both", expand=True)
        hc = ("time", "name", "action", "from", "to", "res")
        self.htree = ttk.Treeview(hw, columns=hc, show="headings", selectmode="browse")
        for c, h, w in (("time", "Дата", 140), ("name", "Программа", 300), ("action", "Действие", 150),
                        ("from", "Было", 110), ("to", "Стало", 110), ("res", "Результат", 140)):
            self.htree.heading(c, text=h)
            self.htree.column(c, width=w, anchor="w", stretch=(c == "name"))
        hs = ttk.Scrollbar(hw, command=self.htree.yview)
        self.htree.configure(yscrollcommand=hs.set)
        self.htree.pack(side="left", fill="both", expand=True)
        hs.pack(side="right", fill="y")
        self.htree.tag_configure("bad", foreground="#b00020")
        self.htree.bind("<<TreeviewSelect>>", self.on_history_select)

        # журнал (размер меняется перетаскиванием разделителя)
        lh = ttk.Frame(logf)
        lh.pack(fill="x", pady=(4, 2))
        ttk.Label(lh, text="Журнал", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(lh, text="  ↕ потяните границу над журналом, чтобы изменить его высоту",
                  foreground="#888").pack(side="left")
        ttk.Button(lh, text="Очистить", width=10, command=self.clear_log).pack(side="right")
        lw = ttk.Frame(logf)
        lw.pack(fill="both", expand=True)
        self.log = tk.Text(lw, height=6, state="disabled", bg="#1e1e1e", fg="#dcdcdc",
                           font=("Consolas", 9), relief="flat", wrap="word")
        ls = ttk.Scrollbar(lw, command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        self.log.tag_configure("errline", foreground="#ff8a80")
        self.log.tag_configure("focus", background="#2b4a6f")

    def init_sash(self):
        try:
            self.pw.sashpos(0, int(self.pw.winfo_height() * 0.70))
        except tk.TclError:
            pass

    def build_menu(self, mb):
        m = tk.Menu(mb, tearoff=0)
        mb["menu"] = m
        self.src_vars = {}
        sm = tk.Menu(m, tearoff=0)
        for k, label, ok in (("winget", "winget", have_winget()),
                             ("msstore", "Microsoft Store (через winget)", have_winget()),
                             ("choco", "Chocolatey", have_choco()),
                             ("scoop", "Scoop", have_scoop())):
            v = tk.BooleanVar(value=self.cfg["sources"].get(k, True))
            self.src_vars[k] = v
            sm.add_checkbutton(label=label + ("" if ok else "  (не найден)"), variable=v,
                               command=self.save_options, state="normal" if ok else "disabled")
        m.add_cascade(label="Источники обновлений", menu=sm)
        self.close_var = tk.StringVar(value=self.cfg["close_mode"])
        cm = tk.Menu(m, tearoff=0)
        for val, label in (("ask", "Спрашивать каждый раз"), ("auto", "Закрывать автоматически"),
                           ("never", "Не закрывать")):
            cm.add_radiobutton(label=label, value=val, variable=self.close_var, command=self.save_options)
        m.add_cascade(label="Закрытие запущенных программ", menu=cm)
        m.add_separator()
        m.add_command(label="Найденные инструменты (winget, Chocolatey, Scoop)…", command=self.show_tools)
        m.add_command(label="Экспорт списка в CSV…", command=self.export_csv)
        m.add_command(label="Открыть папку с настройками",
                      command=lambda: os.startfile(app_dir()) if hasattr(os, "startfile") else None)
        m.add_command(label="Справка", command=lambda: messagebox.showinfo("Справка", HELP_TEXT))
        m.add_command(label=f"О программе (v{VERSION})", command=lambda: messagebox.showinfo(
            "О программе", f"{APP_NAME} {VERSION}\nПроверка и установка обновлений программ для Windows 11.\n"
                           "Источники: winget, Microsoft Store, Chocolatey, Scoop."))

    def bind_keys(self):
        self.bind("<F5>", lambda e: self.start_scan())
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Control-a>", lambda e: self.toggle_all_updates())
        self.tree.bind("<space>", self.space_toggle)

    def show_tools(self):
        lines = [f"winget: {find_winget() or 'не найден'}",
                 f"Chocolatey: {find_choco() or 'не найден'}",
                 f"Scoop: {find_scoop() or 'не найден'}"]
        messagebox.showinfo("Найденные инструменты", "\n".join(lines) +
                            "\n\nChocolatey и Scoop необязательны: без них программа проверяет "
                            "winget и Microsoft Store. Если они установлены — используются автоматически.")

    def save_options(self):
        self.cfg["sources"] = {k: v.get() for k, v in self.src_vars.items()}
        self.cfg["close_mode"] = self.close_var.get()
        save_settings(self.cfg)

    def on_close(self):
        self.cancel.set()
        self.cfg["geometry"] = self.geometry()
        self.cfg["cache"], self.cfg["drv_cache"] = self.items, self.drivers
        save_settings(self.cfg)
        self.destroy()

    def admin(self):
        if messagebox.askyesno("Права администратора", "Перезапустить программу от имени администратора?"):
            self.on_close()
            relaunch_as_admin()

    # ------------------------------------------------------------- таблица
    def write_log(self, text, jid=None):
        tags = ()
        if jid:
            tags = (f"job_{jid}",)
            if any(m in text for m in ERR_MARKS):
                tags += (f"err_{jid}", "errline")
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tags)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_busy(self, busy, msg=None):
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in [self.btn_scan, self.btn_upd, self.btn_all, self.btn_pin, self.btn_rb] + self.drv_btns:
            b.configure(state=st)
        self.btn_stop.configure(state="normal" if self.running_jobs else "disabled")
        if msg:
            self.status.set(msg)

    def pin_of(self, it):
        return self.cfg["pins"].get(key_of(it))

    def eff_status(self, it):
        return "pinned" if self.pin_of(it) else it["status"]

    def status_text(self, it):
        st = self.eff_status(it)
        if st == "pinned":
            extra = f" (есть {it['available']})" if it["status"] == "update" else ""
            return f"📌 Закреплена{extra}"
        return STATUS_LABEL[st]

    def updatable(self, it):
        return it["status"] == "update" and not self.pin_of(it) and bool(it.get("id"))

    def refresh_table(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        q = self.search.get().lower().strip()
        view, srcf = self.view.get(), self.srcf.get()
        keymap = {"Все": None, "Только с обновлениями": "update", "Актуальные": "ok",
                  "Закреплённые": "pinned", "Нет в базах": "unknown"}
        want = keymap[view]
        items = list(self.items)
        if self.sort_col:
            kf = {"name": lambda i: i["name"].lower(), "cur": lambda i: i["version"],
                  "new": lambda i: i["available"], "src": lambda i: SOURCE_LABEL.get(i["source"], ""),
                  "status": lambda i: STATUS_ORDER[self.eff_status(i)]}[self.sort_col]
            items.sort(key=kf, reverse=self.sort_desc)
        else:  # по умолчанию: сначала те, что нужно обновить
            items.sort(key=lambda i: (STATUS_ORDER[self.eff_status(i)], i["name"].lower()))
        for it in items:
            k = key_of(it)
            st = self.eff_status(it)
            if want and st != want:
                continue
            if q and q not in it["name"].lower():
                continue
            if srcf != "Все" and SOURCE_LABEL.get(it["source"]) != srcf:
                continue
            mark = ("☑" if k in self.checked else "☐") if self.updatable(it) else ""
            self.tree.insert("", "end", iid=k, tags=(st,),
                             values=(mark, it["name"], it["version"], it["available"],
                                     SOURCE_LABEL.get(it["source"], "—"), self.status_text(it)))
        for k in sel:
            if self.tree.exists(k):
                self.tree.selection_add(k)
        self.cnt["total"].configure(text=str(len(self.items)))
        self.cnt["update"].configure(text=str(sum(1 for i in self.items if self.updatable(i))))
        self.cnt["pinned"].configure(text=str(len(self.cfg["pins"])))

    def sort_by(self, col):
        self.sort_desc = (not self.sort_desc) if self.sort_col == col else False
        self.sort_col = col
        self.refresh_table()

    def item_by_key(self, k):
        return next((i for i in self.items if key_of(i) == k), None)

    def selected_items(self):
        return [i for i in (self.item_by_key(k) for k in self.tree.selection()) if i]

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            row = self.tree.identify_row(e.y)
            it = self.item_by_key(row) if row else None
            if it and self.updatable(it):
                self.checked.symmetric_difference_update({row})
                self.refresh_table()
            return "break"

    def space_toggle(self, e):
        for it in self.selected_items():
            if self.updatable(it):
                self.checked.symmetric_difference_update({key_of(it)})
        self.refresh_table()
        return "break"

    def toggle_all_updates(self):
        ups = {key_of(i) for i in self.items if self.updatable(i)}
        self.checked = set() if ups and ups <= self.checked else ups
        self.refresh_table()

    def on_double(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) != "#1":
            self.update_selected()

    def context_menu(self, e):
        row = self.tree.identify_row(e.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(e.x_root, e.y_root)

    def copy_name(self):
        s = self.selected_items()
        if s:
            self.clipboard_clear()
            self.clipboard_append(s[0]["name"])

    def open_folder(self):
        s = self.selected_items()
        if s and s[0].get("location") and os.path.isdir(s[0]["location"]) and hasattr(os, "startfile"):
            os.startfile(s[0]["location"])
        else:
            messagebox.showinfo("Папка", "Папка установки этой программы неизвестна.")

    # --------------------------------------------------------------- очередь
    def apply_result(self, h):
        """Сразу отражает результат операции в списках (не дожидаясь повторной проверки)."""
        if not h["ok"]:
            return
        if h.get("kind") == "driver":
            ex = h.get("extra") or {}
            d = (next((x for x in self.drivers if ex.get("device_id") and x["device_id"] == ex["device_id"]), None)
                 or next((x for x in self.drivers if x["name"] == h["name"]), None))
            if d:
                self.drv_checked.discard(drv_key(d))
                if h["action"] == "update":
                    d.update(version=h["to"] or d["version"], available="", uid="", status="ok")
                else:  # откат
                    d.update(version=h["to"] or d["version"], available="", uid="", status="ok")
            return
        it = next((i for i in self.items if i["name"] == h["name"]), None)
        if not it:
            return
        self.checked.discard(key_of(it))
        if h["action"] == "update":
            it.update(version=h["to"] or it["version"], available="", status="ok")
        else:  # откат: новая версия снова считается доступной
            it.update(version=h["to"], available=h["from"], status="update")

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.write_log(data)
                elif kind == "jlog":
                    self.write_log(data[0], data[1])
                elif kind == "progress":
                    self.progress.configure(mode="determinate", maximum=max(data[1], 1), value=data[0])
                    self.status.set(data[2])
                elif kind == "hist":
                    self.cfg["history"].append(data)
                    self.cfg["history"] = self.cfg["history"][-200:]
                    self.apply_result(data)
                    self.cfg["cache"], self.cfg["drv_cache"] = self.items, self.drivers
                    save_settings(self.cfg)
                    self.refresh_history()
                    self.refresh_table()
                    self.drv_refresh_table()
                elif kind == "scan_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if data is not None:
                        self.items = data
                        self.cfg["cache"] = data
                        self.cfg["cache_time"] = time.strftime("%d.%m.%Y %H:%M")
                        save_settings(self.cfg)
                        self.checked &= {key_of(i) for i in data if self.updatable(i)}
                    self.sort_col, self.sort_desc = None, False  # после проверки — обновления сверху
                    self.refresh_table()
                    self.tree.yview_moveto(0)
                    n = int(self.cnt["update"].cget("text"))
                    self.set_busy(False, (f"Готово. Найдено обновлений: {n}. Отметьте нужные ☑ и нажмите «Обновить отмеченные»."
                                          if n else "Готово. Обновлений не найдено."))
                    if winreg and not self.drv_auto_done and self.cfg["drivers"]["scan_on_start"]:
                        self.drv_auto_done = True
                        self.after(400, self.start_drv_scan)   # затем проверяем драйверы
                elif kind == "drv_scan_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    items, wu_ok = data if data else (None, False)
                    if items is not None:
                        self.drivers, self.drv_wu_ok = items, wu_ok
                        self.cfg["drv_cache"] = items
                        self.cfg["drv_cache_time"] = time.strftime("%d.%m.%Y %H:%M")
                        save_settings(self.cfg)
                        self.drv_checked &= {drv_key(d) for d in items if self.drv_updatable(d)}
                    self.drv_sort_col, self.drv_sort_desc = None, False
                    self.drv_refresh_table()
                    self.drv_tree.yview_moveto(0)
                    n = sum(1 for d in self.drivers if self.drv_updatable(d))
                    if items is not None and not self.drv_wu_ok:
                        msg = "Драйверы: Центр обновления Windows недоступен — подробности в журнале."
                    else:
                        msg = (f"Драйверы: найдено обновлений — {n}." if n else "Драйверы: обновлений не найдено.")
                    self.set_busy(False, msg)
                elif kind == "jobs_done":
                    self.running_jobs = False
                    self.set_busy(False, data)
                    if not self.cancel.is_set():
                        self.start_scan()
                elif kind == "drv_jobs_done":
                    self.running_jobs = False
                    self.set_busy(False, data[0])
                    if data[1]:
                        self.ask_reboot()
                    if not self.cancel.is_set():
                        self.start_drv_scan()
                elif kind == "plain_done":
                    self.running_jobs = False
                    self.progress.configure(mode="determinate", value=0)
                    self.set_busy(False, data)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def start_scan(self):
        if self.busy:
            return
        if winreg and not (have_winget() or have_choco() or have_scoop()):
            self.write_log("❌ Не найден ни winget, ни Chocolatey, ни Scoop. Установите «Установщик приложений» из Microsoft Store.")
            return
        self.set_busy(True, "Идёт проверка: читаю список программ и опрашиваю источники…")
        self.write_log("▶ Проверка обновлений…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        srcs = dict(self.cfg["sources"])
        log = lambda t: self.q.put(("log", t))

        def work():
            try:
                self.q.put(("scan_done", scan(srcs, log)))
            except Exception as e:
                self.q.put(("log", f"❌ Ошибка проверки: {e}"))
                self.q.put(("scan_done", None))
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- обновление
    def update_checked(self):
        items = [i for i in self.items if key_of(i) in self.checked and self.updatable(i)]
        if not items:
            items = [i for i in self.selected_items() if self.updatable(i)]
        if not items:
            messagebox.showinfo("Не выбрано", "Отметьте галочкой ☑ программы с обновлениями\n"
                                              "(или кликните по ☑ в заголовке, чтобы отметить все).")
            return
        self.run_updates(items)

    def update_selected(self):
        items = [i for i in self.selected_items() if self.updatable(i)]
        if items:
            self.run_updates(items)
        elif self.selected_items():
            it = self.selected_items()[0]
            messagebox.showinfo("Нет обновления", f"Для «{it['name']}» нет доступного обновления "
                                                  "или версия закреплена.")

    def update_all(self):
        items = [i for i in self.items if self.updatable(i)]
        if not items:
            messagebox.showinfo("Нечего обновлять", "Нет программ с доступными обновлениями.")
            return
        self.run_updates(items)

    def ask_close(self, items):
        """dict key -> [pid,...] для закрытия, либо None, если пользователь отменил."""
        mode = self.cfg["close_mode"]
        if mode == "never":
            return {}
        self.status.set("Ищу запущенные процессы обновляемых программ…")
        self.update_idletasks()
        procs = list_processes()
        found = {key_of(i): processes_for(i, procs) for i in items}
        found = {k: v for k, v in found.items() if v}
        if not found:
            return {}
        if mode == "auto":
            return {k: [p for p, _ in v] for k, v in found.items()}
        lines = []
        for k, v in found.items():
            names = sorted({n for _, n in v})
            lines.append(f" • {self.item_by_key(k)['name']}  ({', '.join(names)}: {len(v)})")
        ans = messagebox.askyesnocancel(
            "Программы запущены",
            "Следующие программы сейчас запущены:\n\n" + "\n".join(lines[:15]) +
            "\n\nЗакрыть их перед обновлением?\n(несохранённые данные могут быть потеряны)\n\n"
            "Да — закрыть и обновить\nНет — обновить, не закрывая\nОтмена — ничего не делать")
        if ans is None:
            return None
        return {k: [p for p, _ in v] for k, v in found.items()} if ans else {}

    def run_updates(self, items):
        closing = self.ask_close(items)
        if closing is None:
            self.status.set("Отменено.")
            return
        if not messagebox.askyesno("Подтверждение", f"Обновить программ: {len(items)}?"):
            return
        jobs = [{"item": i, "action": "update", "close": closing.get(key_of(i), [])} for i in items]
        self.start_jobs(jobs)

    def start_jobs(self, jobs):
        self.cancel = threading.Event()
        self.running_jobs = True
        self.set_busy(True, "Установка…")
        self.progress.configure(mode="determinate", maximum=len(jobs), value=0)
        threading.Thread(target=self.jobs_worker, args=(jobs, self.cancel), daemon=True).start()

    def stop_jobs(self):
        if self.running_jobs:
            self.cancel.set()
            self.status.set("Останавливаю…")

    def jobs_worker(self, jobs, cancel):
        glog = lambda t: self.q.put(("log", t))
        ok_n, total = 0, len(jobs)
        for n, job in enumerate(jobs, 1):
            if cancel.is_set():
                glog("⏹ Остановлено — оставшиеся программы пропущены")
                break
            it, act, ver = job["item"], job["action"], job.get("version")
            jid, lines = f"{int(time.time() * 1000)}_{n}", []

            def jl(t, jid=jid, lines=lines):
                lines.append(t[:300])
                self.q.put(("jlog", (t, jid)))

            title = "Откат" if act == "rollback" else "Обновление"
            self.q.put(("progress", (n - 1, total, f"{title} {n}/{total}: {it['name']}")))
            cmd = rollback_cmd(it, ver) if act == "rollback" else upgrade_cmd(it)
            ok = False
            jl(f"{'↩' if act == 'rollback' else '⬆'} {it['name']} → {ver or it['available']}")
            if not cmd:
                jl(f"⚠ {it['name']}: действие не поддерживается для этого источника")
            else:
                if job.get("close"):
                    jl(f"⏹ Закрываю запущенные процессы: {it['name']}")
                    close_processes(job["close"], jl)
                try:
                    rc = stream_cmd(cmd, jl, cancel)
                except Exception as e:
                    jl(f"   ❌ {e}")
                    rc = -1
                if cancel.is_set():
                    jl("⏹ Остановлено пользователем")
                    break
                if rc == 0:
                    ok = True
                    jl("   ✔ готово")
                elif rc is not None:
                    hint = WINGET_HINTS.get(rc, "попробуйте повторить или запустите программу от администратора")
                    jl(f"   ⚠ код {rc:#x}: {hint}")
            ok_n += ok
            self.q.put(("hist", self.make_hist(it, act, ok, jid, lines, frm=it["version"], to=ver or it["available"])))
        self.q.put(("progress", (total, total, "Завершено")))
        self.q.put(("jobs_done", f"Выполнено успешно: {ok_n} из {total}."
                    + ("" if cancel.is_set() else " Повторная проверка…")))

    def toggle_pin(self):
        sel = self.selected_items()
        if not sel:
            messagebox.showinfo("Закрепление", "Выберите программу в таблице.")
            return
        for it in sel:
            k = key_of(it)
            if k in self.cfg["pins"]:
                del self.cfg["pins"][k]
                self.write_log(f"📌 Версия откреплена: {it['name']}")
            else:
                self.cfg["pins"][k] = {"name": it["name"], "version": it["version"]}
                self.checked.discard(k)
                self.write_log(f"📌 Закреплена версия {it['version'] or '?'}: {it['name']}")
        save_settings(self.cfg)
        self.refresh_table()

    # -------------------------------------------------------------------- откат
    def last_prev_version(self, it):
        for h in reversed(self.cfg["history"]):
            if h["name"] == it["name"] and h["action"] == "update" and h["ok"]:
                return h["from"]
        return ""

    def rollback_selected(self):
        sel = self.selected_items()
        if not sel:
            messagebox.showinfo("Откат", "Выберите программу в таблице.")
            return
        self.do_rollback(sel[0], self.last_prev_version(sel[0]))

    def do_rollback(self, it, prefill):
        if not it.get("id") or it["source"] in ("", "msstore"):
            messagebox.showinfo("Откат недоступен",
                                "Откат работает для программ из winget, Chocolatey и Scoop.\n"
                                "Для Microsoft Store и программ, не найденных в базах, он невозможен.")
            return
        dlg = RollbackDialog(self, it, prefill)
        self.wait_window(dlg)
        if not dlg.result:
            return
        closing = self.ask_close([it])
        if closing is None:
            return
        if messagebox.askyesno("Подтверждение отката",
                               f"Установить «{it['name']}» версии {dlg.result}?\n\n"
                               "После отката рекомендуется закрепить версию (📌), "
                               "чтобы программа не обновилась снова."):
            self.start_jobs([{"item": it, "action": "rollback", "version": dlg.result,
                              "close": closing.get(key_of(it), [])}])

    # -------------------------------------------------------------------- история
    def refresh_history(self):
        self._hist_refreshing = True
        self.htree.delete(*self.htree.get_children())
        for n, h in reversed(list(enumerate(self.cfg["history"]))):
            res = "✔ успешно" if h["ok"] else "✖ ошибка"
            drv = h.get("kind") == "driver"
            act = ("Драйвер: " if drv else "") + ("откат" if h["action"] == "rollback" else "обновление")
            if not drv:
                act = act.capitalize()
            self.htree.insert("", "end", iid=str(n), tags=("" if h["ok"] else "bad",),
                              values=(h["time"], h["name"], act, h["from"], h["to"], res))
        self.after(50, lambda: setattr(self, "_hist_refreshing", False))

    def rollback_history(self):
        s = self.htree.selection()
        if not s:
            messagebox.showinfo("История", "Выберите запись в списке.")
            return
        h = self.cfg["history"][int(s[0])]
        if h.get("kind") == "driver":
            self.drv_rollback_entry(h)
            return
        it = next((i for i in self.items if i["name"] == h["name"]), None)
        if not it:
            it = {"name": h["name"], "id": h["id"], "source": h["source"], "version": h["to"],
                  "available": "", "location": "", "publisher": "", "status": "ok"}
        self.do_rollback(it, h["from"])

    def clear_history(self):
        if self.cfg["history"] and messagebox.askyesno("История", "Очистить всю историю?"):
            self.cfg["history"] = []
            save_settings(self.cfg)
            self.refresh_history()

    # --------------------------------------------------------------------- экспорт
    def export_csv(self):
        if not self.items:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialdir=app_dir(), initialfile="installed_software.csv")
        if path:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Программа", "Установлена", "Доступна", "Издатель", "Источник", "ID", "Статус"])
                for i in self.items:
                    w.writerow([i["name"], i["version"], i["available"], i["publisher"],
                                SOURCE_LABEL.get(i["source"], "—"), i["id"], self.status_text(i)])
            self.write_log(f"💾 Сохранено: {path}")


    # ================================================== журнал: переход из истории
    def make_hist(self, it, act, ok, jid, lines, kind="program", frm=None, to=None, extra=None):
        h = {"time": time.strftime("%d.%m.%Y %H:%M"), "name": it["name"], "id": it.get("id") or it.get("uid") or "",
             "source": it.get("source", ""), "action": act, "from": frm or "", "to": to or "", "ok": ok,
             "kind": kind, "jid": jid, "log": [l[:300] for l in lines[-60:]]}
        if extra:
            h["extra"] = extra
        return h

    def ensure_log_visible(self):
        try:
            total, pos = self.pw.winfo_height(), self.pw.sashpos(0)
            if total > 400 and total - pos < 170:
                self.pw.sashpos(0, max(150, total - 240))
        except tk.TclError:
            pass

    def focus_job(self, jid):
        tag = f"job_{jid}"
        r = self.log.tag_ranges(tag)
        if not r:
            return False
        self.log.tag_remove("focus", "1.0", "end")
        self.log.tag_add("focus", r[0], r[-1])
        er = self.log.tag_ranges(f"err_{jid}")
        self.ensure_log_visible()
        self.log.yview(er[0] if er else r[0])
        return bool(er)

    def jump_to_log(self, h, idx):
        """Показывает в журнале строки выбранной записи истории и подсвечивает ошибку."""
        jid = h.get("jid") or f"h{idx}"
        lines = h.get("log") or []
        if not self.log.tag_ranges(f"job_{jid}"):
            if not lines:
                self.status.set("Для этой записи журнал не сохранился.")
                return
            self.write_log(f"──── Журнал: {h['name']} · {h['time']} ────")
            for l in lines:
                self.write_log(l, jid)
        had_error = self.focus_job(jid)
        self.status.set(f"Журнал: {h['name']} — " + ("показана ошибка." if had_error else "ошибок в записи нет."))

    def on_history_select(self, _e=None):
        if self._hist_refreshing:
            return
        sel = self.htree.selection()
        if sel:
            idx = int(sel[0])
            if 0 <= idx < len(self.cfg["history"]):
                self.jump_to_log(self.cfg["history"][idx], idx)

    # ======================================================================= ДРАЙВЕРА
    def build_drivers_tab(self, tab):
        o = self.cfg["drivers"]
        top = ttk.Frame(tab, padding=(0, 8, 0, 2))
        top.pack(fill="x")
        self.drv_btn_scan = ttk.Button(top, text="🔍 Проверить драйверы", command=self.start_drv_scan)
        self.drv_btn_upd = ttk.Button(top, text="⬆ Обновить отмеченные", command=self.drv_update_checked)
        self.drv_btn_rec = ttk.Button(top, text="⬆⬆ Обновить рекомендуемые", command=self.drv_update_recommended)
        self.drv_btn_rb = ttk.Button(top, text="↩ Откат…", command=self.drv_rollback_selected)
        self.drv_btn_pin = ttk.Button(top, text="📌 Не обновлять", command=self.drv_toggle_pin)
        self.drv_btn_bak = ttk.Button(top, text="💾 Копия всех драйверов", command=self.drv_backup_all)
        self.drv_btns = [self.drv_btn_scan, self.drv_btn_upd, self.drv_btn_rec, self.drv_btn_rb,
                         self.drv_btn_pin, self.drv_btn_bak]
        for b in self.drv_btns:
            b.pack(side="left", padx=(0, 6))
        ttk.Button(top, text="🌐 Сайт производителя", command=self.drv_open_vendor).pack(side="left")

        opt = ttk.LabelFrame(tab, text=" Безопасность и автоматизация ", padding=(8, 2))
        opt.pack(fill="x", pady=(6, 2))
        self.drv_vars = {}
        spec = (("restore_point", "Создавать точку восстановления перед установкой"),
                ("backup", "Сохранять копию текущего драйвера (для отката)"),
                ("skip_high_risk", "«Обновить рекомендуемые» не трогает 🔴 критичные"),
                ("hide_inbox", "Скрывать встроенные драйверы Windows"),
                ("scan_on_start", "Проверять драйверы при запуске"))
        for i, (k, label) in enumerate(spec):
            v = tk.BooleanVar(value=o[k])
            self.drv_vars[k] = v
            cmd = self.drv_save_options if k != "hide_inbox" else self.drv_hide_changed
            ttk.Checkbutton(opt, text=label, variable=v, command=cmd).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24))

        flt = ttk.Frame(tab, padding=(0, 6))
        flt.pack(fill="x")
        ttk.Label(flt, text="Поиск:").pack(side="left")
        self.drv_search = tk.StringVar()
        self.drv_search.trace_add("write", lambda *_: self.drv_refresh_table())
        ttk.Entry(flt, textvariable=self.drv_search, width=24).pack(side="left", padx=(4, 14))
        ttk.Label(flt, text="Показывать:").pack(side="left")
        self.drv_view = tk.StringVar(value="Все")
        c = ttk.Combobox(flt, textvariable=self.drv_view, state="readonly", width=22,
                         values=["Все", "Только с обновлениями", "Не обновлять", "Критичные 🔴"])
        c.pack(side="left", padx=4)
        c.bind("<<ComboboxSelected>>", lambda e: self.drv_refresh_table())
        self.drv_summary = tk.StringVar()
        ttk.Label(flt, textvariable=self.drv_summary, foreground="#555").pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        cols = ("chk", "name", "cls", "cur", "date", "new", "risk", "status")
        self.drv_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for c, h, w, a in (("chk", "☑", 40, "center"), ("name", "Устройство", 300, "w"), ("cls", "Класс", 95, "w"),
                           ("cur", "Установлен", 120, "w"), ("date", "Дата", 85, "w"),
                           ("new", "Доступен", 120, "w"), ("risk", "Риск", 95, "w"), ("status", "Статус", 150, "w")):
            self.drv_tree.heading(c, text=h, command=(self.drv_toggle_all if c == "chk"
                                                      else (lambda c=c: self.drv_sort_by(c))))
            self.drv_tree.column(c, width=w, anchor=a, stretch=(c == "name"))
        sb = ttk.Scrollbar(wrap, command=self.drv_tree.yview)
        self.drv_tree.configure(yscrollcommand=sb.set)
        self.drv_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.drv_tree.tag_configure("update", background="#fff3cd")
        self.drv_tree.tag_configure("high", background="#fde2e1")
        self.drv_tree.tag_configure("pinned", background="#e3f0ff")
        self.drv_tree.bind("<Button-1>", self.drv_on_click)
        self.drv_tree.bind("<<TreeviewSelect>>", self.drv_on_select)
        self.drv_tree.bind("<space>", self.drv_space)
        self.drv_details = tk.StringVar(value="Выберите драйвер, чтобы увидеть подробности и совет.")
        self.drv_detail_lbl = ttk.Label(tab, textvariable=self.drv_details, wraplength=1000, justify="left",
                                        foreground="#333", padding=(2, 6))
        self.drv_detail_lbl.pack(fill="x")
        tab.bind("<Configure>", lambda e: self.drv_detail_lbl.configure(wraplength=max(300, e.width - 20)))

    def drv_save_options(self):
        self.cfg["drivers"].update({k: v.get() for k, v in self.drv_vars.items()})
        save_settings(self.cfg)

    def drv_hide_changed(self):
        self.drv_save_options()
        self.status.set("Параметр применится после следующей проверки драйверов.")

    def drv_pinned(self, d):
        return drv_key(d) in self.cfg["drv_pins"]

    def drv_eff(self, d):
        return "pinned" if self.drv_pinned(d) else d["status"]

    def drv_updatable(self, d):
        return d["status"] == "update" and bool(d.get("uid")) and not self.drv_pinned(d)

    def drv_refresh_table(self):
        sel = set(self.drv_tree.selection())
        self.drv_tree.delete(*self.drv_tree.get_children())
        q, view = self.drv_search.get().lower().strip(), self.drv_view.get()
        items = list(self.drivers)
        if self.drv_sort_col:
            kf = {"name": lambda d: d["name"].lower(), "cls": lambda d: d["cls"].lower(),
                  "cur": lambda d: d["version"], "date": lambda d: d["date"], "new": lambda d: d["available"],
                  "risk": lambda d: RISK_ORDER[d["risk"]], "status": lambda d: STATUS_ORDER.get(self.drv_eff(d), 9)
                  }[self.drv_sort_col]
            items.sort(key=kf, reverse=self.drv_sort_desc)
        else:  # сначала те, что нужно обновить
            items.sort(key=lambda d: (STATUS_ORDER.get(self.drv_eff(d), 9), d["name"].lower()))
        for d in items:
            eff, k = self.drv_eff(d), drv_key(d)
            if view == "Только с обновлениями" and eff != "update":
                continue
            if view == "Не обновлять" and eff != "pinned":
                continue
            if view == "Критичные 🔴" and d["risk"] != "high":
                continue
            if q and q not in d["name"].lower() and q not in d["cls"].lower() and q not in d["manufacturer"].lower():
                continue
            mark = ("☑" if k in self.drv_checked else "☐") if self.drv_updatable(d) else ""
            tags = ("pinned",) if eff == "pinned" else (("high",) if eff == "update" and d["risk"] == "high"
                                                        else (("update",) if eff == "update" else ()))
            status = DRV_STATUS[eff]
            if eff == "pinned" and d["status"] == "update":
                status += f" (есть {d['available']})"
            self.drv_tree.insert("", "end", iid=k, tags=tags, values=(
                mark, d["name"], d["cls"], d["version"], d["date"], d["available"],
                RISK_LABEL[d["risk"]], status))
        for k in sel:
            if self.drv_tree.exists(k):
                self.drv_tree.selection_add(k)
        n_up = sum(1 for d in self.drivers if self.drv_updatable(d))
        when = self.cfg.get("drv_cache_time") or "—"
        self.drv_summary.set(f"Драйверов: {len(self.drivers)}  ·  обновлений: {n_up}  ·  "
                             f"не обновлять: {len(self.cfg['drv_pins'])}  ·  проверка: {when}")

    def drv_sort_by(self, col):
        self.drv_sort_desc = (not self.drv_sort_desc) if self.drv_sort_col == col else False
        self.drv_sort_col = col
        self.drv_refresh_table()

    def drv_by_key(self, k):
        return next((d for d in self.drivers if drv_key(d) == k), None)

    def drv_selected(self):
        return [d for d in (self.drv_by_key(k) for k in self.drv_tree.selection()) if d]

    def drv_on_click(self, e):
        if self.drv_tree.identify_region(e.x, e.y) == "cell" and self.drv_tree.identify_column(e.x) == "#1":
            row = self.drv_tree.identify_row(e.y)
            d = self.drv_by_key(row) if row else None
            if d and self.drv_updatable(d):
                self.drv_checked.symmetric_difference_update({row})
                self.drv_refresh_table()
            return "break"

    def drv_space(self, e):
        for d in self.drv_selected():
            if self.drv_updatable(d):
                self.drv_checked.symmetric_difference_update({drv_key(d)})
        self.drv_refresh_table()
        return "break"

    def drv_toggle_all(self):
        ups = {drv_key(d) for d in self.drivers if self.drv_updatable(d)}
        self.drv_checked = set() if ups and ups <= self.drv_checked else ups
        self.drv_refresh_table()

    def drv_on_select(self, _e=None):
        sel = self.drv_selected()
        if not sel:
            return
        d = sel[0]
        lines = [f"{d['name']}  ·  {d['manufacturer'] or d['provider'] or '—'}  ·  класс: {d['cls'] or '—'}  ·  "
                 f"риск: {RISK_LABEL[d['risk']]}"]
        if d["status"] == "update":
            lines.append(f"Доступно: {d['available']} ({d['avail_date'] or 'дата неизвестна'}) · {d['title']}")
        lines.append("Совет: " + driver_advice(d))
        label, _ = vendor_link(d)
        lines.append(f"Сайт производителя: {label} (кнопка «🌐 Сайт производителя»).")
        self.drv_details.set("\n".join(lines))

    def drv_toggle_pin(self):
        sel = self.drv_selected()
        if not sel:
            messagebox.showinfo("Не обновлять", "Выберите драйвер в таблице.")
            return
        for d in sel:
            k = drv_key(d)
            if k in self.cfg["drv_pins"]:
                del self.cfg["drv_pins"][k]
                self.write_log(f"📌 Драйвер снова будет предлагаться к обновлению: {d['name']}")
            else:
                self.cfg["drv_pins"][k] = {"name": d["name"], "version": d["version"]}
                self.drv_checked.discard(k)
                self.write_log(f"📌 Драйвер не будет обновляться: {d['name']} ({d['version'] or '?'})")
        save_settings(self.cfg)
        self.drv_refresh_table()

    def drv_open_vendor(self):
        sel = self.drv_selected()
        if not sel:
            messagebox.showinfo("Сайт производителя", "Выберите драйвер в таблице.")
            return
        label, url = vendor_link(sel[0])
        self.write_log(f"🌐 Открываю: {label} — {url}")
        webbrowser.open(url)

    # --- проверка
    def start_drv_scan(self):
        if self.busy:
            return
        if winreg is None:
            return
        self.set_busy(True, "Проверка драйверов: опрашиваю Центр обновления Windows (может занять до минуты)…")
        self.write_log("▶ Проверка драйверов…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        hide = self.cfg["drivers"]["hide_inbox"]
        log = lambda t: self.q.put(("log", t))

        def work():
            try:
                self.q.put(("drv_scan_done", scan_drivers(log, hide)))
            except Exception as e:
                self.q.put(("log", f"❌ Ошибка проверки драйверов: {e}"))
                self.q.put(("drv_scan_done", None))
        threading.Thread(target=work, daemon=True).start()

    # --- установка
    def drv_update_checked(self):
        items = [d for d in self.drivers if drv_key(d) in self.drv_checked and self.drv_updatable(d)]
        if not items:
            items = [d for d in self.drv_selected() if self.drv_updatable(d)]
        if not items:
            messagebox.showinfo("Не выбрано", "Отметьте галочкой ☑ драйверы с обновлениями\n"
                                              "(клик по ☑ в заголовке отмечает все).")
            return
        self.drv_confirm_run(items)

    def drv_update_recommended(self):
        skip = self.cfg["drivers"]["skip_high_risk"]
        items = [d for d in self.drivers if self.drv_updatable(d) and not (skip and d["risk"] == "high")]
        if not items:
            messagebox.showinfo("Нечего обновлять", "Нет рекомендуемых обновлений драйверов.\n"
                                                    "Критичные (🔴) отмечайте вручную.")
            return
        self.drv_confirm_run(items)

    def drv_confirm_run(self, items):
        o = self.cfg["drivers"]
        lines = [f" {RISK_LABEL[d['risk']].split()[0]} {d['name']}: {d['version'] or '?'} → {d['available']}"
                 for d in items[:12]]
        if len(items) > 12:
            lines.append(f" …и ещё {len(items) - 12}")
        steps = []
        if o["restore_point"]:
            steps.append("• создаётся точка восстановления (если включена защита системы)")
        if o["backup"]:
            steps.append("• сохраняется копия текущего драйвера для отката")
        steps.append("• драйверы ставятся по одному, затем возможна перезагрузка")
        msg = "Будут установлены драйверы из Центра обновления Windows:\n\n" + "\n".join(lines) + \
              "\n\n" + "\n".join(steps)
        if any(d["risk"] == "high" for d in items):
            msg += "\n\n⚠ Среди них есть критичные (🔴): чипсет, система, накопители или прошивки.\n" \
                   "Продолжайте, только если уверены."
        if messagebox.askyesno("Обновить драйверы?", msg):
            self.start_drv_jobs([{"action": "update", "item": d} for d in items])

    def start_drv_jobs(self, jobs):
        self.cancel = threading.Event()
        self.running_jobs = True
        self.set_busy(True, "Установка драйверов…")
        self.progress.configure(mode="determinate", maximum=len(jobs), value=0)
        threading.Thread(target=self.drv_jobs_worker, args=(jobs, self.cancel), daemon=True).start()

    def drv_jobs_worker(self, jobs, cancel):
        o = dict(self.cfg["drivers"])
        glog = lambda t: self.q.put(("log", t))
        ok_n, total, reboot_any = 0, len(jobs), False
        if o["restore_point"] and any(j["action"] == "update" for j in jobs):
            glog("🛟 Создаю точку восстановления…")
            buf = []
            stream_cmd(ps_enc(RESTORE_PS), lambda t: (buf.append(t), glog(t)), cancel, idle_warn=180, hard_timeout=420)
            if any("WARN:" in l for l in buf) and not any("1440" in l or "already" in l.lower() for l in buf):
                glog("   ⚠ Точка восстановления не создана (возможно, выключена «Защита системы») — продолжаю.")
        for n, job in enumerate(jobs, 1):
            if cancel.is_set():
                glog("⏹ Остановлено — оставшиеся драйверы пропущены")
                break
            d, act = job["item"], job["action"]
            jid, lines = f"{int(time.time() * 1000)}_{n}", []

            def jl(t, jid=jid, lines=lines):
                lines.append(t[:300])
                self.q.put(("jlog", (t, jid)))

            self.q.put(("progress", (n - 1, total, f"Драйвер {n}/{total}: {d['name']}")))
            ok, extra, frm, to = False, None, d["version"], d.get("available", "")
            try:
                if act == "update":
                    jl(f"⬆ Драйвер: {d['name']}: {d['version'] or '?'} → {d['available']}")
                    backup = backup_driver(d, jl, cancel) if o["backup"] else ""
                    rc = stream_cmd(ps_enc(install_script(d["uid"])), jl, cancel, idle_warn=300, hard_timeout=3600)
                    text = "\n".join(lines)
                    reboot_any |= "REBOOT:True" in text
                    ok = rc == 0
                    extra = {"device_id": d["device_id"], "backup": backup, "old_inf": d["inf"], "old_version": d["version"]}
                    jl("   ✔ установлено" + (" (нужна перезагрузка)" if "REBOOT:True" in text else "") if ok else
                       "   ⚠ установка не удалась" + ("" if rc is None else f" (код {rc:#x}) — подробности выше"))
                else:
                    ex = job["extra"]
                    jl(f"↩ Откат драйвера: {d['name']} → {job['to']}")
                    rc = stream_cmd(ps_enc(rollback_script(ex["device_id"], ex.get("old_inf", ""), ex.get("backup", ""))),
                                    jl, cancel, idle_warn=240, hard_timeout=900)
                    ok = rc == 0 and "ROLLBACK:DONE" in "\n".join(lines)
                    reboot_any |= ok
                    frm, to, extra = d["version"], job["to"], ex
                    jl("   ✔ откат выполнен" if ok else "   ⚠ откат не удался — подробности выше")
            except Exception as e:
                jl(f"   ❌ {e}")
            if cancel.is_set():
                jl("⏹ Остановлено пользователем")
                break
            ok_n += ok
            self.q.put(("hist", self.make_hist(d, act, ok, jid, lines, kind="driver", frm=frm, to=to, extra=extra)))
        self.q.put(("progress", (total, total, "Завершено")))
        self.q.put(("drv_jobs_done", (f"Драйверы: выполнено успешно {ok_n} из {total}."
                                      + ("" if cancel.is_set() else " Повторная проверка…"), reboot_any and not cancel.is_set())))

    # --- откат
    def last_driver_update(self, d):
        for h in reversed(self.cfg["history"]):
            ex = h.get("extra") or {}
            if (h.get("kind") == "driver" and h["action"] == "update" and h["ok"]
                    and (h["name"] == d["name"] or (ex.get("device_id") and ex["device_id"] == d["device_id"]))):
                return h
        return None

    def drv_rollback_selected(self):
        sel = self.drv_selected()
        if not sel:
            messagebox.showinfo("Откат", "Выберите драйвер в таблице.")
            return
        h = self.last_driver_update(sel[0])
        if not h:
            messagebox.showinfo("Откат недоступен",
                                "Нет записи об обновлении этого драйвера через программу.\n\n"
                                "Откат доступен для драйверов, обновлённых здесь (см. «История и откат»).\n"
                                "Для остальных: Диспетчер устройств → Свойства → Драйвер → Откатить.")
            return
        self.drv_rollback_entry(h)

    def drv_rollback_entry(self, h):
        ex = h.get("extra") or {}
        if h.get("action") != "update" or not ex.get("device_id"):
            messagebox.showinfo("Откат", "Откатить можно только успешное обновление драйвера.")
            return
        how = ("восстановлена сохранённая копия" if ex.get("backup") and os.path.isdir(ex["backup"])
               else "Windows выберет предыдущий или встроенный драйвер (копии нет)")
        if not messagebox.askyesno("Откат драйвера",
                                   f"Откатить «{h['name']}» с версии {h['to']} на {h['from'] or 'предыдущую'}?\n\n"
                                   f"Текущий пакет драйвера будет удалён, {how}.\n"
                                   "Может потребоваться перезагрузка."):
            return
        d = self.drv_by_key(drv_key({"device_id": ex["device_id"]})) or {
            "name": h["name"], "version": h["to"], "device_id": ex["device_id"], "uid": "", "available": ""}
        self.start_drv_jobs([{"action": "rollback", "item": d, "extra": ex, "to": h["from"]}])

    # --- резервная копия всех драйверов
    def drv_backup_all(self):
        dest = os.path.join(app_dir(), "driver_backups", "all_" + time.strftime("%Y%m%d_%H%M%S"))
        if not messagebox.askyesno("Копия всех драйверов",
                                   f"Сохранить копию всех сторонних драйверов в папку:\n{dest}\n\n"
                                   "Это ваш архив стабильных драйверов; может занять несколько минут."):
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        self.cancel = threading.Event()
        self.running_jobs = True
        self.set_busy(True, "Создаю копию драйверов…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.write_log(f"💾 Копия всех драйверов → {dest}")
        script = f"Export-WindowsDriver -Online -Destination '{dest}' | Out-String; Write-Output 'EXPORT:DONE'"
        log = lambda t: self.q.put(("log", t))

        def work():
            buf = []
            rc = stream_cmd(ps_enc(script), lambda t: (buf.append(t), log(t)), self.cancel, idle_warn=300, hard_timeout=3600)
            ok = rc == 0 and any("EXPORT:DONE" in l for l in buf)
            self.progress.stop()
            self.q.put(("plain_done", "Копия драйверов сохранена." if ok else "Копию драйверов создать не удалось — см. журнал."))
        threading.Thread(target=work, daemon=True).start()

    def ask_reboot(self):
        if messagebox.askyesno("Перезагрузка", "Для завершения установки драйверов нужна перезагрузка.\n\n"
                                               "Перезагрузить компьютер через 15 секунд?\n(сохраните открытые документы)"):
            try:
                subprocess.run(["shutdown", "/r", "/t", "15", "/c", "Update Checker: драйверы"],
                               stdin=DEVNULL, creationflags=NO_WINDOW)
                self.on_close()
            except OSError as e:
                self.write_log(f"❌ Не удалось запустить перезагрузку: {e}")


def main():
    try:  # чёткий текст при масштабировании Windows 11
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # Запуск от имени администратора. Готовый exe уже собран с манифестом UAC;
    # этот блок нужен при запуске из исходников (python update_checker.py).
    if winreg is not None and not is_admin() and "--no-elevate" not in sys.argv:
        if relaunch_as_admin():
            return
    App().mainloop()


if __name__ == "__main__":
    main()
