"""
Hoya Pet App — FastAPI backend.
- JWT authentication (login via .env credentials)
- Plant photo upload + AI identification via GPT-5.2
- Pet configuration (cat/dog + name)
- Hourly pet image generation via OpenAI Images API (chatgpt-image-latest)
- Sensor data API (current, history, stats)
- ESP32 ingest endpoint (unchanged, no auth required)
"""
import os, json, base64, sqlite3, threading, time, logging, hmac, hashlib, random
import requests as http_requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Header, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from jose import jwt, JWTError

# ==================== LOAD .env ====================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOGIN_EMAIL = os.getenv("LOGIN_EMAIL", "pedro@email.com")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "senha123")
JWT_SECRET = os.getenv("JWT_SECRET", "hoya-pet-secret-key-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

SMTP_USER = os.getenv("EMAIL_SMTP", os.getenv("SMTP_USER", ""))
SMTP_PASSWORD = os.getenv("APP_PASSWORD_SMTP", os.getenv("SMTP_PASSWORD", ""))

# Rate limiting for login
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_login_attempts: dict[str, list[float]] = {}

# ==================== CONFIG ====================
ESP32_IP = "http://192.168.101.200"
DB_NAME = "sensor_data.db"
COLLECT_INTERVAL = 5
AUTH_ESP32 = (os.getenv("ESP32_USER", "admin"), os.getenv("ESP32_PASS", "changeme"))
SOIL_WINDOW = 10
EXTERNAL_COLLECTOR = True
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "change-me-in-env")
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
# Pet image generation schedule — 2x per day at business hours (America/Sao_Paulo)
PET_GENERATION_HOURS = {9, 15}    # image + text: 09:00 and 15:00
PET_TEXT_HOURS = {9, 13, 17, 21}  # text only (phrases): every ~4h during the day
MAX_PLANT_SLOTS = 5
USERS_DIR = DATA_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)
# Legacy path (kept for migration only)
PLANTS_DIR = DATA_DIR / "plants"

# Fila de comandos para o ESP32 (ESP32 busca via GET /api/commands)
PENDING_PUMP_FILE = DATA_DIR / "pending_pump.json"
LAST_PUMP_FILE = DATA_DIR / "last_pump.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hoya-pet")

# ==================== PER-USER DATA HELPERS ====================
import re

def _sanitize_email(email: str) -> str:
    """Convert email to a safe directory name."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', email.lower().strip())

def _user_dir(user: str) -> Path:
    """Return per-user data directory: data/users/{sanitized_email}/"""
    d = USERS_DIR / _sanitize_email(user)
    d.mkdir(parents=True, exist_ok=True)
    return d

def _user_plants_dir(user: str) -> Path:
    """Return per-user plants directory: data/users/{sanitized_email}/plants/"""
    d = _user_dir(user) / "plants"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _get_user_prefs(user: str) -> dict:
    prefs_path = _user_dir(user) / "user_prefs.json"
    prefs = _load_json(prefs_path)
    if not prefs:
        prefs = {"active_slot": 1}
        _save_json(prefs_path, prefs)
    return prefs

def _get_active_slot(user: str) -> int:
    return _get_user_prefs(user).get("active_slot", 1)

# ==================== CALIBRACAO DO SOLO ====================
def _load_soil_cal(user: str) -> dict:
    try:
        with open(_user_dir(user) / "soil_cal.json") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_soil_cal(user: str, cal: dict):
    _save_json(_user_dir(user) / "soil_cal.json", cal)

def _recalc_soil_pct(raw_adc: int, cal: dict):
    """Recalcula % umidade do solo usando calibracao do usuario.
    Defaults baseados no sensor capacitivo v1.2 calibrado em 2026-02:
      dry_adc=2970 (seco no ar), soaked_adc=1320 (na agua).
    """
    dry = cal.get('dry_adc', 2970)
    wet = cal.get('soaked_adc', 1320)
    if dry <= wet or raw_adc is None:
        return None
    pct = (dry - raw_adc) / (dry - wet) * 100
    return max(0, min(100, round(pct)))

def _set_active_slot(user: str, slot_id: int):
    prefs = _get_user_prefs(user)
    prefs["active_slot"] = slot_id
    _save_json(_user_dir(user) / "user_prefs.json", prefs)

def _slot_dir(user: str, slot_id: int) -> Path:
    d = _user_plants_dir(user) / str(slot_id)
    d.mkdir(parents=True, exist_ok=True)
    return d

def _list_slots(user: str) -> list[dict]:
    """Return list of configured plant slots with metadata for a specific user."""
    slots = []
    plants_dir = _user_plants_dir(user)
    if not plants_dir.exists():
        return slots
    for d in sorted(plants_dir.iterdir()):
        if d.is_dir() and d.name.isdigit():
            profile = _load_json(d / "plant_profile.json")
            pet_cfg = _load_json(d / "pet_config.json")
            if profile or pet_cfg:
                slots.append({
                    "id": int(d.name),
                    "plant_name": profile.get("nome_popular", "?") if profile else "?",
                    "plant_scientific": profile.get("nome_cientifico", "") if profile else "",
                    "pet_name": pet_cfg.get("name", "?") if pet_cfg else "?",
                    "pet_type": pet_cfg.get("type", "cat") if pet_cfg else "cat",
                    "has_photo": (d / "plant_photo.jpg").exists(),
                })
    return slots

def _next_slot_id(user: str) -> int:
    plants_dir = _user_plants_dir(user)
    existing = [int(d.name) for d in plants_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    return max(existing, default=0) + 1

def _migrate_legacy_data():
    """Move old flat-file data (shared plants/) into the admin user's directory."""
    import shutil
    legacy_files = ["plant_profile.json", "pet_config.json", "pet_state.json", "pet_current.png", "plant_photo.jpg"]

    # Migrate from data/ flat files to admin user
    admin_email = LOGIN_EMAIL.lower().strip()
    if not admin_email:
        return

    has_legacy_flat = any((DATA_DIR / f).exists() for f in legacy_files)
    if has_legacy_flat:
        slot1 = _slot_dir(admin_email, 1)
        already_migrated = any((slot1 / f).exists() for f in legacy_files)
        if not already_migrated:
            logger.info(f"Migrating legacy flat-file data to user {admin_email}/plants/1/")
            for f in legacy_files:
                src = DATA_DIR / f
                if src.exists():
                    shutil.copy2(str(src), str(slot1 / f))
                    logger.info(f"  Copied data/{f} -> users/{_sanitize_email(admin_email)}/plants/1/{f}")
            _set_active_slot(admin_email, 1)
            logger.info("Flat-file migration complete.")

    # Migrate from shared data/plants/ to admin user
    if PLANTS_DIR.exists() and any(PLANTS_DIR.iterdir()):
        user_plants = _user_plants_dir(admin_email)
        for d in sorted(PLANTS_DIR.iterdir()):
            if d.is_dir() and d.name.isdigit():
                target = user_plants / d.name
                if not target.exists():
                    shutil.copytree(str(d), str(target))
                    logger.info(f"  Migrated plants/{d.name}/ -> users/{_sanitize_email(admin_email)}/plants/{d.name}/")
        # Migrate user_prefs.json
        old_prefs = _load_json(DATA_DIR / "user_prefs.json")
        if old_prefs:
            new_prefs_path = _user_dir(admin_email) / "user_prefs.json"
            if not new_prefs_path.exists():
                _save_json(new_prefs_path, old_prefs)
        logger.info("Shared plants/ migration complete.")


# ==================== APP ====================
app = FastAPI(title="Hoya Pet App API", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

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

LOGO_PATH = Path(__file__).parent / "frontend" / "public" / "pixel_hoya_logo.png"

def _email_template(title: str, greeting: str, body_html: str) -> str:
    """Template de email com visual Hoya Pet (parchment + madeira)."""
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

  <!-- Header com logo e título -->
  <tr>
    <td style="background:linear-gradient(180deg,#5a8a3c 0%,#3d5a1e 100%);padding:28px 32px;text-align:center;border-bottom:4px solid #2d4a15;">
      <img src="http://163.176.169.232/pixel_hoya_logo.png" alt="Hoya Pet" width="72" height="72"
           style="image-rendering:pixelated;display:block;margin:0 auto 12px;" />
      <h1 style="color:#ffffff;margin:0;font-size:18px;font-weight:700;letter-spacing:1px;
                 text-shadow:1px 1px 0 #1a3008;font-family:'Courier New',monospace;">
        {title}
      </h1>
    </td>
  </tr>

  <!-- Corpo -->
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
    """Send an HTML email with the Hoya Pet logo attached via direct URL."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP credenciais ausentes no .env. Ignorando envio.")
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
        logger.info(f"Email enviado para {to_email}")
    except Exception as e:
        logger.error(f"Erro enviando email SMTP: {e}")

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
            <a href="http://163.176.169.232"
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

# ==================== DATABASE ====================
def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    
    # 1. Telemetry table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperature REAL,
            humidity REAL,
            soil_moisture INTEGER,
            soil_raw INTEGER,
            soil_status TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ts ON sensor_readings(timestamp)')

    # 2. Users table (Multi-tenant authentication)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_verified BOOLEAN DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 3. Email verification codes table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS email_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at DATETIME NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email)')
    
    # Ensure standard admin from .env exists if table is empty
    admin = conn.execute('SELECT id FROM users WHERE email = ?', (LOGIN_EMAIL.lower().strip(),)).fetchone()
    if not admin and LOGIN_EMAIL and LOGIN_PASSWORD:
        try:
            conn.execute('INSERT INTO users (email, password_hash, is_verified) VALUES (?, ?, 1)', 
                         (LOGIN_EMAIL.lower().strip(), hash_password(LOGIN_PASSWORD)))
        except sqlite3.IntegrityError:
            pass
            
    conn.commit()
    conn.close()

# ==================== JWT AUTH ====================
def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.utcnow(),
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

# ==================== DATA COLLECTOR ====================
class Collector:
    def __init__(self):
        self.running = False
        self.connected = False
        self.last_ok = None

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                r = http_requests.get(f"{ESP32_IP}/data", auth=AUTH_ESP32, timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    conn = get_db()
                    conn.execute(
                        'INSERT INTO sensor_readings (temperature,humidity,soil_moisture,soil_raw,soil_status) VALUES (?,?,?,?,?)',
                        (d.get('temperature'), d.get('humidity'), d.get('soil_moisture'), d.get('soil_raw'), d.get('soil_status'))
                    )
                    conn.commit()
                    conn.close()
                    self.connected = True
                    self.last_ok = datetime.now().isoformat()
                else:
                    self.connected = False
            except Exception:
                self.connected = False
            time.sleep(COLLECT_INTERVAL)

collector = Collector()

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
    """Get ideal ranges — from AI profile if available, otherwise defaults."""
    if user:
        slot = _slot_dir(user, _get_active_slot(user))
        profile_path = slot / "plant_profile.json"
    else:
        profile_path = DATA_DIR / "plant_profile.json"
    if profile_path.exists():
        try:
            p = json.loads(profile_path.read_text())
            return {
                "temp": (p.get("temperatura_ideal_min", 18), p.get("temperatura_ideal_max", 28)),
                "humidity": (p.get("umidade_ar_ideal_min", 40), p.get("umidade_ar_ideal_max", 70)),
                "soil": (p.get("umidade_solo_ideal_min", 15), p.get("umidade_solo_ideal_max", 55)),
            }
        except Exception:
            pass
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

def _read_file_b64(path: Path) -> str:
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode()
    return ""

def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}

def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

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
    # Remove old attempts outside window
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS

@app.post("/api/login")
def api_login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    # Rate limiting
    if _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Muitas tentativas. Tente novamente em {LOGIN_WINDOW_SECONDS // 60} minutos."
        )

    email_clean = req.email.lower().strip()
    conn = get_db()
    user = conn.execute('SELECT password_hash, is_verified FROM users WHERE email = ?', (email_clean,)).fetchone()
    conn.close()

    if user and verify_password(req.password, user['password_hash']):
        if not user['is_verified']:
            # Prevent login without verification
            raise HTTPException(status_code=403, detail="verification_required")
            
        # Clear attempts on success
        _login_attempts.pop(client_ip, None)
        token = create_token(email_clean)
        return {"ok": True, "token": token, "email": email_clean}

    # Record failed attempt
    _login_attempts.setdefault(client_ip, []).append(time.time())
    remaining = LOGIN_MAX_ATTEMPTS - len(_login_attempts.get(client_ip, []))
    raise HTTPException(status_code=401, detail=f"Credenciais invalidas ({remaining} tentativas restantes)")

@app.post("/api/register")
def api_register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    # Use same rate limit bucket to prevent spam
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas.")

    email_clean = req.email.lower().strip()
    if not email_clean or "@" not in email_clean or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="E-mail invalido ou senha muito curta (min = 6).")

    conn = get_db()
    
    # Check if already exists and is verified
    existing = conn.execute('SELECT is_verified FROM users WHERE email = ?', (email_clean,)).fetchone()
    if existing and existing['is_verified']:
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail ja esta em uso e verificado.")
        
    import random
    code = f"{random.randint(0, 999999):06d}"
    expires = datetime.now() + timedelta(minutes=15)
    
    try:
        if not existing:
            # New user
            conn.execute('INSERT INTO users (email, password_hash, is_verified) VALUES (?, ?, 0)', 
                         (email_clean, hash_password(req.password)))
        else:
            # Overwrite unverified user password
            conn.execute('UPDATE users SET password_hash = ? WHERE email = ?', 
                         (hash_password(req.password), email_clean))
            
        # Delete old codes and insert new one
        conn.execute('DELETE FROM email_codes WHERE email = ?', (email_clean,))
        conn.execute('INSERT INTO email_codes (email, code, expires_at) VALUES (?, ?, ?)',
                     (email_clean, code, expires.isoformat()))
                     
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Erro ao registrar.")
    conn.close()

    # Log for debugging if SMTP falls back
    logger.info(f"Codigo gerado para {email_clean}: {code}")
    
    # Send actual email in background thread to prevent blocking the HTTP response
    threading.Thread(target=send_verification_email, args=(email_clean, code)).start()

    return {"ok": True, "message": "Codigo enviado para o e-mail."}

@app.post("/api/verify")
def api_verify(req: VerifyRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas.")

    email_clean = req.email.lower().strip()
    code_clean = req.code.strip()
    
    conn = get_db()
    row = conn.execute('SELECT code, expires_at FROM email_codes WHERE email = ? ORDER BY id DESC LIMIT 1', 
                       (email_clean,)).fetchone()
                       
    if not row or row['code'] != code_clean:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        conn.close()
        raise HTTPException(status_code=400, detail="Codigo invalido ou incorreto.")
        
    expires = datetime.fromisoformat(row['expires_at'])
    if datetime.now() > expires:
        conn.close()
        raise HTTPException(status_code=400, detail="Codigo expirado. Cadastre-se novamente.")
        
    # Success: verify user
    conn.execute('UPDATE users SET is_verified = 1 WHERE email = ?', (email_clean,))
    conn.execute('DELETE FROM email_codes WHERE email = ?', (email_clean,))
    conn.commit()
    conn.close()
    
    # Clear attempts
    _login_attempts.pop(client_ip, None)
    
    # Log them in automatically
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

    conn = get_db()
    existing = conn.execute('SELECT is_verified FROM users WHERE email = ?', (email_clean,)).fetchone()
    
    # We always return success to avoid leaking which emails exist, unless there's an internal error
    if existing and existing['is_verified']:
        import random
        code = f"{random.randint(0, 999999):06d}"
        expires = datetime.now() + timedelta(minutes=15)
        
        try:
            conn.execute('DELETE FROM email_codes WHERE email = ?', (email_clean,))
            conn.execute('INSERT INTO email_codes (email, code, expires_at) VALUES (?, ?, ?)',
                         (email_clean, code, expires.isoformat()))
            conn.commit()
            
            logger.info(f"Codigo de reset gerado para {email_clean}: {code}")
            threading.Thread(target=send_password_reset_email, args=(email_clean, code)).start()
        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao gerar codigo de reset: {e}")
            raise HTTPException(status_code=500, detail="Erro interno.")
    
    conn.close()
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

    conn = get_db()
    
    # Check if user exists and is verified
    user = conn.execute('SELECT id, is_verified FROM users WHERE email = ?', (email_clean,)).fetchone()
    if not user or not user['is_verified']:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        conn.close()
        raise HTTPException(status_code=400, detail="Usuário não encontrado ou não verificado.")

    row = conn.execute('SELECT code, expires_at FROM email_codes WHERE email = ? ORDER BY id DESC LIMIT 1', 
                       (email_clean,)).fetchone()
                       
    if not row or row['code'] != code_clean:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        conn.close()
        raise HTTPException(status_code=400, detail="Codigo invalido ou incorreto.")
        
    expires = datetime.fromisoformat(row['expires_at'])
    if datetime.now() > expires:
        conn.close()
        raise HTTPException(status_code=400, detail="Codigo expirado. Solicite um novo código.")
        
    # Success: update password
    try:
        conn.execute('UPDATE users SET password_hash = ? WHERE email = ?', 
                    (hash_password(req.new_password), email_clean))
        conn.execute('DELETE FROM email_codes WHERE email = ?', (email_clean,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Erro ao redefinir a senha.")
        
    conn.close()
    
    # Clear attempts
    _login_attempts.pop(client_ip, None)
    
    # Log them in automatically
    token = create_token(email_clean)
    return {"ok": True, "token": token, "email": email_clean}

@app.get("/api/auth/check")
def api_auth_check(user: str = Depends(get_current_user)):
    return {"ok": True, "email": user}

# ==================== SETUP ENDPOINTS ====================
@app.get("/api/setup/status")
def api_setup_status(user: str = Depends(get_current_user)):
    slot = _slot_dir(user, _get_active_slot(user))
    plant_ok = (slot / "plant_profile.json").exists()
    pet_ok = (slot / "pet_config.json").exists()
    return {"ok": True, "plant_configured": plant_ok, "pet_configured": pet_ok, "setup_complete": plant_ok and pet_ok}

@app.post("/api/setup/plant")
async def api_setup_plant(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    """Upload plant photo and identify species via GPT-5.2."""
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (max 10MB)")

    # Save photo to active slot
    slot = _slot_dir(user, _get_active_slot(user))
    photo_path = slot / "plant_photo.jpg"
    photo_path.write_bytes(contents)

    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY nao configurada no .env")

    # Analyze with GPT-5.2
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

    # Parse AI response
    raw = response.output_text.strip()
    # Clean markdown fences if present
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

    _save_json(slot / "plant_profile.json", profile)
    return {"ok": True, "profile": profile}

@app.post("/api/setup/plant-photo")
async def api_update_plant_photo(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    """Update plant photo and re-analyze."""
    return await api_setup_plant(file=file, user=user)

@app.get("/api/plant-profile")
def api_plant_profile(user: str = Depends(get_current_user)):
    slot = _slot_dir(user, _get_active_slot(user))
    profile = _load_json(slot / "plant_profile.json")
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
    slot = _slot_dir(user, _get_active_slot(user))
    config = {"name": req.name, "type": req.type, "created_at": datetime.now().isoformat()}
    _save_json(slot / "pet_config.json", config)
    return {"ok": True, "config": config}

@app.get("/api/pet/config")
def api_pet_config(user: str = Depends(get_current_user)):
    slot = _slot_dir(user, _get_active_slot(user))
    config = _load_json(slot / "pet_config.json")
    if not config:
        return {"ok": False}
    return {"ok": True, "config": config}

@app.get("/api/pet/current")
def api_pet_current(user: str = Depends(get_current_user)):
    slot = _slot_dir(user, _get_active_slot(user))
    pet_path = slot / "pet_current.png"
    state = _load_json(slot / "pet_state.json")
    if pet_path.exists():
        img_b64 = _read_file_b64(pet_path)
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
    """Manually trigger pet image generation."""
    try:
        result = generate_pet_image(user=user)
        return {"ok": True, "message": "Pet gerado com sucesso", "details": result}
    except Exception as e:
        logger.error(f"Pet generation failed: {e}")
        raise HTTPException(500, f"Falha na geracao: {str(e)}")

# ==================== MULTI-PLANT ENDPOINTS ====================
@app.get("/api/plants")
def api_list_plants(user: str = Depends(get_current_user)):
    slots = _list_slots(user)
    active = _get_active_slot(user)
    # Include pet image thumbnail for carousel
    plants_dir = _user_plants_dir(user)
    for s in slots:
        slot_id = s["id"]
        pet_img_path = plants_dir / str(slot_id) / "pet_current.png"
        plant_img_path = plants_dir / str(slot_id) / "plant_photo.jpg"
        s["has_pet_image"] = pet_img_path.exists()
        s["plant_photo_url"] = f"/api/plants/{slot_id}/photo" if plant_img_path.exists() else None
        s["pet_image_url"] = f"/api/plants/{slot_id}/pet-image" if pet_img_path.exists() else None
    return {"ok": True, "plants": slots, "active_slot": active}

@app.post("/api/plants")
def api_create_plant(user: str = Depends(get_current_user)):
    slots = _list_slots(user)
    if len(slots) >= MAX_PLANT_SLOTS:
        raise HTTPException(400, f"Maximo de {MAX_PLANT_SLOTS} plantas atingido")
    new_id = _next_slot_id(user)
    _slot_dir(user, new_id)  # Create directory
    _set_active_slot(user, new_id)
    return {"ok": True, "slot_id": new_id, "message": "Novo slot criado. Complete o setup."}

@app.get("/api/plants/{slot_id}/photo")
def get_plant_photo(slot_id: int, user: str = Depends(get_current_user)):
    path = _slot_dir(user, slot_id) / "plant_photo.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/jpeg")

@app.get("/api/plants/{slot_id}/pet-image")
def get_pet_image(slot_id: int, user: str = Depends(get_current_user)):
    path = _slot_dir(user, slot_id) / "pet_current.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/png")

@app.get("/api/plants/{slot_id}/switch")
def api_switch_plant(slot_id: int, user: str = Depends(get_current_user)):
    slot = _slot_dir(user, slot_id)
    if not slot.exists():
        raise HTTPException(404, "Slot nao encontrado")
    _set_active_slot(user, slot_id)
    return {"ok": True, "active_slot": slot_id}

@app.delete("/api/plants/{slot_id}")
def api_delete_plant(slot_id: int, user: str = Depends(get_current_user)):
    import shutil
    slot = _user_plants_dir(user) / str(slot_id)
    if not slot.exists():
        raise HTTPException(404, "Slot nao encontrado")
    configured_slots = _list_slots(user)
    remaining_after = [s for s in configured_slots if s["id"] != slot_id]
    if len(remaining_after) == 0:
        raise HTTPException(400, "Voce precisa ter pelo menos 1 planta configurada")
    shutil.rmtree(str(slot))
    # Switch to first remaining slot if we deleted the active one
    if _get_active_slot(user) == slot_id:
        remaining = _list_slots(user)
        if remaining:
            _set_active_slot(user, remaining[0]["id"])
    return {"ok": True, "message": f"Slot {slot_id} removido"}

# ==================== PET IMAGE GENERATION ====================
def generate_pet_image(timezone: str = "America/Sao_Paulo", user: str = None):
    """Core function: generates a new pet image using OpenAI Images API (chatgpt-image-latest)."""
    if not OPENAI_API_KEY:
        raise Exception("OPENAI_API_KEY nao configurada")
    if not user:
        raise Exception("Usuario nao especificado para geracao de pet")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Load configs from active slot (per-user)
    slot = _slot_dir(user, _get_active_slot(user))
    pet_config = _load_json(slot / "pet_config.json")
    plant_profile = _load_json(slot / "plant_profile.json")
    pet_state = _load_json(slot / "pet_state.json")

    if not pet_config:
        raise Exception("Pet nao configurado")

    pet_name = pet_config.get("name", "Mimi")
    pet_type = "cat" if pet_config.get("type") == "cat" else "dog"
    pet_type_pt = "gato" if pet_config.get("type") == "cat" else "cachorro"
    plant_name = plant_profile.get("nome_popular", "planta") if plant_profile else "planta"

    # Get current sensor data
    conn = get_db()
    row = conn.execute('SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 1').fetchone()
    conn.close()

    temp = row["temperature"] if row else 22
    hum = row["humidity"] if row else 50
    soil = row["soil_moisture"] if row else 30
    ideal = _get_ideal(user)
    health_score = _health(temp or 22, hum or 50, soil or 30, user)

    # Determine time of day
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone))
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

    # Determine pet action — pet always actively caring for the plant
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

    # Plant visual state
    if health_score >= 80:
        plant_visual = "super healthy — lush deep green leaves, sparkling water droplets, tiny glowing sparkle effects radiating vitality"
    elif health_score >= 50:
        plant_visual = "healthy and green, looking well and cared for"
    elif health_score >= 30:
        plant_visual = "wilting slightly, pale drooping leaves, clearly needing attention"
    else:
        plant_visual = "critically struggling — brown leaf tips, dramatically drooping, dry cracked soil, desperate for care"

    # Step 1: Web search for fun event of the day
    event_text = ""
    try:
        date_str = now.strftime("%d de %B de %Y")
        event_response = client.responses.create(
            model="gpt-5.2",
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
        event_text = ""

    # Step 2: Build image generation prompt — retro pixel art style
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

    # Step 3: Generate image via Responses API with multi-turn chaining for visual consistency
    pet_current_path = slot / "pet_current.png"
    previous_response_id = pet_state.get("last_response_id") if pet_state else None

    input_content = [{"type": "input_text", "text": creative_prompt}]

    # Pass previous pet image as visual reference — key for character consistency
    if pet_current_path.exists():
        pet_b64 = _read_file_b64(pet_current_path)
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{pet_b64}",
        })

    gen_kwargs = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": input_content}],
        "tools": [{"type": "image_generation", "quality": "medium", "size": "1024x1024", "background": "transparent"}],
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
        pet_current_path.write_bytes(base64.b64decode(image_data))
        logger.info("Pet image generated and saved")
    else:
        logger.warning("No image data in response")

    # Step 4: Generate creative caption (text only) — 3 phrases with personality
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
        caption_resp = client.responses.create(
            model="gpt-5.2",
            input=caption_prompt,
        )
        raw_caption = caption_resp.output_text.strip()
        phrases = [l.strip() for l in raw_caption.split('\n') if l.strip()][:3]
        caption = phrases[0] if phrases else raw_caption.strip('"').strip("'")
        logger.info(f"Pet phrases: {phrases}")
    except Exception as e:
        logger.warning(f"Caption generation failed: {e}")

    # Save state
    new_state = {
        "last_response_id": img_response.id,
        "last_prompt": creative_prompt,
        "generated_at": datetime.now().isoformat(),
        "event_of_day": event_text,
        "pet_caption": caption,
        "pet_phrases": phrases,
        "health_score": health_score,
        "sensor_data": {"temperature": temp, "humidity": hum, "soil": soil},
    }
    _save_json(slot / "pet_state.json", new_state)

    return new_state


def generate_pet_phrases(timezone: str = "America/Sao_Paulo", user: str = None):
    """Generate only caption/phrases for a user's active pet, without regenerating the image."""
    if not OPENAI_API_KEY:
        raise Exception("OPENAI_API_KEY nao configurada")
    if not user:
        raise Exception("Usuario nao especificado")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    slot = _slot_dir(user, _get_active_slot(user))
    pet_config = _load_json(slot / "pet_config.json")
    plant_profile = _load_json(slot / "plant_profile.json")
    pet_state = _load_json(slot / "pet_state.json") or {}

    if not pet_config:
        raise Exception("Pet nao configurado")

    pet_name = pet_config.get("name", "Mimi")
    pet_type = "cat" if pet_config.get("type") == "cat" else "dog"
    pet_type_pt = "gato" if pet_config.get("type") == "cat" else "cachorro"
    plant_name = plant_profile.get("nome_popular", "planta") if plant_profile else "planta"

    conn = get_db()
    row = conn.execute('SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 1').fetchone()
    conn.close()
    temp = row["temperature"] if row else 22
    hum = row["humidity"] if row else 50
    soil = row["soil_moisture"] if row else 30
    health_score = _health(temp or 22, hum or 50, soil or 30, user)

    # Web search for event of the day
    event_text = ""
    try:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(timezone))
        except Exception:
            now = datetime.now()
        date_str = now.strftime("%d de %B de %Y")
        event_response = client.responses.create(
            model="gpt-5.2",
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
    caption_resp = client.responses.create(model="gpt-5.2", input=caption_prompt)
    raw_caption = caption_resp.output_text.strip()
    phrases = [l.strip() for l in raw_caption.split('\n') if l.strip()][:3]
    caption = phrases[0] if phrases else raw_caption.strip('"').strip("'")
    logger.info(f"Pet phrases (text-only update): {phrases}")

    pet_state["pet_caption"] = caption
    pet_state["pet_phrases"] = phrases
    if event_text:
        pet_state["event_of_day"] = event_text
    _save_json(slot / "pet_state.json", pet_state)
    return {"pet_caption": caption, "pet_phrases": phrases}


# ==================== PET SCHEDULER ====================
pet_scheduler_running = False

def start_pet_scheduler():
    global pet_scheduler_running
    if pet_scheduler_running:
        return
    pet_scheduler_running = True

    def scheduler_loop():
        # Tracks (date_str, hour) slots already processed to avoid double-firing
        generated_slots: set = set()   # image generations
        text_slots: set = set()        # text-only generations

        while True:
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo("America/Sao_Paulo"))
            except Exception:
                now = datetime.now()

            current_slot = (now.strftime("%Y-%m-%d"), now.hour)
            today = now.strftime("%Y-%m-%d")

            # --- Full image + text generation (2x/day) ---
            if now.hour in PET_GENERATION_HOURS and current_slot not in generated_slots:
                generated_slots.add(current_slot)
                generated_slots = {s for s in generated_slots if s[0] == today}

                try:
                    conn = get_db()
                    users = conn.execute('SELECT email FROM users WHERE is_verified = 1').fetchall()
                    conn.close()

                    for user_row in users:
                        user_email = user_row['email']
                        try:
                            user_plants_dir = _user_plants_dir(user_email)
                            if not user_plants_dir.exists():
                                continue

                            active_slot = _get_active_slot(user_email)
                            slot = _slot_dir(user_email, active_slot)
                            pet_cfg = slot / "pet_config.json"
                            plant_cfg = slot / "plant_profile.json"

                            if pet_cfg.exists() and plant_cfg.exists():
                                logger.info(f"Scheduler: generating pet image for {user_email} ({now.hour}h)...")
                                generate_pet_image(user=user_email)
                                logger.info(f"Scheduler: pet image generated for {user_email}")

                                # Weekly Email Reminder Check
                                plant_photo = slot / "plant_photo.jpg"
                                reminder_file = _user_dir(user_email) / "last_reminder_time.txt"
                                if plant_photo.exists():
                                    photo_age_seconds = time.time() - os.path.getmtime(plant_photo)
                                    if photo_age_seconds > 7 * 86400:
                                        send_email_flag = True
                                        if reminder_file.exists():
                                            with open(reminder_file, "r") as f:
                                                last_sent = float(f.read().strip())
                                                if time.time() - last_sent < 7 * 86400:
                                                    send_email_flag = False
                                        if send_email_flag:
                                            pet_config = _load_json(pet_cfg)
                                            plant_profile = _load_json(plant_cfg)
                                            pet_name = pet_config.get("name", "Pet")
                                            plant_name = plant_profile.get("nome_popular", "sua planta")
                                            logger.info(f"Scheduler: Sending weekly reminder to {user_email}")
                                            send_weekly_photo_reminder_email(user_email, plant_name, pet_name)
                                            with open(reminder_file, "w") as f:
                                                f.write(str(time.time()))
                        except Exception as e:
                            logger.error(f"Scheduler: pet image generation failed for {user_email}: {e}")

                except Exception as e:
                    logger.error(f"Scheduler: failed to iterate users for image gen: {e}")

            # --- Text-only generation (every ~4h, skip hours already covered by image gen) ---
            elif now.hour in PET_TEXT_HOURS and current_slot not in text_slots and current_slot not in generated_slots:
                text_slots.add(current_slot)
                text_slots = {s for s in text_slots if s[0] == today}

                try:
                    conn = get_db()
                    users = conn.execute('SELECT email FROM users WHERE is_verified = 1').fetchall()
                    conn.close()

                    for user_row in users:
                        user_email = user_row['email']
                        try:
                            user_plants_dir = _user_plants_dir(user_email)
                            if not user_plants_dir.exists():
                                continue
                            active_slot = _get_active_slot(user_email)
                            slot = _slot_dir(user_email, active_slot)
                            pet_cfg = slot / "pet_config.json"
                            plant_cfg = slot / "plant_profile.json"
                            if pet_cfg.exists() and plant_cfg.exists():
                                logger.info(f"Scheduler: generating pet phrases for {user_email} ({now.hour}h)...")
                                generate_pet_phrases(user=user_email)
                                logger.info(f"Scheduler: pet phrases updated for {user_email}")
                        except Exception as e:
                            logger.error(f"Scheduler: pet phrases failed for {user_email}: {e}")

                except Exception as e:
                    logger.error(f"Scheduler: failed to iterate users for text gen: {e}")

            time.sleep(60)  # check every minute

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    logger.info(f"Pet scheduler started — generates at {sorted(PET_GENERATION_HOURS)}h (America/Sao_Paulo)")

# ==================== INGEST ENDPOINT (ESP32 -> Server) — NO AUTH ====================
class SensorReading(BaseModel):
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[int] = None
    soil_raw: Optional[int] = None
    soil_status: Optional[str] = None

@app.post("/api/ingest")
def api_ingest(reading: SensorReading, x_api_key: str = Header(alias="X-API-Key")):
    """Recebe dados do ESP32 via POST (sem JWT, usa API key propria)."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")
    conn = get_db()
    conn.execute(
        'INSERT INTO sensor_readings (temperature,humidity,soil_moisture,soil_raw,soil_status) VALUES (?,?,?,?,?)',
        (reading.temperature, reading.humidity, reading.soil_moisture, reading.soil_raw, reading.soil_status)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "message": "Leitura registrada"}

def _get_last_pump_at() -> str | None:
    try:
        last = _load_json(LAST_PUMP_FILE)
        return last.get("pumped_at") if last else None
    except Exception:
        return None

# ==================== CONTROLE DE BOMBA ====================
@app.post("/api/water")
def api_water(seconds: int = 5, user: str = Depends(get_current_user)):
    """Enfileira comando de rega para o ESP32 (ESP32 busca via GET /api/commands)."""
    if not (1 <= seconds <= 30):
        raise HTTPException(status_code=400, detail="seconds deve ser entre 1 e 30")
    # Cooldown: nao permite nova rega em menos de 5 minutos
    try:
        last = _load_json(LAST_PUMP_FILE)
        if last and last.get("pumped_at"):
            pumped_at = datetime.fromisoformat(last["pumped_at"])
            elapsed = (datetime.now() - pumped_at).total_seconds()
            cooldown = 300  # 5 minutos
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                raise HTTPException(
                    status_code=429,
                    detail=f"Aguarde {remaining}s antes de regar novamente (proteção da planta)."
                )
    except HTTPException:
        raise
    except Exception:
        pass
    _save_json(PENDING_PUMP_FILE, {
        "seconds": seconds,
        "requested_at": datetime.now().isoformat(),
        "requested_by": user,
    })
    return {"ok": True, "seconds": seconds, "pending": True,
            "message": f"Comando de {seconds}s enfileirado. ESP32 executara em ate 10s."}

# ==================== COMANDOS ESP32 (sem JWT, usa API key) ====================
@app.get("/api/commands")
def api_get_commands(x_api_key: str = Header(alias="X-API-Key")):
    """ESP32 busca comandos pendentes apos cada envio de dados."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")
    try:
        cmd = _load_json(PENDING_PUMP_FILE)
        if cmd and cmd.get("seconds"):
            # Expira comando apos 60 segundos para evitar loop infinito
            requested_at_str = cmd.get("requested_at", "")
            if requested_at_str:
                try:
                    requested_at = datetime.fromisoformat(requested_at_str)
                    age_seconds = (datetime.now() - requested_at).total_seconds()
                    if age_seconds > 60:
                        PENDING_PUMP_FILE.unlink(missing_ok=True)
                        logger.warning(f"[BOMBA] Comando expirado ({age_seconds:.0f}s), descartado.")
                        return {"pump_seconds": 0}
                except Exception:
                    pass
            return {"pump_seconds": cmd["seconds"]}
    except Exception:
        pass
    return {"pump_seconds": 0}

@app.post("/api/commands/done")
def api_commands_done(x_api_key: str = Header(alias="X-API-Key")):
    """ESP32 confirma execucao do comando — limpa a fila e registra timestamp."""
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")
    try:
        cmd = _load_json(PENDING_PUMP_FILE)
        seconds = cmd.get("seconds", 5) if cmd else 5
        PENDING_PUMP_FILE.unlink(missing_ok=True)
        # Registra quando ocorreu a ultima rega
        _save_json(LAST_PUMP_FILE, {
            "pumped_at": datetime.now().isoformat(),
            "seconds": seconds,
        })
        logger.info(f"[BOMBA] Rega confirmada pelo ESP32: {seconds}s")
    except Exception as e:
        logger.warning(f"[BOMBA] Erro ao confirmar done: {e}")
    return {"ok": True}

@app.post("/api/admin/test-email")
def api_test_email(user: str = Depends(get_current_user)):
    """Envia email de teste com o novo template."""
    try:
        send_verification_email(user, "123456")
        return {"ok": True, "message": f"Email de teste enviado para {user}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== CALIBRACAO DO SOLO (endpoints) ====================
@app.post("/api/calibrate/soil")
def api_calibrate_soil_water(user: str = Depends(get_current_user)):
    """Passo 1: Rega o solo por 5s para encharcar como referencia de calibracao."""
    try:
        resp = http_requests.get(
            f"{ESP32_IP}/pump",
            params={"seconds": 5},
            auth=AUTH_ESP32,
            timeout=12
        )
        if resp.status_code == 409:
            raise HTTPException(status_code=409, detail="Bomba ja esta ativa")
        if not resp.ok:
            raise HTTPException(status_code=502, detail=f"ESP32 retornou {resp.status_code}")
        return {"ok": True, "message": "Solo encharcando. Aguarde 30s e salve a calibracao."}
    except http_requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="ESP32 nao acessivel")
    except http_requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Timeout aguardando ESP32")

@app.post("/api/calibrate/soil/save")
def api_calibrate_soil_save(user: str = Depends(get_current_user)):
    """Passo 2: Salva a leitura atual de soil_raw como referencia de solo encharcado (100%)."""
    conn = get_db()
    row = conn.execute('SELECT soil_raw FROM sensor_readings ORDER BY timestamp DESC LIMIT 1').fetchone()
    conn.close()
    if not row or row['soil_raw'] is None:
        raise HTTPException(status_code=400, detail="Sem leitura de ADC disponivel. Verifique se o ESP32 esta enviando dados.")
    soaked_adc = row['soil_raw']
    existing = _load_soil_cal(user)
    dry_adc = existing.get('dry_adc', 3000)
    # Sanidade: solo encharcado deve ser menor que seco
    if soaked_adc >= dry_adc - 100:
        raise HTTPException(status_code=400, detail=f"ADC lido ({soaked_adc}) esta muito proximo do valor seco ({dry_adc}). Verifique o sensor.")
    cal = {
        'soaked_adc': soaked_adc,
        'dry_adc': dry_adc,
        'calibrated_at': datetime.now().isoformat()
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
    conn = get_db()
    row = conn.execute('SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 1').fetchone()
    if not row:
        conn.close()
        return {"ok": False}

    ideal = _get_ideal(user)
    cal = _load_soil_cal(user)

    # Smooth soil: usa calibracao do usuario se disponivel
    if cal.get('soaked_adc'):
        soil_rows = conn.execute(f'SELECT soil_raw FROM sensor_readings ORDER BY timestamp DESC LIMIT {SOIL_WINDOW}').fetchall()
        soil_vals = [v for r in soil_rows if (v := _recalc_soil_pct(r[0], cal)) is not None]
    else:
        soil_rows = conn.execute(f'SELECT soil_moisture FROM sensor_readings ORDER BY timestamp DESC LIMIT {SOIL_WINDOW}').fetchall()
        soil_vals = [r[0] for r in soil_rows if r[0] is not None]
    soil_smoothed = round(sum(soil_vals) / len(soil_vals), 1) if soil_vals else (row["soil_moisture"] or 0)

    trend_rows = conn.execute(
        "SELECT temperature, humidity, soil_moisture, soil_raw FROM sensor_readings WHERE timestamp >= datetime('now','-30 minutes') ORDER BY timestamp"
    ).fetchall()
    temps = [r[0] for r in trend_rows if r[0] is not None]
    hums  = [r[1] for r in trend_rows if r[1] is not None]
    if cal.get('soaked_adc'):
        soils = [v for r in trend_rows if (v := _recalc_soil_pct(r[3], cal)) is not None]
    else:
        soils = [r[2] for r in trend_rows if r[2] is not None]
    conn.close()

    t = row["temperature"] or 0
    h = row["humidity"] or 0
    s_trend = _trend(soils)
    hs = _health(t, h, soil_smoothed, user)

    if EXTERNAL_COLLECTOR:
        last_ts = row["timestamp"]
        try:
            dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00")) if "Z" in last_ts else datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            is_connected = (datetime.now() - dt).total_seconds() < 30
        except Exception:
            is_connected = True
        last_ok_ts = last_ts
    else:
        is_connected = collector.connected
        last_ok_ts = collector.last_ok

    return {
        "ok": True,
        "temperature": round(t, 1),
        "humidity": round(h, 1),
        "soil_raw": row["soil_moisture"] or 0,
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
        "last_ok": last_ok_ts,
        "ideal": ideal,
        "last_pump_at": _get_last_pump_at(),
    }

@app.get("/api/history")
def api_history(hours: int = Query(default=24, ge=1, le=168), user: str = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        f"SELECT timestamp, temperature, humidity, soil_moisture FROM sensor_readings WHERE timestamp >= datetime('now','-{hours} hours') ORDER BY timestamp"
    ).fetchall()
    conn.close()

    if not rows:
        return {"timestamps": [], "temperature": [], "humidity": [], "soil_raw": [], "soil_smoothed": [], "count": 0}

    ts = [r["timestamp"] + "Z" for r in rows]
    temp = [round(r["temperature"], 1) if r["temperature"] is not None else None for r in rows]
    hum = [round(r["humidity"], 1) if r["humidity"] is not None else None for r in rows]
    soil_raw = [r["soil_moisture"] for r in rows]
    soil_smooth = _smooth_soil([s if s is not None else 0 for s in soil_raw])

    return {
        "timestamps": ts,
        "temperature": temp,
        "humidity": hum,
        "soil_raw": soil_raw,
        "soil_smoothed": soil_smooth,
        "count": len(rows),
    }

@app.get("/api/stats")
def api_stats(hours: int = Query(default=24, ge=1, le=168), user: str = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute(f'''
        SELECT AVG(temperature),MIN(temperature),MAX(temperature),
               AVG(humidity),MIN(humidity),MAX(humidity),
               AVG(soil_moisture),MIN(soil_moisture),MAX(soil_moisture),
               COUNT(*)
        FROM sensor_readings WHERE timestamp >= datetime('now','-{hours} hours')
    ''').fetchone()

    hourly_rows = conn.execute(f'''
        SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour,
               AVG(temperature), AVG(humidity), AVG(soil_moisture)
        FROM sensor_readings
        WHERE timestamp >= datetime('now','-{hours} hours')
        GROUP BY hour ORDER BY hour
    ''').fetchall()

    soil_rows = conn.execute(
        f"SELECT soil_moisture FROM sensor_readings WHERE timestamp >= datetime('now','-{hours} hours') AND soil_moisture IS NOT NULL"
    ).fetchall()
    conn.close()

    soil_arr = [r[0] for r in soil_rows] if soil_rows else []
    if len(soil_arr) > 1:
        _mean_raw = sum(soil_arr) / len(soil_arr)
        soil_std_raw = round((sum((x - _mean_raw) ** 2 for x in soil_arr) / len(soil_arr)) ** 0.5, 2)
    else:
        soil_std_raw = 0
    soil_smooth = _smooth_soil(soil_arr) if soil_arr else []
    if len(soil_smooth) > 1:
        _mean_smooth = sum(soil_smooth) / len(soil_smooth)
        soil_std_smooth = round((sum((x - _mean_smooth) ** 2 for x in soil_smooth) / len(soil_smooth)) ** 0.5, 2)
    else:
        soil_std_smooth = 0

    ideal = _get_ideal(user)
    total = row[9] or 0
    if total == 0:
        return {"ok": False}

    return {
        "ok": True,
        "temperature": {
            "avg": round(row[0], 1), "min": round(row[1], 1), "max": round(row[2], 1),
            "in_range": ideal["temp"][0] <= row[0] <= ideal["temp"][1],
        },
        "humidity": {
            "avg": round(row[3], 1), "min": round(row[4], 1), "max": round(row[5], 1),
            "in_range": ideal["humidity"][0] <= row[3] <= ideal["humidity"][1],
        },
        "soil": {
            "avg": round(row[6], 1), "min": round(row[7], 1), "max": round(row[8], 1),
            "in_range": ideal["soil"][0] <= row[6] <= ideal["soil"][1],
            "std_raw": soil_std_raw,
            "std_smooth": soil_std_smooth,
        },
        "total_readings": total,
        "hourly": {
            "hours": [r[0] for r in hourly_rows],
            "temperature": [round(r[1], 1) for r in hourly_rows],
            "humidity": [round(r[2], 1) for r in hourly_rows],
            "soil": [round(r[3], 1) for r in hourly_rows],
        },
    }

# ==================== STARTUP ====================
@app.on_event("startup")
def on_startup():
    init_db()
    _migrate_legacy_data()
    if not EXTERNAL_COLLECTOR:
        collector.start()
    else:
        logger.info("Coletor externo ativo — coletor interno desabilitado")
    start_pet_scheduler()

# ==================== STATIC FILES (production) ====================
dist_dir = Path(__file__).parent / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")
