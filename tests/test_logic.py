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


import base64, json
G1 = "11111111-2222-3333-4444-555555555555"; G2 = "66666666-7777-8888-9999-000000000000"; G3 = "abcdefab-cdef-abcd-efab-cdefabcdefab"
INV = [
 {"name": "NVIDIA GeForce RTX 3060", "manufacturer": "NVIDIA", "cls": "Display", "version": "31.0.15.3623", "date": "2024-01-10", "inf": "oem12.inf", "hwid": "PCI\\VEN_10DE&DEV_2503", "device_id": "PCI\\VEN_10DE&DEV_2503&SUBSYS_1\\4&1", "provider": "NVIDIA"},
 {"name": "Intel(R) Wi-Fi 6 AX201", "manufacturer": "Intel", "cls": "Net", "version": "22.0.0.1", "date": "2023-05-01", "inf": "oem7.inf", "hwid": "PCI\\VEN_8086&DEV_A0F0", "device_id": "PCI\\VEN_8086&DEV_A0F0&SUBSYS_2\\3&2", "provider": "Intel"},
 {"name": "Intel(R) Management Engine Interface", "manufacturer": "Intel", "cls": "System", "version": "2300.5.1", "date": "2023-01-01", "inf": "oem9.inf", "hwid": "PCI\\VEN_8086&DEV_A0E0", "device_id": "PCI\\VEN_8086&DEV_A0E0&SUBSYS_3\\3&3", "provider": "Intel"},
 {"name": "HID-compliant mouse", "manufacturer": "Microsoft", "cls": "HIDClass", "version": "10.0.1", "date": "2022-01-01", "inf": "input.inf", "hwid": "HID\\VID_1", "device_id": "HID\\VID_1\\5", "provider": "Microsoft"},
 {"name": "USB Root Hub", "manufacturer": "Microsoft", "cls": "USB", "version": "10.0.2", "date": "2022-01-01", "inf": "usb.inf", "hwid": "USB\\ROOT_HUB30", "device_id": "USB\\ROOT_HUB30\\6", "provider": "Microsoft"},
]
UPS = [
 {"uid": G1, "title": "NVIDIA - Display - 31.0.15.5123", "model": "NVIDIA GeForce RTX 3060", "manufacturer": "NVIDIA", "provider": "NVIDIA", "cls": "Display", "hwid": "PCI\\VEN_10DE&DEV_2503", "date": "2024-06-01", "ver": "", "size": 700000000, "reboot": False},
 {"uid": G2, "title": "Intel - System - 2400.1.2", "model": "Intel(R) Management Engine Interface", "manufacturer": "Intel", "provider": "Intel", "cls": "System", "hwid": "PCI\\VEN_8086&DEV_A0E0", "date": "2024-02-01", "ver": "", "size": 5000000, "reboot": True},
 {"uid": G3, "title": "Realtek - Extension - 6.0.9", "model": "Realtek Audio Effects", "manufacturer": "Realtek", "provider": "Realtek", "cls": "Extension", "hwid": "HDAUDIO\\FUNC_01", "date": "2024-03-01", "ver": "", "size": 1000, "reboot": False},
]
WU_ERROR = False


def decode(cmd):
    if "-EncodedCommand" in cmd:
        return base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
    return ""


def fake(cmd, timeout=240, tag=None):
    j = " ".join(cmd)
    out = ""
    ps = decode(cmd)
    if "Win32_PnPSignedDriver" in ps and "ConvertTo-Json" in ps: out = json.dumps(INV)
    elif "Type='Driver'" in ps: out = "ERROR:0x8024402C" if WU_ERROR else json.dumps(UPS)
    elif cmd[:2] == ["winget", "upgrade"]: out = WG_UP
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

# ================= драйверы =================
logs = []
items, wu_ok = m.scan_drivers(logs.append, hide_inbox=True)
by = {d["name"]: d for d in items}
assert wu_ok
nv = by["NVIDIA GeForce RTX 3060"]
assert nv["status"] == "update" and nv["available"] == "31.0.15.5123" and nv["version"] == "31.0.15.3623", nv   # версия из заголовка
assert nv["uid"] == G1 and nv["risk"] == "mid" and nv["device_id"].startswith("PCI"), nv
me = by["Intel(R) Management Engine Interface"]
assert me["status"] == "update" and me["risk"] == "high" and me["reboot"] is True
rl = by["Realtek Audio Effects"]            # устройство в системе не найдено — всё равно показываем
assert rl["status"] == "update" and rl["version"] == "" and rl["risk"] == "low"
assert by["Intel(R) Wi-Fi 6 AX201"]["status"] == "ok"
assert "HID-compliant mouse" not in by and "USB Root Hub" not in by   # встроенные скрыты
assert [d["name"] for d in items][:3] == sorted([d["name"] for d in items][:3], key=str.lower)  # обновления первыми
items2, _ = m.scan_drivers(logs.append, hide_inbox=False)
assert "HID-compliant mouse" in {d["name"] for d in items2}
print("drivers scan OK")

WU_ERROR = True
logs = []
items3, wu_ok3 = m.scan_drivers(logs.append)
assert wu_ok3 is False and any("8024402C" in l for l in logs), logs
assert all(d["status"] == "ok" for d in items3) and len(items3) >= 3        # список установленных остаётся
WU_ERROR = False
print("drivers WU error OK")

assert m.driver_risk("Firmware") == "high" and m.driver_risk("Display") == "mid" and m.driver_risk("HIDClass") == "low"
assert m.extract_version("Intel - System - 10.1.2.3") == "10.1.2.3" and m.extract_version("no digits") == ""
assert "NVIDIA" in m.vendor_link(nv)[0] and "bing.com" in m.vendor_link({"name": "Foo", "manufacturer": "", "provider": ""})[1]
assert "NVIDIA" in m.driver_advice(nv)
# защита от подстановки в скрипты
assert G1 in m.install_script(G1)
for bad in ("", "x'; Remove-Item C:\\ -Recurse; '", G1 + "'"):
    try: m.install_script(bad); raise SystemExit("install_script принял мусор: " + bad)
    except ValueError: pass
rs = m.rollback_script("PCI\\VEN_10DE&DEV_2503\\4&1", "oem12.inf", "")
assert "Копии нет" in rs and "oem12.inf" in rs and "ROLLBACK:DONE" in rs
try: m.rollback_script("x'; calc; '", "", ""); raise SystemExit("rollback_script принял мусор")
except ValueError: pass
assert "'" not in decode(m.ps_enc("Write-Output 1")).replace("'SilentlyContinue'", "")
print("drivers helpers OK")
print("LOGIC OK")
