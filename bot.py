import logging
import asyncio
import sqlite3
import os
from datetime import date
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    Message, CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ===================== CONFIG =====================
BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8665030198:AAEBbRSpUzM7sWonzWfPDJy4vd7oAtzCgfI")
ADMIN_IDS  = list(map(int, os.getenv("ADMIN_IDS", "8223476380").split(",")))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://your-app.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== DATABASE =====================
DB = "kino_bot.db"

def get_db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = get_db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
            joined_at TEXT DEFAULT (date('now')));
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY, name TEXT,
            added_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE, channel_name TEXT, channel_url TEXT);
        CREATE TABLE IF NOT EXISTS movies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE, name TEXT, description TEXT,
            photo_id TEXT, video_id TEXT,
            protect_content INTEGER DEFAULT 1,
            uploaded_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, movie_code TEXT,
            requested_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    c.commit(); c.close()

# --- users ---
def db_add_user(uid, name, uname):
    c = get_db()
    c.execute("INSERT OR IGNORE INTO users(user_id,name,username,joined_at) VALUES(?,?,?,date('now'))", (uid,name,uname))
    c.commit(); c.close()

# --- admins ---
def db_is_admin(uid):
    if uid in ADMIN_IDS: return True
    c = get_db(); r = c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone(); c.close(); return r is not None

def db_get_admins():
    c = get_db(); r = [dict(x) for x in c.execute("SELECT * FROM admins").fetchall()]; c.close(); return r

def db_add_admin(uid, name):
    c = get_db(); c.execute("INSERT OR IGNORE INTO admins(user_id,name) VALUES(?,?)", (uid,name)); c.commit(); c.close()

def db_del_admin(uid):
    c = get_db(); c.execute("DELETE FROM admins WHERE user_id=?", (uid,)); c.commit(); c.close()

# --- channels ---
def db_get_channels():
    c = get_db(); r = [dict(x) for x in c.execute("SELECT * FROM channels").fetchall()]; c.close(); return r

def db_add_channel(cid, cname, curl):
    c = get_db(); c.execute("INSERT OR IGNORE INTO channels(channel_id,channel_name,channel_url) VALUES(?,?,?)", (cid,cname,curl)); c.commit(); c.close()

def db_del_channel(cid):
    c = get_db(); c.execute("DELETE FROM channels WHERE channel_id=?", (cid,)); c.commit(); c.close()

# --- movies ---
def db_get_movie(code):
    c = get_db(); r = c.execute("SELECT * FROM movies WHERE code=?", (code,)).fetchone(); c.close(); return dict(r) if r else None

def db_get_movies():
    c = get_db(); r = [dict(x) for x in c.execute("SELECT * FROM movies ORDER BY uploaded_at DESC").fetchall()]; c.close(); return r

def db_add_movie(code, name, desc, photo, video):
    c = get_db(); c.execute("INSERT OR REPLACE INTO movies(code,name,description,photo_id,video_id,protect_content) VALUES(?,?,?,?,?,1)", (code,name,desc,photo,video)); c.commit(); c.close()

def db_del_movie(code):
    c = get_db(); c.execute("DELETE FROM movies WHERE code=?", (code,)); c.commit(); c.close()

def db_toggle_protect(code):
    c = get_db(); c.execute("UPDATE movies SET protect_content=1-protect_content WHERE code=?", (code,))
    c.commit(); r = c.execute("SELECT protect_content FROM movies WHERE code=?", (code,)).fetchone(); c.close()
    return r["protect_content"] if r else 1

def db_log_req(uid, code):
    c = get_db(); c.execute("INSERT INTO requests(user_id,movie_code,requested_at) VALUES(?,?,datetime('now'))", (uid,code)); c.commit(); c.close()

# --- settings ---
def db_get(key):
    c = get_db(); r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone(); c.close(); return r["value"] if r else None

def db_set(key, val):
    c = get_db(); c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key,val)); c.commit(); c.close()

# --- stats ---
def db_stats():
    c = get_db()
    today = date.today().isoformat(); month = today[:7]
    s = {
        "total_users":    c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "today_users":    c.execute("SELECT COUNT(*) FROM users WHERE joined_at=?", (today,)).fetchone()[0],
        "month_users":    c.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{month}%",)).fetchone()[0],
        "total_movies":   c.execute("SELECT COUNT(*) FROM movies").fetchone()[0],
        "today_req":      c.execute("SELECT COUNT(*) FROM requests WHERE requested_at LIKE ?", (f"{today}%",)).fetchone()[0],
        "month_req":      c.execute("SELECT COUNT(*) FROM requests WHERE requested_at LIKE ?", (f"{month}%",)).fetchone()[0],
        "top":            [dict(x) for x in c.execute("SELECT movie_code,COUNT(*) cnt FROM requests GROUP BY movie_code ORDER BY cnt DESC LIMIT 5").fetchall()]
    }
    c.close(); return s

# ===================== BOT =====================
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ===================== STATES =====================
class UploadMovie(StatesGroup):
    code = State(); photo = State(); name = State(); desc = State(); video = State()

class NewChannel(StatesGroup):
    waiting = State()

class NewAdmin(StatesGroup):
    waiting = State()

class KinoChannel(StatesGroup):
    waiting = State()
# ===================== KEYBOARDS =====================
def admin_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎬 Kino yuklash"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="📢 Majburiy kanallar"), KeyboardButton(text="🎥 Kinolar ro'yxati")],
        [KeyboardButton(text="⚙️ Admin")]
    ], resize_keyboard=True)

def sub_kb(channels):
    btns = [[InlineKeyboardButton(text=f"📢 {ch['channel_name']}", url=ch['channel_url'])] for ch in channels]
    btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

async def check_sub(uid):
    channels = db_get_channels(); not_sub = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch['channel_id'], uid)
            if m.status in ['left','kicked','banned']: not_sub.append(ch)
        except: not_sub.append(ch)
    return not not_sub, not_sub

# ===================== /start =====================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id; name = msg.from_user.first_name
    db_add_user(uid, name, msg.from_user.username or "")

    if db_is_admin(uid):
        await msg.answer(f"👑 Xush kelibsiz, <b>{name}</b>!\nAdmin panel:", parse_mode="HTML", reply_markup=admin_kb())
        return

    ok, not_sub = await check_sub(uid)
    if not ok:
        await msg.answer("⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:", reply_markup=sub_kb(not_sub))
        return

    await msg.answer(
        f"🎬 Assalomu alaykum, <b>{name}</b>!\n\nBizning botga xush kelibsiz.\n\n🎥 Kino kodini kiriting:",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    ok, not_sub = await check_sub(cb.from_user.id)
    if not ok:
        await cb.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=sub_kb(not_sub)); return
    await cb.message.delete()
    name = cb.from_user.first_name
    await cb.message.answer(
        f"✅ Assalomu alaykum, <b>{name}</b>!\n\nBizning botga xush kelibsiz.\n\n🎥 Kino kodini kiriting:",
        parse_mode="HTML"
    )

# ===================== STATISTIKA =====================
@dp.message(F.text == "📊 Statistika")
async def stats_handler(msg: Message):
    if not db_is_admin(msg.from_user.id): return
    s = db_stats()
    top = "".join(f"  {i+1}. <code>{m['movie_code']}</code> — {m['cnt']} marta\n" for i,m in enumerate(s["top"])) or "  Hali so'rov yo'q\n"
    await msg.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 <b>Foydalanuvchilar:</b>\n"
        f"  • Jami: <b>{s['total_users']}</b>\n"
        f"  • Bugun: <b>{s['today_users']}</b>\n"
        f"  • Bu oy: <b>{s['month_users']}</b>\n\n"
        f"🎬 <b>Kinolar:</b> <b>{s['total_movies']}</b>\n\n"
        f"📥 <b>So'rovlar:</b>\n"
        f"  • Bugun: <b>{s['today_req']}</b>\n"
        f"  • Bu oy: <b>{s['month_req']}</b>\n\n"
        f"🔝 <b>Top 5 kino:</b>\n{top}",
        parse_mode="HTML"
    )

# ===================== MAJBURIY KANALLAR =====================
def channels_kb():
    chs = db_get_channels()
    btns = [[InlineKeyboardButton(text=f"📢 {ch['channel_name']}", callback_data=f"ch:{ch['channel_id']}")] for ch in chs]
    btns.append([InlineKeyboardButton(text="➕ Yangi kanal qo'shish", callback_data="ch_add")])
    return InlineKeyboardMarkup(inline_keyboard=btns), bool(chs)

@dp.message(F.text == "📢 Majburiy kanallar")
async def channels_handler(msg: Message):
    if not db_is_admin(msg.from_user.id): return
    kb, has = channels_kb()
    await msg.answer("📢 <b>Majburiy kanallar:</b>" if has else "📢 Hozircha kanal yo'q.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("ch:"))
async def cb_ch_info(cb: CallbackQuery):
    cid = cb.data[3:]
    chs = db_get_channels(); ch = next((c for c in chs if c['channel_id']==cid), None)
    if not ch: await cb.answer("Topilmadi!"); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Kanalni uzish", callback_data=f"ch_del:{cid}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="ch_back")]
    ])
    await cb.message.edit_text(f"📢 <b>{ch['channel_name']}</b>\nID: <code>{ch['channel_id']}</code>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("ch_del:"))
async def cb_ch_del(cb: CallbackQuery):
    db_del_channel(cb.data[7:]); await cb.answer("✅ Kanal o'chirildi!")
    kb, has = channels_kb()
    await cb.message.edit_text("📢 <b>Majburiy kanallar:</b>" if has else "📢 Hozircha kanal yo'q.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "ch_back")
async def cb_ch_back(cb: CallbackQuery):
    kb, has = channels_kb()
    await cb.message.edit_text("📢 <b>Majburiy kanallar:</b>" if has else "📢 Hozircha kanal yo'q.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "ch_add")
async def cb_ch_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewChannel.waiting)
    await cb.message.answer(
        "📢 Yangi kanal ma'lumotlarini yuboring:\n\n"
        "<code>kanal_id|Kanal nomi|https://t.me/kanal</code>\n\n"
        "Masalan:\n<code>-1001234567890|Kino Kanal|https://t.me/kinokanal</code>",
        parse_mode="HTML"
    ); await cb.answer()

@dp.message(NewChannel.waiting)
async def msg_ch_add(msg: Message, state: FSMContext):
    if not db_is_admin(msg.from_user.id): return
    try:
        p = msg.text.strip().split("|")
        db_add_channel(p[0].strip(), p[1].strip(), p[2].strip())
        await state.clear()
        await msg.answer(f"✅ <b>{p[1].strip()}</b> qo'shildi!", parse_mode="HTML", reply_markup=admin_kb())
    except:
        await msg.answer("❌ Format noto'g'ri!\n<code>kanal_id|Kanal nomi|https://t.me/kanal</code>", parse_mode="HTML")
  # ===================== KINOLAR RO'YXATI =====================
def movies_kb():
    mvs = db_get_movies()
    btns = []
    for m in mvs:
        ico = "🔒" if m['protect_content'] else "🔓"
        btns.append([InlineKeyboardButton(text=f"{ico} {m['name']} | #{m['code']}", callback_data=f"mv:{m['code']}")])
    return InlineKeyboardMarkup(inline_keyboard=btns), bool(mvs)

@dp.message(F.text == "🎥 Kinolar ro'yxati")
async def movies_handler(msg: Message):
    if not db_is_admin(msg.from_user.id): return
    kb, has = movies_kb()
    if not has: await msg.answer("🎥 Hozircha kinolar yo'q."); return
    await msg.answer("🎥 <b>Kinolar ro'yxati:</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("mv:"))
async def cb_mv_info(cb: CallbackQuery):
    code = cb.data[3:]; movie = db_get_movie(code)
    if not movie: await cb.answer("Topilmadi!"); return
    ps = "🔒 Yoniq" if movie['protect_content'] else "🔓 O'chiq"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Kinoni o'chirish", callback_data=f"mv_del:{code}")],
        [InlineKeyboardButton(text=f"Content himoya: {ps}", callback_data=f"mv_prot:{code}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="mv_back")]
    ])
    await cb.message.edit_text(
        f"🎬 <b>{movie['name']}</b>\n🔑 Kod: <code>{movie['code']}</code>\n📝 {movie['description']}\n🛡 Himoya: {ps}",
        parse_mode="HTML", reply_markup=kb
    )

@dp.callback_query(F.data.startswith("mv_del:"))
async def cb_mv_del(cb: CallbackQuery):
    db_del_movie(cb.data[7:]); await cb.answer("✅ Kino o'chirildi!")
    kb, has = movies_kb()
    if not has: await cb.message.edit_text("🎥 Hozircha kinolar yo'q."); return
    await cb.message.edit_text("🎥 <b>Kinolar ro'yxati:</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("mv_prot:"))
async def cb_mv_prot(cb: CallbackQuery):
    code = cb.data[8:]; new = db_toggle_protect(code)
    await cb.answer(f"Content himoya: {'🔒 Yoniq' if new else '🔓 O'chiq'}")
    movie = db_get_movie(code)
    if not movie: return
    ps = "🔒 Yoniq" if movie['protect_content'] else "🔓 O'chiq"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Kinoni o'chirish", callback_data=f"mv_del:{code}")],
        [InlineKeyboardButton(text=f"Content himoya: {ps}", callback_data=f"mv_prot:{code}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="mv_back")]
    ])
    await cb.message.edit_text(
        f"🎬 <b>{movie['name']}</b>\n🔑 Kod: <code>{movie['code']}</code>\n📝 {movie['description']}\n🛡 Himoya: {ps}",
        parse_mode="HTML", reply_markup=kb
    )

@dp.callback_query(F.data == "mv_back")
async def cb_mv_back(cb: CallbackQuery):
    kb, has = movies_kb()
    if not has: await cb.message.edit_text("🎥 Hozircha kinolar yo'q."); return
    await cb.message.edit_text("🎥 <b>Kinolar ro'yxati:</b>", parse_mode="HTML", reply_markup=kb)

# ===================== ADMIN PANEL =====================
@dp.message(F.text == "⚙️ Admin")
async def admin_panel(msg: Message):
    if not db_is_admin(msg.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Kino kanal", callback_data="kino_ch")],
        [InlineKeyboardButton(text="👑 Adminlar", callback_data="admins")]
    ])
    await msg.answer("⚙️ <b>Admin sozlamalari:</b>", parse_mode="HTML", reply_markup=kb)

# Kino kanal
@dp.callback_query(F.data == "kino_ch")
async def cb_kino_ch(cb: CallbackQuery, state: FSMContext):
    cur = db_get("kino_channel")
    text = f"📣 Hozirgi kanal: <code>{cur}</code>\n\n" if cur else "📣 Kanal o'rnatilmagan.\n\n"
    await state.set_state(KinoChannel.waiting)
    await cb.message.answer(text + "Yangi kanal ID yuboring:\n<i>Masalan: -1001234567890</i>", parse_mode="HTML")
    await cb.answer()

@dp.message(KinoChannel.waiting)
async def msg_kino_ch(msg: Message, state: FSMContext):
    if not db_is_admin(msg.from_user.id): return
    db_set("kino_channel", msg.text.strip())
    await state.clear()
    await msg.answer(f"✅ Kino kanal: <code>{msg.text.strip()}</code>", parse_mode="HTML", reply_markup=admin_kb())

# Adminlar
def admins_kb():
    admins = db_get_admins()
    btns = [[InlineKeyboardButton(text=f"👤 {a['name']} ({a['user_id']})", callback_data=f"adm:{a['user_id']}")] for a in admins]
    btns.append([InlineKeyboardButton(text="➕ Yangi admin qo'shish", callback_data="adm_add")])
    return InlineKeyboardMarkup(inline_keyboard=btns), bool(admins)

@dp.callback_query(F.data == "admins")
async def cb_admins(cb: CallbackQuery):
    kb, has = admins_kb()
    await cb.message.edit_text("👑 <b>Adminlar:</b>" if has else "👑 Qo'shimcha adminlar yo'q.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm:"))
async def cb_adm_info(cb: CallbackQuery):
    uid = int(cb.data[4:])
    admins = db_get_admins(); a = next((x for x in admins if x['user_id']==uid), None)
    if not a: await cb.answer("Topilmadi!"); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Adminni o'chirish", callback_data=f"adm_del:{uid}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admins")]
    ])
    await cb.message.edit_text(f"👤 <b>{a['name']}</b>\nID: <code>{a['user_id']}</code>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_del:"))
async def cb_adm_del(cb: CallbackQuery):
    uid = int(cb.data[8:]); db_del_admin(uid); await cb.answer("✅ Admin o'chirildi!")
    kb, has = admins_kb()
    await cb.message.edit_text("👑 <b>Adminlar:</b>" if has else "👑 Qo'shimcha adminlar yo'q.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewAdmin.waiting)
    await cb.message.answer("👤 Yangi admin:\n<code>user_id|Ism</code>\n\nMasalan:\n<code>123456789|Ali</code>", parse_mode="HTML")
    await cb.answer()

@dp.message(NewAdmin.waiting)
async def msg_adm_add(msg: Message, state: FSMContext):
    if not db_is_admin(msg.from_user.id): return
    try:
        p = msg.text.strip().split("|")
        db_add_admin(int(p[0].strip()), p[1].strip())
        await state.clear()
        await msg.answer(f"✅ <b>{p[1].strip()}</b> admin bo'ldi!", parse_mode="HTML", reply_markup=admin_kb())
    except:
        await msg.answer("❌ Format noto'g'ri!\n<code>user_id|Ism</code>", parse_mode="HTML")

# ===================== KINO YUKLASH =====================
@dp.message(F.text == "🎬 Kino yuklash")
async def upload_start(msg: Message, state: FSMContext):
    if not db_is_admin(msg.from_user.id): return
    await state.set_state(UploadMovie.code)
    await msg.answer("🎬 Kino uchun <b>kod</b> kiriting:\n<i>Masalan: 001, AVENGERS</i>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

@dp.message(UploadMovie.code)
async def upload_code(msg: Message, state: FSMContext):
    code = msg.text.strip()
    if db_get_movie(code): await msg.answer(f"❌ <code>{code}</code> kodi band! Boshqa kod:", parse_mode="HTML"); return
    await state.update_data(code=code); await state.set_state(UploadMovie.photo)
    await msg.answer("🖼 Kino <b>rasmini</b> yuboring:", parse_mode="HTML")

@dp.message(UploadMovie.photo, F.photo)
async def upload_photo(msg: Message, state: FSMContext):
    await state.update_data(photo_id=msg.photo[-1].file_id); await state.set_state(UploadMovie.name)
    await msg.answer("📝 Kino <b>nomini</b> kiriting:", parse_mode="HTML")

@dp.message(UploadMovie.photo)
async def upload_photo_err(msg: Message): await msg.answer("❌ Rasm yuboring!")

@dp.message(UploadMovie.name)
async def upload_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip()); await state.set_state(UploadMovie.desc)
    await msg.answer("📄 Kino <b>tavsifini</b> kiriting:", parse_mode="HTML")

@dp.message(UploadMovie.desc)
async def upload_desc(msg: Message, state: FSMContext):
    await state.update_data(description=msg.text.strip()); await state.set_state(UploadMovie.video)
    await msg.answer("🎥 Kino <b>videosini</b> yuboring:", parse_mode="HTML")

@dp.message(UploadMovie.video, F.video)
async def upload_video(msg: Message, state: FSMContext):
    data = await state.get_data(); await state.clear()
    code, name, desc = data['code'], data['name'], data['description']
    photo_id, video_id = data['photo_id'], msg.video.file_id
    db_add_movie(code, name, desc, photo_id, video_id)
  # Kino kanalga e'lon
    kino_ch = db_get("kino_channel")
    if kino_ch:
        try:
            me = await bot.get_me()
            caption = f"🎬 <b>{name}</b>\n\n📝 {desc}\n\n🔑 Kod: <code>{code}</code>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎬 Kinoni ko'rish", url=f"https://t.me/{me.username}?start={code}")
            ]])
            await bot.send_photo(chat_id=kino_ch, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=kb, protect_content=True)
        except Exception as e:
            logger.error(f"Kanalga yuborishda xato: {e}")

    await msg.answer(
        f"✅ Kino yuklandi!\n\n🎬 <b>{name}</b>\n🔑 Kod: <code>{code}</code>",
        parse_mode="HTML", reply_markup=admin_kb()
    )

@dp.message(UploadMovie.video)
async def upload_video_err(msg: Message): await msg.answer("❌ Video yuboring!")

# ===================== FOYDALANUVCHI - KINO IZLASH =====================
@dp.message()
async def user_search(msg: Message):
    uid = msg.from_user.id
    if db_is_admin(uid):
        await msg.answer("❓ Noma'lum buyruq.", reply_markup=admin_kb()); return

    ok, not_sub = await check_sub(uid)
    if not ok:
        await msg.answer("⚠️ Obuna bo'ling:", reply_markup=sub_kb(not_sub)); return

    movie = db_get_movie(msg.text.strip())
    if not movie:
        await msg.answer("❌ Bunday kino topilmadi.\n\n🎥 Kino kodini kiriting:"); return

    db_log_req(uid, movie['code'])
    p = bool(movie['protect_content'])
    caption = f"🎬 <b>{movie['name']}</b>\n\n📝 {movie['description']}\n\n🔑 Kod: <code>{movie['code']}</code>"
    await bot.send_photo(uid, photo=movie['photo_id'], caption=caption, parse_mode="HTML", protect_content=p)
    await bot.send_video(uid, video=movie['video_id'], caption=f"🎬 {movie['name']}", protect_content=p)

# ===================== WEBHOOK =====================
async def on_startup(app):
    init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.session.close()

def main():
    init_db()
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
