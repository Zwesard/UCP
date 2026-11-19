"""Интерфейсный тест (локально, под xvfb): xvfb-run -a python3 tests/test_gui.py. На Windows пропускается."""
import base64, copy, os, sys, tempfile, time, types
if sys.platform == "win32":
    print("skip (GUI-тест только для Linux/xvfb)"); sys.exit(0)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_checker as m
tmp = tempfile.mkdtemp()
m.SETTINGS_PATH = os.path.join(tmp, "settings.json"); m.app_dir = lambda: tmp
m.winreg = types.SimpleNamespace()               # притворяемся Windows: включает автопроверку
m.find_winget = lambda: "winget"; m.find_choco = lambda: None; m.find_scoop = lambda: None
from tkinter import messagebox
messagebox.showinfo = messagebox.showwarning = messagebox.showerror = lambda *a, **k: None
messagebox.askyesno = lambda *a, **k: True
messagebox.askyesnocancel = lambda *a, **k: True

G_OK, G_BAD, G_ME = "11111111-2222-3333-4444-555555555555", "99999999-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"
def drv(name, cls, ver, new="", uid="", inf="oem1.inf", dev=None):
    return {"name": name, "manufacturer": "X", "cls": cls, "version": ver, "date": "2024-01-01", "inf": inf, "hwid": "H" + name,
            "device_id": dev or ("DEV\\" + name), "provider": "X", "available": new, "avail_date": "2024-06-01", "uid": uid,
            "title": f"X - {cls} - {new}", "size": 1, "reboot": False, "status": "update" if uid else "ok", "risk": m.driver_risk(cls)}
DRIVERS = [drv("Audio", "MEDIA", "1.0", "1.1", G_OK), drv("Camera", "Camera", "2.0", "2.1", G_BAD),
           drv("Chipset ME", "System", "9.0", "9.5", G_ME), drv("Wi-Fi", "Net", "5.0"), drv("Mouse", "HIDClass", "1.0")]
PROGRAMS = [{"name": "Zoom", "version": "5", "available": "6", "publisher": "", "id": "Zoom.Zoom", "source": "winget", "status": "update", "location": ""},
            {"name": "Alpha", "version": "1", "available": "", "publisher": "", "id": "a", "source": "winget", "status": "ok", "location": ""}]
m.scan = lambda s, log=None: copy.deepcopy(PROGRAMS)
m.scan_drivers = lambda log=None, hide=True, **k: (copy.deepcopy(DRIVERS), True)
calls = []
def decode(cmd): return base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le") if "-EncodedCommand" in cmd else ""
def fake_stream(cmd, log, cancel, **k):
    ps = decode(cmd); calls.append(ps or cmd[0])
    if cmd[0] == "pnputil.exe": log("   экспортирован"); return 0
    if "Checkpoint-Computer" in ps: log("RESULT:OK"); return 0
    if "CreateUpdateInstaller" in ps:
        if G_BAD in ps: log("Загрузка драйвера..."); log("ERROR: загрузка не удалась, код 4"); return 4
        log("Установка драйвера..."); log("RESULT:2 REBOOT:True HRESULT:0x00000000")
        for d in DRIVERS:   # «система» действительно обновилась
            if d["uid"] and d["uid"] in ps: d.update(status="ok", version=d["available"], available="", uid="")
        return 0
    if "ROLLBACK" in ps: log("Удалять нечего"); log("ROLLBACK:DONE"); return 0
    return 0
m.stream_cmd = fake_stream
app = m.App()
reboot = []; app.ask_reboot = lambda: reboot.append(1)
def pump(t=1.0):
    e = time.time() + t
    while time.time() < e: app.update(); time.sleep(0.02)

# --- вкладка «Драйвера» стоит после «Программы»; проверка драйверов запускается сама после программ
tabs = [app.nb.tab(i, "text").strip() for i in range(app.nb.index("end"))]
assert tabs == ["Программы", "Драйвера", "История и откат"], tabs
pump(2.5)
names = [app.drv_tree.set(k, "name") for k in app.drv_tree.get_children()]
print("драйверы:", names)
assert len(names) == 5 and set(names[:3]) == {"Audio", "Camera", "Chipset ME"}      # обновления сверху
assert "обновлений: 3" in app.drv_summary.get()

# --- детали и совет
app.drv_tree.selection_set(m.drv_key(DRIVERS[2])); app.update()
assert "Совет" in app.drv_details.get() and "🔴" in app.drv_details.get()
# --- «рекомендуемые» не включают критичные (🔴 Chipset ME)
seen = []
app.drv_confirm_run = lambda items: seen.extend(d["name"] for d in items)
app.drv_update_recommended(); assert sorted(seen) == ["Audio", "Camera"], seen
# --- закрепление исключает из обновлений
app.drv_tree.selection_set(m.drv_key(DRIVERS[1])); app.drv_toggle_pin(); app.update()
assert "не обновлять: 1" in app.drv_summary.get()
seen.clear(); app.drv_update_recommended(); assert seen == ["Audio"], seen
app.drv_toggle_pin(); app.update()     # открепили
# --- отметка и установка: успешный (Audio) + неуспешный (Camera)
app.drv_confirm_run = m.App.drv_confirm_run.__get__(app)
app.drv_checked = {m.drv_key(DRIVERS[0]), m.drv_key(DRIVERS[1])}
app.drv_update_checked(); pump(3.0)
hist = [h for h in app.cfg["history"] if h.get("kind") == "driver"]
print("история:", [(h["name"], h["ok"]) for h in hist])
assert [(h["name"], h["ok"]) for h in hist] == [("Audio", True), ("Camera", False)]
assert hist[0]["extra"]["backup"].startswith(tmp), hist[0]["extra"]   # копия сохранена рядом с программой
assert any("Checkpoint-Computer" in c for c in calls if isinstance(c, str)), "точка восстановления не создавалась"
assert reboot, "не предложена перезагрузка"
audio = [d for d in app.drivers if d["name"] == "Audio"][0]
assert audio["status"] == "ok" and audio["version"] == "1.1"            # исчез из списка обновлений
assert [d for d in app.drivers if d["name"] == "Camera"][0]["status"] == "update"   # неудача — остался

# --- история: клик по записи переходит к ошибке в журнале
bad_idx = [i for i, h in enumerate(app.cfg["history"]) if h["name"] == "Camera"][0]
app.nb.select(2); pump(0.5)
app.clear_log()                                          # журнал очищен -> строки восстановятся из сохранённых
app.htree.selection_set(str(bad_idx)); pump(0.5)
txt = app.log.get("1.0", "end")
assert "Журнал: Camera" in txt and "загрузка не удалась" in txt, txt
assert app.log.tag_ranges("focus"), "нет подсветки"
jid = app.cfg["history"][bad_idx]["jid"]
err = app.log.tag_ranges(f"err_{jid}")
assert err, "ошибка не помечена"
err_text = app.log.get(err[0], err[1]); print("ошибка в журнале:", err_text.strip())
assert "ERROR" in err_text or "⚠" in err_text
assert "показана ошибка" in app.status.get()
# повторный клик по успешной записи — ошибок нет
ok_idx = [i for i, h in enumerate(app.cfg["history"]) if h["name"] == "Audio"][0]
app.htree.selection_set(str(ok_idx)); pump(0.3)
assert "ошибок в записи нет" in app.status.get()
# запись программы (старого формата, без лога) — аккуратное сообщение
app.cfg["history"].append({"time": "t", "name": "Old", "id": "o", "source": "winget", "action": "update", "from": "1", "to": "2", "ok": True})
app.refresh_history(); pump(0.2); app.htree.selection_set(str(len(app.cfg["history"]) - 1)); pump(0.3)
assert "не сохранился" in app.status.get()

# --- откат драйвера из истории
calls.clear()
app.cfg["history"][ok_idx]["extra"]["device_id"] = "DEV\\Audio"
app.htree.selection_set(str(ok_idx)); pump(0.2); app.rollback_history(); pump(2.0)
assert any("ROLLBACK" in c for c in calls if isinstance(c, str)), calls
assert app.cfg["history"][-1]["action"] == "rollback" and app.cfg["history"][-1]["ok"]
app.on_close(); print("GUI OK")
