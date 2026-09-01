
import asyncio, csv, io, logging, os, sqlite3
from datetime import datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH","warranty.db")
OVERDUE_DAYS = int(os.getenv("OVERDUE_DAYS","14"))
NP_API_KEY = os.getenv("NP_API_KEY","")
NP_INTERVAL = int(os.getenv("NP_INTERVAL_MINUTES","30"))

STATUS = {
 "received":"🟡 Прийнято від клієнта","sent":"🔵 Відправлено в сервіс",
 "accepted":"🛠 Прийнято сервісом","diagnostics":"🔧 Діагностика",
 "parts":"⏳ Очікування запчастини","repaired":"✅ Відремонтовано",
 "returning":"📦 Повертається з сервісу","ready":"🟢 Готово до видачі",
 "rejected":"❌ Відмова в гарантії","returned":"🔴 Повернено клієнту"
}
FINAL={"ready","rejected","returned"}
router=Router()

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def db():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def init_db():
 c=db()
 c.executescript("""
 CREATE TABLE IF NOT EXISTS users(telegram_id INTEGER PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'worker',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,phone TEXT,address TEXT,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS repairs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT UNIQUE,imei TEXT,model TEXT NOT NULL,client_name TEXT,
 client_phone TEXT,client_telegram_id INTEGER,issue TEXT,service_id INTEGER,tracking TEXT,tracking_status TEXT,
 status TEXT NOT NULL DEFAULT 'received',notes TEXT,photo_file_id TEXT,created_by INTEGER,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,sent_at TEXT,closed_at TEXT);
 CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY AUTOINCREMENT,repair_id INTEGER NOT NULL,old_status TEXT,new_status TEXT NOT NULL,comment TEXT,changed_by INTEGER,changed_at TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_imei ON repairs(imei); CREATE INDEX IF NOT EXISTS idx_ttn ON repairs(tracking);
 """)
 for a in ADMIN_IDS:
  c.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?,?)",(a,"Адміністратор","admin",1,now()))
  c.execute("UPDATE users SET role='admin',active=1 WHERE telegram_id=?",(a,))
 c.commit(); c.close()

def allowed(uid):
 c=db(); r=c.execute("SELECT active FROM users WHERE telegram_id=?",(uid,)).fetchone(); c.close()
 return bool(r and r["active"])
def admin(uid):
 c=db(); r=c.execute("SELECT role FROM users WHERE telegram_id=? AND active=1",(uid,)).fetchone(); c.close()
 return bool(r and r["role"]=="admin")

def main_menu(is_admin=False):
 rows=[
  [InlineKeyboardButton(text="➕ Новий ремонт",callback_data="add")],
  [InlineKeyboardButton(text="🔎 Пошук",callback_data="search"),InlineKeyboardButton(text="📋 В ремонті",callback_data="active")],
  [InlineKeyboardButton(text="⏰ Прострочені",callback_data="overdue"),InlineKeyboardButton(text="📊 Статистика",callback_data="stats")],
  [InlineKeyboardButton(text="📤 Експорт CSV",callback_data="export")]
 ]
 if is_admin: rows.append([InlineKeyboardButton(text="👥 Працівники",callback_data="users"),InlineKeyboardButton(text="🏢 Сервіси",callback_data="services")])
 return InlineKeyboardMarkup(inline_keyboard=rows)

def service_name(c, sid):
 if not sid:return "—"
 r=c.execute("SELECT name FROM services WHERE id=?",(sid,)).fetchone()
 return r["name"] if r else "—"

def fmt(r):
 return "\n".join([
 f"<b>📱 {r['code']}</b>",f"<b>Модель:</b> {r['model']}",f"<b>IMEI:</b> {r['imei'] or '—'}",
 f"<b>Клієнт:</b> {r['client_name'] or '—'}",f"<b>Телефон:</b> {r['client_phone'] or '—'}",
 f"<b>Несправність:</b> {r['issue'] or '—'}",f"<b>Сервіс:</b> {r['service_name'] if 'service_name' in r.keys() else '—'}",
 f"<b>ТТН:</b> {r['tracking'] or '—'}",f"<b>Нова пошта:</b> {r['tracking_status'] or '—'}",
 f"<b>Статус:</b> {STATUS.get(r['status'],r['status'])}",f"<b>Створено:</b> {r['created_at']}",
 f"<b>Оновлено:</b> {r['updated_at']}",f"<b>Примітка:</b> {r['notes'] or '—'}"
 ])

def status_kb(rid):
 keys=list(STATUS)
 return InlineKeyboardMarkup(inline_keyboard=[
  [InlineKeyboardButton(text=STATUS[k],callback_data=f"st:{rid}:{k}") for k in keys[i:i+2]]
  for i in range(0,len(keys),2)
 ]+[[InlineKeyboardButton(text="⬅️ Меню",callback_data="menu")]])

class Add(StatesGroup):
 imei=State(); model=State(); client=State(); phone=State(); client_tg=State()
 issue=State(); service=State(); ttn=State(); notes=State(); photo=State()
class Search(StatesGroup): q=State()
class Comment(StatesGroup): text=State()
class User(StatesGroup): id=State(); name=State()
class Service(StatesGroup): name=State(); phone=State(); address=State()

async def guard(m):
 if not allowed(m.from_user.id):
  await m.answer("⛔ Немає доступу. Зверніться до адміністратора."); return False
 return True

@router.message(CommandStart())
async def start(m):
 if await guard(m): await m.answer("📱 <b>Гарантійні ремонти</b>\nОберіть дію:",reply_markup=main_menu(admin(m.from_user.id)))

@router.callback_query(F.data=="menu")
async def menu(q,state:FSMContext):
 await state.clear(); await q.message.answer("📱 <b>Гарантійні ремонти</b>",reply_markup=main_menu(admin(q.from_user.id))); await q.answer()

@router.callback_query(F.data=="add")
async def add(q,state:FSMContext):
 if not allowed(q.from_user.id): return await q.answer("Немає доступу",show_alert=True)
 await state.set_state(Add.imei); await q.message.answer("IMEI (або —):"); await q.answer()

@router.message(Command("add"))
async def addcmd(m,state):
 if await guard(m): await state.set_state(Add.imei); await m.answer("IMEI (або —):")

@router.message(Add.imei)
async def a1(m,s): await s.update_data(imei=None if m.text.strip()=="—" else m.text.strip()); await s.set_state(Add.model); await m.answer("Модель телефону:")
@router.message(Add.model)
async def a2(m,s): await s.update_data(model=m.text.strip()); await s.set_state(Add.client); await m.answer("ПІБ клієнта (або —):")
@router.message(Add.client)
async def a3(m,s): await s.update_data(client=None if m.text.strip()=="—" else m.text.strip()); await s.set_state(Add.phone); await m.answer("Телефон клієнта (або —):")
@router.message(Add.phone)
async def a4(m,s): await s.update_data(phone=None if m.text.strip()=="—" else m.text.strip()); await s.set_state(Add.client_tg); await m.answer("Telegram ID клієнта для сповіщень (або —):")
@router.message(Add.client_tg)
async def a5(m,s):
 v=None if m.text.strip()=="—" else int(m.text.strip()) if m.text.strip().isdigit() else None
 await s.update_data(client_tg=v); await s.set_state(Add.issue); await m.answer("Несправність:")
@router.message(Add.issue)
async def a6(m,s): await s.update_data(issue=m.text.strip()); await s.set_state(Add.service); await m.answer("ID сервісного центру (див. /services) або —:")
@router.message(Add.service)
async def a7(m,s): await s.update_data(service=None if m.text.strip()=="—" else int(m.text.strip())); await s.set_state(Add.ttn); await m.answer("ТТН Нової пошти (або —):")
@router.message(Add.ttn)
async def a8(m,s): await s.update_data(ttn=None if m.text.strip()=="—" else m.text.strip()); await s.set_state(Add.notes); await m.answer("Примітка (або —):")
@router.message(Add.notes)
async def a9(m,s): await s.update_data(notes=None if m.text.strip()=="—" else m.text.strip()); await s.set_state(Add.photo); await m.answer("Фото телефону/акта або напишіть —:")
@router.message(Add.photo, F.photo)
async def a10photo(m,s): await s.update_data(photo=m.photo[-1].file_id); await save_repair(m,s)
@router.message(Add.photo)
async def a10(m,s): await s.update_data(photo=None); await save_repair(m,s)

async def save_repair(m,s):
 d=await s.get_data(); c=db(); cur=c.execute("""INSERT INTO repairs
 (model,imei,client_name,client_phone,client_telegram_id,issue,service_id,tracking,status,notes,photo_file_id,created_by,created_at,updated_at)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(d["model"],d["imei"],d["client"],d["phone"],d["client_tg"],d["issue"],d["service"],d["ttn"],"received",d["notes"],d.get("photo"),m.from_user.id,now(),now()))
 rid=cur.lastrowid; code=f"GAR-{datetime.now().year}-{rid:05d}"
 c.execute("UPDATE repairs SET code=? WHERE id=?",(code,rid)); c.execute("INSERT INTO status_history(repair_id,new_status,comment,changed_by,changed_at) VALUES(?,?,?,?,?)",(rid,"received","Створено заявку",m.from_user.id,now()))
 c.commit(); c.close(); await s.clear()
 await m.answer(f"✅ Створено <b>{code}</b>",reply_markup=status_kb(rid))

def query_rows(where="",params=()):
 c=db(); rows=c.execute(f"""SELECT r.*,s.name service_name FROM repairs r LEFT JOIN services s ON s.id=r.service_id {where} ORDER BY r.id DESC""",params).fetchall(); c.close(); return rows

@router.callback_query(F.data=="search")
async def search(q,s):
 if not allowed(q.from_user.id): return await q.answer("Немає доступу",show_alert=True)
 await s.set_state(Search.q); await q.message.answer("🔎 IMEI / ТТН / GAR / ПІБ / модель:"); await q.answer()
@router.message(Command("search"))
async def searchcmd(m,s):
 if await guard(m): await s.set_state(Search.q); await m.answer("🔎 IMEI / ТТН / GAR / ПІБ / модель:")
@router.message(Search.q)
async def dosearch(m,s):
 q=m.text.strip(); rows=query_rows("WHERE r.code LIKE ? OR r.imei LIKE ? OR r.tracking LIKE ? OR r.client_name LIKE ? OR r.model LIKE ?",tuple("%"+q+"%" for _ in range(5))); await s.clear()
 if not rows: return await m.answer("❌ Нічого не знайдено.",reply_markup=main_menu(admin(m.from_user.id)))
 for r in rows[:20]: await m.answer(fmt(r),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔧 Статус",callback_data=f"choose:{r['id']}")]]))

@router.callback_query(F.data=="active")
async def active(q):
 if not allowed(q.from_user.id): return await q.answer("Немає доступу",show_alert=True)
 rows=query_rows("WHERE r.status NOT IN ('ready','rejected','returned')")
 await q.message.answer(f"📋 В ремонті: <b>{len(rows)}</b>")
 for r in rows[:30]: await q.message.answer(fmt(r),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔧 Статус",callback_data=f"choose:{r['id']}")]]))
 await q.answer()

@router.callback_query(F.data=="overdue")
async def overdue(q):
 if not allowed(q.from_user.id): return await q.answer("Немає доступу",show_alert=True)
 cutoff=(datetime.now()-timedelta(days=OVERDUE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
 rows=query_rows("WHERE r.updated_at < ? AND r.status NOT IN ('ready','rejected','returned')",(cutoff,))
 await q.message.answer(f"⏰ Прострочено: <b>{len(rows)}</b>")
 for r in rows[:30]: await q.message.answer(fmt(r))
 await q.answer()

@router.callback_query(F.data=="stats")
async def stats(q):
 if not allowed(q.from_user.id): return await q.answer("Немає доступу",show_alert=True)
 c=db(); total=c.execute("SELECT COUNT(*) c FROM repairs").fetchone()["c"]; active=c.execute("SELECT COUNT(*) c FROM repairs WHERE status NOT IN ('ready','rejected','returned')").fetchone()["c"]; rows=c.execute("SELECT status,COUNT(*) c FROM repairs GROUP BY status").fetchall(); c.close()
 text=[f"📊 <b>Всього:</b> {total}",f"<b>В роботі:</b> {active}",""]+[f"{STATUS.get(r['status'],r['status'])}: {r['c']}" for r in rows]
 await q.message.answer("\n".join(text)); await q.answer()

@router.callback_query(F.data.startswith("choose:"))
async def choose(q):
 rid=int(q.data.split(":")[1]); await q.message.answer("Оберіть статус:",reply_markup=status_kb(rid)); await q.answer()

@router.callback_query(F.data.startswith("st:"))
async def setst(q,s):
 _,rid,new=q.data.split(":"); rid=int(rid); c=db(); r=c.execute("SELECT * FROM repairs WHERE id=?",(rid,)).fetchone()
 if not r: c.close(); return await q.answer("Не знайдено",show_alert=True)
 await s.update_data(repair_id=rid,new_status=new,old_status=r["status"]); await s.set_state(Comment.text); c.close()
 await q.message.answer("Коментар до зміни статусу (або —):"); await q.answer()

@router.message(Comment.text)
async def comment(m,s):
 d=await s.get_data(); rid=d["repair_id"]; new=d["new_status"]; c=db()
 c.execute("""UPDATE repairs SET status=?,updated_at=?,sent_at=CASE WHEN ?='sent' AND sent_at IS NULL THEN ? ELSE sent_at END,
 closed_at=CASE WHEN ? IN ('ready','rejected','returned') THEN ? ELSE closed_at END WHERE id=?""",(new,now(),new,now(),new,now(),rid))
 c.execute("INSERT INTO status_history(repair_id,old_status,new_status,comment,changed_by,changed_at) VALUES(?,?,?,?,?,?)",(rid,d["old_status"],new,None if m.text=="—" else m.text,m.from_user.id,now()))
 r=c.execute("SELECT r.*,s.name service_name FROM repairs r LEFT JOIN services s ON s.id=r.service_id WHERE r.id=?",(rid,)).fetchone(); c.commit(); c.close(); await s.clear()
 await m.answer("✅ Статус змінено.\n\n"+fmt(r))
 if r["client_telegram_id"]:
  try: await m.bot.send_message(r["client_telegram_id"],f"📱 Зміна статусу вашого гарантійного ремонту <b>{r['code']}</b>:\n{STATUS[new]}")
  except: pass

@router.callback_query(F.data=="export")
async def export(q):
 if not admin(q.from_user.id): return await q.answer("Лише адмін",show_alert=True)
 rows=query_rows(); out=io.StringIO(); w=csv.writer(out); w.writerow(["GAR","IMEI","Модель","Клієнт","Телефон","Несправність","Сервіс","ТТН","ТТН статус","Статус","Створено","Оновлено","Примітка"])
 for r in rows: w.writerow([r["code"],r["imei"],r["model"],r["client_name"],r["client_phone"],r["issue"],r["service_name"],r["tracking"],r["tracking_status"],STATUS.get(r["status"],r["status"]),r["created_at"],r["updated_at"],r["notes"]])
 path="/tmp/warranty_export.csv"; open(path,"w",encoding="utf-8-sig",newline="").write(out.getvalue()); await q.message.answer_document(FSInputFile(path)); await q.answer()

@router.message(Command("services"))
async def services_cmd(m):
 if not admin(m.from_user.id): return await m.answer("⛔ Лише адмін.")
 c=db(); rows=c.execute("SELECT * FROM services ORDER BY id").fetchall(); c.close()
 text=["🏢 <b>Сервісні центри</b>"]
 for r in rows: text.append(f"<b>{r['id']}</b> — {r['name']} | {r['phone'] or '—'} | {r['address'] or '—'}")
 text.append("\n/addservice — додати сервіс")
 await m.answer("\n".join(text))

@router.callback_query(F.data=="services")
async def services(q):
 if not admin(q.from_user.id): return await q.answer("Лише адмін",show_alert=True)
 await q.message.answer("🏢 /services — список\n/addservice — додати сервіс"); await q.answer()

@router.message(Command("addservice"))
async def addservice(m,s):
 if not admin(m.from_user.id): return await m.answer("⛔ Лише адмін.")
 await s.set_state(Service.name); await m.answer("Назва сервісного центру:")
@router.message(Service.name)
async def sn(m,s): await s.update_data(name=m.text); await s.set_state(Service.phone); await m.answer("Телефон сервісу (або —):")
@router.message(Service.phone)
async def sp(m,s): await s.update_data(phone=None if m.text=="—" else m.text); await s.set_state(Service.address); await m.answer("Адреса (або —):")
@router.message(Service.address)
async def sa(m,s):
 d=await s.get_data(); c=db(); c.execute("INSERT OR IGNORE INTO services(name,phone,address,created_at) VALUES(?,?,?,?)",(d["name"],d["phone"],None if m.text=="—" else m.text,now())); c.commit(); c.close(); await s.clear(); await m.answer("✅ Сервіс додано.")

@router.message(Command("users"))
async def users(m):
 if not admin(m.from_user.id): return await m.answer("⛔ Лише адмін.")
 c=db(); rows=c.execute("SELECT * FROM users ORDER BY role DESC,name").fetchall(); c.close()
 await m.answer("\n".join(["👥 <b>Працівники</b>"]+[f"{'👑' if r['role']=='admin' else '👤'} {r['name']} — <code>{r['telegram_id']}</code> — {'активний' if r['active'] else 'заблокований'}" for r in rows])+ "\n\n/adduser ID — додати працівника")
@router.message(Command("adduser"))
async def adduser(m):
 if not admin(m.from_user.id): return await m.answer("⛔ Лише адмін.")
 p=m.text.split(maxsplit=2)
 if len(p)<3 or not p[1].isdigit(): return await m.answer("Формат: /adduser TELEGRAM_ID Ім'я")
 c=db(); c.execute("INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name,active=1",(int(p[1]),p[2],"worker",1,now())); c.commit(); c.close(); await m.answer("✅ Працівника додано.")

async def np_track():
 if not NP_API_KEY: return
 while True:
  try:
   c=db(); rows=c.execute("SELECT id,tracking FROM repairs WHERE tracking IS NOT NULL AND tracking!='' AND status NOT IN ('ready','returned')").fetchall(); c.close()
   if rows:
    async with httpx.AsyncClient(timeout=20) as x:
     payload={"apiKey":NP_API_KEY,"modelName":"TrackingDocument","calledMethod":"getStatusDocuments","methodProperties":{"Documents":[{"DocumentNumber":r["tracking"],"Phone":""} for r in rows]}}
     res=(await x.post("https://api.novaposhta.ua/v2.0/json/",json=payload)).json()
    data=res.get("data",[])
    c=db()
    for item in data:
     num=item.get("Number"); status=item.get("Status")
     if num and status:
      c.execute("UPDATE repairs SET tracking_status=?,updated_at=updated_at WHERE tracking=?",(status,num))
    c.commit(); c.close()
  except Exception: logging.exception("Nova Poshta tracking error")
  await asyncio.sleep(NP_INTERVAL*60)

async def overdue_notify(bot):
 while True:
  try:
   cutoff=(datetime.now()-timedelta(days=OVERDUE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
   rows=query_rows("WHERE r.updated_at < ? AND r.status NOT IN ('ready','rejected','returned')",(cutoff,))
   if rows:
    txt="⏰ <b>Прострочені гарантійні ремонти</b>\n"+"\n".join(f"• {r['code']} — {r['model']}" for r in rows[:15])
    for a in ADMIN_IDS:
     try: await bot.send_message(a,txt)
     except: pass
  except: logging.exception("overdue error")
  await asyncio.sleep(86400)

async def main():
 if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN не задано")
 init_db(); logging.basicConfig(level=logging.INFO)
 bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
 dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
 tasks=[asyncio.create_task(overdue_notify(bot))]
 if NP_API_KEY: tasks.append(asyncio.create_task(np_track()))
 try: await dp.start_polling(bot)
 finally:
  for t in tasks:t.cancel()
  await bot.session.close()

if __name__=="__main__": asyncio.run(main())
