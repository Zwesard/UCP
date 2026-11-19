import sys, os, tempfile, time, copy, threading
if sys.platform == 'win32': print('skip'); sys.exit(0)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import update_checker as m
m.SETTINGS_PATH=os.path.join(tempfile.mkdtemp(),"settings.json")
from tkinter import messagebox
messagebox.showinfo=messagebox.showwarning=lambda *a,**k:None
messagebox.askyesno=lambda *a,**k:True
messagebox.askyesnocancel=lambda *a,**k:True
def mk(n,v,a,i,s,st): return {"name":n,"version":v,"available":a,"publisher":"","id":i,"source":s,"status":st,"location":""}
data=[mk("Zebra","1","","z","winget","ok"),mk("Alpha","1","","a","winget","ok"),
      mk("Mozilla Firefox","130.0","131.0","Mozilla.Firefox","winget","update"),
      mk("Zoom","5","6","Zoom.Zoom","winget","update"), mk("Old","1","","","","unknown")]
m.scan=lambda s,log=None:copy.deepcopy(data)
m.list_processes=lambda:[(111,"firefox.exe",r"C:\Program Files\Mozilla Firefox\firefox.exe")]
m.close_processes=lambda pids,log:None
calls=[]
def fake_stream(cmd,log,cancel,**k):
    calls.append(cmd); log("   Starting package install...")
    if "Mozilla.Firefox" in cmd:
        for d in data:
            if d["id"]=="Mozilla.Firefox": d.update(status="ok",version="131.0",available="")  # «реальная» система обновилась
    if "Zoom.Zoom" in cmd:                       # «зависший» установщик: ждём отмены
        for _ in range(100):
            if cancel.is_set(): return None
            time.sleep(0.1)
    return 0
m.stream_cmd=fake_stream
app=m.App(); app.winfo_toplevel()
def pump(t=1.0):
    e=time.time()+t
    while time.time()<e: app.update(); time.sleep(0.02)
# 1) авто-проверка при запуске (winreg=None на Linux -> запускаем вручную как это делает __init__)
app.start_scan(); pump(1.2)
order=[app.tree.set(k,"name") for k in app.tree.get_children()]
print("order:",order)
assert order[:2]==["Mozilla Firefox","Zoom"], order            # 4) обновления сверху
# 4b) сортировка по клику сбрасывается после новой проверки
app.sort_by("name"); app.sort_by("name"); app.start_scan(); pump(1.2)
assert [app.tree.set(k,"name") for k in app.tree.get_children()][:2]==["Mozilla Firefox","Zoom"]
# 5) журнал растягивается: сдвигаем разделитель
app.update_idletasks(); before=app.pw.sashpos(0); app.pw.sashpos(0,before-120); app.update_idletasks()
assert app.pw.sashpos(0)==before-120; print("sash",before,"->",app.pw.sashpos(0))
# 7) после успешного обновления программа исчезает из списка обновлений
assert app.cnt["update"].cget("text")=="2"
# мгновенное отражение результата (до повторной проверки)
app.q.put(("hist",{"time":"t","name":"Mozilla Firefox","id":"Mozilla.Firefox","source":"winget","action":"update","from":"130.0","to":"131.0","ok":True}))
pump(0.3)
fx=app.item_by_key("mozillafirefox"); assert fx["status"]=="ok" and app.cnt["update"].cget("text")=="1"
for i in app.items: pass
# сквозной сценарий
app.items=copy.deepcopy([dict(d,status="update",version="130.0",available="131.0") if d["id"]=="Mozilla.Firefox" else d for d in data]); app.refresh_table()
app.checked={"mozillafirefox"}; app.update_checked(); pump(2.0)
fx=app.item_by_key("mozillafirefox"); print("firefox after:",fx["status"],fx["version"])
assert fx["status"]=="ok" and fx["version"]=="131.0" and fx["available"]==""
assert app.cnt["update"].cget("text")=="1"
# 6) зависшая установка прерывается кнопкой «Остановить»
pump(1.5)  # дождаться авто-пересканирования после первого задания
app.checked={"zoom"}; app.update_checked(); pump(0.5)
assert app.running_jobs and str(app.btn_stop.cget("state"))=="normal"
app.stop_jobs(); pump(1.2)
assert not app.running_jobs and str(app.btn_stop.cget("state"))=="disabled"
assert app.item_by_key("zoom")["status"]=="update"            # не обновилась -> осталась в списке
print("stop OK; log tail:", app.log.get("end-4l","end").strip().splitlines()[-2:])
# 3) в меню больше нет опции «проверять при запуске», версия 1.2.0
assert m.VERSION=="1.3.0" and "1.3.0" in app.title()
assert "scan_on_start" not in app.cfg and "drivers" in app.cfg
app.on_close(); print("GUI2 OK")
