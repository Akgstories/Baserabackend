import os
import uuid
import json
import io
import base64
import hmac
import hashlib
import urllib.request
import urllib.error
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from PIL import Image
from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks, Request, status
from fastapi.responses import JSONResponse, Response, FileResponse
from pydantic import BaseModel, Field

from sqlalchemy import create_engine, String, Integer, Boolean, Float, Text, DateTime, or_, and_, text, inspect, ForeignKey
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, Mapped, mapped_column

# ─── SECURITY & AUTHENTICATION UTILITIES ──────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    import secrets
    SECRET_KEY = secrets.token_urlsafe(32)
ACCESS_TOKEN_EXPIRE_DAYS = 30
def hash_password(password: str) -> str:
    """Secure PBKDF2 password hasher with cryptographic salt."""
    salt = os.urandom(16)
    kdf = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return f"{salt.hex()}${kdf.hex()}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against stored PBKDF2 hash with constant-time comparison."""
    if not hashed_password:
        return False
    if "$" not in hashed_password:
        return plain_password == hashed_password
    try:
        salt_hex, key_hex = hashed_password.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt, 100000)
        return hmac.compare_digest(key, new_key)
    except Exception:
        return False

def create_jwt_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": int(expire.timestamp())})
    
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode("utf-8")).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(to_encode).encode("utf-8")).decode().rstrip("=")
    
    secret_bytes = (SECRET_KEY or "basera-super-secure-production-key-2026-xyz-8899").encode("utf-8")
    msg_bytes = f"{header_b64}.{payload_b64}".encode("utf-8")
    
    signature = hmac.new(secret_bytes, msg_bytes, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    
    return f"{header_b64}.{payload_b64}.{sig_b64}"

def decode_jwt_token(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        
        secret_bytes = (SECRET_KEY or "basera-super-secure-production-key-2026-xyz-8899").encode("utf-8")
        msg_bytes = f"{header_b64}.{payload_b64}".encode("utf-8")
        
        expected_sig = hmac.new(secret_bytes, msg_bytes, hashlib.sha256).digest()
        actual_sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        
        payload_json = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
        payload = json.loads(payload_json)
        
        exp = payload.get("exp")
        if exp and datetime.now(timezone.utc).timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


# ─── DATABASE CONFIGURATION ─────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./basera.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass


# ─── RAZORPAY PAYMENT CLIENT SETUP ──────────────────────────────────
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_YOUR_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "YOUR_SECRET_KEY")

try:
    import razorpay
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception as e:
    razorpay_client = None
    print(f"[RAZORPAY NOTICE] Client in fallback mode: {e}")


# ─── AUTOMATIC IMAGE CONVERSION & COMPRESSION ENGINE ─────────────────
def compress_and_convert_to_webp(base64_data: str, max_size=(1024, 1024), quality=75) -> str:
    if not base64_data or not base64_data.startswith("data:image"):
        return base64_data

    try:
        header, encoded = base64_data.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        img = Image.open(io.BytesIO(image_bytes))

        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode in ("RGBA", "LA"):
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="WEBP", quality=quality, optimize=True)
        compressed_encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return f"data:image/webp;base64,{compressed_encoded}"
    except Exception as e:
        return base64_data


# ─── ROBUST MULTI-PROVIDER EMAIL DISPATCHER ────────────────────────
EMAIL_AUDIT_LOG = []

def log_email_event(recipient: str, subject: str, provider: str, status: str, error_detail: str = ""):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "recipient": recipient,
        "subject": subject,
        "provider": provider,
        "status": status,
        "error_detail": error_detail
    }
    EMAIL_AUDIT_LOG.insert(0, entry)
    if len(EMAIL_AUDIT_LOG) > 50:
        EMAIL_AUDIT_LOG.pop()

def send_email_notification(recipient_email: str, subject: str, body_text: str, html_content: Optional[str] = None):
    if not recipient_email or "@" not in recipient_email:
        return

    clean_brevo_key = os.getenv("BREVO_API_KEY", os.getenv("BREVO_SMTP_KEY", "")).strip()
    sender_email = os.getenv("BREVO_SENDER_EMAIL", os.getenv("SENDER_EMAIL", "basera4you@gmail.com")).strip()
    
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com" if gmail_user else "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", gmail_user).strip()
    smtp_password = os.getenv("SMTP_PASSWORD", gmail_pass).strip()

    dispatch_attempted = False

    if clean_brevo_key and clean_brevo_key.startswith("xkeysib-"):
        dispatch_attempted = True
        try:
            url = "https://api.brevo.com/v3/smtp/email"
            payload = {
                "sender": {"name": "Basera Platform", "email": sender_email},
                "to": [{"email": recipient_email}],
                "subject": subject,
                "textContent": body_text
            }
            if html_content:
                payload["htmlContent"] = html_content

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={
                    "accept": "application/json",
                    "api-key": clean_brevo_key,
                    "content-type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status in (200, 201):
                    log_email_event(recipient_email, subject, "Brevo REST API", "SUCCESS")
                    return
                else:
                    resp_body = response.read().decode("utf-8")
                    log_email_event(recipient_email, subject, "Brevo REST API", "FAILED", f"HTTP {response.status}: {resp_body}")
        except Exception as e:
            log_email_event(recipient_email, subject, "Brevo REST API", "FAILED", str(e))

    if smtp_server and smtp_user and smtp_password:
        dispatch_attempted = True
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"Basera Platform <{smtp_user}>"
            msg["To"] = recipient_email
            msg.attach(MIMEText(body_text, "plain"))
            if html_content:
                msg.attach(MIMEText(html_content, "html"))

            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=8) as server:
                    server.login(smtp_user, smtp_password)
                    server.sendmail(smtp_user, recipient_email, msg.as_string())
            else:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=8) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.sendmail(smtp_user, recipient_email, msg.as_string())

            log_email_event(recipient_email, subject, f"SMTP ({smtp_server})", "SUCCESS")
            return
        except Exception as e:
            log_email_event(recipient_email, subject, f"SMTP ({smtp_server})", "FAILED", str(e))

    if not dispatch_attempted:
        log_email_event(recipient_email, subject, "Simulation / Log Only", "QUEUED", "Set BREVO_API_KEY or SMTP credentials in environment for live external transmission.")


# ─── SQLALCHEMY ORM MODELS ────────────────────────────────────────
class DBUser(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, index=True, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False) # Stores salted hash securely in the existing 'password' column
    phone: Mapped[str] = mapped_column(String, default="")
    address: Mapped[str] = mapped_column(String, default="GEC Bokaro Hostel, Room 101")
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro")
    role: Mapped[str] = mapped_column(String, default="student")
    token: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    razorpay_account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    bank_account_no: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    bank_ifsc: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    account_holder_name: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    pan_number: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    settlement_tenure: Mapped[str] = mapped_column(String, default="instant", server_default="instant")
    auto_settle: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default="now()")

class DBPGListing(Base):
    __tablename__ = "pg_listings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    gender_pref: Mapped[str] = mapped_column(String, nullable=False)
    sharing: Mapped[str] = mapped_column(String, nullable=False)
    has_ac: Mapped[bool] = mapped_column(Boolean, default=False)
    monthly_price: Mapped[int] = mapped_column(Integer, nullable=False)
    tag_label: Mapped[str] = mapped_column(String, default="Verified PG")
    address: Mapped[str] = mapped_column(Text, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=Chandankiyari+Bokaro")
    rating: Mapped[str] = mapped_column(String, default="4.8 (25)")
    amenities: Mapped[str] = mapped_column(Text, default="[]")
    images: Mapped[str] = mapped_column(Text, default="[]")

class DBMessListing(Base):
    __tablename__ = "mess_listings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider_name: Mapped[str] = mapped_column(String, nullable=False)
    monthly_price: Mapped[int] = mapped_column(Integer, nullable=False)
    diet_type: Mapped[str] = mapped_column(String, nullable=False)
    meals_per_day: Mapped[str] = mapped_column(String, default="Flexible Plan Options")
    rating: Mapped[str] = mapped_column(String, default="4.9 (40)")
    address: Mapped[str] = mapped_column(Text, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro+Main+Gate")
    description: Mapped[str] = mapped_column(Text, default="Freshly prepared hygienic meals tailored for students.")

class DBMessPricing(Base):
    __tablename__ = "mess_pricing"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    mess_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    location_name: Mapped[str] = mapped_column(String, nullable=False)
    breakfast_rate: Mapped[int] = mapped_column(Integer, default=30)
    lunch_rate: Mapped[int] = mapped_column(Integer, default=40)
    dinner_rate: Mapped[int] = mapped_column(Integer, default=40)

class DBPGRoom(Base):
    __tablename__ = "pg_rooms"
    room_number: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    pg_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("pg_listings.id"), nullable=True, index=True)
    room_type: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tenant_name: Mapped[str] = mapped_column(String, default="-")
    tenant_phone: Mapped[str] = mapped_column(String, default="-")
    tenant_address: Mapped[str] = mapped_column(String, default="-")
    monthly_rent: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="vacant")
    images: Mapped[str] = mapped_column(Text, default="[]")

class DBVacateRequest(Base):
    __tablename__ = "vacate_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    room_number: Mapped[str] = mapped_column(String, nullable=False)
    pg_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    student_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    student_name: Mapped[str] = mapped_column(String, nullable=False)
    student_phone: Mapped[str] = mapped_column(String, nullable=False)
    booking_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBMessStudent(Base):
    __tablename__ = "mess_students"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro+Hostel")
    mess_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mess_name: Mapped[str] = mapped_column(String, default="Annapurna Homely Mess")
    plan: Mapped[str] = mapped_column(String, nullable=False)
    diet: Mapped[str] = mapped_column(String, default="Veg")
    base_price: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    start_date: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d"))
    expiry_date: Mapped[str] = mapped_column(String, default=lambda: (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))

class DBMealCancellation(Base):
    __tablename__ = "meal_cancellations"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    student_name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    mess_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    meal: Mapped[str] = mapped_column(String, nullable=False)
    refund_amount: Mapped[int] = mapped_column(Integer, default=50)
    date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)

class DBWeeklyMenu(Base):
    __tablename__ = "weekly_menu"
    day_name: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    breakfast: Mapped[str] = mapped_column(String, default="Aloo Paratha & Tea")
    lunch: Mapped[str] = mapped_column(String, default="Rice, Dal, Veg Sabzi & Salad")
    dinner: Mapped[str] = mapped_column(String, default="Roti, Paneer Butter Masala / Chicken Curry")

class DBBooking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    user_phone: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    item_name: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    move_in_date: Mapped[str] = mapped_column(String, nullable=False)
    expiry_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    special_requests: Mapped[str] = mapped_column(Text, default="")
    monthly_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_method: Mapped[str] = mapped_column(String, default="Razorpay")
    transaction_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="Active")

class DBPaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    transaction_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    order_id: Mapped[str] = mapped_column(String, nullable=False)
    booking_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    payer_name: Mapped[str] = mapped_column(String, nullable=False)
    payer_phone: Mapped[str] = mapped_column(String, nullable=False)
    vendor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    vendor_account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 'amount' = original Supabase column (total paid). Keep to avoid NOT NULL violation.
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    platform_fee: Mapped[float] = mapped_column(Float, default=0.0)
    vendor_payout_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payment_method: Mapped[str] = mapped_column(String, default="Razorpay")
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    route_transfer_status: Mapped[str] = mapped_column(String, default="Settled")
    route_transfer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tenure_days: Mapped[int] = mapped_column(Integer, default=0)
    settlement_due_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payment_date: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))  # original Supabase column

class DBSettlement(Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    vendor_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String, nullable=False)
    transaction_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="Pending Approval")
    payout_mode: Mapped[str] = mapped_column(String, default="Razorpay Route / Bank")
    settlement_tenure: Mapped[str] = mapped_column(String, default="admin_approval")
    requested_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    approved_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
class DBComplaint(Base):
    __tablename__ = "complaints"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    user_phone: Mapped[str] = mapped_column(String, nullable=False)
    target_owner_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBNotification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    recipient_role: Mapped[str] = mapped_column(String, nullable=False)
    recipient_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String, default="general")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


# ─── DATABASE MIGRATIONS & SEEDING ──────────────────────────────────
def seed_database():
    try:
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)

        with engine.connect() as conn:
            tables = inspector.get_table_names()
            if "users" in tables:
                cols = [c["name"] for c in inspector.get_columns("users")]
                if "settlement_tenure" not in cols:
                    try: conn.execute(text("ALTER TABLE users ADD COLUMN settlement_tenure VARCHAR DEFAULT 'instant';"))
                    except Exception: pass
                if "auto_settle" not in cols:
                    try: conn.execute(text("ALTER TABLE users ADD COLUMN auto_settle BOOLEAN DEFAULT true;"))
                    except Exception: pass
                # Drop NOT NULL on optional vendor fields so students can register with NULL
                for _col in ["razorpay_account_id", "bank_account_no", "bank_ifsc", "account_holder_name", "pan_number"]:
                    try: conn.execute(text(f"ALTER TABLE users ALTER COLUMN {_col} DROP NOT NULL;"))
                    except Exception: pass
                # Drop UNIQUE constraints on those vendor columns (NULL is fine for multiple rows)
                try:
                    conn.execute(text("""
                        DO $$ DECLARE r RECORD; BEGIN
                            FOR r IN (
                                SELECT tc.constraint_name FROM information_schema.table_constraints tc
                                JOIN information_schema.key_column_usage kcu
                                  ON tc.constraint_name=kcu.constraint_name AND tc.table_name=kcu.table_name
                                WHERE tc.table_name='users' AND tc.constraint_type='UNIQUE'
                                  AND kcu.column_name IN ('razorpay_account_id','bank_account_no','bank_ifsc','account_holder_name','pan_number')
                            ) LOOP
                                EXECUTE 'ALTER TABLE users DROP CONSTRAINT IF EXISTS ' || quote_ident(r.constraint_name);
                            END LOOP;
                        END $$;
                    """))
                except Exception: pass

            if "mess_students" in tables:
                ms_cols = [c["name"] for c in inspector.get_columns("mess_students")]
                for col, col_type in [("user_id", "VARCHAR DEFAULT NULL"), ("start_date", "VARCHAR DEFAULT NULL"), ("expiry_date", "VARCHAR DEFAULT NULL")]:
                    if col not in ms_cols:
                        conn.execute(text(f"ALTER TABLE mess_students ADD COLUMN {col} {col_type};"))

            if "payment_receipts" in tables:
                cols = [c["name"] for c in inspector.get_columns("payment_receipts")]
                # Add all new columns that may be missing in older Supabase schema
                for col_def in [
                    ("total_amount",          "FLOAT DEFAULT 0"),
                    ("vendor_payout_amount",  "FLOAT DEFAULT 0"),
                    ("platform_fee",          "FLOAT DEFAULT 0"),
                    ("vendor_id",             "VARCHAR DEFAULT NULL"),
                    ("vendor_account_id",     "VARCHAR DEFAULT NULL"),
                    ("payer_id",              "VARCHAR DEFAULT NULL"),
                    ("booking_id",            "VARCHAR DEFAULT NULL"),
                    ("description",           "VARCHAR DEFAULT ''"),
                    ("route_transfer_status", "VARCHAR DEFAULT 'Settled'"),
                    ("route_transfer_id",     "VARCHAR DEFAULT NULL"),
                    ("tenure_days",           "INTEGER DEFAULT 0"),
                    ("settlement_due_date",   "VARCHAR DEFAULT NULL"),
                    ("payment_date",          "VARCHAR DEFAULT ''"),
                    ("payment_method",        "VARCHAR DEFAULT 'Razorpay'"),
                    ("payer_name",            "VARCHAR DEFAULT ''"),
                    ("payer_phone",           "VARCHAR DEFAULT ''"),
                    ("order_id",              "VARCHAR DEFAULT ''"),
                    ("date",                  "VARCHAR DEFAULT ''"),   # original Supabase column
                ]:
                    if col_def[0] not in cols:
                        try: conn.execute(text(f"ALTER TABLE payment_receipts ADD COLUMN {col_def[0]} {col_def[1]};"))
                        except Exception: pass
                # Drop NOT NULL on original columns to prevent constraint violations
                for _col in ["amount", "description", "date", "payer_name", "payer_phone", "order_id"]:
                    try: conn.execute(text(f"ALTER TABLE payment_receipts ALTER COLUMN {_col} DROP NOT NULL;"))
                    except Exception: pass

            if "pg_listings" in tables:
                cols = [c["name"] for c in inspector.get_columns("pg_listings")]
                if "owner_id" not in cols:
                    try: conn.execute(text("ALTER TABLE pg_listings ADD COLUMN owner_id VARCHAR DEFAULT NULL;"))
                    except Exception: pass

            if "mess_listings" in tables:
                cols = [c["name"] for c in inspector.get_columns("mess_listings")]
                if "owner_id" not in cols:
                    try: conn.execute(text("ALTER TABLE mess_listings ADD COLUMN owner_id VARCHAR DEFAULT NULL;"))
                    except Exception: pass

            if "bookings" in tables:
                cols = [c["name"] for c in inspector.get_columns("bookings")]
                for col_def in [
                    ("target_id",       "VARCHAR DEFAULT NULL"),
                    ("expiry_date",     "VARCHAR DEFAULT NULL"),
                    ("special_requests","TEXT DEFAULT ''"),
                    ("monthly_amount",  "INTEGER DEFAULT 0"),
                    ("payment_method",  "VARCHAR DEFAULT 'Razorpay'"),
                    ("transaction_id",  "VARCHAR DEFAULT ''"),
                    ("status",          "VARCHAR DEFAULT 'Active'"),
                    ("user_phone",      "VARCHAR DEFAULT ''"),
                    ("item_name",       "VARCHAR DEFAULT ''"),
                    ("target_type",     "VARCHAR DEFAULT ''"),
                    ("move_in_date",    "VARCHAR DEFAULT ''"),
                ]:
                    if col_def[0] not in cols:
                        try: conn.execute(text(f"ALTER TABLE bookings ADD COLUMN {col_def[0]} {col_def[1]};"))
                        except Exception: pass

            conn.commit()

        db = SessionLocal()
        
        # 1. Seed Admin User
        admin_user = db.query(DBUser).filter(or_(DBUser.role == "admin", DBUser.id == "usr-admin01", DBUser.email == "akgstories02@gmail.com")).first()
        if not admin_user:
            admin_user = DBUser(
                id="usr-admin01",
                full_name="Platform Admin",
                email="akgstories02@gmail.com",
                password=hash_password("admin@2026"),
                phone="9155118661",
                address="Admin Office, GEC Bokaro",
                google_map_url="https://maps.google.com/?q=GEC+Bokaro",
                role="admin"
            )
            admin_user.token = create_jwt_token({"user_id": admin_user.id, "email": admin_user.email, "role": admin_user.role})
            db.add(admin_user)
            db.commit()

        # 2. Seed Default Mess Partners
        mess1_partner = db.query(DBUser).filter(or_(DBUser.id == "usr-mess01", DBUser.email == "ramesh.mess@gecbokaro.ac.in")).first()
        if not mess1_partner:
            mess1_partner = DBUser(
                id="usr-mess01",
                full_name="Ramesh Sharma",
                email="ramesh.mess@gecbokaro.ac.in",
                password=hash_password("mess@2026"),
                phone="9876543201",
                address="Near GEC Main Gate",
                role="mess_partner",
                settlement_tenure="instant"
            )
            mess1_partner.token = create_jwt_token({"user_id": mess1_partner.id, "email": mess1_partner.email, "role": mess1_partner.role})
            db.add(mess1_partner)
            db.commit()

        mess2_partner = db.query(DBUser).filter(or_(DBUser.id == "usr-mess02", DBUser.email == "geeta.mess@gecbokaro.ac.in")).first()
        if not mess2_partner:
            mess2_partner = DBUser(
                id="usr-mess02",
                full_name="Geeta Devi",
                email="geeta.mess@gecbokaro.ac.in",
                password=hash_password("mess@2026"),
                phone="9876543202",
                address="Vill-Ghoragara, Chandankiyari",
                role="mess_partner",
                settlement_tenure="admin_approval"
            )
            mess2_partner.token = create_jwt_token({"user_id": mess2_partner.id, "email": mess2_partner.email, "role": mess2_partner.role})
            db.add(mess2_partner)
            db.commit()

        mess3_partner = db.query(DBUser).filter(or_(DBUser.id == "usr-mess03", DBUser.email == "archana.mess@gecbokaro.ac.in")).first()
        if not mess3_partner:
            mess3_partner = DBUser(
                id="usr-mess03",
                full_name="Archana Devi",
                email="archana.mess@gecbokaro.ac.in",
                password=hash_password("mess@2026"),
                phone="9876543203",
                address="Near GEC Bokaro",
                role="mess_partner",
                settlement_tenure="15_days"
            )
            mess3_partner.token = create_jwt_token({"user_id": mess3_partner.id, "email": mess3_partner.email, "role": mess3_partner.role})
            db.add(mess3_partner)
            db.commit()

        # 3. Seed Default PG Owner
        pg_owner1 = db.query(DBUser).filter(or_(DBUser.id == "usr-pgowner01", DBUser.email == "pgowner.powergrid@gecbokaro.ac.in")).first()
        if not pg_owner1:
            pg_owner1 = DBUser(
                id="usr-pgowner01",
                full_name="Power Grid Hostels",
                email="pgowner.powergrid@gecbokaro.ac.in",
                password=hash_password("pg@2026"),
                phone="9876543211",
                address="Vill-Ghoragara, Chandankiyari",
                role="pg_owner",
                settlement_tenure="instant"
            )
            pg_owner1.token = create_jwt_token({"user_id": pg_owner1.id, "email": pg_owner1.email, "role": pg_owner1.role})
            db.add(pg_owner1)
            db.commit()

        if not db.query(DBMessListing).first():
            db.add_all([
                DBMessListing(
                    id="mess-1", owner_id=mess1_partner.id, name="Annapurna Homely Mess", provider_name="Ramesh Sharma",
                    monthly_price=3000, diet_type="Veg & Non-Veg", meals_per_day="Flexible Plan Options",
                    rating="4.9 (42 reviews)", address="📍 Near GEC Bokaro Main Gate",
                    google_map_url="https://maps.google.com/?q=GEC+Bokaro+Main+Gate",
                    description="Freshly prepared hygienic meals tailored for engineering students."
                ),
                DBMessListing(
                    id="mess-2", owner_id=mess2_partner.id, name="Shuddha Shakahari Mess", provider_name="Geeta Devi",
                    monthly_price=2600, diet_type="Pure Veg", meals_per_day="Flexible Plan Options",
                    rating="4.8 (31 reviews)", address="📍 Vill-Ghoragara, Chandankiyari",
                    google_map_url="https://maps.google.com/?q=Chandankiyari+Bokaro",
                    description="100% Pure Vegetarian North & South Indian meals cooked with pure desi ghee."
                ),
                DBMessListing(
                    id="mess-3", owner_id=mess3_partner.id, name="Archana mess", provider_name="Archana Devi",
                    monthly_price=3600, diet_type="Veg & Non-Veg", meals_per_day="Flexible Plan Options",
                    rating="5.0 (New)", address="📍 Near GEC Bokaro",
                    google_map_url="https://maps.google.com/?q=GEC+Bokaro",
                    description="Premium freshly prepared hygienic meals with weekly special treats."
                )
            ])
            db.commit()

        if not db.query(DBMessPricing).first():
            db.add_all([
                DBMessPricing(mess_id="mess-1", location_name="GEC Main Gate", three_time_rate=100, two_time_rate=80),
                DBMessPricing(mess_id="mess-1", location_name="Chandankiyari", three_time_rate=90, two_time_rate=70),
                DBMessPricing(mess_id="mess-1", location_name="Ghoragara", three_time_rate=110, two_time_rate=85),
                DBMessPricing(mess_id="mess-2", location_name="GEC Main Gate", three_time_rate=90, two_time_rate=70),
                DBMessPricing(mess_id="mess-2", location_name="Chandankiyari", three_time_rate=80, two_time_rate=65),
                DBMessPricing(mess_id="mess-2", location_name="Ghoragara", three_time_rate=95, two_time_rate=75),
                DBMessPricing(mess_id="mess-3", location_name="GEC Main Gate", three_time_rate=120, two_time_rate=95),
                DBMessPricing(mess_id="mess-3", location_name="Chandankiyari", three_time_rate=110, two_time_rate=85),
                DBMessPricing(mess_id="mess-3", location_name="Ghoragara", three_time_rate=125, two_time_rate=100)
            ])
            db.commit()

        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for d in days:
            if not db.query(DBWeeklyMenu).filter(DBWeeklyMenu.day_name == d).first():
                db.add(DBWeeklyMenu(
                    day_name=d,
                    breakfast=f"{d} Special Aloo Paratha & Hot Tea",
                    lunch=f"{d} Standard Basmati Rice, Yellow Dal & Seasonal Green Sabzi",
                    dinner=f"{d} Fresh Tawa Roti, Paneer Butter Masala / Desi Chicken Curry"
                ))
        db.commit()

        if not db.query(DBPGListing).first():
            db.add_all([
                DBPGListing(
                    id="pg-1", owner_id=pg_owner1.id, name="Power Grid Scholars Boys PG", distance_km=0.4,
                    gender_pref="Boys PG", sharing="Double Sharing", has_ac=True, monthly_price=4200,
                    tag_label="Vacant", address="📍 Vill-Ghoragara, P.O-Kherabera, Chandankiyari",
                    google_map_url="https://maps.google.com/?q=23.5750,86.3500",
                    rating="4.9 (28)", amenities=json.dumps(["High-Speed Wi-Fi", "Homely Mess Available", "24/7 Power Backup", "RO Purified Water"]), images="[]"
                ),
                DBPGListing(
                    id="pg-2", owner_id=pg_owner1.id, name="Chandankiyari Comfort Girls PG", distance_km=0.2,
                    gender_pref="Girls PG", sharing="Single Room", has_ac=True, monthly_price=4800,
                    tag_label="1 left", address="📍 P.O-Kherabera, Chandankiyari, Bokaro",
                    google_map_url="https://maps.google.com/?q=23.5780,86.3520",
                    rating="4.8 (19)", amenities=json.dumps(["24/7 CCTV Security", "3-Time Food Option", "Geyser", "Study Table & Wardrobe"]), images="[]"
                )
            ])
            db.commit()

        if not db.query(DBPGRoom).first():
            db.add_all([
                DBPGRoom(room_number="101", pg_id="pg-1", room_type="Single AC", tenant_name="Rahul Kumar", tenant_phone="9876543210", tenant_address="GEC Bokaro Hostel Block A", monthly_rent=8000, status="occupied", images="[]"),
                DBPGRoom(room_number="102", pg_id="pg-1", room_type="Double Non-AC", tenant_name="-", tenant_phone="-", tenant_address="-", monthly_rent=4200, status="vacant", images="[]"),
                DBPGRoom(room_number="201", pg_id="pg-2", room_type="Single Room AC", tenant_name="-", tenant_phone="-", tenant_address="-", monthly_rent=4800, status="vacant", images="[]")
            ])
            db.commit()

        db.close()
    except Exception as e:
        print(f"[DATABASE NOTICE] Seed completed: {e}")


# ─── FASTAPI APPLICATION INITIALIZATION ──────────────────────────────
app = FastAPI(
    title="Basera Multi-Portal Platform API",
    description="Production-ready student housing and mess subscription engine.",
    version="17.0.0"
)

@app.middleware("http")
async def cors_and_security_headers_middleware(request: Request, call_next):
    origin = request.headers.get("origin", "*")
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    try:
        response = await call_next(request)
    except Exception as exc:
        response = JSONResponse(
            status_code=500,
            content={"detail": f"Internal Server Error: {str(exc)}"}
        )

    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

@app.on_event("startup")
def on_startup():
    seed_database()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── TARGETED NOTIFICATION ENGINE ──────────────────────────────────
def notify_owner_and_email(
    db: Session,
    background_tasks: BackgroundTasks,
    recipient_role: str,
    title: str,
    message: str,
    event_type: str = "general",
    recipient_user_id: Optional[str] = None,
    include_admin: bool = False
):
    db.add(DBNotification(
        id=f"notif-{uuid.uuid4().hex[:8]}",
        recipient_role=recipient_role,
        recipient_id=recipient_user_id,
        title=title,
        message=message,
        event_type=event_type,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M")
    ))

    if recipient_user_id:
        query_filter = [DBUser.id == recipient_user_id]
        if include_admin:
            query_filter.append(DBUser.role == "admin")
        target_users = db.query(DBUser).filter(or_(*query_filter)).all()
    else:
        query_filter = [DBUser.role == recipient_role]
        if include_admin:
            query_filter.append(DBUser.role == "admin")
        target_users = db.query(DBUser).filter(or_(*query_filter)).all()

    for target in target_users:
        if target.email:
            background_tasks.add_task(
                send_email_notification,
                target.email,
                f"[{recipient_role.upper()} ALERT] {title}",
                f"Hello {target.full_name},\n\n{message}\n\nEvent Type: {event_type}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n— Basera Portal Automated Notification System"
            )


# ─── AUTHENTICATION DEPENDENCIES ───────────────────────────────────
def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid authentication token.")
    
    token = authorization.split(" ")[1].strip()
    payload = decode_jwt_token(token)
    user = None
    if payload and "user_id" in payload:
        user = db.query(DBUser).filter(DBUser.id == payload["user_id"]).first()
    
    if not user:
        user = db.query(DBUser).filter(DBUser.token == token).first()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid token. Please sign in again.")
    
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "address": user.address,
        "google_map_url": user.google_map_url,
        "role": user.role,
        "settlement_tenure": user.settlement_tenure,
        "razorpay_account_id": user.razorpay_account_id
    }

def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin authorization required.")
    return user

def require_vendor_or_admin(user: dict = Depends(get_current_user)):
    if user["role"] not in ["mess_partner", "pg_owner", "admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Vendor or Admin authorization required.")
    return user


# ─── SCHEMAS / PYDANTIC MODELS (STRING TYPES PREVENTING VALIDATOR ERRORS) ──
class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str = Field(min_length=4)
    phone: str
    address: Optional[str] = "GEC Bokaro Hostel"
    google_map_url: Optional[str] = "https://maps.google.com/?q=GEC+Bokaro"
    role: Optional[str] = "student"

class LoginRequest(BaseModel):
    email: str
    password: str
    target_role: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: str
    phone: str
    new_password: str = Field(min_length=4)

class ProfileUpdateRequest(BaseModel):
    phone: str
    address: str
    google_map_url: Optional[str] = ""

class UpdateMessPriceRequest(BaseModel):
    mess_id: str
    monthly_price: int

class UpdateMessLocationPricingRequest(BaseModel):
    mess_id: str
    location_name: str
    breakfast_rate: int
    lunch_rate: int
    dinner_rate: int

class MealCancelRequest(BaseModel):
    meal_type: str
    date: Optional[str] = None

class WeeklyMenuUpdateRequest(BaseModel):
    day_name: str
    breakfast: str
    lunch: str
    dinner: str

class CreateMessListingRequest(BaseModel):
    name: str
    provider_name: Optional[str] = None
    monthly_price: int
    diet_type: str = "Veg & Non-Veg"
    meals_per_day: str = "Flexible Plan Options"
    address: str
    google_map_url: str
    description: str

class CreateOrderRequest(BaseModel):
    amount: float
    item_name: str

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    item_name: str
    monthly_amount: int
    move_in_date: str
    special_requests: Optional[str] = ""
    duration_days: Optional[int] = 30
    diet_preference: Optional[str] = "Veg"

class RequestStudentVacate(BaseModel):
    booking_id: str

class ApproveVacateRequest(BaseModel):
    request_id: str

class AddRoomRequest(BaseModel):
    pg_id: Optional[str] = None
    room_number: str
    room_type: str
    monthly_rent: int

class UpdateRoomImagesRequest(BaseModel):
    room_number: str
    images: List[str]

class ComplaintCreateRequest(BaseModel):
    category: str
    title: str
    description: str

class ResolveComplaintRequest(BaseModel):
    complaint_id: str

class TestEmailRequest(BaseModel):
    recipient: str
    subject: Optional[str] = "Basera Diagnostic Test Email"
    body: Optional[str] = "This is a live test email sent from the Basera Admin Console."

class CreatePGRequest(BaseModel):
    name: str
    distance_km: float
    gender_pref: str
    sharing: str
    monthly_price: int
    address: str
    rating: str = "4.8"
    amenities: str = "Wi-Fi, RO Water, Security"

class UnsubscribeRequest(BaseModel):
    booking_id: Optional[str] = None
    item_name: Optional[str] = None

class VendorRouteOnboardRequest(BaseModel):
    account_holder_name: str
    bank_account_no: str
    bank_ifsc: str
    pan_number: str
    settlement_tenure: Optional[str] = "instant"

class ApproveSettlementRequest(BaseModel):
    settlement_id: str
    action: str = "approve"


# ─── API ENDPOINTS ─────────────────────────────────────────────────

@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept", "")
    index_path = os.path.join(os.path.dirname(__file__), "index.html")
    if "text/html" in accept and os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "status": "online",
        "platform": "Basera Cloud Production Engine",
        "version": "17.0.0",
        "database": "Active & Synchronized",
        "timestamp": datetime.now().isoformat()
    }

# ─── AUTHENTICATION ENDPOINTS ──────────────────────────────────────

@app.post("/api/auth/register")
def register_user(req: RegisterRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    role = req.role or "student"
    if role == "admin":
        raise HTTPException(status_code=400, detail="Admin account registration is restricted.")

    clean_phone = req.phone.strip()
    if len(clean_phone) != 10 or not clean_phone.isdigit():
        raise HTTPException(status_code=400, detail="Mobile phone number must be exactly 10 digits.")

    existing = db.query(DBUser).filter(DBUser.email == req.email.lower().strip()).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"An account with email '{req.email}' is already registered.")

    user_id = f"usr-{uuid.uuid4().hex[:8]}"
    map_url = req.google_map_url or "https://maps.google.com/?q=GEC+Bokaro"
    pass_hash = hash_password(req.password)

    new_user = DBUser(
        id=user_id,
        full_name=req.full_name.strip(),
        email=req.email.lower().strip(),
        password=pass_hash,
        phone=clean_phone,
        address=req.address or "GEC Bokaro Hostel",
        google_map_url=map_url,
        role=role,
        settlement_tenure="instant"
    )
    
    token = create_jwt_token({"user_id": new_user.id, "email": new_user.email, "role": new_user.role})
    new_user.token = token
    
    db.add(new_user)
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        req.email,
        "Welcome to Basera Platform!",
        f"Hello {req.full_name},\n\nYour account has been registered successfully as a {role.replace('_', ' ').upper()} on Basera.\n\nThank you for joining Basera!"
    )

    return {
        "status": "success",
        "token": token,
        "user": {
            "id": new_user.id,
            "full_name": new_user.full_name,
            "email": new_user.email,
            "phone": new_user.phone,
            "address": new_user.address,
            "google_map_url": new_user.google_map_url,
            "role": new_user.role,
            "settlement_tenure": new_user.settlement_tenure
        }
    }

@app.post("/api/auth/login")
def login_user(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.email == req.email.lower().strip()).first()
    
    if not user or not verify_password(req.password, user.password):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password credentials. Please verify your details."
        )

    if req.target_role and user.role != req.target_role and user.role != "admin":
        role_label = req.target_role.replace('_', ' ').title()
        raise HTTPException(
            status_code=401,
            detail=f"This account is registered as '{user.role.replace('_', ' ').title()}', not as '{role_label}'."
        )

    token = create_jwt_token({"user_id": user.id, "email": user.email, "role": user.role})
    user.token = token
    db.commit()

    return {
        "status": "success",
        "token": token,
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "address": user.address,
            "google_map_url": user.google_map_url,
            "role": user.role,
            "settlement_tenure": user.settlement_tenure,
            "razorpay_account_id": user.razorpay_account_id
        }
    }

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {"status": "success", "user": user}

@app.post("/api/auth/profile/update")
def update_profile(req: ProfileUpdateRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    clean_phone = req.phone.strip()
    if len(clean_phone) != 10 or not clean_phone.isdigit():
        raise HTTPException(status_code=400, detail="Mobile number must be exactly 10 digits.")

    u = db.query(DBUser).filter(DBUser.id == user["id"]).first()
    if u:
        u.phone = clean_phone
        u.address = req.address
        if req.google_map_url:
            u.google_map_url = req.google_map_url

    ms = db.query(DBMessStudent).filter(or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"]))).first()
    if ms:
        ms.phone = clean_phone
        ms.address = req.address
        if req.google_map_url:
            ms.google_map_url = req.google_map_url

    db.commit()
    return {"status": "success", "message": "Profile address, phone, and Google Map location updated in database!"}

@app.post("/api/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.email == req.email.lower().strip(), DBUser.phone == req.phone.strip()).first()
    if not user:
        raise HTTPException(status_code=404, detail="No registered account found matching this email and phone number.")
    
    user.password = hash_password(req.new_password)
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        req.email,
        "Basera Password Reset Confirmation",
        "Your Basera account password has been updated successfully."
    )

    return {"status": "success", "message": "Password updated successfully!"}


# ─── MESS LISTINGS & DYNAMIC LOCATION PRICING ENDPOINTS ─────────────

@app.get("/api/mess-pricing")
def get_mess_pricing(mess_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(DBMessPricing)
    if mess_id:
        query = query.filter(DBMessPricing.mess_id == mess_id)
    pricing = query.all()
    return [
        {
            "id": p.id,
            "mess_id": p.mess_id,
            "location_name": p.location_name,
            "breakfast_rate": getattr(p, "breakfast_rate", 30),
            "lunch_rate": getattr(p, "lunch_rate", 40),
            "dinner_rate": getattr(p, "dinner_rate", 40)
        } for p in pricing
    ]
@app.post("/api/mess/update-location-price")
def update_mess_location_price(
    req: UpdateMessLocationPricingRequest,
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    item = db.query(DBMessPricing).filter(
        DBMessPricing.mess_id == req.mess_id,
        DBMessPricing.location_name.ilike(req.location_name.strip())
    ).first()
    
    if not item:
        item = DBMessPricing(
            mess_id=req.mess_id,
            location_name=req.location_name.strip(),
            breakfast_rate=req.breakfast_rate,
            lunch_rate=req.lunch_rate,
            dinner_rate=req.dinner_rate
        )
        db.add(item)
    else:
        item.breakfast_rate = req.breakfast_rate
        item.lunch_rate = req.lunch_rate
        item.dinner_rate = req.dinner_rate

    db.commit()
    return {"status": "success", "message": f"Updated rates for {req.location_name} successfully!"}

@app.get("/api/mess-listings")
def get_mess_listings(db: Session = Depends(get_db)):
    listings = db.query(DBMessListing).all()
    return [
        {
            "id": m.id,
            "owner_id": m.owner_id,
            "name": m.name,
            "provider_name": m.provider_name,
            "monthly_price": m.monthly_price,
            "diet_type": m.diet_type,
            "meals_per_day": m.meals_per_day,
            "rating": m.rating,
            "address": m.address,
            "google_map_url": m.google_map_url,
            "description": m.description
        }
        for m in listings
    ]

@app.post("/api/mess/add-listing")
def create_mess_listing(
    req: CreateMessListingRequest,
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    mess_id = f"mess-{uuid.uuid4().hex[:6]}"
    new_mess = DBMessListing(
        id=mess_id,
        owner_id=user["id"],
        name=req.name.strip(),
        provider_name=req.provider_name or user["full_name"],
        monthly_price=req.monthly_price,
        diet_type=req.diet_type,
        meals_per_day=req.meals_per_day,
        rating="5.0 (New)",
        address=req.address,
        google_map_url=req.google_map_url,
        description=req.description
    )
    db.add(new_mess)

    db.add_all([
        DBMessPricing(mess_id=mess_id, location_name="GEC Main Gate", three_time_rate=int(req.monthly_price / 30), two_time_rate=int(req.monthly_price / 30 * 0.8)),
        DBMessPricing(mess_id=mess_id, location_name="Chandankiyari", three_time_rate=int(req.monthly_price / 30 * 0.95), two_time_rate=int(req.monthly_price / 30 * 0.75)),
        DBMessPricing(mess_id=mess_id, location_name="Ghoragara", three_time_rate=int(req.monthly_price / 30 * 1.05), two_time_rate=int(req.monthly_price / 30 * 0.85))
    ])

    db.commit()
    return {"status": "success", "message": f"Mess '{req.name}' listed successfully with dynamic location pricing!", "mess_id": mess_id}

@app.delete("/api/mess-listings/{mess_id}")
def delete_mess_listing(mess_id: str, user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    mess = db.query(DBMessListing).filter(DBMessListing.id == mess_id).first()
    if not mess:
        mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{mess_id}%")).first()
    if not mess:
        raise HTTPException(status_code=404, detail="Mess listing not found.")

    db.query(DBMessPricing).filter(DBMessPricing.mess_id == mess.id).delete()
    db.delete(mess)
    db.commit()
    return {"status": "success", "message": f"Mess listing '{mess.name}' deleted successfully."}

@app.post("/api/mess/update-price")
def update_mess_price(req: UpdateMessPriceRequest, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    mess = db.query(DBMessListing).filter(DBMessListing.id == req.mess_id).first()
    if not mess:
        mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{req.mess_id}%")).first()
    if not mess:
        raise HTTPException(status_code=404, detail="Mess listing not found.")
    
    mess.monthly_price = req.monthly_price
    db.commit()
    return {"status": "success", "message": f"Updated monthly price for '{mess.name}' to ₹{req.monthly_price} in database!"}


# ─── PG LISTINGS & ROOM MANAGEMENT ENDPOINTS ───────────────────────

@app.get("/api/pgs")
def get_pgs(gender_pref: Optional[str] = Query(None), search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(DBPGListing)

    if gender_pref and gender_pref != "All":
        query = query.filter(DBPGListing.gender_pref.ilike(f"%{gender_pref}%"))

    if search:
        q = f"%{search.lower().strip()}%"
        query = query.filter(or_(DBPGListing.name.ilike(q), DBPGListing.address.ilike(q)))

    all_rooms = db.query(DBPGRoom).all()
    room_images = []
    for r in all_rooms:
        if r.images:
            try:
                room_images.extend(json.loads(r.images))
            except Exception:
                pass

    result = []
    for l in query.all():
        pg_imgs = json.loads(l.images) if l.images else []
        combined_imgs = pg_imgs if pg_imgs else room_images[:5]

        result.append({
            "id": l.id,
            "owner_id": l.owner_id,
            "name": l.name,
            "distance_km": l.distance_km,
            "gender_pref": l.gender_pref,
            "sharing": l.sharing,
            "has_ac": l.has_ac,
            "monthly_price": l.monthly_price,
            "tag_label": l.tag_label,
            "address": l.address,
            "google_map_url": l.google_map_url,
            "rating": l.rating,
            "amenities": json.loads(l.amenities) if l.amenities else [],
            "images": combined_imgs
        })

    return result

@app.get("/api/admin/pg-listings")
def get_admin_pg_listings(user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(DBPGListing).all()

@app.post("/api/admin/pg-listings")
@app.post("/api/pg/add-listing")
def create_pg_listing(req: CreatePGRequest, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    new_pg = DBPGListing(
        id=f"pg-{uuid.uuid4().hex[:6]}",
        owner_id=user["id"],
        name=req.name.strip(),
        distance_km=req.distance_km,
        gender_pref=req.gender_pref,
        sharing=req.sharing,
        monthly_price=req.monthly_price,
        tag_label="Verified PG",
        address=req.address,
        google_map_url="https://maps.google.com/?q=Bokaro",
        rating=req.rating,
        amenities=json.dumps([a.strip() for a in req.amenities.split(",")]),
        images="[]"
    )
    db.add(new_pg)
    db.commit()
    return {"status": "success", "message": "PG Listing added to database successfully!"}

@app.delete("/api/admin/pg-listings/{pg_id}")
def delete_admin_pg_listing(pg_id: str, user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    pg = db.query(DBPGListing).filter(DBPGListing.id == pg_id).first()
    if not pg:
        raise HTTPException(status_code=404, detail="PG listing not found.")
    
    db.query(DBPGRoom).filter(DBPGRoom.pg_id == pg_id).delete()
    db.delete(pg)
    db.commit()
    return {"status": "success", "message": "PG Listing and affiliated rooms deleted successfully."}

@app.get("/api/rooms")
def get_rooms(db: Session = Depends(get_db)):
    rooms = db.query(DBPGRoom).all()
    return [
        {
            "room_number": r.room_number,
            "pg_id": r.pg_id,
            "room_type": r.room_type,
            "tenant_name": r.tenant_name,
            "tenant_phone": r.tenant_phone,
            "tenant_address": r.tenant_address,
            "monthly_rent": r.monthly_rent,
            "status": r.status,
            "images": json.loads(r.images) if r.images else []
        }
        for r in rooms
    ]

@app.post("/api/pg/add-room")
def add_room(req: AddRoomRequest, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    existing = db.query(DBPGRoom).filter(DBPGRoom.room_number == req.room_number).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Room number {req.room_number} already exists.")

    db.add(DBPGRoom(
        room_number=req.room_number.strip(),
        pg_id=req.pg_id,
        room_type=req.room_type.strip(),
        monthly_rent=req.monthly_rent,
        status="vacant",
        images="[]"
    ))
    db.commit()
    return {"status": "success", "message": f"Room {req.room_number} added successfully!"}

@app.post("/api/pg/update-room-images")
def update_room_images(req: UpdateRoomImagesRequest, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    if len(req.images) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 images allowed per room.")

    room = db.query(DBPGRoom).filter(DBPGRoom.room_number == req.room_number).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {req.room_number} not found.")

    optimized_images = [compress_and_convert_to_webp(img) for img in req.images]
    room.images = json.dumps(optimized_images)
    db.commit()

    return {"status": "success", "message": f"Saved {len(optimized_images)} photo(s) for Room {req.room_number}!", "images": optimized_images}


# ─── PAYMENT SPLITS, SETTLEMENT TENURE & RAZORPAY ROUTE ─────────────

@app.post("/api/vendor/onboard-route")
def onboard_vendor_razorpay_route(
    req: VendorRouteOnboardRequest,
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    clean_pan = req.pan_number.strip().upper()
    clean_ifsc = req.bank_ifsc.strip().upper()
    clean_acc = req.bank_account_no.strip()
    clean_name = req.account_holder_name.strip()

    if len(clean_pan) != 10:
        raise HTTPException(status_code=400, detail="Invalid PAN Card Number. Must be exactly 10 alphanumeric characters.")

    db_user = db.query(DBUser).filter(DBUser.id == user["id"]).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User account not found.")

    db_user.bank_account_no = clean_acc
    db_user.bank_ifsc = clean_ifsc
    db_user.account_holder_name = clean_name
    db_user.pan_number = clean_pan
    db_user.settlement_tenure = req.settlement_tenure or "instant"

    route_status_msg = "Bank details & settlement tenure saved successfully!"

    if razorpay_client:
        try:
            account_payload = {
                "email": db_user.email,
                "phone": db_user.phone or "9155118661",
                "legal_business_name": clean_name,
                "business_type": "individual",
                "profile": {
                    "category": "housing",
                    "subcategory": "real_estate_agents"
                },
                "legal_info": {
                    "pan": clean_pan
                }
            }
            acc_response = razorpay_client.account.create(account_payload) # type: ignore
            linked_account_id = acc_response.get("id")
            if linked_account_id:
                bank_payload = {
                    "ifsc_code": clean_ifsc,
                    "account_number": clean_acc,
                    "beneficiary_name": clean_name
                }
                razorpay_client.account.bank_account(linked_account_id, bank_payload) # type: ignore
                db_user.razorpay_account_id = linked_account_id
                route_status_msg = f"Direct Bank Settlement & Razorpay Route ({linked_account_id}) activated!"
        except Exception as e:
            fallback_acc_id = f"acc_linked_{uuid.uuid4().hex[:8]}"
            db_user.razorpay_account_id = fallback_acc_id
    else:
        db_user.razorpay_account_id = f"acc_linked_{uuid.uuid4().hex[:8]}"

    db.commit()

    return {
        "status": "success",
        "message": route_status_msg,
        "razorpay_account_id": db_user.razorpay_account_id,
        "settlement_tenure": db_user.settlement_tenure
    }

@app.post("/api/payments/create-order")
def create_payment_order(req: CreateOrderRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        total_paise = int(round(float(req.amount) * 100))
        if total_paise < 100:
            total_paise = 100

        vendor_user = None
        is_mess_item = any(k in req.item_name.lower() for k in ["mess", "thali", "meal", "food", "archana", "annapurna"])

        if is_mess_item:
            extracted_name = req.item_name.split("(")[0].strip() if "(" in req.item_name else req.item_name
            mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{extracted_name}%")).first()
            if mess and mess.owner_id:
                vendor_user = db.query(DBUser).filter(DBUser.id == mess.owner_id).first()
            elif mess:
                vendor_user = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess.provider_name}%")).first()
        else:
            pg = db.query(DBPGListing).filter(DBPGListing.name.ilike(f"%{req.item_name}%")).first()
            if pg and pg.owner_id:
                vendor_user = db.query(DBUser).filter(DBUser.id == pg.owner_id).first()

        vendor_account_id = vendor_user.razorpay_account_id if vendor_user else None
        settlement_tenure = vendor_user.settlement_tenure if vendor_user else "instant"
        on_hold_flag = 1 if settlement_tenure in ["admin_approval", "7_days", "15_days", "30_days"] else 0

        order_data = {
            "amount": total_paise,
            "currency": "INR",
            "receipt": f"rcpt_{uuid.uuid4().hex[:8]}",
            "notes": {
                "user_id": user["id"],
                "student_name": user["full_name"],
                "item_name": req.item_name,
                "vendor_id": vendor_user.id if vendor_user else "",
                "settlement_tenure": settlement_tenure
            }
        }

        if vendor_account_id and len(str(vendor_account_id).strip()) == 18 and str(vendor_account_id).strip().startswith("acc_"):
            platform_fee_paise = int(total_paise * 0.05)
            vendor_payout_paise = total_paise - platform_fee_paise

            order_data["transfers"] = [
                {
                    "account": str(vendor_account_id).strip(),
                    "amount": vendor_payout_paise,
                    "currency": "INR",
                    "on_hold": on_hold_flag,
                    "notes": {
                        "item_name": req.item_name,
                        "student_name": user["full_name"],
                        "settlement_tenure": settlement_tenure
                    }
                }
            ]

        order_id = f"order_{uuid.uuid4().hex[:14]}"
        if razorpay_client:
            try:
                order = razorpay_client.order.create(data=order_data) # type: ignore
                order_id = order.get("id", order_id)
            except Exception as rz_err:
                print(f"[RAZORPAY ORDER NOTICE] Fallback order generated: {rz_err}")

        return {
            "status": "success",
            "order_id": order_id,
            "amount": total_paise,
            "currency": "INR",
            "key_id": RAZORPAY_KEY_ID,
            "direct_split_active": bool(vendor_account_id),
            "vendor_tenure": settlement_tenure,
            "on_hold": on_hold_flag == 1
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create split order: {str(e)}")

@app.post("/api/payments/verify")
def verify_payment_and_fulfill(
    req: VerifyPaymentRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if RAZORPAY_KEY_SECRET and RAZORPAY_KEY_SECRET != "YOUR_SECRET_KEY":
        secret_bytes = RAZORPAY_KEY_SECRET.encode("utf-8")
        msg_bytes = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode("utf-8")
        generated_signature = hmac.new(secret_bytes, msg_bytes, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(generated_signature, req.razorpay_signature):
            raise HTTPException(status_code=400, detail="Invalid payment signature.")

    txn_id = req.razorpay_payment_id
    total_amt = float(req.monthly_amount)
    platform_fee = round(total_amt * 0.05, 2)
    vendor_payout = round(total_amt - platform_fee, 2)

    is_mess_item = any(k in req.item_name.lower() for k in ["mess", "thali", "meal", "food", "archana", "annapurna"])
    target_type = "Mess Subscription" if is_mess_item else "PG Room"
    booking_id = f"b-{uuid.uuid4().hex[:8]}"

    vendor_user = None
    target_id = None
    if is_mess_item:
        extracted_name = req.item_name.split("(")[0].strip() if "(" in req.item_name else req.item_name
        mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{extracted_name}%")).first()
        if mess:
            target_id = mess.id
            if mess.owner_id:
                vendor_user = db.query(DBUser).filter(DBUser.id == mess.owner_id).first()
            if not vendor_user:
                vendor_user = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess.provider_name}%")).first()
    else:
        pg = db.query(DBPGListing).filter(DBPGListing.name.ilike(f"%{req.item_name}%")).first()
        if pg:
            target_id = pg.id
            if pg.owner_id:
                vendor_user = db.query(DBUser).filter(DBUser.id == pg.owner_id).first()

    vendor_tenure = vendor_user.settlement_tenure if vendor_user else "instant"
    transfer_status = "Pending Approval" if vendor_tenure == "admin_approval" else ("On Hold" if vendor_tenure in ["7_days", "15_days", "30_days"] else "Settled")
    
    tenure_days_map = {"instant": 0, "7_days": 7, "15_days": 15, "30_days": 30, "admin_approval": 0}
    tenure_days = tenure_days_map.get(vendor_tenure, 0)
    settlement_due = (datetime.now() + timedelta(days=tenure_days)).strftime("%Y-%m-%d") if tenure_days > 0 else datetime.now().strftime("%Y-%m-%d")

    receipt = DBPaymentReceipt(
        transaction_id=txn_id,
        order_id=req.razorpay_order_id,
        booking_id=booking_id,
        payer_id=user["id"],
        payer_name=user["full_name"],
        payer_phone=user["phone"] or user["email"],
        vendor_id=vendor_user.id if vendor_user else None,
        vendor_account_id=vendor_user.razorpay_account_id if vendor_user else None,
        amount=total_amt,
        total_amount=total_amt,
        platform_fee=platform_fee,
        vendor_payout_amount=vendor_payout,
        payment_method="Razorpay (UPI/Card/NetBanking)",
        description=f"Paid for: {req.item_name}",
        route_transfer_status=transfer_status,
        tenure_days=tenure_days,
        settlement_due_date=settlement_due,
        payment_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # original Supabase NOT NULL column
    )
    db.add(receipt)

    # 1. Resolve admin fallback user BEFORE instantiating DBSettlement
    admin_user = db.query(DBUser).filter(DBUser.role == "admin").first()
    admin_id = admin_user.id if admin_user else "usr-admin01"

    # 2. Instantiate DBSettlement with valid vendor_id and closed parenthesis
    settlement_entry = DBSettlement(
        id=f"stl-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor_user.id if vendor_user else admin_id,
        vendor_name=vendor_user.full_name if vendor_user else "Platform Default",
        transaction_id=txn_id,
        amount=vendor_payout,
        platform_fee=platform_fee,
        status=transfer_status,
        settlement_tenure=vendor_tenure,
        requested_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    db.add(settlement_entry)

    duration = req.duration_days if req.duration_days and req.duration_days > 0 else 30
    new_expiry = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d")

    db.add(DBBooking(
        id=booking_id,
        user_id=user["id"],
        user_phone=user["phone"] or user["email"],
        target_type=target_type,
        target_id=target_id,
        item_name=req.item_name,
        move_in_date=req.move_in_date,
        expiry_date=new_expiry,
        special_requests=req.special_requests or "",
        monthly_amount=req.monthly_amount,
        payment_method="Razorpay",
        transaction_id=txn_id,
        status="Active"
    ))

    if is_mess_item:
        extracted_mess_name = req.item_name.split("(")[0].strip() if "(" in req.item_name else req.item_name
        diet_choice = req.diet_preference or "Veg"

        ms = db.query(DBMessStudent).filter(or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"]))).first()
        if ms:
            ms.is_active = True
            ms.base_price = req.monthly_amount
            ms.start_date = req.move_in_date
            ms.expiry_date = new_expiry
            ms.plan = req.item_name
            ms.diet = diet_choice
            ms.mess_id = target_id
            ms.mess_name = extracted_mess_name
        else:
            db.add(DBMessStudent(
                id=f"ms-{uuid.uuid4().hex[:6]}",
                user_id=user["id"],
                name=user["full_name"],
                phone=user["phone"] or "9155118661",
                address=user["address"] or "Hostel",
                google_map_url=user["google_map_url"] or "",
                mess_id=target_id,
                mess_name=extracted_mess_name,
                plan=req.item_name,
                diet=diet_choice,
                base_price=req.monthly_amount,
                is_active=True,
                start_date=req.move_in_date,
                expiry_date=new_expiry
            ))

        notify_owner_and_email(
            db=db,
            background_tasks=background_tasks,
            recipient_role="mess_partner",
            recipient_user_id=vendor_user.id if vendor_user else None,
            title="New Mess Subscription Confirmed",
            message=f"Student '{user['full_name']}' ({user['phone']}) subscribed to '{req.item_name}'. Amount: ₹{req.monthly_amount}. Net Payout: ₹{vendor_payout} (Settlement Status: {transfer_status}). Delivery Address: {user['address']}",
            event_type="mess_payment",
            include_admin=False
        )
    else:
        vacant_room = db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").first()
        if vacant_room:
            vacant_room.status = "occupied"
            vacant_room.tenant_id = user["id"]
            vacant_room.tenant_name = user["full_name"]
            vacant_room.tenant_phone = user["phone"]
            vacant_room.tenant_address = user["address"]

        notify_owner_and_email(
            db=db,
            background_tasks=background_tasks,
            recipient_role="pg_owner",
            recipient_user_id=vendor_user.id if vendor_user else None,
            title="New Room Booking Confirmed",
            message=f"Student '{user['full_name']}' ({user['phone']}) paid ₹{req.monthly_amount} for '{req.item_name}'. Net Payout: ₹{vendor_payout} (Settlement Status: {transfer_status}). Move-in Date: {req.move_in_date}.",
            event_type="booking",
            include_admin=False
        )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Payment Receipt: {req.item_name}",
        f"Hi {user['full_name']},\n\nYour payment has been successfully verified!\n\nTransaction ID: {txn_id}\nItem: {req.item_name}\nAmount Paid: ₹{req.monthly_amount}\nValid Through: {new_expiry}\n\nThank you for using Basera!"
    )

    return {
        "status": "success",
        "message": f"Payment verified successfully! {req.item_name} activated.",
        "settlement_status": transfer_status,
        "vendor_payout": vendor_payout,
        "platform_fee": platform_fee
    }


# ─── ADMIN SETTLEMENT RELEASE & TENURE APPROVAL WORKFLOW ───────────

@app.get("/api/admin/settlements")
def get_admin_settlements(
    status_filter: Optional[str] = Query(None),
    user: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    query = db.query(DBSettlement)
    if status_filter and status_filter != "All":
        query = query.filter(DBSettlement.status == status_filter)
    settlements = query.order_by(DBSettlement.requested_at.desc()).all()

    return [
        {
            "id": s.id,
            "vendor_id": s.vendor_id,
            "vendor_name": s.vendor_name,
            "transaction_id": s.transaction_id,
            "amount": s.amount,
            "platform_fee": s.platform_fee,
            "status": s.status,
            "payout_mode": s.payout_mode,
            "settlement_tenure": s.settlement_tenure,
            "requested_at": s.requested_at,
            "approved_at": s.approved_at,
            "approved_by": s.approved_by
        }
        for s in settlements
    ]

@app.post("/api/admin/settlements/approve")
def approve_and_release_settlement(
    req: ApproveSettlementRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    settlement = db.query(DBSettlement).filter(DBSettlement.id == req.settlement_id).first()
    if not settlement:
        raise HTTPException(status_code=404, detail="Settlement record not found.")

    if req.action == "approve":
        settlement.status = "Settled"
        settlement.approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        settlement.approved_by = user["full_name"]

        if settlement.transaction_id:
            receipt = db.query(DBPaymentReceipt).filter(DBPaymentReceipt.transaction_id == settlement.transaction_id).first()
            if receipt:
                receipt.route_transfer_status = "Settled"

        vendor = db.query(DBUser).filter(DBUser.id == settlement.vendor_id).first()
        if vendor and vendor.email:
            background_tasks.add_task(
                send_email_notification,
                vendor.email,
                "Vendor Settlement Approved & Dispatched",
                f"Hello {vendor.full_name},\n\nYour settlement payout of ₹{settlement.amount} for Transaction #{settlement.transaction_id or settlement.id} has been APPROVED and released by Platform Admin."
            )
        msg = f"Settlement #{settlement.id} approved and marked as Settled!"
    else:
        settlement.status = "Rejected"
        settlement.approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        settlement.approved_by = user["full_name"]
        msg = f"Settlement #{settlement.id} rejected."

    db.commit()
    return {"status": "success", "message": msg}


# ─── VENDOR EARNINGS & MONTHLY CREDITS ENDPOINTS ───────────────────

@app.get("/api/vendor/dashboard-summary")
def get_vendor_dashboard_summary(user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    txns = []
    active_students = 0

    if user["role"] == "mess_partner":
        my_messes = db.query(DBMessListing).filter(
            or_(DBMessListing.owner_id == user["id"], DBMessListing.provider_name.ilike(f"%{user['full_name']}%"))
        ).all()
        my_mess_ids = [m.id for m in my_messes]
        my_mess_names = [m.name for m in my_messes]

        if my_mess_ids or my_mess_names:
            txns = db.query(DBPaymentReceipt).filter(
                or_(
                    DBPaymentReceipt.vendor_id == user["id"],
                    *[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_mess_names]
                )
            ).all()

            active_students = db.query(DBMessStudent).filter(
                DBMessStudent.is_active == True,
                or_(
                    DBMessStudent.mess_id.in_(my_mess_ids),
                    *[DBMessStudent.mess_name.ilike(f"%{name}%") for name in my_mess_names]
                )
            ).count()

    elif user["role"] == "pg_owner":
        my_pgs = db.query(DBPGListing).filter(DBPGListing.owner_id == user["id"]).all()
        my_pg_ids = [p.id for p in my_pgs]
        my_pg_names = [p.name for p in my_pgs]

        txns = db.query(DBPaymentReceipt).filter(
            or_(
                DBPaymentReceipt.vendor_id == user["id"],
                *[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_pg_names]
            )
        ).all()

        active_students = db.query(DBBooking).filter(
            DBBooking.target_type == "PG Room",
            DBBooking.status == "Active",
            or_(
                DBBooking.target_id.in_(my_pg_ids),
                *[DBBooking.item_name.ilike(f"%{name}%") for name in my_pg_names]
            )
        ).count()

    else:
        txns = db.query(DBPaymentReceipt).all()
        active_students = db.query(DBBooking).filter(DBBooking.status == "Active").count()

    total_gross = sum((t.total_amount or 0.0) for t in txns)
    total_commission = sum((t.platform_fee or 0.0) for t in txns)
    total_net = sum((t.vendor_payout_amount or 0.0) for t in txns)
    total_settled = sum((t.vendor_payout_amount or 0.0) for t in txns if t.route_transfer_status == "Settled")
    total_pending_approval = sum((t.vendor_payout_amount or 0.0) for t in txns if t.route_transfer_status in ["Pending Approval", "On Hold"])
    return {
        "status": "success",
        "total_gross": round(total_gross, 2),
        "total_commission": round(total_commission, 2),
        "total_earnings": round(total_net, 2),
        "total_settled": round(total_settled, 2),
        "total_pending_approval": round(total_pending_approval, 2),
        "total_transactions": len(txns),
        "active_subscribers": active_students,
        "settlement_tenure": user.get("settlement_tenure", "instant")
    }

@app.get("/api/vendor/monthly-credits")
def get_vendor_monthly_credits(
    month: str = Query(default=datetime.now().strftime("%Y-%m")),
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    query = db.query(DBPaymentReceipt).filter(DBPaymentReceipt.payment_date.like(f"{month}%"))

    if user["role"] == "mess_partner":
        my_messes = db.query(DBMessListing).filter(
            or_(DBMessListing.owner_id == user["id"], DBMessListing.provider_name.ilike(f"%{user['full_name']}%"))
        ).all()
        my_mess_names = [m.name for m in my_messes]
        query = query.filter(
            or_(
                DBPaymentReceipt.vendor_id == user["id"],
                *[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_mess_names]
            )
        )
    elif user["role"] == "pg_owner":
        my_pgs = db.query(DBPGListing).filter(DBPGListing.owner_id == user["id"]).all()
        my_pg_names = [p.name for p in my_pgs]
        query = query.filter(
            or_(
                DBPaymentReceipt.vendor_id == user["id"],
                *[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_pg_names]
            )
        )

    txns = query.order_by(DBPaymentReceipt.payment_date.desc()).all()

    total_gross = sum((t.total_amount or 0.0) for t in txns)
    total_commission = sum((t.platform_fee or 0.0) for t in txns)
    total_net = sum((t.vendor_payout_amount or 0.0) for t in txns)

    formatted_txns = [
        {
            "id": t.transaction_id,
            "date": t.payment_date.split(" ")[0] if t.payment_date else "",
            "student_name": t.payer_name,
            "student_phone": t.payer_phone,
            "description": t.description,
            "gross_amount": t.total_amount,
            "commission_fee": t.platform_fee,
            "net_credit": t.vendor_payout_amount,
            "status": t.route_transfer_status,
            "due_date": t.settlement_due_date
        }
        for t in txns
    ]

    return {
        "status": "success",
        "month": month,
        "summary": {
            "total_gross": round(total_gross, 2),
            "total_commission": round(total_commission, 2),
            "total_net_payout": round(total_net, 2)
        },
        "transactions": formatted_txns
    }


# ─── ACCURATE MESS CANCEL & RESTORE ENGINE ──────────────────────────

@app.post("/api/mess/cancel-meal")
def cancel_meal(
    req: MealCancelRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_date = req.date if req.date else datetime.now().strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().time()

    if target_date < today_str:
        raise HTTPException(status_code=400, detail="Cannot cancel meals for past dates.")

    student = db.query(DBMessStudent).filter(
        or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"])),
        DBMessStudent.is_active == True
    ).first()

    if not student or not student.is_active or student.plan == "Unsubscribed":
        raise HTTPException(
            status_code=403,
            detail="No active mess subscription found. Please subscribe to a mess plan first."
        )

    if student.expiry_date and target_date > student.expiry_date:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel meals beyond your active subscription expiry date ({student.expiry_date})."
        )

    cutoffs = {
        "Breakfast": datetime.strptime("07:00", "%H:%M").time(),
        "Lunch": datetime.strptime("10:00", "%H:%M").time(),
        "Dinner": datetime.strptime("18:00", "%H:%M").time()
    }

    if req.meal_type not in cutoffs:
        raise HTTPException(status_code=400, detail="Invalid meal type. Must be Breakfast, Lunch, or Dinner.")

    if target_date == today_str and now_time > cutoffs[req.meal_type]:
        raise HTTPException(
            status_code=400,
            detail=f"Cutoff time ({cutoffs[req.meal_type].strftime('%I:%M %p')}) for {req.meal_type} has passed for today."
        )

    base_price = student.base_price if student.base_price > 0 else 3000
    daily_rate = base_price / 30.0

    meal_weights = {"Breakfast": 0.25, "Lunch": 0.375, "Dinner": 0.375}
    refund_amount = int(round(daily_rate * meal_weights.get(req.meal_type, 0.33)))

    month_prefix = target_date[:7]
    existing_cancels = db.query(DBMealCancellation).filter(
        or_(DBMealCancellation.user_id == user["id"], DBMealCancellation.student_name == user["full_name"]),
        DBMealCancellation.date.like(f"{month_prefix}%")
    ).all()

    current_total_refunds = sum(c.refund_amount for c in existing_cancels)
    if current_total_refunds >= base_price:
        raise HTTPException(
            status_code=400,
            detail="Maximum refund limit reached for this billing cycle. Total refunds cannot exceed your monthly base price."
        )

    if current_total_refunds + refund_amount > base_price:
        refund_amount = int(base_price - current_total_refunds)

    existing = db.query(DBMealCancellation).filter(
        or_(DBMealCancellation.user_id == user["id"], DBMealCancellation.student_name == user["full_name"]),
        DBMealCancellation.date == target_date,
        DBMealCancellation.meal == req.meal_type
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail=f"{req.meal_type} for {target_date} is already canceled.")

    new_cancel = DBMealCancellation(
        id=f"mc-{uuid.uuid4().hex[:8]}",
        user_id=user["id"],
        student_name=user["full_name"],
        phone=user["phone"] or user["email"],
        mess_id=student.mess_id,
        meal=req.meal_type,
        refund_amount=refund_amount,
        date=target_date,
        timestamp=datetime.now().strftime("%I:%M %p")
    )
    db.add(new_cancel)

    target_owner = None
    if student.mess_name:
        mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{student.mess_name}%")).first()
        if mess_listing and mess_listing.owner_id:
            target_owner = db.query(DBUser).filter(DBUser.id == mess_listing.owner_id).first()
        elif mess_listing:
            target_owner = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")).first()

    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role="mess_partner",
        recipient_user_id=target_owner.id if target_owner else None,
        title=f"Meal Canceled: {req.meal_type} ({target_date})",
        message=f"Student '{user['full_name']}' ({user['phone']}) canceled {req.meal_type} on {target_date}. Refund credited: ₹{refund_amount}.",
        event_type="meal_cancel",
        include_admin=False
    )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Cancellation Confirmed: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour request to cancel {req.meal_type} on {target_date} has been processed. ₹{refund_amount} has been credited to your monthly ledger."
    )

    return {
        "status": "success",
        "message": f"Canceled {req.meal_type} for {target_date}! ₹{refund_amount} credited to your ledger.",
        "refund_amount": refund_amount
    }

@app.post("/api/mess/uncancel-meal")
def uncancel_meal(
    req: MealCancelRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_date = req.date if req.date else datetime.now().strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().time()

    if target_date < today_str:
        raise HTTPException(status_code=400, detail="Cannot modify meal status for past dates.")

    cutoffs = {
        "Breakfast": datetime.strptime("07:00", "%H:%M").time(),
        "Lunch": datetime.strptime("10:00", "%H:%M").time(),
        "Dinner": datetime.strptime("18:00", "%H:%M").time()
    }

    if req.meal_type in cutoffs and target_date == today_str and now_time > cutoffs[req.meal_type]:
        raise HTTPException(status_code=400, detail=f"Cutoff time passed. You cannot restore {req.meal_type} for today.")

    cancel_rec = db.query(DBMealCancellation).filter(
        or_(DBMealCancellation.user_id == user["id"], DBMealCancellation.student_name == user["full_name"]),
        DBMealCancellation.date == target_date,
        DBMealCancellation.meal == req.meal_type
    ).first()

    if not cancel_rec:
        raise HTTPException(status_code=400, detail=f"No active cancellation record found for {req.meal_type} on {target_date}.")

    db.delete(cancel_rec)

    student_rec = db.query(DBMessStudent).filter(or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"]))).first()
    target_owner = None
    if student_rec and student_rec.mess_name:
        mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{student_rec.mess_name}%")).first()
        if mess_listing and mess_listing.owner_id:
            target_owner = db.query(DBUser).filter(DBUser.id == mess_listing.owner_id).first()
        elif mess_listing:
            target_owner = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")).first()

    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role="mess_partner",
        recipient_user_id=target_owner.id if target_owner else None,
        title=f"Meal Restored: {req.meal_type} ({target_date})",
        message=f"Student '{user['full_name']}' restored {req.meal_type} for {target_date}. Please count this meal in kitchen dispatch.",
        event_type="meal_restore",
        include_admin=False
    )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Restored: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour {req.meal_type} for {target_date} has been restored successfully."
    )

    return {"status": "success", "message": f"Successfully restored {req.meal_type} for {target_date}!"}

@app.post("/api/mess/unsubscribe")
def unsubscribe_mess(
    req: UnsubscribeRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_booking = None
    if req.booking_id:
        target_booking = db.query(DBBooking).filter(
            DBBooking.id == req.booking_id,
            or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"])
        ).first()

    if not target_booking and req.item_name:
        target_booking = db.query(DBBooking).filter(
            or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"]),
            DBBooking.target_type == "Mess Subscription",
            DBBooking.item_name.ilike(f"%{req.item_name}%"),
            DBBooking.status == "Active"
        ).first()

    if not target_booking:
        target_booking = db.query(DBBooking).filter(
            or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"]),
            DBBooking.target_type == "Mess Subscription",
            DBBooking.status == "Active"
        ).first()

    if not target_booking:
        raise HTTPException(status_code=404, detail="Active mess booking record not found.")

    target_booking.status = "Unsubscribed"
    mess_title = target_booking.item_name

    mess_student = db.query(DBMessStudent).filter(
        or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"]))
    ).first()

    if mess_student:
        mess_student.is_active = False
        mess_student.plan = "Unsubscribed"

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        "Mess Subscription Unsubscribed",
        f"Hi {user['full_name']},\n\nYour subscription for '{mess_title}' has been successfully unsubscribed."
    )

    return {"status": "success", "message": f"Successfully unsubscribed from {mess_title}!"}


# ─── STUDENT CALENDAR, REMINDERS & BOOKINGS ─────────────────────────

@app.get("/api/mess/student-calendar")
def get_student_calendar(
    month: str = Query(default=datetime.now().strftime("%Y-%m")),
    student_name: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_name = student_name if (student_name and user["role"] in ["mess_partner", "admin"]) else user["full_name"]
    target_student = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(target_name)).first()
    
    base_price = target_student.base_price if target_student and target_student.is_active else 0
    is_active = target_student.is_active if target_student else False

    cancels = db.query(DBMealCancellation).filter(
        DBMealCancellation.student_name == target_name,
        DBMealCancellation.date.like(f"{month}%")
    ).all()

    cancel_map = {}
    total_refund = 0
    for c in cancels:
        try:
            day_num = int(c.date.split("-")[2])
            if day_num not in cancel_map:
                cancel_map[day_num] = {}
            cancel_map[day_num][c.meal] = c.refund_amount
            total_refund += c.refund_amount
        except Exception:
            pass

    daily_breakdown = []
    for day in range(1, 31):
        day_str = f"{month}-{day:02d}"
        b_status = "Canceled" if day in cancel_map and "Breakfast" in cancel_map[day] else ("Served" if is_active else "Unsubscribed")
        l_status = "Canceled" if day in cancel_map and "Lunch" in cancel_map[day] else ("Served" if is_active else "Unsubscribed")
        d_status = "Canceled" if day in cancel_map and "Dinner" in cancel_map[day] else ("Served" if is_active else "Unsubscribed")

        daily_breakdown.append({
            "day_number": day,
            "date": day_str,
            "breakfast": b_status,
            "lunch": l_status,
            "dinner": d_status,
            "coins_refunded": sum(cancel_map.get(day, {}).values())
        })

    return {
        "student_name": target_name,
        "is_active": is_active,
        "month": month,
        "base_price": base_price,
        "total_refund_amount": total_refund,
        "final_verified_bill": max(0, base_price - total_refund) if is_active else 0,
        "daily_breakdown": daily_breakdown
    }

@app.get("/api/mess/daily-stats")
def get_mess_daily_stats(
    date: str = Query(default=datetime.now().strftime("%Y-%m-%d")),
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    query_students = db.query(DBMessStudent).filter(DBMessStudent.is_active == True)

    if user["role"] == "mess_partner":
        mess = db.query(DBMessListing).filter(
            or_(DBMessListing.owner_id == user["id"], DBMessListing.provider_name.ilike(f"%{user['full_name']}%"))
        ).first()
        target_mess_name = mess.name if mess else user["full_name"]
        query_students = query_students.filter(
            or_(
                DBMessStudent.mess_name.ilike(f"%{target_mess_name}%"),
                DBMessStudent.plan.ilike(f"%{target_mess_name}%")
            )
        )

    active_students = query_students.all()
    active_student_names = [s.name for s in active_students]

    cancels = db.query(DBMealCancellation).filter(
        DBMealCancellation.date == date,
        DBMealCancellation.student_name.in_(active_student_names)
    ).all()

    return {
        "date": date,
        "total_enrolled": len(active_students),
        "total_cancellations": len(cancels),
        "breakfast_cancels": sum(1 for c in cancels if c.meal == "Breakfast"),
        "lunch_cancels": sum(1 for c in cancels if c.meal == "Lunch"),
        "dinner_cancels": sum(1 for c in cancels if c.meal == "Dinner"),
        "cancelled_orders_list": [
            {"id": c.id, "student_name": c.student_name, "phone": c.phone, "meal": c.meal, "refund": c.refund_amount, "timestamp": c.timestamp}
            for c in cancels
        ],
        "enrolled_students": [
            {"id": s.id, "name": s.name, "phone": s.phone, "address": s.address, "google_map_url": s.google_map_url, "plan": s.plan, "diet": s.diet, "is_active": s.is_active}
            for s in active_students
        ]
    }

@app.get("/api/mess/owner-monthly-billing")
def get_owner_monthly_billing(
    month: str = Query(default=datetime.now().strftime("%Y-%m")),
    user: dict = Depends(require_vendor_or_admin),
    db: Session = Depends(get_db)
):
    query_subscribers = db.query(DBMessStudent)

    if user["role"] == "mess_partner":
        mess = db.query(DBMessListing).filter(
            or_(DBMessListing.owner_id == user["id"], DBMessListing.provider_name.ilike(f"%{user['full_name']}%"))
        ).first()
        target_mess_name = mess.name if mess else user["full_name"]
        query_subscribers = query_subscribers.filter(
            or_(
                DBMessStudent.mess_name.ilike(f"%{target_mess_name}%"),
                DBMessStudent.plan.ilike(f"%{target_mess_name}%")
            )
        )

    subscribers = query_subscribers.all()
    billing_data = []

    for s in subscribers:
        cancels = db.query(DBMealCancellation).filter(
            DBMealCancellation.student_name == s.name,
            DBMealCancellation.date.like(f"{month}%")
        ).all()

        total_refund = sum(c.refund_amount for c in cancels)
        billing_data.append({
            "student_id": s.id,
            "name": s.name,
            "phone": s.phone,
            "address": s.address,
            "google_map_url": s.google_map_url,
            "plan": s.plan,
            "diet": s.diet,
            "is_active": s.is_active,
            "base_price": s.base_price if s.is_active else 0,
            "total_cancellations": len(cancels),
            "total_refund_amount": total_refund,
            "final_verified_bill": max(0, s.base_price - total_refund) if s.is_active else 0
        })

    return {"month": month, "subscribers": billing_data}

@app.get("/api/student/reminders")
def get_student_reminders(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.now()
    mess_student = db.query(DBMessStudent).filter(or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"]))).first()
    
    mess_days_left = 30
    mess_exp_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    if mess_student and mess_student.expiry_date:
        try:
            exp_dt = datetime.strptime(mess_student.expiry_date, "%Y-%m-%d")
            mess_days_left = max(0, (exp_dt - today).days + 1)
            mess_exp_date = mess_student.expiry_date
        except Exception:
            pass

    booking = db.query(DBBooking).filter(
        or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"]),
        DBBooking.target_type == "PG Room",
        DBBooking.status == "Active"
    ).first()

    pg_days_left = 30
    pg_due_date = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    return {
        "mess_reminder": {
            "days_left": mess_days_left,
            "expiry_date": mess_exp_date,
            "should_alert": mess_days_left <= 3 and (mess_student.is_active if mess_student else False),
            "message": f"Your Mess Subscription is expiring in {mess_days_left} days ({mess_exp_date})."
        },
        "pg_reminder": {
            "has_booking": True if booking else False,
            "days_left": pg_days_left,
            "due_date": pg_due_date,
            "should_alert": pg_days_left <= 3 and booking is not None,
            "message": f"Your PG Monthly Rent of ₹{booking.monthly_amount if booking else 4200} is due soon."
        }
    }

@app.get("/api/bookings/my")
def get_my_bookings(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    b_list = db.query(DBBooking).filter(or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"])).all()
    return [
        {
            "id": b.id,
            "target_type": b.target_type,
            "item_name": b.item_name,
            "move_in_date": b.move_in_date,
            "expiry_date": b.expiry_date,
            "monthly_amount": b.monthly_amount,
            "payment_method": b.payment_method,
            "transaction_id": b.transaction_id,
            "status": b.status
        }
        for b in b_list
    ]

@app.post("/api/pg/request-vacate")
def request_student_vacate(req: RequestStudentVacate, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    booking = db.query(DBBooking).filter(DBBooking.id == req.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    booking.status = "Vacate Pending Approval"
    
    room = db.query(DBPGRoom).filter(or_(DBPGRoom.tenant_id == user["id"], DBPGRoom.tenant_name == user["full_name"])).first()
    room_num = room.room_number if room else "101"

    vreq_id = f"vreq-{uuid.uuid4().hex[:6]}"
    db.add(DBVacateRequest(
        id=vreq_id,
        room_number=room_num,
        pg_id=room.pg_id if room else None,
        student_id=user["id"],
        student_name=user["full_name"],
        student_phone=user["phone"] or user["email"],
        booking_id=booking.id,
        status="Pending"
    ))

    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role="pg_owner",
        title="Room Vacate Request Submitted",
        message=f"Student '{user['full_name']}' ({user['phone']}) requested to vacate Room {room_num}.",
        event_type="vacate_request",
        include_admin=False
    )

    db.commit()
    return {"status": "success", "message": "Vacate request submitted for PG Owner review!"}

@app.get("/api/pg/vacate-requests")
def get_vacate_requests(db: Session = Depends(get_db)):
    return [
        {
            "id": vr.id,
            "room_number": vr.room_number,
            "student_name": vr.student_name,
            "student_phone": vr.student_phone,
            "booking_id": vr.booking_id,
            "status": vr.status,
            "created_at": vr.created_at
        }
        for vr in db.query(DBVacateRequest).all()
    ]

@app.post("/api/pg/approve-vacate")
def approve_vacate_request(req: ApproveVacateRequest, background_tasks: BackgroundTasks, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    v_req = db.query(DBVacateRequest).filter(DBVacateRequest.id == req.request_id).first()
    if not v_req:
        raise HTTPException(status_code=404, detail="Vacate request not found.")

    v_req.status = "Approved"
    b = db.query(DBBooking).filter(DBBooking.id == v_req.booking_id).first()
    if b:
        b.status = "Vacated"
    r = db.query(DBPGRoom).filter(or_(DBPGRoom.room_number == v_req.room_number, DBPGRoom.tenant_name == v_req.student_name)).first()
    if r:
        r.status, r.tenant_id, r.tenant_name, r.tenant_phone, r.tenant_address = "vacant", None, "-", "-", "-"
    
    db.commit()

    student_user = db.query(DBUser).filter(or_(DBUser.id == v_req.student_id, DBUser.full_name == v_req.student_name)).first()
    if student_user and student_user.email:
        background_tasks.add_task(
            send_email_notification,
            student_user.email,
            "Room Vacate Request Approved",
            f"Hello {v_req.student_name},\n\nYour request to vacate Room {v_req.room_number} has been APPROVED by the PG owner."
        )

    return {"status": "success", "message": "Vacate request approved and room status set to vacant!"}


# ─── MENU & COMPLAINTS & NOTIFICATIONS ──────────────────────────────

@app.get("/api/mess/weekly-menu")
def get_weekly_menu(db: Session = Depends(get_db)):
    return [
        {"day_name": m.day_name, "breakfast": m.breakfast, "lunch": m.lunch, "dinner": m.dinner}
        for m in db.query(DBWeeklyMenu).all()
    ]

@app.post("/api/mess/weekly-menu/update")
def update_weekly_menu(req: WeeklyMenuUpdateRequest, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    item = db.query(DBWeeklyMenu).filter(DBWeeklyMenu.day_name == req.day_name).first()
    if not item:
        item = DBWeeklyMenu(day_name=req.day_name)
        db.add(item)
    item.breakfast, item.lunch, item.dinner = req.breakfast, req.lunch, req.dinner
    db.commit()
    return {"status": "success", "message": f"Updated menu for {req.day_name} in database!"}

@app.get("/api/complaints")
def get_complaints(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    role = user["role"]
    if role == "student":
        comps = db.query(DBComplaint).filter(or_(DBComplaint.user_id == user["id"], DBComplaint.user_phone == user["phone"])).all()
    elif role == "pg_owner":
        comps = db.query(DBComplaint).filter(
            DBComplaint.category == "PG Maintenance",
            or_(DBComplaint.target_owner_id == user["id"], DBComplaint.target_owner_id == None)
        ).all()
    elif role == "mess_partner":
        comps = db.query(DBComplaint).filter(
            DBComplaint.category == "Mess Quality",
            or_(DBComplaint.target_owner_id == user["id"], DBComplaint.target_owner_id == None)
        ).all()
    else:
        comps = db.query(DBComplaint).all()

    return [
        {
            "id": c.id,
            "user_name": c.user_name,
            "user_phone": c.user_phone,
            "category": c.category,
            "title": c.title,
            "description": c.description,
            "status": c.status,
            "created_at": c.created_at
        }
        for c in comps
    ]

@app.post("/api/complaints/create")
@app.post("/api/complaints")
def raise_complaint(req: ComplaintCreateRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cmp_id = f"cmp-{uuid.uuid4().hex[:6]}"
    target_owner_id = None
    target_owner_user = None

    if req.category == "Mess Quality":
        mess_sub = db.query(DBMessStudent).filter(
            or_(DBMessStudent.user_id == user["id"], DBMessStudent.name.ilike(user["full_name"])),
            DBMessStudent.is_active == True
        ).first()
        if mess_sub and mess_sub.mess_name:
            mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{mess_sub.mess_name}%")).first()
            if mess_listing and mess_listing.owner_id:
                target_owner_user = db.query(DBUser).filter(DBUser.id == mess_listing.owner_id).first()
    elif req.category == "PG Maintenance":
        pg_booking = db.query(DBBooking).filter(
            or_(DBBooking.user_id == user["id"], DBBooking.user_phone == user["phone"]),
            DBBooking.target_type == "PG Room",
            DBBooking.status == "Active"
        ).first()
        if pg_booking:
            pg_listing = db.query(DBPGListing).filter(DBPGListing.name.ilike(f"%{pg_booking.item_name}%")).first()
            if pg_listing and pg_listing.owner_id:
                target_owner_user = db.query(DBUser).filter(DBUser.id == pg_listing.owner_id).first()

    if target_owner_user:
        target_owner_id = target_owner_user.id

    new_complaint = DBComplaint(
        id=cmp_id,
        user_id=user["id"],
        user_name=user["full_name"],
        user_phone=user["phone"] or user["email"],
        target_owner_id=target_owner_id,
        category=req.category,
        title=req.title.strip(),
        description=req.description.strip(),
        status="Pending"
    )
    db.add(new_complaint)

    target_role = "mess_partner" if req.category == "Mess Quality" else "pg_owner"
    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role=target_role,
        recipient_user_id=target_owner_id,
        title=f"New Complaint Raised ({req.category})",
        message=f"Student '{user['full_name']}' ({user['phone']}) raised a complaint.\nTitle: {req.title}\nDetails: {req.description}",
        event_type="complaint",
        include_admin=False
    )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Complaint Ticket #{cmp_id}: {req.title}",
        f"Hi {user['full_name']},\n\nWe received your complaint regarding '{req.title}'. The respective provider has been notified directly."
    )

    return {"status": "success", "message": "Complaint logged successfully in database and dispatched to provider!"}

@app.post("/api/complaints/resolve")
def resolve_complaint(req: ResolveComplaintRequest, background_tasks: BackgroundTasks, user: dict = Depends(require_vendor_or_admin), db: Session = Depends(get_db)):
    comp = db.query(DBComplaint).filter(DBComplaint.id == req.complaint_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Complaint not found.")

    comp.status = "Resolved"
    db.commit()

    student_user = db.query(DBUser).filter(or_(DBUser.phone == comp.user_phone, DBUser.email == comp.user_phone, DBUser.full_name == comp.user_name)).first()
    if student_user and student_user.email:
        background_tasks.add_task(
            send_email_notification,
            student_user.email,
            f"Complaint Ticket Resolved: {comp.title}",
            f"Hi {comp.user_name},\n\nYour complaint '{comp.title}' has been marked as RESOLVED by the provider."
        )

    return {"status": "success", "message": f"Complaint '{comp.title}' marked as RESOLVED!"}

@app.get("/api/notifications")
def get_notifications(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] == "admin":
        notifs = db.query(DBNotification).order_by(DBNotification.created_at.desc()).all()
    else:
        notifs = db.query(DBNotification).filter(
            or_(
                DBNotification.recipient_id == user["id"],
                and_(DBNotification.recipient_id == None, DBNotification.recipient_role == user["role"]),
                DBNotification.recipient_role == "all"
            )
        ).order_by(DBNotification.created_at.desc()).all()

    return [
        {"id": n.id, "title": n.title, "message": n.message, "event_type": n.event_type, "created_at": n.created_at}
        for n in notifs
    ]


# ─── ADMIN METRICS & USER DIRECTORY ─────────────────────────────────

@app.get("/api/admin/metrics")
def get_admin_metrics(user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    all_receipts = db.query(DBPaymentReceipt).all()
    total_revenue = sum((t.total_amount or 0.0) for t in all_receipts)
    total_commission = sum((t.platform_fee or 0.0) for t in all_receipts)
    
    # Add this line to define pending_settlements
    pending_settlements = db.query(DBSettlement).filter(DBSettlement.status.in_(["Pending Approval", "On Hold"])).count()

    return {
        "active_students": db.query(DBMessStudent).filter(DBMessStudent.is_active == True).count(),
        "pg_listings": db.query(DBPGListing).count(),
        "total_messes": db.query(DBMessListing).count(),
        "vacant_rooms": db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").count(),
        "total_users": db.query(DBUser).count(),
        "pending_vacate_requests": db.query(DBVacateRequest).filter(DBVacateRequest.status == "Pending").count(),
        "pending_settlements": pending_settlements,
        "platform_commission": f"₹{round(total_commission, 2)}",
        "monthly_gmv": f"₹{round(total_revenue, 2)}"
    }

@app.get("/api/admin/users")
def get_admin_users(user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    return [
        {
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "address": u.address,
            "google_map_url": u.google_map_url,
            "role": u.role,
            "settlement_tenure": u.settlement_tenure,
            "razorpay_account_id": u.razorpay_account_id
        }
        for u in db.query(DBUser).all()
    ]

@app.get("/api/admin/email-status")
def get_email_status(user: dict = Depends(require_admin)):
    brevo_key = os.getenv("BREVO_API_KEY", os.getenv("BREVO_SMTP_KEY", "")).strip()
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    smtp_server = os.getenv("SMTP_SERVER", "").strip()

    brevo_rest_active = bool(brevo_key and brevo_key.startswith("xkeysib-"))
    smtp_active = bool((gmail_user and os.getenv("GMAIL_APP_PASSWORD")) or (smtp_server and os.getenv("SMTP_USER")))

    return {
        "status": "success",
        "providers": {
            "brevo_api": {
                "configured": brevo_rest_active,
                "type": "Brevo REST API (Port 443)" if brevo_rest_active else "Not set",
                "sender": os.getenv("BREVO_SENDER_EMAIL", "basera4you@gmail.com")
            },
            "smtp": {
                "configured": smtp_active,
                "server": smtp_server or ("smtp.gmail.com" if gmail_user else "Not set"),
                "user": gmail_user or os.getenv("SMTP_USER", "Not set")
            }
        },
        "recent_logs": EMAIL_AUDIT_LOG
    }

@app.get("/api/admin/email-logs")
def get_email_logs(user: dict = Depends(require_admin)):
    return EMAIL_AUDIT_LOG

@app.post("/api/admin/send-test-email")
def send_test_email(req: TestEmailRequest, background_tasks: BackgroundTasks, user: dict = Depends(require_admin)):
    background_tasks.add_task(
        send_email_notification,
        req.recipient,
        req.subject or "Basera Diagnostic Test Email",
        req.body or "This is an automated diagnostic test email dispatched from Basera Admin Console."
    )
    return {"status": "success", "message": f"Test email queued for {req.recipient}."}

@app.post("/api/admin/reset-entire-database")
def reset_entire_database(user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        db.query(DBMealCancellation).delete()
        db.query(DBVacateRequest).delete()
        db.query(DBComplaint).delete()
        db.query(DBNotification).delete()
        db.query(DBSettlement).delete()
        db.query(DBPaymentReceipt).delete()
        db.query(DBBooking).delete()
        db.query(DBMessStudent).delete()
        db.query(DBMessPricing).delete()
        db.query(DBMessListing).delete()
        db.query(DBPGRoom).delete()
        db.query(DBPGListing).delete()
        db.query(DBUser).filter(DBUser.role != "admin").delete()
        db.commit()
        return {"status": "success", "message": "Database successfully reset to pristine condition."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to reset database: {str(e)}")


