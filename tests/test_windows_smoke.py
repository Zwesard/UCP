"""Проверка на настоящей Windows (CI): читаем реестр, драйверы и Центр обновления. Только чтение — ничего не меняет."""
import os, sys, time
if sys.platform != "win32":
    print("skip (только Windows)"); sys.exit(0)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import update_checker as m

print("admin:", m.is_admin())
print("winget:", m.find_winget(), "| choco:", m.find_choco(), "| scoop:", m.find_scoop())
reg = m.read_installed()
print("программ в реестре:", len(reg)); assert reg, "реестр программ пуст"
t = time.time(); inv = m.driver_inventory()
print(f"драйверов в системе: {len(inv)} ({time.time() - t:.1f} c)"); print("пример:", inv[:2])
print("ошибки инвентаря:", m.ERRS); assert inv, "список драйверов пуст — проверьте PowerShell-скрипт"
assert all(d["name"] and "device_id" in d for d in inv)
m.ERRS.clear(); t = time.time()
ups = m.driver_updates(timeout=150)
print(f"Центр обновления: {'недоступен' if ups is None else len(ups)} ({time.time() - t:.1f} c); ошибки:", m.ERRS)
if ups: print("пример обновления:", ups[0])
items, wu_ok = m.scan_drivers(print, wu_timeout=150)
print("scan_drivers:", len(items), "wu_ok:", wu_ok)

# --- winget / Chocolatey на настоящем выводе (то, из-за чего раньше «ничего не находило»)
if m.find_winget():
    m.ERRS.clear(); t = time.time()
    wl = m.winget_installed(); wu = m.winget_outdated()
    print(f"winget list: {len(wl)} шт., upgrade: {len(wu)} шт. ({time.time() - t:.1f} c); ошибки: {m.ERRS}")
    print("пример list:", wl[:2]); print("пример upgrade:", wu[:2])
    print("--- сырой вывод winget list (начало):"); print("\n".join((m.RAW.get("winget list") or "").splitlines()[:8]))
    print("--- сырой вывод winget upgrade (начало):"); print("\n".join((m.RAW.get("winget upgrade") or "").splitlines()[:8]))
if m.find_choco():
    ci, co = m.choco_installed(), m.choco_outdated()
    print(f"choco: установлено {len(ci)}, обновлений {len(co)}; пример: {ci[:2]}")
logs = []
items = m.scan({"winget": True, "msstore": True, "choco": True, "scoop": True}, logs.append)
print("полная проверка программ:", len(items), "из них с обновлением:", sum(1 for i in items if i["status"] == "update"),
      "| известны менеджеру:", sum(1 for i in items if i["id"]))
print("\n".join(logs))
print("SMOKE OK")
