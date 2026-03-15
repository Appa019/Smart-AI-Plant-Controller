"""
Hoya Pet App — FastAPI backend (Vercel + Supabase edition).
- JWT authentication with email verification
- Plant photo upload + AI identification via GPT-5.2
- Pet configuration (cat/dog + name)
- Async pet image generation via Supabase Edge Functions
- Sensor data API (current, history, stats)
- ESP32 ingest endpoint (no auth required, uses API key)
- Vercel Cron endpoints for scheduled pet generation
"""
import os, json, base64, threading, time, logging, hmac, hashlib, random, io
from PIL import Image
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Query, Header, HTTPException, Depends, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from dotenv import load_dotenv
from jose import jwt, JWTError
from supabase import create_client, Client

# ==================== LOAD .env ====================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "hoya-pet-secret-key-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

SMTP_USER = os.getenv("EMAIL_SMTP", os.getenv("SMTP_USER", ""))
SMTP_PASSWORD = os.getenv("APP_PASSWORD_SMTP", os.getenv("SMTP_PASSWORD", ""))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SITE_URL = os.getenv("SITE_URL", "https://hoya-pet.vercel.app")
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Rate limiting for login (in-memory, resets on cold start — acceptable for serverless)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
_login_attempts: dict[str, list[float]] = {}

# ==================== CONFIG ====================
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "change-me-in-env")
SOIL_WINDOW = 10
MAX_PLANT_SLOTS = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hoya-pet")

# ==================== SUPABASE CLIENT (lazy init) ====================
_supabase_client: Client | None = None

def _sb() -> Client:
    """Return Supabase client, lazy-initialized on first use."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise HTTPException(500, "Supabase not configured — check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client

# ==================== SUPABASE STORAGE HELPERS ====================
def _upload_storage(bucket: str, path: str, data: bytes, content_type: str = "image/jpeg"):
    """Upload file to Supabase Storage, overwriting if exists."""
    sb = _sb()
    try:
        sb.storage.from_(bucket).update(path, data, {"content-type": content_type, "upsert": "true"})
    except Exception:
        sb.storage.from_(bucket).upload(path, data, {"content-type": content_type})

def _download_storage(bucket: str, path: str) -> bytes | None:
    """Download file from Supabase Storage."""
    try:
        return _sb().storage.from_(bucket).download(path)
    except Exception:
        return None

def _storage_exists(bucket: str, path: str) -> bool:
    """Check if file exists in Supabase Storage."""
    return _download_storage(bucket, path) is not None

def _storage_public_url(bucket: str, path: str) -> str:
    """Get public URL for a file in Supabase Storage."""
    return _sb().storage.from_(bucket).get_public_url(path)

# ==================== PER-USER DATA HELPERS (Supabase DB) ====================
import re

def _sanitize_email(email: str) -> str:
    """Sanitize email for use as storage path prefix."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', email.lower().strip())

def _get_user_prefs(user: str) -> dict:
    result = _sb().table("user_prefs").select("*").eq("user_email", user).execute()
    if result.data:
        return result.data[0]
    # Create default prefs
    default = {"user_email": user, "active_slot": 1, "soil_cal": {}}
    _sb().table("user_prefs").upsert(default).execute()
    return default

def _get_active_slot(user: str) -> int:
    return _get_user_prefs(user).get("active_slot", 1)

def _set_active_slot(user: str, slot_id: int):
    _sb().table("user_prefs").upsert({"user_email": user, "active_slot": slot_id}).execute()

# ==================== SOIL CALIBRATION ====================
def _load_soil_cal(user: str) -> dict:
    prefs = _get_user_prefs(user)
    return prefs.get("soil_cal") or {}

def _save_soil_cal(user: str, cal: dict):
    _sb().table("user_prefs").upsert({"user_email": user, "soil_cal": cal}).execute()

def _recalc_soil_pct(raw_adc: int, cal: dict):
    """Recalculate soil moisture % using user calibration.
    Defaults based on capacitive sensor v1.2 calibrated 2026-02:
      dry_adc=2970 (dry in air), soaked_adc=1320 (in water).
    """
    dry = cal.get('dry_adc', 2970)
    wet = cal.get('soaked_adc', 1320)
    if dry <= wet or raw_adc is None:
        return None
    pct = (dry - raw_adc) / (dry - wet) * 100
    return max(0, min(100, round(pct)))

# ==================== PLANT SLOT HELPERS (Supabase DB) ====================
def _get_slot(user: str, slot_id: int) -> dict | None:
    result = _sb().table("plant_slots").select("*").eq("user_email", user).eq("slot_id", slot_id).execute()
    return result.data[0] if result.data else None

def _upsert_slot(user: str, slot_id: int, updates: dict):
    data = {"user_email": user, "slot_id": slot_id, **updates}
    _sb().table("plant_slots").upsert(data, on_conflict="user_email,slot_id").execute()

def _get_slot_field(user: str, slot_id: int, field: str) -> dict:
    slot = _get_slot(user, slot_id)
    if slot and slot.get(field):
        return slot[field] if isinstance(slot[field], dict) else {}
    return {}

def _list_slots(user: str) -> list[dict]:
    """Return list of configured plant slots with metadata."""
    result = _sb().table("plant_slots").select("*").eq("user_email", user).order("slot_id").execute()
    slots = []
    for row in result.data:
        profile = row.get("plant_profile") or {}
        pet_cfg = row.get("pet_config") or {}
        if profile or pet_cfg:
            sid = row["slot_id"]
            safe = _sanitize_email(user)
            has_photo = _storage_exists("plant-photos", f"{safe}/{sid}/plant_photo.jpg")
            slots.append({
                "id": sid,
                "plant_name": profile.get("nome_popular", "?"),
                "plant_scientific": profile.get("nome_cientifico", ""),
                "pet_name": pet_cfg.get("name", "?"),
                "pet_type": pet_cfg.get("type", "cat"),
                "has_photo": has_photo,
            })
    return slots

def _next_slot_id(user: str) -> int:
    result = _sb().table("plant_slots").select("slot_id").eq("user_email", user).order("slot_id", desc=True).limit(1).execute()
    if result.data:
        return result.data[0]["slot_id"] + 1
    return 1

# ==================== APP ====================
app = FastAPI(title="Hoya Pet App API", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

@app.get("/api/health")
def api_health():
    """Health check — verifies Supabase connection."""
    try:
        sb = _sb()
        sb.table("sensor_readings").select("id").limit(1).execute()
        return {"ok": True, "supabase": "connected"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    path = request.url.path
    if path.startswith("/api/current") or path.startswith("/api/history") or path.startswith("/api/ingest"):
        response.headers["Cache-Control"] = "no-store"
    elif "/photo" in path or "/pet-image" in path:
        response.headers["Cache-Control"] = "public, max-age=3600"
    elif path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response

# ==================== AUTH HELPERS ====================
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return f"{salt.hex()}:{pwd_hash.hex()}"

def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_hex, hash_hex = hashed.split(':')
        salt = bytes.fromhex(salt_hex)
        pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return hmac.compare_digest(pwd_hash.hex(), hash_hex)
    except Exception:
        return False

def _email_template(title: str, greeting: str, body_html: str) -> str:
    """Email template with Hoya Pet visual (parchment + wood)."""
    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#eff1f3;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eff1f3;padding:32px 0;">
<tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#faf4e1;border:4px solid #5c3a21;box-shadow:6px 6px 0 #5c3a21;">

  <!-- Header with logo and title -->
  <tr>
    <td style="background:linear-gradient(180deg,#5a8a3c 0%,#3d5a1e 100%);padding:28px 32px;text-align:center;border-bottom:4px solid #2d4a15;">
      <img src="{SITE_URL}/pixel_hoya_logo.png" alt="Hoya Pet" width="72" height="72"
           style="image-rendering:pixelated;display:block;margin:0 auto 12px;" />
      <h1 style="color:#ffffff;margin:0;font-size:18px;font-weight:700;letter-spacing:1px;
                 text-shadow:1px 1px 0 #1a3008;font-family:'Courier New',monospace;">
        {title}
      </h1>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:28px 32px;background:#faf4e1;border:3px solid #e8d5a3;border-left:none;border-right:none;">
      <p style="color:#3d2b1f;font-size:16px;margin:0 0 20px;line-height:1.6;">{greeting}</p>
      {body_html}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f0e6cc;padding:16px 32px;border-top:3px dashed #c4a265;text-align:center;">
      <p style="color:#5c4033;font-size:11px;margin:0;line-height:1.6;">
        Este e-mail foi enviado automaticamente pelo<br>
        <strong style="color:#5a8a3c;">🌱 Hoya Pet</strong> — Monitoramento Inteligente de Plantas
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

def _send_email(to_email: str, subject: str, html_content: str):
    """Send an HTML email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP credentials missing in .env. Skipping email.")
        return

    msg = MIMEMultipart("alternative")
    msg['From'] = f"Hoya Pet <{SMTP_USER}>"
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"Email sent to {to_email}")
    except Exception as e:
        logger.error(f"SMTP email error: {e}")

def send_verification_email(to_email: str, code: str):
    html = _email_template(
        title="VERIFICAÇÃO DE CONTA",
        greeting="Olá! Bem-vindo(a) ao Hoya Pet 🌿",
        body_html=f"""
        <p style="color:#3d2b1f;font-size:15px;margin:0 0 16px;line-height:1.6;">
            Seu código para ativar a conta é:
        </p>
        <div style="background:#faf4e1;border:3px solid #5c3a21;box-shadow:4px 4px 0 #5c3a21;
                    padding:24px;text-align:center;margin:0 0 20px;">
            <span style="font-size:36px;font-weight:800;letter-spacing:12px;
                         color:#5a8a3c;font-family:'Courier New',monospace;
                         text-shadow:2px 2px 0 #3d5a1e;">{code}</span>
        </div>
        <p style="color:#5c4033;font-size:13px;margin:0;line-height:1.5;
                  border-left:3px solid #c4a265;padding-left:12px;">
            ⏰ Expira em <strong>15 minutos</strong>.<br>
            Se não foi você, ignore este e-mail.
        </p>
        """
    )
    _send_email(to_email, "🌱 Código de verificação — Hoya Pet", html)

def send_password_reset_email(to_email: str, code: str):
    html = _email_template(
        title="RECUPERAÇÃO DE SENHA",
        greeting="Recebemos um pedido para redefinir sua senha.",
        body_html=f"""
        <p style="color:#3d2b1f;font-size:15px;margin:0 0 16px;line-height:1.6;">
            Use este código para criar uma nova senha:
        </p>
        <div style="background:#faf4e1;border:3px solid #5c3a21;box-shadow:4px 4px 0 #5c3a21;
                    padding:24px;text-align:center;margin:0 0 20px;">
            <span style="font-size:36px;font-weight:800;letter-spacing:12px;
                         color:#5a8a3c;font-family:'Courier New',monospace;
                         text-shadow:2px 2px 0 #3d5a1e;">{code}</span>
        </div>
        <p style="color:#5c4033;font-size:13px;margin:0 0 8px;line-height:1.5;
                  border-left:3px solid #c4a265;padding-left:12px;">
            ⏰ Expira em <strong>15 minutos</strong>.<br>
            Se não foi você quem solicitou, sua senha está segura — ignore este e-mail.
        </p>
        """
    )
    _send_email(to_email, "🔐 Recuperação de senha — Hoya Pet", html)

def send_weekly_photo_reminder_email(to_email: str, plant_name: str, pet_name: str):
    html = _email_template(
        title="HORA DA FOTINHA! 📸",
        greeting=f"Olá, cuidador(a) da {plant_name}!",
        body_html=f"""
        <p style="color:#3d2b1f;font-size:15px;margin:0 0 16px;line-height:1.6;">
            Já faz uma semana desde a última foto da sua <strong>{plant_name}</strong>.
            O(a) <strong>{pet_name}</strong> está sentindo falta de ver como ela cresceu! 🌿
        </p>
        <p style="color:#3d2b1f;font-size:15px;margin:0 0 24px;line-height:1.6;">
            Tire uma nova foto para a IA analisar a saúde atual e gerar um novo retrato do pet.
        </p>
        <div style="text-align:center;margin:0 0 20px;">
            <a href="{SITE_URL}"
               style="background:linear-gradient(180deg,#5a8a3c,#3d5a1e);color:#fff;
                      padding:14px 28px;text-decoration:none;font-weight:700;font-size:14px;
                      display:inline-block;border:3px solid #2d4a15;box-shadow:4px 4px 0 #2d4a15;
                      text-shadow:1px 1px 0 #1a3008;letter-spacing:1px;">
                🌱 ABRIR HOYA PET
            </a>
        </div>
        <p style="color:#8b6914;font-size:12px;margin:0;text-align:center;">
            Sua planta agradece! 💚
        </p>
        """
    )
    _send_email(to_email, f"📸 {pet_name} quer uma foto nova da {plant_name}!", html)

# ==================== JWT AUTH ====================
def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub", "")
    except JWTError:
        return ""

async def get_current_user(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")
    token = auth.split(" ", 1)[1]
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Token invalido ou expirado")
    return user

# ==================== HELPERS ====================
def _smooth_soil(values):
    if not values:
        return []
    w = max(1, min(SOIL_WINDOW, len(values)))
    result = []
    for i in range(len(values)):
        start = max(0, i - w // 2)
        end = min(len(values), i + w // 2 + 1)
        window = values[start:end]
        result.append(round(sum(window) / len(window), 1))
    return result

def _trend(values):
    if len(values) < 4:
        return "estavel"
    mid = len(values) // 2
    first_half = values[:mid]
    second_half = values[mid:]
    first = sum(first_half) / len(first_half)
    second = sum(second_half) / len(second_half)
    d = second - first
    if d > 1.0:
        return "subindo"
    elif d < -1.0:
        return "descendo"
    return "estavel"

def _get_ideal(user: str = None):
    """Get ideal ranges from AI profile if available, otherwise defaults."""
    if user:
        slot_id = _get_active_slot(user)
        profile = _get_slot_field(user, slot_id, "plant_profile")
        if profile:
            return {
                "temp": (profile.get("temperatura_ideal_min", 18), profile.get("temperatura_ideal_max", 28)),
                "humidity": (profile.get("umidade_ar_ideal_min", 40), profile.get("umidade_ar_ideal_max", 70)),
                "soil": (profile.get("umidade_solo_ideal_min", 15), profile.get("umidade_solo_ideal_max", 55)),
            }
    return {"temp": (18, 28), "humidity": (40, 70), "soil": (15, 55)}

def _health(t, h, s, user: str = None):
    ideal = _get_ideal(user)
    score = 100.0
    tl, th = ideal["temp"]
    hl, hh = ideal["humidity"]
    sl, sh = ideal["soil"]
    if t < tl: score -= min(30, (tl - t) * 5)
    elif t > th: score -= min(30, (t - th) * 5)
    if h < hl: score -= min(20, (hl - h) * 2)
    elif h > hh: score -= min(10, (h - hh))
    if s < sl: score -= min(50, (sl - s) * 5)
    elif s > sh: score -= min(30, (s - sh) * 3)
    return max(0, min(100, round(score)))

def _health_label(score):
    if score >= 85: return "Excelente"
    if score >= 65: return "Bom"
    if score >= 45: return "Atencao"
    return "Critico"

def _irrigation(soil, trend, user: str = None):
    ideal = _get_ideal(user)
    if soil < 15: return {"message": "Irrigar agora — solo muito seco", "level": "critical"}
    if soil < ideal["soil"][0]:
        if trend == "descendo":
            return {"message": "Irrigar em breve — solo secando", "level": "warning"}
        return {"message": "Solo levemente seco — monitorar", "level": "caution"}
    if soil <= ideal["soil"][1]:
        return {"message": "Nivel ideal — nao irrigar", "level": "ok"}
    if soil <= 60:
        return {"message": "Solo umido — aguardar secagem", "level": "wet"}
    return {"message": "Solo encharcado — verificar drenagem", "level": "soaked"}

# ==================== AUTH ENDPOINTS ====================
class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    email: str
    password: str

class VerifyRequest(BaseModel):
    email: str
    code: str

class ForgotRequest(BaseModel):
    email: str

class ResetRequest(BaseModel):
    email: str
    code: str
    new_password: str

def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limited (too many attempts)."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS

@app.post("/api/login")
def api_login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    if _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Muitas tentativas. Tente novamente em {LOGIN_WINDOW_SECONDS // 60} minutos."
        )

    email_clean = req.email.lower().strip()
    result = _sb().table("users").select("password_hash, is_verified").eq("email", email_clean).execute()
    user = result.data[0] if result.data else None

    if user and verify_password(req.password, user['password_hash']):
        if not user['is_verified']:
            raise HTTPException(status_code=403, detail="verification_required")

        _login_attempts.pop(client_ip, None)
        token = create_token(email_clean)
        return {"ok": True, "token": token, "email": email_clean}

    _login_attempts.setdefault(client_ip, []).append(time.time())
    remaining = LOGIN_MAX_ATTEMPTS - len(_login_attempts.get(client_ip, []))
    raise HTTPException(status_code=401, detail=f"Credenciais invalidas ({remaining} tentativas restantes)")

@app.post("/api/register")
def api_register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas.")

    email_clean = req.email.lower().strip()
    if not email_clean or "@" not in email_clean or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="E-mail invalido ou senha muito curta (min = 6).")

    sb = _sb()
    existing = sb.table("users").select("is_verified").eq("email", email_clean).execute()

    if existing.data and existing.data[0]['is_verified']:
        raise HTTPException(status_code=400, detail="Este e-mail ja esta em uso e verificado.")

    code = f"{random.randint(0, 999999):06d}"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    try:
        if not existing.data:
            sb.table("users").insert({
                "email": email_clean,
                "password_hash": hash_password(req.password),
                "is_verified": False
            }).execute()
        else:
            sb.table("users").update({
                "password_hash": hash_password(req.password)
            }).eq("email", email_clean).execute()

        sb.table("email_codes").delete().eq("email", email_clean).execute()
        sb.table("email_codes").insert({
            "email": email_clean,
            "code": code,
            "expires_at": expires
        }).execute()
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Erro ao registrar.")

    logger.info(f"Code generated for {email_clean}: {code}")
    threading.Thread(target=send_verification_email, args=(email_clean, code)).start()

    return {"ok": True, "message": "Codigo enviado para o e-mail."}

@app.post("/api/verify")
def api_verify(req: VerifyRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas.")

    email_clean = req.email.lower().strip()
    code_clean = req.code.strip()

    sb = _sb()
    result = sb.table("email_codes").select("code, expires_at").eq("email", email_clean).order("id", desc=True).limit(1).execute()
    row = result.data[0] if result.data else None

    if not row or row['code'] != code_clean:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        raise HTTPException(status_code=400, detail="Codigo invalido ou incorreto.")

    expires = datetime.fromisoformat(row['expires_at'].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=400, detail="Codigo expirado. Cadastre-se novamente.")

    sb.table("users").update({"is_verified": True}).eq("email", email_clean).execute()
    sb.table("email_codes").delete().eq("email", email_clean).execute()

    _login_attempts.pop(client_ip, None)
    token = create_token(email_clean)
    return {"ok": True, "token": token, "email": email_clean}

@app.post("/api/password/forgot")
def api_password_forgot(req: ForgotRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")

    email_clean = req.email.lower().strip()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="E-mail invalido.")

    sb = _sb()
    existing = sb.table("users").select("is_verified").eq("email", email_clean).execute()

    if existing.data and existing.data[0]['is_verified']:
        code = f"{random.randint(0, 999999):06d}"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

        try:
            sb.table("email_codes").delete().eq("email", email_clean).execute()
            sb.table("email_codes").insert({
                "email": email_clean,
                "code": code,
                "expires_at": expires
            }).execute()

            logger.info(f"Reset code generated for {email_clean}: {code}")
            threading.Thread(target=send_password_reset_email, args=(email_clean, code)).start()
        except Exception as e:
            logger.error(f"Reset code generation error: {e}")
            raise HTTPException(status_code=500, detail="Erro interno.")

    return {"ok": True, "message": "Se o e-mail existir e estiver verificado, você receberá um código."}

@app.post("/api/password/reset")
def api_password_reset(req: ResetRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")

    email_clean = req.email.lower().strip()
    code_clean = req.code.strip()

    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="A nova senha deve ter pelo menos 6 caracteres.")

    sb = _sb()
    user_result = sb.table("users").select("id, is_verified").eq("email", email_clean).execute()
    user = user_result.data[0] if user_result.data else None

    if not user or not user['is_verified']:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        raise HTTPException(status_code=400, detail="Usuário não encontrado ou não verificado.")

    code_result = sb.table("email_codes").select("code, expires_at").eq("email", email_clean).order("id", desc=True).limit(1).execute()
    row = code_result.data[0] if code_result.data else None

    if not row or row['code'] != code_clean:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        raise HTTPException(status_code=400, detail="Codigo invalido ou incorreto.")

    expires = datetime.fromisoformat(row['expires_at'].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=400, detail="Codigo expirado. Solicite um novo código.")

    sb.table("users").update({"password_hash": hash_password(req.new_password)}).eq("email", email_clean).execute()
    sb.table("email_codes").delete().eq("email", email_clean).execute()

    _login_attempts.pop(client_ip, None)
    token = create_token(email_clean)
    return {"ok": True, "token": token, "email": email_clean}

@app.get("/api/auth/check")
def api_auth_check(user: str = Depends(get_current_user)):
    return {"ok": True, "email": user}

# ==================== SETUP ENDPOINTS ====================
@app.get("/api/setup/status")
def api_setup_status(user: str = Depends(get_current_user)):
    slot_id = _get_active_slot(user)
    plant_profile = _get_slot_field(user, slot_id, "plant_profile")
    pet_config = _get_slot_field(user, slot_id, "pet_config")
    plant_ok = bool(plant_profile)
    pet_ok = bool(pet_config)
    return {"ok": True, "plant_configured": plant_ok, "pet_configured": pet_ok, "setup_complete": plant_ok and pet_ok}

@app.post("/api/setup/plant")
async def api_setup_plant(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    """Upload plant photo and identify species via GPT-5.2."""
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (max 10MB)")

    slot_id = _get_active_slot(user)
    safe = _sanitize_email(user)
    storage_path = f"{safe}/{slot_id}/plant_photo.jpg"
    _upload_storage("plant-photos", storage_path, contents, "image/jpeg")

    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY nao configurada no .env")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    img_b64 = base64.b64encode(contents).decode()

    response = client.responses.create(
        model="gpt-5.2",
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Analise esta foto de planta com muita atencao. Identifique a especie exata. "
                        "Retorne SOMENTE um JSON valido (sem markdown, sem explicacao) com os campos: "
                        '{"nome_popular": "...", "nome_cientifico": "...", '
                        '"temperatura_ideal_min": N, "temperatura_ideal_max": N, '
                        '"umidade_ar_ideal_min": N, "umidade_ar_ideal_max": N, '
                        '"umidade_solo_ideal_min": N, "umidade_solo_ideal_max": N, '
                        '"descricao_curta": "...", "cuidados_especiais": "..."}'
                    )
                },
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{img_b64}",
                },
            ],
        }],
    )

    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        profile = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse plant profile JSON: {raw[:200]}")
        profile = {
            "nome_popular": "Nao identificada",
            "nome_cientifico": "Desconhecida",
            "temperatura_ideal_min": 18, "temperatura_ideal_max": 28,
            "umidade_ar_ideal_min": 40, "umidade_ar_ideal_max": 70,
            "umidade_solo_ideal_min": 15, "umidade_solo_ideal_max": 55,
            "descricao_curta": "Planta nao identificada automaticamente.",
            "cuidados_especiais": "Verifique manualmente a especie.",
            "_raw_response": raw[:500],
        }

    _upsert_slot(user, slot_id, {"plant_profile": profile})
    return {"ok": True, "profile": profile}

@app.post("/api/setup/plant-photo")
async def api_update_plant_photo(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    """Update plant photo and re-analyze."""
    return await api_setup_plant(file=file, user=user)

@app.get("/api/plant-profile")
def api_plant_profile(user: str = Depends(get_current_user)):
    slot_id = _get_active_slot(user)
    profile = _get_slot_field(user, slot_id, "plant_profile")
    if not profile:
        return {"ok": False, "message": "Planta nao configurada"}
    return {"ok": True, "profile": profile}

# ==================== PET ENDPOINTS ====================
class PetConfigRequest(BaseModel):
    name: str
    type: str  # "cat" or "dog"

@app.post("/api/pet/configure")
def api_pet_configure(req: PetConfigRequest, user: str = Depends(get_current_user)):
    if req.type not in ("cat", "dog"):
        raise HTTPException(400, "Tipo deve ser 'cat' ou 'dog'")
    if not req.name or len(req.name.strip()) < 1 or len(req.name.strip()) > 20:
        raise HTTPException(400, "Nome do pet deve ter entre 1 e 20 caracteres")
    slot_id = _get_active_slot(user)
    config = {"name": req.name, "type": req.type, "created_at": datetime.now(timezone.utc).isoformat()}
    _upsert_slot(user, slot_id, {"pet_config": config})
    return {"ok": True, "config": config}

@app.post("/api/pet/upload-photo")
async def api_pet_upload_photo(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    """Upload a real pet photo to use as reference for pixel art generation."""
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (max 10MB)")
    slot_id = _get_active_slot(user)
    safe = _sanitize_email(user)
    _upload_storage("pet-references", f"{safe}/{slot_id}/pet_reference.jpg", contents, "image/jpeg")
    # Update pet_config to mark reference photo exists
    config = _get_slot_field(user, slot_id, "pet_config")
    config["has_reference_photo"] = True
    _upsert_slot(user, slot_id, {"pet_config": config})
    return {"ok": True}

@app.get("/api/pet/reference-photo")
def api_pet_reference_photo(user: str = Depends(get_current_user)):
    """Return the uploaded real pet photo."""
    slot_id = _get_active_slot(user)
    safe = _sanitize_email(user)
    data = _download_storage("pet-references", f"{safe}/{slot_id}/pet_reference.jpg")
    if not data:
        raise HTTPException(404, "Sem foto de referencia")
    return JSONResponse(
        content={"ok": True, "image": base64.b64encode(data).decode()},
        media_type="application/json"
    )

@app.get("/api/pet/config")
def api_pet_config(user: str = Depends(get_current_user)):
    slot_id = _get_active_slot(user)
    config = _get_slot_field(user, slot_id, "pet_config")
    if not config:
        return {"ok": False}
    return {"ok": True, "config": config}

@app.get("/api/pet/current")
def api_pet_current(user: str = Depends(get_current_user)):
    slot_id = _get_active_slot(user)
    safe = _sanitize_email(user)
    state = _get_slot_field(user, slot_id, "pet_state")
    img_data = _download_storage("pet-images", f"{safe}/{slot_id}/pet_current.png")

    if img_data:
        img_b64 = base64.b64encode(img_data).decode()
        return {
            "ok": True,
            "image": img_b64,
            "prompt_used": state.get("last_prompt", ""),
            "generated_at": state.get("generated_at", ""),
            "event_of_day": state.get("event_of_day", ""),
            "pet_caption": state.get("pet_caption", ""),
            "pet_phrases": state.get("pet_phrases", []),
        }
    return {"ok": False, "message": "Nenhuma imagem gerada ainda"}

@app.post("/api/pet/generate")
def api_pet_generate(user: str = Depends(get_current_user)):
    """Queue async pet image generation via Supabase Edge Function."""
    slot_id = _get_active_slot(user)
    sb = _sb()

    # Create a job in pet_jobs table — Supabase webhook triggers Edge Function
    result = sb.table("pet_jobs").insert({
        "user_email": user,
        "slot_id": slot_id,
        "job_type": "image",
        "status": "pending"
    }).execute()

    job_id = result.data[0]["id"] if result.data else None
    return {"ok": True, "job_id": job_id, "status": "pending",
            "message": "Geracao de pet enfileirada. Aguarde alguns minutos."}

# ==================== MULTI-PLANT ENDPOINTS ====================
@app.get("/api/plants")
def api_list_plants(user: str = Depends(get_current_user)):
    slots = _list_slots(user)
    active = _get_active_slot(user)
    safe = _sanitize_email(user)
    for s in slots:
        sid = s["id"]
        s["has_pet_image"] = _storage_exists("pet-images", f"{safe}/{sid}/pet_current.png")
        s["plant_photo_url"] = f"/api/plants/{sid}/photo" if s["has_photo"] else None
        s["pet_image_url"] = f"/api/plants/{sid}/pet-image" if s["has_pet_image"] else None
    return {"ok": True, "plants": slots, "active_slot": active}

@app.post("/api/plants")
def api_create_plant(user: str = Depends(get_current_user)):
    slots = _list_slots(user)
    if len(slots) >= MAX_PLANT_SLOTS:
        raise HTTPException(400, f"Maximo de {MAX_PLANT_SLOTS} plantas atingido")
    new_id = _next_slot_id(user)
    _upsert_slot(user, new_id, {})
    _set_active_slot(user, new_id)
    return {"ok": True, "slot_id": new_id, "message": "Novo slot criado. Complete o setup."}

@app.get("/api/plants/{slot_id}/photo")
def get_plant_photo(slot_id: int, user: str = Depends(get_current_user)):
    safe = _sanitize_email(user)
    data = _download_storage("plant-photos", f"{safe}/{slot_id}/plant_photo.jpg")
    if not data:
        raise HTTPException(404)
    return Response(content=data, media_type="image/jpeg")

@app.get("/api/plants/{slot_id}/pet-image")
def get_pet_image(slot_id: int, user: str = Depends(get_current_user)):
    safe = _sanitize_email(user)
    data = _download_storage("pet-images", f"{safe}/{slot_id}/pet_current.png")
    if not data:
        raise HTTPException(404)
    return Response(content=data, media_type="image/png")

@app.get("/api/plants/{slot_id}/switch")
def api_switch_plant(slot_id: int, user: str = Depends(get_current_user)):
    slot = _get_slot(user, slot_id)
    if not slot:
        raise HTTPException(404, "Slot nao encontrado")
    _set_active_slot(user, slot_id)
    return {"ok": True, "active_slot": slot_id}

@app.delete("/api/plants/{slot_id}")
def api_delete_plant(slot_id: int, user: str = Depends(get_current_user)):
    slot = _get_slot(user, slot_id)
    if not slot:
        raise HTTPException(404, "Slot nao encontrado")
    configured_slots = _list_slots(user)
    remaining_after = [s for s in configured_slots if s["id"] != slot_id]
    if len(remaining_after) == 0:
        raise HTTPException(400, "Voce precisa ter pelo menos 1 planta configurada")

    sb = _sb()
    sb.table("plant_slots").delete().eq("user_email", user).eq("slot_id", slot_id).execute()

    # Clean up storage
    safe = _sanitize_email(user)
    for bucket in ["plant-photos", "pet-images", "pet-references"]:
        try:
            files = sb.storage.from_(bucket).list(f"{safe}/{slot_id}")
            if files:
                paths = [f"{safe}/{slot_id}/{f['name']}" for f in files]
                sb.storage.from_(bucket).remove(paths)
        except Exception:
            pass

    if _get_active_slot(user) == slot_id:
        remaining = _list_slots(user)
        if remaining:
            _set_active_slot(user, remaining[0]["id"])
    return {"ok": True, "message": f"Slot {slot_id} removido"}

# ==================== PET IMAGE GENERATION (for cron/edge functions) ====================
def generate_pet_image(user: str):
    """Core function: generates a new pet image using OpenAI Images API.
    Called by cron endpoints or directly for testing.
    """
    if not OPENAI_API_KEY:
        raise Exception("OPENAI_API_KEY nao configurada")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    slot_id = _get_active_slot(user)
    pet_config = _get_slot_field(user, slot_id, "pet_config")
    plant_profile = _get_slot_field(user, slot_id, "plant_profile")
    pet_state = _get_slot_field(user, slot_id, "pet_state")

    if not pet_config:
        raise Exception("Pet nao configurado")

    pet_name = pet_config.get("name", "Mimi")
    pet_type = "cat" if pet_config.get("type") == "cat" else "dog"
    pet_type_pt = "gato" if pet_config.get("type") == "cat" else "cachorro"
    plant_name = plant_profile.get("nome_popular", "planta") if plant_profile else "planta"

    # Get current sensor data from Supabase
    sb = _sb()
    sensor_result = sb.table("sensor_readings").select("*").order("timestamp", desc=True).limit(1).execute()
    row = sensor_result.data[0] if sensor_result.data else None

    temp = row["temperature"] if row else 22
    hum = row["humidity"] if row else 50
    soil = row["soil_moisture"] if row else 30
    ideal = _get_ideal(user)
    health_score = _health(temp or 22, hum or 50, soil or 30, user)

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        now = datetime.now()

    hour = now.hour
    if 5 <= hour < 9:
        time_period = "dawn with soft golden light and morning dew on leaves"
    elif 9 <= hour < 12:
        time_period = "bright sunny morning, cheerful warm light"
    elif 12 <= hour < 15:
        time_period = "midday with strong overhead sun"
    elif 15 <= hour < 18:
        time_period = "warm golden afternoon light, long soft shadows"
    elif 18 <= hour < 21:
        time_period = "dusk with purple-orange gradient sky"
    else:
        time_period = "cozy night scene with moonlight and twinkling stars"

    # Determine pet action based on sensor data
    if soil is not None and soil < 15:
        action = "frantically running with an oversized watering can, clearly panicking to save the extremely dry plant"
    elif soil is not None and soil < ideal["soil"][0]:
        action = "carefully watering the plant with a tiny cute watering can, very focused and gentle"
    elif soil is not None and soil > 60:
        action = "holding a tiny umbrella over the plant, looking worried about excess water"
    elif temp is not None and temp < ideal["temp"][0]:
        action = "wrapping the plant pot in a tiny cozy scarf and blanket, shivering adorably"
    elif temp is not None and temp > ideal["temp"][1]:
        action = "fanning the plant with a giant leaf fan, sweating with effort"
    elif hum is not None and hum < ideal["humidity"][0]:
        action = "misting the air around the plant with a cute spray bottle, being very thorough"
    elif health_score >= 85:
        action = random.choice([
            "playing a tiny ukulele and serenading the thriving plant with a happy song",
            "doing an excited victory dance next to the lush healthy plant",
            "polishing each leaf gently with a tiny soft cloth, humming contentedly",
            "measuring the plant height with a mini ruler, beaming with pride",
            "taking a selfie with the plant using a tiny smartphone, both looking adorable",
            "placing a tiny golden trophy next to the plant, celebrating its perfect health",
            "watering with a fancy can while whistling, doing a little dance step",
        ])
    else:
        action = random.choice([
            "carefully checking soil moisture with a tiny probe, looking studious",
            "adding plant food pellets to the soil, reading the instructions",
            "gently trimming a leaf with tiny scissors, being very precise",
            "consulting a tiny plant care handbook with a focused expression",
            "giving the plant an encouraging thumbs up and a warm determined smile",
        ])

    if health_score >= 85:
        mood_suffix = "with sparkling star-shaped eyes, radiating pure joy and pride"
    elif health_score < 40:
        mood_suffix = "with a deeply worried furrowed brow and tiny stress sweat drops"
    else:
        mood_suffix = "with a caring determined expression and focused anime eyes"

    if health_score >= 80:
        plant_visual = "super healthy — lush deep green leaves, sparkling water droplets, tiny glowing sparkle effects radiating vitality"
    elif health_score >= 50:
        plant_visual = "healthy and green, looking well and cared for"
    elif health_score >= 30:
        plant_visual = "wilting slightly, pale drooping leaves, clearly needing attention"
    else:
        plant_visual = "critically struggling — brown leaf tips, dramatically drooping, dry cracked soil, desperate for care"

    # Web search for fun event of the day
    event_text = ""
    try:
        date_str = now.strftime("%d de %B de %Y")
        event_response = client.responses.create(
            model="gpt-4.1-mini",
            tools=[{"type": "web_search"}],
            input=(
                f"Hoje e {date_str}. Encontre 1 evento curioso, engraçado ou feriado "
                f"internacional/nacional de hoje. Seja breve e criativo. "
                f"Responda em uma frase curta descrevendo o evento."
            ),
        )
        event_text = event_response.output_text.strip()
        logger.info(f"Event of the day: {event_text}")
    except Exception as e:
        logger.warning(f"Web search failed: {e}")

    event_element = (
        f" Include a subtle fun prop referencing today's special event: {event_text} (e.g., a tiny themed hat or item on the ground)."
        if event_text else ""
    )

    creative_prompt = (
        f"MAXIMUM CUTENESS retro pixel art sprite of an EXTREMELY ADORABLE chubby chibi {pet_type} named '{pet_name}' "
        f"{action}, {mood_suffix}. "
        f"CUTENESS RULES: oversized round head (60% of body), body is a soft squishy ball shape, "
        f"tiny stubby legs, GIGANTIC pixel eyes with a single tiny 1-pixel white highlight dot (NO glare, NO bloom, NO heavy shine), rosy blushing cheeks, "
        f"small cute mouth with a tiny smile or worried pout. Irresistibly kawaii — like a premium Tamagotchi or Pokémon sprite. "
        f"The {pet_type} is beside a {plant_name} plant with heart-shaped leaves on a dark vertical trellis in a terracotta pot. "
        f"PIXEL ART STYLE: NES / Game Boy Color / early SNES era — chunky visible square pixels, "
        f"bold black pixel outlines on every shape, flat color fills, NO gradients, NO soft shading. "
        f"Limited palette 16-32 colors. Hard pixel edges. Raw classic retro videogame sprite quality. "
        f"CHARACTER CONSISTENCY RULE: The {pet_type}'s fur color, fur pattern, markings, eye color and "
        f"body proportions are LOCKED to the name '{pet_name}' — every generation with this name MUST produce "
        f"the IDENTICAL character design. If a previous image exists, copy its exact visual style pixel by pixel. "
        f"PLANT STATE: {plant_name} is {plant_visual}. {time_period} lighting. "
        f"Chunky pixel soil near the pot. Pixel water droplets if watering. "
        f"COMPOSITION: Side view. Plant on the left, {pet_type} on the right. Both fully visible. "
        f"BACKGROUND: Pure white — fully isolated sprite, no floor tile, no drop shadow. "
        f"ABSOLUTE RULES: ZERO text, ZERO letters, ZERO numbers, ZERO words anywhere in the image — "
        f"not on signs, pots, clothing, or anywhere else. No speech bubbles with text.{event_element}"
    )
    logger.info(f"Creative prompt: {creative_prompt[:200]}...")

    safe = _sanitize_email(user)
    previous_response_id = pet_state.get("last_response_id") if pet_state else None

    # Check if user uploaded a real pet reference photo
    ref_data = _download_storage("pet-references", f"{safe}/{slot_id}/pet_reference.jpg")
    if ref_data:
        creative_prompt = (
            "REFERENCE PHOTO: The attached photo shows the REAL pet this pixel art is based on. "
            "You MUST match its exact fur color, pattern, markings, eye color and distinctive features "
            "in the pixel art style. This is the #1 priority for character design. "
        ) + creative_prompt

    input_content = [{"type": "input_text", "text": creative_prompt}]

    if ref_data:
        ref_b64 = base64.b64encode(ref_data).decode()
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{ref_b64}",
        })

    # Pass previous pet image as visual reference for consistency
    prev_img = _download_storage("pet-images", f"{safe}/{slot_id}/pet_current.png")
    if prev_img:
        pet_b64 = base64.b64encode(prev_img).decode()
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{pet_b64}",
        })

    gen_kwargs = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": input_content}],
        "tools": [{"type": "image_generation", "quality": "high", "size": "1024x1024", "output_format": "png", "background": "transparent"}],
        "store": True,
    }
    if previous_response_id:
        gen_kwargs["previous_response_id"] = previous_response_id

    img_response = client.responses.create(**gen_kwargs)

    image_data = None
    for output in img_response.output:
        if output.type == "image_generation_call":
            image_data = output.result
            break

    if image_data:
        raw_bytes = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(raw_bytes))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        _upload_storage("pet-images", f"{safe}/{slot_id}/pet_current.png", buf.getvalue(), "image/png")
        logger.info("Pet image generated, optimized and saved to Supabase Storage")
    else:
        logger.warning("No image data in response")

    # Generate creative caption
    caption = ""
    phrases = []
    try:
        caption_prompt = (
            f"Você é {pet_name}, um {pet_type_pt} com personalidade única cuidando de uma {plant_name}.\n"
            f"{'Personalidade: curioso, dramático, faz trocadilhos com plantas.' if pet_config.get('type') == 'cat' else 'Personalidade: leal, animado, adora dar conselhos de jardinagem.'}\n"
            f"Estado atual: temp {temp:.0f}°C, umidade {hum:.0f}%, solo {soil:.0f}%, saúde {health_score}/100.\n"
            f"{'Evento de hoje: ' + event_text if event_text else ''}\n"
            f"Gere EXATAMENTE 3 frases curtas e fofas (max 12 palavras cada) como se fosse {pet_name} falando.\n"
            f"Cada frase em uma linha. Cada frase deve ter 1 emoji no início.\n"
            f"Tom: {'preocupado e urgente' if health_score < 40 else 'animado e orgulhoso' if health_score > 80 else 'dedicado e cuidadoso'}.\n"
            f"Não use aspas, markdown ou numeração."
        )
        caption_resp = client.responses.create(model="gpt-4.1-mini", input=caption_prompt)
        raw_caption = caption_resp.output_text.strip()
        phrases = [l.strip() for l in raw_caption.split('\n') if l.strip()][:3]
        caption = phrases[0] if phrases else raw_caption.strip('"').strip("'")
        logger.info(f"Pet phrases: {phrases}")
    except Exception as e:
        logger.warning(f"Caption generation failed: {e}")

    new_state = {
        "last_response_id": img_response.id,
        "last_prompt": creative_prompt[:500],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_of_day": event_text,
        "pet_caption": caption,
        "pet_phrases": phrases,
        "health_score": health_score,
        "sensor_data": {"temperature": temp, "humidity": hum, "soil": soil},
    }
    _upsert_slot(user, slot_id, {"pet_state": new_state})

    return new_state


def generate_pet_phrases(user: str):
    """Generate only caption/phrases for a user's active pet, without regenerating the image."""
    if not OPENAI_API_KEY:
        raise Exception("OPENAI_API_KEY nao configurada")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    slot_id = _get_active_slot(user)
    pet_config = _get_slot_field(user, slot_id, "pet_config")
    plant_profile = _get_slot_field(user, slot_id, "plant_profile")
    pet_state = _get_slot_field(user, slot_id, "pet_state")

    if not pet_config:
        raise Exception("Pet nao configurado")

    pet_name = pet_config.get("name", "Mimi")
    pet_type_pt = "gato" if pet_config.get("type") == "cat" else "cachorro"
    plant_name = plant_profile.get("nome_popular", "planta") if plant_profile else "planta"

    sb = _sb()
    sensor_result = sb.table("sensor_readings").select("*").order("timestamp", desc=True).limit(1).execute()
    row = sensor_result.data[0] if sensor_result.data else None
    temp = row["temperature"] if row else 22
    hum = row["humidity"] if row else 50
    soil = row["soil_moisture"] if row else 30
    health_score = _health(temp or 22, hum or 50, soil or 30, user)

    event_text = ""
    try:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/Sao_Paulo"))
        except Exception:
            now = datetime.now()
        date_str = now.strftime("%d de %B de %Y")
        event_response = client.responses.create(
            model="gpt-4.1-mini",
            tools=[{"type": "web_search"}],
            input=(
                f"Hoje e {date_str}. Encontre 1 evento curioso, engraçado ou feriado "
                f"internacional/nacional de hoje. Seja breve e criativo. "
                f"Responda em uma frase curta descrevendo o evento."
            ),
        )
        event_text = event_response.output_text.strip()
    except Exception as e:
        logger.warning(f"Phrases web search failed: {e}")

    caption_prompt = (
        f"Você é {pet_name}, um {pet_type_pt} com personalidade única cuidando de uma {plant_name}.\n"
        f"{'Personalidade: curioso, dramático, faz trocadilhos com plantas.' if pet_config.get('type') == 'cat' else 'Personalidade: leal, animado, adora dar conselhos de jardinagem.'}\n"
        f"Estado atual: temp {temp:.0f}°C, umidade {hum:.0f}%, solo {soil:.0f}%, saúde {health_score}/100.\n"
        f"{'Evento de hoje: ' + event_text if event_text else ''}\n"
        f"Gere EXATAMENTE 3 frases curtas e fofas (max 12 palavras cada) como se fosse {pet_name} falando.\n"
        f"Cada frase em uma linha. Cada frase deve ter 1 emoji no início.\n"
        f"Tom: {'preocupado e urgente' if health_score < 40 else 'animado e orgulhoso' if health_score > 80 else 'dedicado e cuidadoso'}.\n"
        f"Não use aspas, markdown ou numeração."
    )
    caption_resp = client.responses.create(model="gpt-4.1-mini", input=caption_prompt)
    raw_caption = caption_resp.output_text.strip()
    phrases = [l.strip() for l in raw_caption.split('\n') if l.strip()][:3]
    caption = phrases[0] if phrases else raw_caption.strip('"').strip("'")
    logger.info(f"Pet phrases (text-only update): {phrases}")

    pet_state["pet_caption"] = caption
    pet_state["pet_phrases"] = phrases
    if event_text:
        pet_state["event_of_day"] = event_text
    _upsert_slot(user, slot_id, {"pet_state": pet_state})
    return {"pet_caption": caption, "pet_phrases": phrases}


# ==================== INGEST ENDPOINT (ESP32 -> Server) — NO AUTH ====================
class SensorReading(BaseModel):
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[int] = None
    soil_raw: Optional[int] = None
    soil_status: Optional[str] = None

@app.post("/api/ingest")
def api_ingest(reading: SensorReading, x_api_key: str = Header(alias="X-API-Key")):
    """Receive data from ESP32 via POST (no JWT, uses its own API key)."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")
    _sb().table("sensor_readings").insert({
        "temperature": reading.temperature,
        "humidity": reading.humidity,
        "soil_moisture": reading.soil_moisture,
        "soil_raw": reading.soil_raw,
        "soil_status": reading.soil_status,
    }).execute()
    return {"ok": True, "message": "Leitura registrada"}

# ==================== PUMP CONTROL (via Supabase pump_commands table) ====================
@app.post("/api/water")
def api_water(seconds: int = 5, user: str = Depends(get_current_user)):
    """Queue watering command for ESP32."""
    if not (1 <= seconds <= 30):
        raise HTTPException(status_code=400, detail="seconds deve ser entre 1 e 30")

    sb = _sb()
    # Cooldown: check last executed pump command
    last_result = sb.table("pump_commands").select("executed_at").eq("status", "done").order("executed_at", desc=True).limit(1).execute()
    if last_result.data and last_result.data[0].get("executed_at"):
        executed_at = datetime.fromisoformat(last_result.data[0]["executed_at"].replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - executed_at).total_seconds()
        cooldown = 300
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            raise HTTPException(
                status_code=429,
                detail=f"Aguarde {remaining}s antes de regar novamente (proteção da planta)."
            )

    sb.table("pump_commands").insert({
        "seconds": seconds,
        "requested_by": user,
        "status": "pending"
    }).execute()

    return {"ok": True, "seconds": seconds, "pending": True,
            "message": f"Comando de {seconds}s enfileirado. ESP32 executara em ate 10s."}

# ==================== ESP32 COMMANDS (no JWT, uses API key) ====================
@app.get("/api/commands")
def api_get_commands(x_api_key: str = Header(alias="X-API-Key")):
    """ESP32 fetches pending commands after each data send."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")

    sb = _sb()
    result = sb.table("pump_commands").select("*").eq("status", "pending").order("requested_at", desc=True).limit(1).execute()

    if result.data:
        cmd = result.data[0]
        requested_at = datetime.fromisoformat(cmd["requested_at"].replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - requested_at).total_seconds()
        if age_seconds > 60:
            # Expire stale command
            sb.table("pump_commands").update({"status": "expired"}).eq("id", cmd["id"]).execute()
            logger.warning(f"[PUMP] Command expired ({age_seconds:.0f}s), discarded.")
            return {"pump_seconds": 0}
        return {"pump_seconds": cmd["seconds"]}

    return {"pump_seconds": 0}

@app.post("/api/commands/done")
def api_commands_done(x_api_key: str = Header(alias="X-API-Key")):
    """ESP32 confirms command execution."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")

    sb = _sb()
    result = sb.table("pump_commands").select("*").eq("status", "pending").order("requested_at", desc=True).limit(1).execute()

    if result.data:
        cmd = result.data[0]
        sb.table("pump_commands").update({
            "status": "done",
            "executed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", cmd["id"]).execute()
        logger.info(f"[PUMP] Watering confirmed by ESP32: {cmd['seconds']}s")

    return {"ok": True}

# ==================== SOIL CALIBRATION ENDPOINTS ====================
@app.post("/api/calibrate/soil/save")
def api_calibrate_soil_save(user: str = Depends(get_current_user)):
    """Save current soil_raw reading as soaked reference (100%)."""
    sb = _sb()
    result = sb.table("sensor_readings").select("soil_raw").order("timestamp", desc=True).limit(1).execute()
    row = result.data[0] if result.data else None
    if not row or row['soil_raw'] is None:
        raise HTTPException(status_code=400, detail="Sem leitura de ADC disponivel. Verifique se o ESP32 esta enviando dados.")
    soaked_adc = row['soil_raw']
    existing = _load_soil_cal(user)
    dry_adc = existing.get('dry_adc', 3000)
    if soaked_adc >= dry_adc - 100:
        raise HTTPException(status_code=400, detail=f"ADC lido ({soaked_adc}) esta muito proximo do valor seco ({dry_adc}). Verifique o sensor.")
    cal = {
        'soaked_adc': soaked_adc,
        'dry_adc': dry_adc,
        'calibrated_at': datetime.now(timezone.utc).isoformat()
    }
    _save_soil_cal(user, cal)
    return {"ok": True, "soaked_adc": soaked_adc, "dry_adc": dry_adc}

@app.get("/api/calibrate/soil")
def api_get_soil_cal(user: str = Depends(get_current_user)):
    cal = _load_soil_cal(user)
    return {"ok": True, "calibration": cal}

# ==================== API ENDPOINTS (protected) ====================
@app.get("/api/current")
def api_current(user: str = Depends(get_current_user)):
    sb = _sb()
    result = sb.table("sensor_readings").select("*").order("timestamp", desc=True).limit(1).execute()
    row = result.data[0] if result.data else None
    if not row:
        return {"ok": False}

    ideal = _get_ideal(user)
    cal = _load_soil_cal(user)

    # Smooth soil using user calibration if available
    if cal.get('soaked_adc'):
        soil_rows = sb.table("sensor_readings").select("soil_raw").order("timestamp", desc=True).limit(SOIL_WINDOW).execute()
        soil_vals = [v for r in soil_rows.data if (v := _recalc_soil_pct(r["soil_raw"], cal)) is not None]
    else:
        soil_rows = sb.table("sensor_readings").select("soil_moisture").order("timestamp", desc=True).limit(SOIL_WINDOW).execute()
        soil_vals = [r["soil_moisture"] for r in soil_rows.data if r["soil_moisture"] is not None]
    soil_smoothed = round(sum(soil_vals) / len(soil_vals), 1) if soil_vals else (row["soil_moisture"] or 0)

    # Trend data (last 30 minutes)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    trend_result = sb.table("sensor_readings").select("temperature, humidity, soil_moisture, soil_raw").gte("timestamp", cutoff).order("timestamp").execute()
    trend_rows = trend_result.data

    temps = [r["temperature"] for r in trend_rows if r["temperature"] is not None]
    hums = [r["humidity"] for r in trend_rows if r["humidity"] is not None]
    if cal.get('soaked_adc'):
        soils = [v for r in trend_rows if (v := _recalc_soil_pct(r["soil_raw"], cal)) is not None]
    else:
        soils = [r["soil_moisture"] for r in trend_rows if r["soil_moisture"] is not None]

    t = row["temperature"] or 0
    h = row["humidity"] or 0
    s_trend = _trend(soils)
    hs = _health(t, h, soil_smoothed, user)

    # Connection status
    last_ts = row["timestamp"]
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        is_connected = (datetime.now(timezone.utc) - dt).total_seconds() < 30
    except Exception:
        is_connected = True

    # Last pump time
    last_pump = sb.table("pump_commands").select("executed_at").eq("status", "done").order("executed_at", desc=True).limit(1).execute()
    last_pump_at = last_pump.data[0]["executed_at"] if last_pump.data else None

    return {
        "ok": True,
        "temperature": round(t, 1),
        "humidity": round(h, 1),
        "soil_raw": row["soil_raw"] or 0,
        "soil_smoothed": soil_smoothed,
        "soil_status": row["soil_status"] or "",
        "timestamp": row["timestamp"],
        "trends": {
            "temperature": _trend(temps),
            "humidity": _trend(hums),
            "soil": s_trend,
        },
        "health": {"score": hs, "label": _health_label(hs)},
        "irrigation": _irrigation(soil_smoothed, s_trend, user),
        "connected": is_connected,
        "last_ok": last_ts,
        "ideal": ideal,
        "last_pump_at": last_pump_at,
    }

@app.get("/api/history")
def api_history(hours: int = Query(default=24, ge=1, le=168), user: str = Depends(get_current_user)):
    sb = _sb()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = sb.table("sensor_readings").select("timestamp, temperature, humidity, soil_moisture, soil_raw").gte("timestamp", cutoff).order("timestamp").execute()
    rows = result.data

    if not rows:
        return {"timestamps": [], "temperature": [], "humidity": [], "soil_raw": [], "soil_smoothed": [], "count": 0}

    cal = _load_soil_cal(user)
    ts = [r["timestamp"] for r in rows]
    temp = [round(r["temperature"], 1) if r["temperature"] is not None else None for r in rows]
    hum = [round(r["humidity"], 1) if r["humidity"] is not None else None for r in rows]

    if cal.get('soaked_adc'):
        soil_pct = [_recalc_soil_pct(r["soil_raw"], cal) if r["soil_raw"] is not None else r["soil_moisture"] for r in rows]
    else:
        soil_pct = [r["soil_moisture"] for r in rows]
    soil_smooth = _smooth_soil([s if s is not None else 0 for s in soil_pct])

    return {
        "timestamps": ts,
        "temperature": temp,
        "humidity": hum,
        "soil_raw": soil_pct,
        "soil_smoothed": soil_smooth,
        "count": len(rows),
    }

@app.get("/api/stats")
def api_stats(hours: int = Query(default=24, ge=1, le=168), user: str = Depends(get_current_user)):
    sb = _sb()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = sb.table("sensor_readings").select("*").gte("timestamp", cutoff).order("timestamp").execute()
    rows = result.data

    if not rows:
        return {"ok": False}

    temps = [r["temperature"] for r in rows if r["temperature"] is not None]
    hums = [r["humidity"] for r in rows if r["humidity"] is not None]
    soils = [r["soil_moisture"] for r in rows if r["soil_moisture"] is not None]

    if not temps or not hums or not soils:
        return {"ok": False}

    # Calculate stats manually (Supabase doesn't support aggregation in client)
    temp_avg = sum(temps) / len(temps)
    temp_min = min(temps)
    temp_max = max(temps)
    hum_avg = sum(hums) / len(hums)
    hum_min = min(hums)
    hum_max = max(hums)
    soil_avg = sum(soils) / len(soils)
    soil_min = min(soils)
    soil_max = max(soils)

    # Soil std
    if len(soils) > 1:
        _mean_raw = sum(soils) / len(soils)
        soil_std_raw = round((sum((x - _mean_raw) ** 2 for x in soils) / len(soils)) ** 0.5, 2)
    else:
        soil_std_raw = 0
    soil_smooth = _smooth_soil(soils)
    if len(soil_smooth) > 1:
        _mean_smooth = sum(soil_smooth) / len(soil_smooth)
        soil_std_smooth = round((sum((x - _mean_smooth) ** 2 for x in soil_smooth) / len(soil_smooth)) ** 0.5, 2)
    else:
        soil_std_smooth = 0

    # Hourly aggregation
    hourly: dict[int, dict] = {}
    for r in rows:
        try:
            h = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).hour
        except Exception:
            continue
        if h not in hourly:
            hourly[h] = {"temps": [], "hums": [], "soils": []}
        if r["temperature"] is not None:
            hourly[h]["temps"].append(r["temperature"])
        if r["humidity"] is not None:
            hourly[h]["hums"].append(r["humidity"])
        if r["soil_moisture"] is not None:
            hourly[h]["soils"].append(r["soil_moisture"])

    sorted_hours = sorted(hourly.keys())

    ideal = _get_ideal(user)

    return {
        "ok": True,
        "temperature": {
            "avg": round(temp_avg, 1), "min": round(temp_min, 1), "max": round(temp_max, 1),
            "in_range": ideal["temp"][0] <= temp_avg <= ideal["temp"][1],
        },
        "humidity": {
            "avg": round(hum_avg, 1), "min": round(hum_min, 1), "max": round(hum_max, 1),
            "in_range": ideal["humidity"][0] <= hum_avg <= ideal["humidity"][1],
        },
        "soil": {
            "avg": round(soil_avg, 1), "min": round(soil_min, 1), "max": round(soil_max, 1),
            "in_range": ideal["soil"][0] <= soil_avg <= ideal["soil"][1],
            "std_raw": soil_std_raw,
            "std_smooth": soil_std_smooth,
        },
        "total_readings": len(rows),
        "hourly": {
            "hours": sorted_hours,
            "temperature": [round(sum(hourly[h]["temps"]) / len(hourly[h]["temps"]), 1) if hourly[h]["temps"] else 0 for h in sorted_hours],
            "humidity": [round(sum(hourly[h]["hums"]) / len(hourly[h]["hums"]), 1) if hourly[h]["hums"] else 0 for h in sorted_hours],
            "soil": [round(sum(hourly[h]["soils"]) / len(hourly[h]["soils"]), 1) if hourly[h]["soils"] else 0 for h in sorted_hours],
        },
    }

# ==================== CRON ENDPOINTS (Vercel Cron Jobs) ====================
def _verify_cron_secret(request: Request):
    """Verify that cron requests come from Vercel."""
    auth = request.headers.get("Authorization", "")
    if not CRON_SECRET or auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

@app.post("/api/cron/pet-images")
def cron_pet_images(request: Request):
    """Generate pet images for all users. Called by Vercel Cron."""
    _verify_cron_secret(request)
    sb = _sb()
    users = sb.table("users").select("email").eq("is_verified", True).execute()
    results = []

    for user_row in users.data:
        user_email = user_row['email']
        try:
            slot_id = _get_active_slot(user_email)
            pet_config = _get_slot_field(user_email, slot_id, "pet_config")
            plant_profile = _get_slot_field(user_email, slot_id, "plant_profile")

            if pet_config and plant_profile:
                # Queue job via pet_jobs table (Edge Function will process)
                sb.table("pet_jobs").insert({
                    "user_email": user_email,
                    "slot_id": slot_id,
                    "job_type": "image",
                    "status": "pending"
                }).execute()
                results.append({"user": user_email, "status": "queued"})

                # Weekly email reminder check
                prefs = _get_user_prefs(user_email)
                safe = _sanitize_email(user_email)
                plant_photo_exists = _storage_exists("plant-photos", f"{safe}/{slot_id}/plant_photo.jpg")
                if plant_photo_exists:
                    last_reminder = prefs.get("last_reminder_time")
                    should_send = True
                    if last_reminder:
                        try:
                            lr = datetime.fromisoformat(last_reminder.replace("Z", "+00:00"))
                            if (datetime.now(timezone.utc) - lr).total_seconds() < 7 * 86400:
                                should_send = False
                        except Exception:
                            pass
                    if should_send:
                        pet_name = pet_config.get("name", "Pet")
                        plant_name = plant_profile.get("nome_popular", "sua planta")
                        logger.info(f"Cron: Sending weekly reminder to {user_email}")
                        send_weekly_photo_reminder_email(user_email, plant_name, pet_name)
                        sb.table("user_prefs").upsert({
                            "user_email": user_email,
                            "last_reminder_time": datetime.now(timezone.utc).isoformat()
                        }).execute()
        except Exception as e:
            results.append({"user": user_email, "status": "error", "error": str(e)})
            logger.error(f"Cron pet-images failed for {user_email}: {e}")

    return {"ok": True, "results": results}

@app.post("/api/cron/pet-phrases")
def cron_pet_phrases(request: Request):
    """Generate pet phrases for all users. Called by Vercel Cron."""
    _verify_cron_secret(request)
    sb = _sb()
    users = sb.table("users").select("email").eq("is_verified", True).execute()
    results = []

    for user_row in users.data:
        user_email = user_row['email']
        try:
            slot_id = _get_active_slot(user_email)
            pet_config = _get_slot_field(user_email, slot_id, "pet_config")
            plant_profile = _get_slot_field(user_email, slot_id, "plant_profile")

            if pet_config and plant_profile:
                generate_pet_phrases(user=user_email)
                results.append({"user": user_email, "status": "done"})
        except Exception as e:
            results.append({"user": user_email, "status": "error", "error": str(e)})
            logger.error(f"Cron pet-phrases failed for {user_email}: {e}")

    return {"ok": True, "results": results}

@app.post("/api/admin/test-email")
def api_test_email(user: str = Depends(get_current_user)):
    """Send test email with new template."""
    try:
        send_verification_email(user, "123456")
        return {"ok": True, "message": f"Email de teste enviado para {user}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
