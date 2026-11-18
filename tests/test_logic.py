import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_checker as m

WG_UP = """Имя                 Код              Версия   Доступно  Источник
-------------------------------------------------------------------
Mozilla Firefox     Mozilla.Firefox  130.0    131.0     winget
7-Zip 23.01 (x64)   7zip.7zip        23.01    24.08     winget
Telegram            9NZTWSQNTD0S     4.0      5.0       msstore
3 обновлений доступно.
"""
WG_LS = """Имя                 Код                  Версия   Источник
---------------------------------------------------------------
Mozilla Firefox     Mozilla.Firefox      130.0    winget
Notepad++           Notepad++.Notepad++  8.6      winget
Some Local App      ARP\\Machine\\X64\\Foo 1.0
"""
SCOOP_ST = """Name  Installed Version Latest Version Missing Dependencies Info
----  ----------------- -------------- -------------------- ----
git   2.40.0            2.41.0
"""
SCOOP_LS = """Name Version Source Updated Info
---- ------- ------ ------- ----
git  2.40.0  main   2024-01-01
jq   1.7     main   2024-01-01
"""
CH_OUT = "Chocolatey v2.2\ngooglechrome|120.0|121.0|false\n"
CH_LS = "googlechrome|120.0\nvlc|3.0.18\n"


def fake(cmd, timeout=240, tag=None):
    j = " ".join(cmd)
    out = ""
    if cmd[:2] == ["winget", "upgrade"]: out = WG_UP
    elif cmd[:2] == ["winget", "list"]: out = WG_LS
    elif cmd[0] == "choco" and "outdated" in cmd: out = CH_OUT
    elif cmd[0] == "choco" and "list" in cmd: out = CH_LS
    elif "scoop status" in j: out = SCOOP_ST
    elif "scoop list" in j: out = SCOOP_LS
    if tag: m.RAW[tag] = out
    return out


m.run_text = fake
m.find_winget = lambda: "winget"; m.find_choco = lambda: "choco"; m.find_scoop = lambda: "scoop"
m.read_installed = lambda: {
    "Mozilla Firefox (x64 ru)": {"version": "130.0", "publisher": "Mozilla", "location": r"C:\Program Files\Mozilla Firefox"},
    "7-Zip 23.01 (x64)": {"version": "23.01", "publisher": "Igor", "location": ""},
    "Notepad++ (64-bit)": {"version": "8.6", "publisher": "", "location": ""},
    "Google Chrome": {"version": "120.0", "publisher": "Google", "location": ""},
    "Random Tool": {"version": "1", "publisher": "", "location": ""},
}
logs = []
items = m.scan({"winget": True, "msstore": True, "choco": True, "scoop": True}, logs.append)
st = {i["name"]: (i["status"], i["source"]) for i in items}
assert st["Mozilla Firefox (x64 ru)"] == ("update", "winget"), st
assert st["7-Zip 23.01 (x64)"] == ("update", "winget")
assert st["Notepad++ (64-bit)"] == ("ok", "winget")
assert st["Google Chrome"] == ("update", "choco")
assert st["Random Tool"][0] == "unknown"
assert st["git"] == ("update", "scoop") and st["jq"][0] == "ok"
assert st["Telegram"] == ("update", "msstore")
assert any("winget: известных программ" in l for l in logs), logs
it2 = m.scan({"winget": True, "msstore": False, "choco": False, "scoop": False})
assert all(i["source"] not in ("msstore", "choco", "scoop") for i in it2)
print("scan OK")

# --- разбор: колонки «съехали» из-за широких символов -> запасной разбор по 2+ пробелам
SHIFT = """Имя                 Код              Версия   Доступно  Источник
-------------------------------------------------------------------
日本語アプリ  Foo.Bar  1.0  2.0  winget
"""
valid = lambda c: len(c) >= 4 and c[1] and " " not in c[1] and c[2] and m.has_digit(c[3])
rows = m.table_rows(SHIFT, valid)
assert rows and rows[0][1] == "Foo.Bar" and rows[0][3] == "2.0", rows
# пустой/мусорный вывод не ломает разбор
assert m.table_rows("", valid) == [] and m.table_rows("Не найдено", valid) == []
print("parse OK")

# --- потоковый запуск
buf = []
rc = m.stream_cmd([sys.executable, "-c", "print('hello');print('world')"], buf.append, threading.Event())
assert rc == 0 and any("hello" in l for l in buf), (rc, buf)
rc = m.stream_cmd([sys.executable, "-c", "import sys;sys.exit(3)"], buf.append, threading.Event())
assert rc == 3
# предупреждение о «зависании»
buf = []; ev = threading.Event(); threading.Timer(3.5, ev.set).start()
rc = m.stream_cmd([sys.executable, "-c", "import time;time.sleep(60)"], buf.append, ev, idle_warn=1)
assert rc is None and any("Нет вывода" in l for l in buf), buf
# отмена кнопкой «Остановить» убивает процесс
buf = []; ev = threading.Event(); threading.Timer(1.5, ev.set).start()
t0 = time.time()
rc = m.stream_cmd([sys.executable, "-c", "print('x',flush=True);import time;time.sleep(60)"], buf.append, ev)
assert rc is None and time.time() - t0 < 15
print("stream OK")
print("LOGIC OK")
