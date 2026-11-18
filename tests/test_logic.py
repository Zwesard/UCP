import sys; sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), '..'))
import update_checker as m

WG_UP = """Имя                 Код              Версия   Доступно  Источник
-------------------------------------------------------------------
Mozilla Firefox     Mozilla.Firefox  130.0    131.0     winget
7-Zip 23.01 (x64)   7zip.7zip        23.01    24.08     winget
Telegram            9NZTWSQNTD0S     4.0      5.0       msstore
3 обновлений доступно.
"""
WG_LS = """Имя                 Код              Версия   Источник
-----------------------------------------------------------
Mozilla Firefox     Mozilla.Firefox  130.0    winget
Notepad++           Notepad++.Notepad++ 8.6   winget
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

def fake(cmd, timeout=240):
    j=" ".join(cmd)
    if cmd[:2]==["winget","upgrade"]: return WG_UP
    if cmd[:2]==["winget","list"]: return WG_LS
    if "choco" in cmd and "outdated" in cmd: return CH_OUT
    if "choco" in cmd and "list" in cmd: return CH_LS
    if "scoop status" in j: return SCOOP_ST
    if "scoop list" in j: return SCOOP_LS
    return ""
m.run_text=fake
m.have_winget=m.have_choco=m.have_scoop=lambda: True
m.read_installed=lambda: {
 "Mozilla Firefox (x64 ru)":{"version":"130.0","publisher":"Mozilla","location":r"C:\Program Files\Mozilla Firefox"},
 "7-Zip 23.01 (x64)":{"version":"23.01","publisher":"Igor","location":""},
 "Notepad++ (64-bit)":{"version":"8.6","publisher":"","location":""},
 "Google Chrome":{"version":"120.0","publisher":"Google","location":""},
 "Random Tool":{"version":"1","publisher":"","location":""},
}
items=m.scan({"winget":True,"msstore":True,"choco":True,"scoop":True})
for i in items: print(i["name"],"|",i["version"],"->",i["available"],"|",i["source"],i["id"],i["status"])
st={i["name"]:(i["status"],i["source"]) for i in items}
assert st["Mozilla Firefox (x64 ru)"]==("update","winget")
assert st["7-Zip 23.01 (x64)"]==("update","winget")
assert st["Notepad++ (64-bit)"]==("ok","winget")
assert st["Google Chrome"]==("update","choco")
assert st["Random Tool"][0]=="unknown"
assert st["git"]==("update","scoop") and st["jq"][0]=="ok"
assert st["Telegram"]==("update","msstore")
# отключение источника
it2=m.scan({"winget":True,"msstore":False,"choco":False,"scoop":False})
assert all(i["source"] not in("msstore","choco","scoop") for i in it2); print("sources filter ok")

print("LOGIC OK")
