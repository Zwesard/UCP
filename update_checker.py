"""
Update Checker — проверка и установка обновлений программ в Windows 11 (аналог SUMo).

Источники: winget, Microsoft Store, Chocolatey, Scoop.
Портативный режим: все данные лежат в settings.json рядом с программой.
"""
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
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

try:
    import winreg
except ImportError:  # запуск не на Windows (только для разработки)
    winreg = None

VERSION = "1.0.0"
APP_NAME = "Update Checker"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WG_FLAGS = ["--accept-source-agreements", "--disable-interactivity"]
WG_PKG = ["--accept-package-agreements", "--accept-source-agreements", "--silent"]

SOURCE_LABEL = {"winget": "winget", "msstore": "Microsoft Store", "choco": "Chocolatey",
                "scoop": "Scoop", "": "—"}
STATUS_LABEL = {"update": "⬆ Доступно обновление", "ok": "✔ Актуальна",
                "unknown": "· Нет в базах", "pinned": "📌 Закреплена"}
STATUS_ORDER = {"update": 0, "pinned": 1, "ok": 2, "unknown": 3}

UNINSTALL_KEYS = [
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", 0x0100),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM", 0),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU", 0),
]

HELP_TEXT = """КАК ПОЛЬЗОВАТЬСЯ

1. «Проверить» — программа читает список установленного ПО и спрашивает
   winget, Microsoft Store, Chocolatey и Scoop о новых версиях.
2. Поставьте галочки ☑ слева от программ (или кликните по ☑ в заголовке —
   отметятся все с обновлениями) и нажмите «Обновить отмеченные».
3. Если программа запущена, вас спросят, можно ли её закрыть. Сначала
   отправляется обычная команда закрытия окна, и лишь потом — принудительная.

📌 ЗАКРЕПИТЬ ВЕРСИЮ
   Закреплённая программа не обновляется ни кнопкой «Обновить все», ни
   отметкой. Нужна, когда новая версия вас не устраивает.

↩ ОТКАТ
   Установит выбранную старую версию поверх текущей. Работает для winget,
   Chocolatey и Scoop (Microsoft Store не поддерживает откат). После отката
   можно закрепить версию, чтобы она не обновилась снова. Вкладка «История»
   показывает все обновления — выберите запись и нажмите «Откатить».

ПРАВА АДМИНИСТРАТОРА
   Нужны для Chocolatey и части программ в Program Files.
   Кнопка «🛡 От администратора» перезапустит программу с нужными правами.
"""


# ------------------------------------------------------------ portable I/O ---
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_PATH = os.path.join(app_dir(), "settings.json")
DEFAULTS = {
    "geometry": "1180x720", "scan_on_start": True, "close_mode": "ask",
    "sources": {"winget": True, "msstore": True, "choco": True, "scoop": True},
    "pins": {}, "history": [], "cache": [], "cache_time": "",
}


def load_settings():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k == "sources" and isinstance(v, dict):
                cfg["sources"].update(v)
            elif k in cfg and type(v) is type(cfg[k]):
                cfg[k] = v
    except (OSError, ValueError):
        pass
    cfg["cache"] = [c for c in cfg["cache"] if isinstance(c, dict) and "name" in c]
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
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
    else:
        exe, params = sys.executable, f'"{os.path.abspath(__file__)}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


# ------------------------------------------------------------------ утилиты ---
def norm(s):
    return re.sub(r"[^0-9a-zа-я]+", "", (s or "").lower())


def key_of(item):
    return norm(item["name"])


def run_text(cmd, timeout=240):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=NO_WINDOW,
                              timeout=timeout).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def ps_cmd(command):
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]


def have_scoop():
    return bool(shutil.which("scoop")) or os.path.isdir(os.path.expanduser("~/scoop"))


def have_choco():
    return bool(shutil.which("choco"))


def have_winget():
    return bool(shutil.which("winget"))


def parse_table(text):
    """Универсальный разбор таблиц winget/scoop по позициям колонок (любая локаль)."""
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
        rows.append([line[a:b].strip() for a, b in zip(starts, starts[1:] + [None])])
    return rows


def has_digit(s):
    return bool(re.search(r"\d", s or ""))


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
    rows = []
    for c in parse_table(run_text(["winget", "list"] + WG_FLAGS)):
        if len(c) < 4:
            continue
        name, pid, ver, src = c[0], c[1], c[2], c[-1].lower()
        if pid and " " not in pid and src in ("winget", "msstore"):
            rows.append({"name": name, "id": pid, "version": ver.lstrip("<> "), "source": src})
    return rows


def winget_outdated():
    rows = []
    for c in parse_table(run_text(["winget", "upgrade"] + WG_FLAGS)):
        if len(c) < 4:
            continue
        name, pid, cur, avail = c[:4]
        src = c[4].lower() if len(c) > 4 and c[4] else "winget"
        if pid and " " not in pid and cur and has_digit(avail):
            rows.append({"name": name, "id": pid, "version": cur.lstrip("<> "),
                         "available": avail.lstrip("<> "),
                         "source": "msstore" if src.startswith("msstore") else "winget"})
    return rows


def choco_installed():
    out = []
    for line in run_text(["choco", "list", "--local-only", "-r", "--no-color"]).splitlines():
        p = line.strip().split("|")
        if len(p) == 2 and has_digit(p[1]):
            out.append({"name": p[0], "id": p[0], "version": p[1], "source": "choco"})
    return out


def choco_outdated():
    out = []
    for line in run_text(["choco", "outdated", "-r", "--no-color"]).splitlines():
        p = line.strip().split("|")
        if len(p) >= 3 and has_digit(p[1]) and has_digit(p[2]):
            out.append({"name": p[0], "id": p[0], "version": p[1], "available": p[2],
                        "source": "choco"})
    return out


def scoop_installed():
    out = []
    for c in parse_table(run_text(ps_cmd("scoop list"))):
        if len(c) >= 2 and c[0] and " " not in c[0] and has_digit(c[1]):
            out.append({"name": c[0], "id": c[0], "version": c[1], "source": "scoop"})
    return out


def scoop_outdated():
    out = []
    for c in parse_table(run_text(ps_cmd("scoop status"))):
        if len(c) >= 3 and c[0] and " " not in c[0] and has_digit(c[1]) and has_digit(c[2]):
            out.append({"name": c[0], "id": c[0], "version": c[1], "available": c[2],
                        "source": "scoop"})
    return out


def match(nm, table):
    """Сопоставление названий: точное, затем по вхождению."""
    if nm in table:
        return table[nm]
    if len(nm) >= 4:
        for k, v in table.items():
            if len(k) >= 4 and (k in nm or nm in k):
                return v
    return None


def scan(sources):
    """Возвращает список программ (dict) с источником, id и статусом."""
    reg = read_installed()
    tasks = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        if sources.get("winget") or sources.get("msstore"):
            if have_winget():
                tasks["w_inv"] = ex.submit(winget_installed)
                tasks["w_up"] = ex.submit(winget_outdated)
        if sources.get("choco") and have_choco():
            tasks["c_inv"] = ex.submit(choco_installed)
            tasks["c_up"] = ex.submit(choco_outdated)
        if sources.get("scoop") and have_scoop():
            tasks["s_inv"] = ex.submit(scoop_installed)
            tasks["s_up"] = ex.submit(scoop_outdated)
        res = {k: f.result() for k, f in tasks.items()}

    def want(rows):
        return [r for r in rows if sources.get(r["source"], True)]

    def tab(rows):
        return {norm(r["name"]): r for r in want(rows)}

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
        return ["choco", "upgrade", i, "-y", "--no-progress"]
    if s == "scoop":
        return ps_cmd(f"scoop update {i}")
    return None


def rollback_cmd(it, ver):
    s, i = it["source"], it["id"]
    if s == "winget":
        return ["winget", "install", "--id", i, "--exact", "--version", ver, "--force"] + WG_PKG
    if s == "choco":
        return ["choco", "install", i, "--version", ver, "-y", "--force",
                "--allow-downgrade", "--no-progress"]
    if s == "scoop":
        return ps_cmd(f"scoop uninstall {i}; scoop install {i}@{ver}")
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
        for line in run_text(["choco", "list", i, "--exact", "--all-versions", "-r"]).splitlines():
            p = line.strip().split("|")
            if len(p) == 2 and has_digit(p[1]):
                vers.append(p[1])
    return vers


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
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    deadline = time.time() + 8
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.5)
    for pid in pids:
        if pid_alive(pid):
            log(f"   принудительное закрытие PID {pid}")
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                           creationflags=NO_WINDOW)


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
        self.checked = set()
        self.q = queue.Queue()
        self.busy = False
        self.sort_col, self.sort_desc = None, False
        self.title(f"{APP_NAME} {VERSION}" + ("  —  Администратор" if is_admin() else ""))
        self.geometry(self.cfg["geometry"])
        self.minsize(960, 560)
        self.build_ui()
        self.bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_table()
        self.refresh_history()
        if self.items:
            self.status.set(f"Показан результат прошлой проверки ({self.cfg['cache_time']}). "
                            "Нажмите «Проверить» для обновления.")
        else:
            self.status.set("Нажмите «Проверить», чтобы найти обновления.")
        if winreg is None:
            messagebox.showwarning("Только Windows", "Программа работает только в Windows.")
        elif self.cfg["scan_on_start"]:
            self.after(300, self.start_scan)
        self.after(100, self.poll)

    # ------------------------------------------------------------ интерфейс
    def build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Card.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("CardCap.TLabel", foreground="#666")
        style.configure("Treeview", rowheight=26)

        # карточки-счётчики
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

        # панель кнопок
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        self.btn_scan = ttk.Button(bar, text="🔍 Проверить", command=self.start_scan)
        self.btn_upd = ttk.Button(bar, text="⬆ Обновить отмеченные", command=self.update_checked)
        self.btn_all = ttk.Button(bar, text="⬆⬆ Обновить все", command=self.update_all)
        self.btn_pin = ttk.Button(bar, text="📌 Закрепить версию", command=self.toggle_pin)
        self.btn_rb = ttk.Button(bar, text="↩ Откат…", command=self.rollback_selected)
        for b in (self.btn_scan, self.btn_upd, self.btn_all, self.btn_pin, self.btn_rb):
            b.pack(side="left", padx=(0, 6))
        mb = ttk.Menubutton(bar, text="⚙ Настройки")
        self.build_menu(mb)
        mb.pack(side="right")
        if not is_admin() and winreg:
            ttk.Button(bar, text="🛡 От администратора", command=self.admin).pack(side="right", padx=6)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10)
        tab1, tab2 = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.nb.add(tab1, text="  Программы  ")
        self.nb.add(tab2, text="  История и откат  ")

        # --- вкладка «Программы»
        flt = ttk.Frame(tab1, padding=(0, 8))
        flt.pack(fill="x")
        ttk.Label(flt, text="Поиск:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh_table())
        self.search_entry = ttk.Entry(flt, textvariable=self.search, width=24)
        self.search_entry.pack(side="left", padx=(4, 14))
        ttk.Label(flt, text="Показывать:").pack(side="left")
        self.view = tk.StringVar(value="Все")
        c1 = ttk.Combobox(flt, textvariable=self.view, state="readonly", width=18,
                          values=["Все", "Только с обновлениями", "Актуальные",
                                  "Закреплённые", "Нет в базах"])
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

        # --- вкладка «История»
        hb = ttk.Frame(tab2, padding=(0, 8))
        hb.pack(fill="x")
        ttk.Button(hb, text="↩ Откатить выбранное обновление", command=self.rollback_history).pack(side="left")
        ttk.Button(hb, text="Очистить историю", command=self.clear_history).pack(side="left", padx=6)
        ttk.Label(hb, text="Выберите запись и нажмите «Откатить», чтобы вернуть версию, которая была до обновления.",
                  foreground="#666").pack(side="left", padx=10)
        hw = ttk.Frame(tab2)
        hw.pack(fill="both", expand=True)
        hc = ("time", "name", "action", "from", "to", "res")
        self.htree = ttk.Treeview(hw, columns=hc, show="headings", selectmode="browse")
        for c, h, w in (("time", "Дата", 140), ("name", "Программа", 300), ("action", "Действие", 100),
                        ("from", "Было", 110), ("to", "Стало", 110), ("res", "Результат", 140)):
            self.htree.heading(c, text=h)
            self.htree.column(c, width=w, anchor="w", stretch=(c == "name"))
        hs = ttk.Scrollbar(hw, command=self.htree.yview)
        self.htree.configure(yscrollcommand=hs.set)
        self.htree.pack(side="left", fill="both", expand=True)
        hs.pack(side="right", fill="y")
        self.htree.tag_configure("bad", foreground="#b00020")

        # лог и статус
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(8, 0))
        self.log = tk.Text(self, height=7, state="disabled", bg="#1e1e1e", fg="#dcdcdc",
                           font=("Consolas", 9), relief="flat")
        self.log.pack(fill="x", padx=10, pady=(6, 0))
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(10, 6)).pack(fill="x")

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
            sm.add_checkbutton(label=label + ("" if ok else "  (не установлен)"), variable=v,
                               command=self.save_options, state="normal" if ok else "disabled")
        m.add_cascade(label="Источники обновлений", menu=sm)
        self.close_var = tk.StringVar(value=self.cfg["close_mode"])
        cm = tk.Menu(m, tearoff=0)
        for val, label in (("ask", "Спрашивать каждый раз"), ("auto", "Закрывать автоматически"),
                           ("never", "Не закрывать")):
            cm.add_radiobutton(label=label, value=val, variable=self.close_var, command=self.save_options)
        m.add_cascade(label="Закрытие запущенных программ", menu=cm)
        self.start_var = tk.BooleanVar(value=self.cfg["scan_on_start"])
        m.add_checkbutton(label="Проверять при запуске", variable=self.start_var, command=self.save_options)
        m.add_separator()
        m.add_command(label="Экспорт списка в CSV…", command=self.export_csv)
        m.add_command(label="Открыть папку с настройками", command=lambda: os.startfile(app_dir()) if hasattr(os, "startfile") else None)
        m.add_command(label="Справка", command=lambda: messagebox.showinfo("Справка", HELP_TEXT))
        m.add_command(label=f"О программе (v{VERSION})", command=lambda: messagebox.showinfo(
            "О программе", f"{APP_NAME} {VERSION}\nПортативный аналог SUMo для Windows 11.\n"
                           "Источники: winget, Microsoft Store, Chocolatey, Scoop."))

    def bind_keys(self):
        self.bind("<F5>", lambda e: self.start_scan())
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Control-a>", lambda e: self.toggle_all_updates())
        self.tree.bind("<space>", self.space_toggle)

    def save_options(self):
        self.cfg["sources"] = {k: v.get() for k, v in self.src_vars.items()}
        self.cfg["close_mode"] = self.close_var.get()
        self.cfg["scan_on_start"] = self.start_var.get()
        save_settings(self.cfg)

    def on_close(self):
        self.cfg["geometry"] = self.geometry()
        self.cfg["cache"] = self.items
        save_settings(self.cfg)
        self.destroy()

    def admin(self):
        if messagebox.askyesno("Права администратора", "Перезапустить программу от имени администратора?"):
            self.on_close()
            relaunch_as_admin()

    # ------------------------------------------------------------- таблица
    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy, msg=None):
        self.busy = busy
        for b in (self.btn_scan, self.btn_upd, self.btn_all, self.btn_pin, self.btn_rb):
            b.configure(state="disabled" if busy else "normal")
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
        else:
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
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.write_log(data)
                elif kind == "progress":
                    self.progress.configure(mode="determinate", maximum=data[1], value=data[0])
                    self.status.set(data[2])
                elif kind == "hist":
                    self.cfg["history"].append(data)
                    self.cfg["history"] = self.cfg["history"][-300:]
                    save_settings(self.cfg)
                    self.refresh_history()
                elif kind == "scan_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if data is not None:
                        self.items = data
                        self.cfg["cache"] = data
                        self.cfg["cache_time"] = time.strftime("%d.%m.%Y %H:%M")
                        save_settings(self.cfg)
                        self.checked &= {key_of(i) for i in data if self.updatable(i)}
                    self.refresh_table()
                    n = int(self.cnt["update"].cget("text"))
                    self.set_busy(False, (f"Готово. Найдено обновлений: {n}. Отметьте нужные ☑ и нажмите «Обновить отмеченные»."
                                          if n else "Готово. Все отслеживаемые программы актуальны."))
                elif kind == "jobs_done":
                    self.set_busy(False, data)
                    self.start_scan()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # --------------------------------------------------------------- проверка
    def start_scan(self):
        if self.busy:
            return
        if not (have_winget() or have_choco() or have_scoop()) and winreg:
            self.write_log("❌ Не найден ни winget, ни Chocolatey, ни Scoop. Установите «Установщик приложений» из Microsoft Store.")
            return
        self.set_busy(True, "Идёт проверка: читаю список программ и опрашиваю источники…")
        self.write_log("▶ Проверка обновлений…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        srcs = dict(self.cfg["sources"])

        def work():
            try:
                self.q.put(("scan_done", scan(srcs)))
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
        """Возвращает dict key -> [pid,...] для закрытия, либо None если пользователь отменил."""
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
        self.set_busy(True, "Установка…")
        self.progress.configure(mode="determinate", maximum=len(jobs), value=0)
        threading.Thread(target=self.jobs_worker, args=(jobs,), daemon=True).start()

    def jobs_worker(self, jobs):
        log = lambda t: self.q.put(("log", t))
        ok_n = 0
        for n, job in enumerate(jobs, 1):
            it, act, ver = job["item"], job["action"], job.get("version")
            title = "Откат" if act == "rollback" else "Обновление"
            self.q.put(("progress", (n - 1, len(jobs), f"{title} {n}/{len(jobs)}: {it['name']}")))
            cmd = rollback_cmd(it, ver) if act == "rollback" else upgrade_cmd(it)
            ok = False
            if not cmd:
                log(f"⚠ {it['name']}: действие не поддерживается для этого источника")
            else:
                if job.get("close"):
                    log(f"⏹ Закрываю запущенные процессы: {it['name']}")
                    close_processes(job["close"], log)
                log(f"{'↩' if act == 'rollback' else '⬆'} {it['name']} → {ver or it['available']}")
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                    for line in p.stdout:
                        line = line.strip()
                        if line and not re.fullmatch(r"[-\\|/\s█▒]+", line):
                            log("   " + line)
                    p.wait()
                    ok = p.returncode == 0
                    log("   ✔ готово" if ok else f"   ⚠ завершено с кодом {p.returncode}"
                        + (" (попробуйте «От администратора»)" if p.returncode else ""))
                except Exception as e:
                    log(f"   ❌ {e}")
            ok_n += ok
            self.q.put(("hist", {"time": time.strftime("%d.%m.%Y %H:%M"), "name": it["name"],
                                 "id": it["id"], "source": it["source"], "action": act,
                                 "from": it["version"], "to": ver or it["available"], "ok": ok}))
        self.q.put(("progress", (len(jobs), len(jobs), "Завершено")))
        self.q.put(("jobs_done", f"Выполнено успешно: {ok_n} из {len(jobs)}. Повторная проверка…"))

    # ----------------------------------------------------------------- закрепление
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
        self.htree.delete(*self.htree.get_children())
        for n, h in reversed(list(enumerate(self.cfg["history"]))):
            res = "✔ успешно" if h["ok"] else "✖ ошибка"
            self.htree.insert("", "end", iid=str(n), tags=("" if h["ok"] else "bad",),
                              values=(h["time"], h["name"], "Откат" if h["action"] == "rollback" else "Обновление",
                                      h["from"], h["to"], res))

    def rollback_history(self):
        s = self.htree.selection()
        if not s:
            messagebox.showinfo("История", "Выберите запись в списке.")
            return
        h = self.cfg["history"][int(s[0])]
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


def main():
    try:  # чёткий текст при масштабировании Windows 11
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
