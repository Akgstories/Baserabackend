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
from datetime import datetime, timedelta
from typing import List, Optional

import razorpay
from PIL import Image

from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from sqlalchemy import create_engine, String, Integer, Boolean, Float, Text, DateTime, or_, and_, text, inspect
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, Mapped, mapped_column

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

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


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
        print(f"[IMAGE COMPRESSION NOTICE] Fallback to raw data: {e}")
        return base64_data


# ─── ROBUST MULTI-PROVIDER EMAIL DISPATCHER & AUDIT ENGINE ─────────
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
    if len(EMAIL_AUDIT_LOG) > 25:
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
                    print(f"[BREVO REST SUCCESS] Sent to {recipient_email}")
                    log_email_event(recipient_email, subject, "Brevo REST API", "SUCCESS")
                    return
                else:
                    resp_body = response.read().decode("utf-8")
                    log_email_event(recipient_email, subject, "Brevo REST API", "FAILED", f"HTTP {response.status}: {resp_body}")
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8")
            log_email_event(recipient_email, subject, "Brevo REST API", "FAILED", f"HTTP {e.code}: {err_text}")
        except Exception as e:
            log_email_event(recipient_email, subject, "Brevo REST API", "FAILED", str(e))

    if clean_brevo_key and clean_brevo_key.startswith("xsmtpsib-"):
        dispatch_attempted = True
        for port, use_ssl in [(465, True), (587, False)]:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = f"Basera Platform <{sender_email}>"
                msg["To"] = recipient_email
                msg.attach(MIMEText(body_text, "plain"))
                if html_content:
                    msg.attach(MIMEText(html_content, "html"))

                if use_ssl:
                    with smtplib.SMTP_SSL("smtp-relay.brevo.com", port, timeout=6) as server:
                        server.login(sender_email, clean_brevo_key)
                        server.sendmail(sender_email, recipient_email, msg.as_string())
                else:
                    with smtplib.SMTP("smtp-relay.brevo.com", port, timeout=6) as server:
                        server.starttls()
                        server.login(sender_email, clean_brevo_key)
                        server.sendmail(sender_email, recipient_email, msg.as_string())

                print(f"[BREVO SMTP SUCCESS] Sent to {recipient_email} via Port {port}")
                log_email_event(recipient_email, subject, "Brevo SMTP Relay", "SUCCESS")
                return
            except Exception as e:
                print(f"[BREVO SMTP NOTICE] Port {port} failed: {e}")
        
        log_email_event(recipient_email, subject, "Brevo SMTP Relay", "FAILED", "Render blocked outbound SMTP ports 587/465. Switch to REST API key (xkeysib-).")

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

            print(f"[SMTP EMAIL SUCCESS] Sent to {recipient_email} via {smtp_server}")
            log_email_event(recipient_email, subject, f"SMTP ({smtp_server})", "SUCCESS")
            return
        except Exception as e:
            print(f"[SMTP EMAIL ERROR] Failed via {smtp_server}: {e}")
            log_email_event(recipient_email, subject, f"SMTP ({smtp_server})", "FAILED", str(e))

    if not dispatch_attempted:
        print(f"[EMAIL DISPATCH NOTICE] No active email provider configured for {recipient_email}")
        log_email_event(recipient_email, subject, "Unconfigured", "Missing Brevo API/SMTP key or generic SMTP credentials in environment variables.")


# ─── SQLALCHEMY ORM MODELS ────────────────────────────────────────
class DBUser(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, index=True, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, default="")
    address: Mapped[str] = mapped_column(String, default="GEC Bokaro Hostel, Room 101")
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro")
    role: Mapped[str] = mapped_column(String, default="student")
    token: Mapped[str] = mapped_column(String, nullable=True)

    # Razorpay Route Linked Account Fields
    razorpay_account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True) # acc_XXXXXXXXXXXXXX
    bank_account_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bank_ifsc: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    account_holder_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pan_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DBPGListing(Base):
    __tablename__ = "pg_listings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # <-- ADD THIS LINE
    name: Mapped[str] = mapped_column(String, nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    gender_pref: Mapped[str] = mapped_column(String, nullable=False)
    sharing: Mapped[str] = mapped_column(String, nullable=False)
    has_ac: Mapped[bool] = mapped_column(Boolean, default=False)
    monthly_price: Mapped[int] = mapped_column(Integer, nullable=False)
    tag_label: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=Chandankiyari+Bokaro")
    rating: Mapped[str] = mapped_column(String, nullable=False)
    amenities: Mapped[str] = mapped_column(Text, nullable=False)
    images: Mapped[str] = mapped_column(Text, default="[]")

class DBMessListing(Base):
    __tablename__ = "mess_listings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider_name: Mapped[str] = mapped_column(String, nullable=False)
    monthly_price: Mapped[int] = mapped_column(Integer, nullable=False)
    diet_type: Mapped[str] = mapped_column(String, nullable=False)
    meals_per_day: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro+Main+Gate")
    description: Mapped[str] = mapped_column(Text, nullable=False)

class DBMessPricing(Base):
    __tablename__ = "mess_pricing"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    mess_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    location_name: Mapped[str] = mapped_column(String, nullable=False)
    three_time_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    two_time_rate: Mapped[int] = mapped_column(Integer, nullable=False)

class DBPGRoom(Base):
    __tablename__ = "pg_rooms"
    room_number: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    room_type: Mapped[str] = mapped_column(String, nullable=False)
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
    student_name: Mapped[str] = mapped_column(String, nullable=False)
    student_phone: Mapped[str] = mapped_column(String, nullable=False)
    booking_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBMessStudent(Base):
    __tablename__ = "mess_students"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro+Hostel")
    mess_name: Mapped[str] = mapped_column(String, default="Annapurna Homely Mess")
    plan: Mapped[str] = mapped_column(String, nullable=False)
    diet: Mapped[str] = mapped_column(String, nullable=False)
    base_price: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expiry_date: Mapped[str] = mapped_column(String, default=lambda: (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))

class DBMealCancellation(Base):
    __tablename__ = "meal_cancellations"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    student_name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    meal: Mapped[str] = mapped_column(String, nullable=False)
    refund_amount: Mapped[int] = mapped_column(Integer, default=50)
    date: Mapped[str] = mapped_column(String, nullable=False)
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
    user_phone: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    item_name: Mapped[str] = mapped_column(String, nullable=False)
    move_in_date: Mapped[str] = mapped_column(String, nullable=False)
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
    payer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    vendor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    vendor_account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payer_name: Mapped[str] = mapped_column(String, nullable=False)
    payer_phone: Mapped[str] = mapped_column(String, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Float, default=0.0)
    vendor_payout_amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_method: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    route_transfer_status: Mapped[str] = mapped_column(String, default="Pending")
    payment_date: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

class DBComplaint(Base):
    __tablename__ = "complaints"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    user_phone: Mapped[str] = mapped_column(String, nullable=False)
    target_owner_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True) # <-- ADD THIS
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBNotification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    recipient_role: Mapped[str] = mapped_column(String, nullable=False)
    recipient_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String, default="general")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

class VendorRouteOnboardRequest(BaseModel):
    account_holder_name: str
    bank_account_no: str
    bank_ifsc: str
    pan_number: str

# ─── DATABASE INITIALIZATION ──────────────────────────────────────────
def seed_database():
    try:
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)

        with engine.connect() as conn:
            if "notifications" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("notifications")]
                if "recipient_id" not in cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN recipient_id TEXT DEFAULT NULL;"))

            if "mess_pricing" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("mess_pricing")]
                if "mess_id" not in cols:
                    conn.execute(text("ALTER TABLE mess_pricing ADD COLUMN mess_id VARCHAR DEFAULT 'mess-1';"))

            if "pg_rooms" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("pg_rooms")]
                if "images" not in cols:
                    conn.execute(text("ALTER TABLE pg_rooms ADD COLUMN images TEXT DEFAULT '[]';"))

            if "pg_listings" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("pg_listings")]
                if "images" not in cols:
                    conn.execute(text("ALTER TABLE pg_listings ADD COLUMN images TEXT DEFAULT '[]';"))

            if "mess_students" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("mess_students")]
                if "mess_name" not in cols:
                    conn.execute(text("ALTER TABLE mess_students ADD COLUMN mess_name TEXT DEFAULT 'Annapurna Homely Mess';"))

            if "complaints" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("complaints")]
                if "target_owner_id" not in cols:
                    conn.execute(text("ALTER TABLE complaints ADD COLUMN target_owner_id TEXT DEFAULT NULL;"))
            conn.commit()

        db = SessionLocal()
        admin_user = db.query(DBUser).filter(DBUser.role == "admin").first()
        if not admin_user:
            db.add(DBUser(
                id="usr-admin01", full_name="Platform Admin", email="akgstories02@gmail.com",
                password="admin@2026", phone="9155118661", address="Admin Office, GEC Bokaro",
                google_map_url="https://maps.google.com/?q=GEC+Bokaro", role="admin", token="tok-admin123"
            ))

        if not db.query(DBMessListing).first():
            db.add_all([
                DBMessListing(
                    id="mess-1", name="Annapurna Homely Mess", provider_name="Ramesh Sharma",
                    monthly_price=3000, diet_type="Veg & Non-Veg", meals_per_day="Flexible Plan Options",
                    rating="4.9 (42 reviews)", address="📍 Near GEC Bokaro Main Gate",
                    google_map_url="https://maps.google.com/?q=GEC+Bokaro+Main+Gate",
                    description="Freshly prepared hygienic meals tailored for engineering students."
                ),
                DBMessListing(
                    id="mess-2", name="Shuddha Shakahari Mess", provider_name="Geeta Devi",
                    monthly_price=2600, diet_type="Pure Veg", meals_per_day="Flexible Plan Options",
                    rating="4.8 (31 reviews)", address="📍 Vill-Ghoragara, Chandankiyari",
                    google_map_url="https://maps.google.com/?q=Chandankiyari+Bokaro",
                    description="100% Pure Vegetarian North & South Indian meals cooked with pure desi ghee."
                ),
                DBMessListing(
                    id="mess-3", name="Archana mess", provider_name="Archana Devi",
                    monthly_price=3600, diet_type="Veg & Non-Veg", meals_per_day="Flexible Plan Options",
                    rating="5.0 (New)", address="📍 Near GEC Bokaro",
                    google_map_url="https://maps.google.com/?q=GEC+Bokaro",
                    description="Freshly prepared hygienic meals."
                )
            ])

        if not db.query(DBMessPricing).first():
            db.add_all([
                # Annapurna Homely Mess (mess-1)
                DBMessPricing(mess_id="mess-1", location_name="GEC Main Gate", three_time_rate=100, two_time_rate=80),
                DBMessPricing(mess_id="mess-1", location_name="Chandankiyari", three_time_rate=90, two_time_rate=70),
                DBMessPricing(mess_id="mess-1", location_name="Ghoragara", three_time_rate=110, two_time_rate=85),

                # Shuddha Shakahari Mess (mess-2)
                DBMessPricing(mess_id="mess-2", location_name="GEC Main Gate", three_time_rate=90, two_time_rate=70),
                DBMessPricing(mess_id="mess-2", location_name="Chandankiyari", three_time_rate=80, two_time_rate=65),
                DBMessPricing(mess_id="mess-2", location_name="Ghoragara", three_time_rate=95, two_time_rate=75),

                # Archana mess (mess-3)
                DBMessPricing(mess_id="mess-3", location_name="GEC Main Gate", three_time_rate=120, two_time_rate=95),
                DBMessPricing(mess_id="mess-3", location_name="Chandankiyari", three_time_rate=110, two_time_rate=85),
                DBMessPricing(mess_id="mess-3", location_name="Ghoragara", three_time_rate=125, two_time_rate=100)
            ])

        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for d in days:
            if not db.query(DBWeeklyMenu).filter(DBWeeklyMenu.day_name == d).first():
                db.add(DBWeeklyMenu(
                    day_name=d,
                    breakfast=f"{d} Special Paratha & Tea",
                    lunch=f"{d} Standard Rice, Dal & Seasonal Sabzi",
                    dinner=f"{d} Special Paneer / Non-Veg Curry with Roti"
                ))

        if not db.query(DBPGListing).first():
            db.add_all([
                DBPGListing(
                    id="pg-1", name="Power Grid Scholars Boys PG", distance_km=0.4,
                    gender_pref="Boys PG", sharing="Double Sharing", has_ac=True, monthly_price=4200,
                    tag_label="Vacant", address="📍 Vill-Ghoragara, P.O-Kherabera, Chandankiyari",
                    google_map_url="https://maps.google.com/?q=23.5750,86.3500",
                    rating="4.9 (28)", amenities=json.dumps(["Wi-Fi", "Homely Mess", "Power Backup"]), images="[]"
                ),
                DBPGListing(
                    id="pg-2", name="Chandankiyari Comfort Girls PG", distance_km=0.2,
                    gender_pref="Girls PG", sharing="Single Room", has_ac=True, monthly_price=4800,
                    tag_label="1 left", address="📍 P.O-Kherabera, Chandankiyari, Bokaro",
                    google_map_url="https://maps.google.com/?q=23.5780,86.3520",
                    rating="4.8 (19)", amenities=json.dumps(["CCTV", "3-Time Food", "Geyser"]), images="[]"
                )
            ])

        if not db.query(DBPGRoom).first():
            db.add_all([
                DBPGRoom(room_number="101", room_type="Single AC", tenant_name="Rahul Kumar", tenant_phone="9876543210", tenant_address="GEC Bokaro Hostel Block A", monthly_rent=8000, status="occupied", images="[]"),
                DBPGRoom(room_number="102", room_type="Double Non-AC", tenant_name="-", tenant_phone="-", tenant_address="-", monthly_rent=5200, status="vacant", images="[]")
            ])

        if not db.query(DBMessStudent).first():
            db.add_all([
                DBMessStudent(id="ms-101", name="Aditya Kumar", phone="9155118661", address="GEC Bokaro Hostel, Room 101", google_map_url="https://maps.google.com/?q=GEC+Bokaro+Hostel", mess_name="Annapurna Homely Mess", plan="3-Time Standard Daily Plan", diet="Non-Veg", base_price=3000, is_active=True),
                DBMessStudent(id="ms-102", name="Tushar Das", phone="9876542170", address="Power Grid Boys PG, Room 204", google_map_url="https://maps.google.com/?q=23.5750,86.3500", mess_name="Shuddha Shakahari Mess", plan="2-Time Standard Plan", diet="Veg", base_price=2500, is_active=True)
            ])

        db.commit()
        db.close()
        print("[DATABASE] Connection and seeding successful!")
    except Exception as e:
        print(f"[DATABASE NOTICE] Seed skipped or DB offline: {e}")


app = FastAPI(title="Basera Multi-Portal API", version="16.0.0")

@app.middleware("http")
async def cors_handler(request: Request, call_next):
    origin = request.headers.get("origin", "*")
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
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
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
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

# ─── TARGETED NOTIFICATION ENGINE (ADMIN EXCLUSION SUPPORT) ─────────
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


# ─── SCHEMAS ────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
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
    new_password: str

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
    three_time_rate: int
    two_time_rate: int

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
    provider_name: str
    monthly_price: int
    diet_type: str
    meals_per_day: str
    address: str
    google_map_url: str
    description: str


class CreateOrderRequest(BaseModel):
    amount: float  # Change int to float
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
    rating: str = "4.5"
    amenities: str = "Wi-Fi, RO Water, Security"

class UnsubscribeRequest(BaseModel):
    booking_id: Optional[str] = None
    item_name: Optional[str] = None



def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authentication token.")
    token = authorization.split(" ")[1]
    user = db.query(DBUser).filter(DBUser.token == token).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session expired.")
    return {"id": user.id, "full_name": user.full_name, "email": user.email, "phone": user.phone, "address": user.address, "google_map_url": user.google_map_url, "role": user.role}


# ─── ENDPOINTS ───────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "online", "platform": "Basera Engine", "database": "Active"}

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
            "three_time_rate": p.three_time_rate,
            "two_time_rate": p.two_time_rate
        } for p in pricing
    ]

@app.post("/api/mess/update-location-price")
def update_mess_location_price(
    req: UpdateMessLocationPricingRequest, 
    user: dict = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if user["role"] not in ["mess_partner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized action.")
    
    item = db.query(DBMessPricing).filter(
        DBMessPricing.mess_id == req.mess_id,
        DBMessPricing.location_name.ilike(req.location_name)
    ).first()
    
    if not item:
        item = DBMessPricing(
            mess_id=req.mess_id,
            location_name=req.location_name,
            three_time_rate=req.three_time_rate,
            two_time_rate=req.two_time_rate
        )
        db.add(item)
    else:
        item.three_time_rate = req.three_time_rate
        item.two_time_rate = req.two_time_rate

    db.commit()
    return {"status": "success", "message": f"Updated rates for {req.location_name} successfully!"}

@app.post("/api/auth/register")
def register_user(req: RegisterRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    role = req.role or "student"
    if role == "admin":
        raise HTTPException(status_code=400, detail="Admin account creation is disabled. Sign in with admin credentials.")

    clean_phone = req.phone.strip()
    if len(clean_phone) != 10 or not clean_phone.isdigit():
        raise HTTPException(status_code=400, detail="Mobile number must be exactly 10 digits.")

    existing = db.query(DBUser).filter(DBUser.email == req.email, DBUser.role == role).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Account with email '{req.email}' already exists for role '{role}'.")

    user_id = f"usr-{uuid.uuid4().hex[:8]}"
    token = f"tok-{uuid.uuid4().hex}"
    map_url = req.google_map_url or "https://maps.google.com/?q=GEC+Bokaro"

    new_user = DBUser(
        id=user_id, full_name=req.full_name, email=req.email,
        password=req.password, phone=clean_phone, address=req.address or "GEC Bokaro Hostel",
        google_map_url=map_url, role=role, token=token
    )
    db.add(new_user)

    if role == "student":
        db.add(DBMessStudent(
            id=f"ms-{uuid.uuid4().hex[:6]}", name=new_user.full_name,
            phone=clean_phone, address=new_user.address, google_map_url=map_url,
            mess_name="Annapurna Homely Mess", plan="3-Time Standard Daily Plan", diet="Veg", base_price=3000, is_active=True
        ))

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        req.email,
        "Welcome to Basera Platform!",
        f"Hello {req.full_name},\n\nYour account has been registered successfully as a {role.upper()} on Basera.\n\nPhone: {clean_phone}\nAddress: {req.address}\n\nThank you for joining Basera!"
    )

    return {"status": "success", "token": token, "user": {"id": new_user.id, "full_name": new_user.full_name, "email": new_user.email, "phone": new_user.phone, "address": new_user.address, "google_map_url": new_user.google_map_url, "role": new_user.role}}

@app.post("/api/auth/login")
def login_user(req: LoginRequest, db: Session = Depends(get_db)):
    query = db.query(DBUser).filter(
        DBUser.email == req.email, 
        DBUser.password == req.password
    )
    
    if req.target_role:
        query = query.filter(DBUser.role == req.target_role)

    user = query.first()
    if not user:
        role_label = req.target_role.replace('_', ' ').title() if req.target_role else "selected"
        raise HTTPException(
            status_code=401, 
            detail=f"No account registered under the '{role_label}' role with these credentials. Please select the correct role or create an account."
        )

    user.token = f"tok-{uuid.uuid4().hex}"
    db.commit()
    return {
        "status": "success", 
        "token": user.token, 
        "user": {
            "id": user.id, 
            "full_name": user.full_name, 
            "email": user.email, 
            "phone": user.phone, 
            "address": user.address, 
            "google_map_url": user.google_map_url, 
            "role": user.role
        }
    }

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

    ms = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(user["full_name"])).first()
    if ms:
        ms.phone = clean_phone
        ms.address = req.address
        if req.google_map_url:
            ms.google_map_url = req.google_map_url

    db.commit()
    return {"status": "success", "message": "Profile address, phone, and Google Map location updated successfully!"}

@app.post("/api/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    users = db.query(DBUser).filter(DBUser.email == req.email, DBUser.phone == req.phone).all()
    if not users:
        raise HTTPException(status_code=404, detail="Email and Phone combination not found.")
    for u in users:
        u.password = req.new_password
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        req.email,
        "Basera Password Reset Confirmation",
        "Your Basera account password has been updated successfully. If you did not request this change, please contact platform support immediately."
    )

    return {"status": "success", "message": "Password updated successfully!"}

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {"status": "success", "user": user}

@app.get("/api/mess-listings")
def get_mess_listings(db: Session = Depends(get_db)):
    listings = db.query(DBMessListing).all()
    return [
        {
            "id": m.id, "name": m.name, "provider_name": m.provider_name,
            "monthly_price": m.monthly_price, "diet_type": m.diet_type,
            "meals_per_day": m.meals_per_day, "rating": m.rating,
            "address": m.address, "google_map_url": m.google_map_url, "description": m.description
        }
        for m in listings
    ]

@app.post("/api/mess/add-listing")
def create_mess_listing(
    req: CreateMessListingRequest, 
    user: dict = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if user["role"] not in ["mess_partner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized action.")
        
    mess_id = f"mess-{uuid.uuid4().hex[:6]}"
    new_mess = DBMessListing(
        id=mess_id,
        name=req.name,
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
        DBMessPricing(mess_id=mess_id, location_name="GEC Main Gate", three_time_rate=100, two_time_rate=80),
        DBMessPricing(mess_id=mess_id, location_name="Chandankiyari", three_time_rate=90, two_time_rate=70),
        DBMessPricing(mess_id=mess_id, location_name="Ghoragara", three_time_rate=110, two_time_rate=85)
    ])

    db.commit()
    return {"status": "success", "message": f"Mess '{req.name}' listed successfully with dynamic location pricing!", "mess_id": mess_id}

@app.delete("/api/mess-listings/{mess_id}")
def delete_mess_listing(mess_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete mess listings.")

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
def update_mess_price(req: UpdateMessPriceRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] not in ["mess_partner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized action.")
    
    mess = db.query(DBMessListing).filter(DBMessListing.id == req.mess_id).first()
    if not mess:
        mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{req.mess_id}%")).first()
    if not mess:
        raise HTTPException(status_code=404, detail="Mess listing not found.")
    
    mess.monthly_price = req.monthly_price
    db.commit()
    return {"status": "success", "message": f"Updated monthly price for '{mess.name}' to ₹{req.monthly_price}!"}

@app.get("/api/student/reminders")
def get_student_reminders(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.now()

    mess_student = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(user["full_name"])).first()
    mess_days_left = 3
    mess_exp_date = (today + timedelta(days=3)).strftime("%Y-%m-%d")

    if mess_student and mess_student.expiry_date:
        try:
            exp_dt = datetime.strptime(mess_student.expiry_date, "%Y-%m-%d")
            mess_days_left = max(0, (exp_dt - today).days + 1)
            mess_exp_date = mess_student.expiry_date
        except Exception:
            pass

    booking = db.query(DBBooking).filter(or_(DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"]), DBBooking.status == "Active").first()
    pg_days_left = 2
    pg_due_date = (today + timedelta(days=2)).strftime("%Y-%m-%d")

    return {
        "mess_reminder": {
            "days_left": mess_days_left, "expiry_date": mess_exp_date,
            "should_alert": mess_days_left <= 3,
            "message": f"Your Mess Subscription is expiring in {mess_days_left} days ({mess_exp_date})."
        },
        "pg_reminder": {
            "has_booking": True if booking else False,
            "days_left": pg_days_left, "due_date": pg_due_date,
            "should_alert": pg_days_left <= 3 and booking is not None,
            "message": f"Your PG Monthly Rent of ₹{booking.monthly_amount if booking else 4200} is due in {pg_days_left} days."
        }
    }

@app.post("/api/student/send-reminder-emails")
def send_reminder_emails(background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    reminders = get_student_reminders(user=user, db=db)
    mess_rem = reminders["mess_reminder"]
    pg_rem = reminders["pg_reminder"]

    if mess_rem["should_alert"]:
        background_tasks.add_task(
            send_email_notification,
            user["email"],
            "Reminder: Mess Subscription Expiring Soon",
            f"Hi {user['full_name']},\n\n{mess_rem['message']}\n\nPlease renew your subscription to ensure uninterrupted meal services."
        )

    if pg_rem["should_alert"]:
        background_tasks.add_task(
            send_email_notification,
            user["email"],
            "Reminder: Monthly PG Rent Payment Due",
            f"Hi {user['full_name']},\n\n{pg_rem['message']}\n\nPlease clear your monthly rent payment from your student portal dashboard."
        )

    return {"status": "success", "message": "Reminder emails dispatched to student inbox!"}

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

@app.post("/api/payments/verify")
def verify_payment_and_fulfill(
    req: VerifyPaymentRequest, 
    background_tasks: BackgroundTasks, 
    user: dict = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    generated_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode(),
        hashlib.sha256
    ).hexdigest()

    if generated_signature != req.razorpay_signature:
        raise HTTPException(status_code=400, detail="Payment verification failed! Invalid signature.")

    txn_id = req.razorpay_payment_id
    db.add(DBPaymentReceipt(
        transaction_id=txn_id,
        payer_name=user["full_name"],
        payer_phone=user["phone"] or user["email"],
        amount=req.monthly_amount,
        payment_method="Razorpay (UPI/Card/NetBanking)",
        description=f"Paid for: {req.item_name}",
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    is_mess_item = any(k in req.item_name.lower() for k in ["mess", "thali", "meal", "food", "archana", "annapurna"])
    target_type = "Mess Subscription" if is_mess_item else "PG Room"

    booking_id = f"b-{uuid.uuid4().hex[:8]}"
    db.add(DBBooking(
        id=booking_id,
        user_phone=user["phone"] or user["email"],
        target_type=target_type,
        item_name=req.item_name,
        move_in_date=req.move_in_date,
        special_requests=req.special_requests or "",
        monthly_amount=req.monthly_amount,
        payment_method="Razorpay",
        transaction_id=txn_id,
        status="Active"
    ))

    if is_mess_item:
        extracted_mess_name = req.item_name.split("(")[0].strip() if "(" in req.item_name else req.item_name
        mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{extracted_mess_name}%")).first()
        target_owner = None
        if mess_listing:
            target_owner = db.query(DBUser).filter(
                DBUser.role == "mess_partner", 
                DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")
            ).first()

        ms = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(user["full_name"])).first()
        duration = req.duration_days if req.duration_days and req.duration_days > 0 else 30
        new_expiry = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d")
        diet_choice = req.diet_preference or "Veg"

        if ms:
            ms.is_active = True
            ms.base_price = req.monthly_amount
            ms.expiry_date = new_expiry
            ms.plan = req.item_name
            ms.diet = diet_choice
            ms.mess_name = extracted_mess_name
        else:
            db.add(DBMessStudent(
                id=f"ms-{uuid.uuid4().hex[:6]}",
                name=user["full_name"],
                phone=user["phone"] or "9155118661",
                address=user["address"] or "Hostel",
                google_map_url=user["google_map_url"] or "",
                mess_name=extracted_mess_name,
                plan=req.item_name,
                diet=diet_choice,
                base_price=req.monthly_amount,
                is_active=True,
                expiry_date=new_expiry
            ))

        # Notify strictly Mess Owner (include_admin=False)
        notify_owner_and_email(
            db=db,
            background_tasks=background_tasks,
            recipient_role="mess_partner",
            recipient_user_id=target_owner.id if target_owner else None,
            title="New Mess Subscription Confirmed",
            message=f"Student '{user['full_name']}' ({user['phone']}) subscribed to '{req.item_name}' (Diet Preference: {diet_choice}, Amount Paid: ₹{req.monthly_amount}, Duration: {duration} days). Delivery Address: {user['address']}",
            event_type="mess_payment",
            include_admin=False
        )
    else:
        vacant_room = db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").first()
        if vacant_room:
            vacant_room.status = "occupied"
            vacant_room.tenant_name = user["full_name"]
            vacant_room.tenant_phone = user["phone"]
            vacant_room.tenant_address = user["address"]

        # Notify strictly PG Owner (include_admin=False)
        notify_owner_and_email(
            db=db,
            background_tasks=background_tasks,
            recipient_role="pg_owner",
            title="New Room Booking Paid & Confirmed",
            message=f"Student '{user['full_name']}' ({user['phone']}) paid ₹{req.monthly_amount} for '{req.item_name}'. Move-in Date: {req.move_in_date}.",
            event_type="booking",
            include_admin=False
        )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Payment & Service Receipt: {req.item_name}",
        f"Hi {user['full_name']},\n\nYour payment has been successfully processed!\n\nTransaction ID: {txn_id}\nItem/Service: {req.item_name}\nAmount Paid: ₹{req.monthly_amount}\nEffective Date: {req.move_in_date}\n\nThank you for using Basera!"
    )

    return {"status": "success", "message": f"Payment verified successfully! {req.item_name} activated."}

@app.post("/api/mess/cancel-meal")
def cancel_meal(req: MealCancelRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    target_date = req.date if req.date else datetime.now().strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().time()

    if target_date < today_str:
        raise HTTPException(status_code=400, detail="Cannot cancel meals for past dates.")

    cutoffs = {
        "Breakfast": (datetime.strptime("07:00", "%H:%M").time(), 30),
        "Lunch": (datetime.strptime("10:00", "%H:%M").time(), 50),
        "Dinner": (datetime.strptime("18:00", "%H:%M").time(), 50)
    }

    if req.meal_type not in cutoffs:
        raise HTTPException(status_code=400, detail="Invalid meal type.")

    cutoff_time, refund_coins = cutoffs[req.meal_type]

    if target_date == today_str and now_time > cutoff_time:
        raise HTTPException(status_code=400, detail=f"Cutoff time ({cutoff_time.strftime('%I:%M %p')}) for {req.meal_type} has passed for today.")

    existing = db.query(DBMealCancellation).filter(
        DBMealCancellation.student_name == user["full_name"],
        DBMealCancellation.date == target_date,
        DBMealCancellation.meal == req.meal_type
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail=f"{req.meal_type} for {target_date} is already canceled.")

    new_cancel = DBMealCancellation(
        id=f"mc-{uuid.uuid4().hex[:8]}", student_name=user["full_name"],
        phone=user["phone"] or "9155118661", meal=req.meal_type,
        refund_amount=refund_coins, date=target_date, timestamp=datetime.now().strftime("%I:%M %p")
    )
    db.add(new_cancel)

    student_rec = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(user["full_name"])).first()
    target_owner = None
    if student_rec and student_rec.mess_name:
        mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{student_rec.mess_name}%")).first()
        if mess_listing:
            target_owner = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")).first()

    # Admin explicitly excluded (include_admin=False)
    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role="mess_partner",
        recipient_user_id=target_owner.id if target_owner else None,
        title=f"Meal Canceled: {req.meal_type} ({target_date})",
        message=f"Student '{user['full_name']}' ({user['phone']}) canceled {req.meal_type} for date {target_date}. Refund credited: ₹{refund_coins}.",
        event_type="meal_cancel",
        include_admin=False
    )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Cancellation Confirmed: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour request to cancel {req.meal_type} for {target_date} has been processed.\n\n₹{refund_coins} refund has been credited to your monthly ledger statement."
    )

    return {"status": "success", "message": f"Canceled {req.meal_type} for {target_date}! ₹{refund_coins} credited."}

@app.post("/api/mess/uncancel-meal")
def uncancel_meal(req: MealCancelRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
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
        DBMealCancellation.student_name == user["full_name"],
        DBMealCancellation.date == target_date,
        DBMealCancellation.meal == req.meal_type
    ).first()

    if not cancel_rec:
        raise HTTPException(status_code=400, detail=f"No active cancellation record found for {req.meal_type} on {target_date}.")

    db.delete(cancel_rec)

    student_rec = db.query(DBMessStudent).filter(DBMessStudent.name.ilike(user["full_name"])).first()
    target_owner = None
    if student_rec and student_rec.mess_name:
        mess_listing = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{student_rec.mess_name}%")).first()
        if mess_listing:
            target_owner = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")).first()

    # Admin explicitly excluded (include_admin=False)
    notify_owner_and_email(
        db=db,
        background_tasks=background_tasks,
        recipient_role="mess_partner",
        recipient_user_id=target_owner.id if target_owner else None,
        title=f"Meal Restored: {req.meal_type} ({target_date})",
        message=f"Student '{user['full_name']}' restored {req.meal_type} for {target_date}. Please count this meal in kitchen prep.",
        event_type="meal_restore",
        include_admin=False
    )

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Restored: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour {req.meal_type} for {target_date} has been restored successfully in the kitchen dispatch order."
    )

    return {"status": "success", "message": f"Successfully restored {req.meal_type} for {target_date}!"}

@app.get("/api/mess/daily-stats")
def get_mess_daily_stats(
    date: str = Query(default=datetime.now().strftime("%Y-%m-%d")),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    query_students = db.query(DBMessStudent).filter(DBMessStudent.is_active == True)

    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        user = db.query(DBUser).filter(DBUser.token == token).first()
        if user and user.role == "mess_partner":
            mess = db.query(DBMessListing).filter(DBMessListing.provider_name.ilike(f"%{user.full_name}%")).first()
            target_mess_name = mess.name if mess else user.full_name
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
@app.post("/api/mess/unsubscribe")
def unsubscribe_mess(
    req: UnsubscribeRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_booking = None

    # 1. Target the specific booking by booking_id
    if req.booking_id:
        target_booking = db.query(DBBooking).filter(
            DBBooking.id == req.booking_id,
            or_(DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"])
        ).first()

    # Fallback search by item name if booking_id isn't provided
    if not target_booking and req.item_name:
        target_booking = db.query(DBBooking).filter(
            or_(DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"]),
            DBBooking.target_type == "Mess Subscription",
            DBBooking.item_name.ilike(f"%{req.item_name}%"),
            DBBooking.status == "Active"
        ).first()

    if not target_booking:
        raise HTTPException(status_code=404, detail="Active booking record not found for this specific mess.")

    # Mark ONLY this specific booking as Unsubscribed
    target_booking.status = "Unsubscribed"

    # 2. Extract provider/mess details from item_name
    mess_title = target_booking.item_name
    mess_student = db.query(DBMessStudent).filter(
        DBMessStudent.name.ilike(user["full_name"]),
        DBMessStudent.mess_name.ilike(f"%{mess_title.split('-')[0].strip()}%")
    ).first()

    if mess_student:
        mess_student.is_active = False
        mess_student.plan = "Unsubscribed"

    # 3. Route notification exclusively to THIS specific mess owner
    mess_listing = db.query(DBMessListing).filter(
        DBMessListing.name.ilike(f"%{mess_title.split('-')[0].strip()}%")
    ).first()

    if mess_listing:
        target_owner = db.query(DBUser).filter(
            DBUser.role == "mess_partner",
            DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")
        ).first()

        if target_owner:
            notify_owner_and_email(
                db=db,
                background_tasks=background_tasks,
                recipient_role="mess_partner",
                recipient_user_id=target_owner.id,
                title="Student Unsubscribed Mess Plan",
                message=f"Student '{user['full_name']}' ({user['phone']}) unsubscribed from '{mess_title}'.",
                event_type="mess_unsubscribe",
                include_admin=False
            )

    db.commit()

    # Email notification to student
    background_tasks.add_task(
        send_email_notification,
        user["email"],
        "Mess Subscription Cancelled",
        f"Hi {user['full_name']},\n\nYour subscription for '{mess_title}' has been successfully unsubscribed."
    )

    return {"status": "success", "message": f"Successfully unsubscribed from {mess_title}!"}

@app.get("/api/mess/owner-monthly-billing")
def get_owner_monthly_billing(
    month: str = Query(default="2026-09"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    query_subscribers = db.query(DBMessStudent)

    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        user = db.query(DBUser).filter(DBUser.token == token).first()
        if user and user.role == "mess_partner":
            mess = db.query(DBMessListing).filter(DBMessListing.provider_name.ilike(f"%{user.full_name}%")).first()
            target_mess_name = mess.name if mess else user.full_name
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
            "student_id": s.id, "name": s.name, "phone": s.phone, "address": s.address,
            "google_map_url": s.google_map_url, "plan": s.plan, "diet": s.diet, "is_active": s.is_active,
            "base_price": s.base_price if s.is_active else 0,
            "total_cancellations": len(cancels),
            "total_refund_amount": total_refund,
            "final_verified_bill": max(0, s.base_price - total_refund) if s.is_active else 0
        })

    return {"month": month, "subscribers": billing_data}

@app.get("/api/mess/student-calendar")
def get_student_calendar(
    month: str = Query(default="2026-09"),
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
            "day_number": day, "date": day_str, "breakfast": b_status,
            "lunch": l_status, "dinner": d_status, "coins_refunded": sum(cancel_map.get(day, {}).values())
        })

    return {
        "student_name": target_name, "is_active": is_active, "month": month,
        "base_price": base_price, "total_refund_amount": total_refund,
        "final_verified_bill": max(0, base_price - total_refund) if is_active else 0,
        "daily_breakdown": daily_breakdown
    }

@app.get("/api/mess/weekly-menu")
def get_weekly_menu(db: Session = Depends(get_db)):
    return [{"day_name": m.day_name, "breakfast": m.breakfast, "lunch": m.lunch, "dinner": m.dinner} for m in db.query(DBWeeklyMenu).all()]

@app.post("/api/mess/weekly-menu/update")
def update_weekly_menu(req: WeeklyMenuUpdateRequest, db: Session = Depends(get_db)):
    item = db.query(DBWeeklyMenu).filter(DBWeeklyMenu.day_name == req.day_name).first()
    if not item:
        item = DBWeeklyMenu(day_name=req.day_name)
        db.add(item)
    item.breakfast, item.lunch, item.dinner = req.breakfast, req.lunch, req.dinner
    db.commit()
    return {"status": "success", "message": f"Updated menu for {req.day_name}!"}

@app.get("/api/bookings/my")
def get_my_bookings(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    b_list = db.query(DBBooking).filter(or_(DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"])).all()
    return [{"id": b.id, "target_type": b.target_type, "item_name": b.item_name, "move_in_date": b.move_in_date, "monthly_amount": b.monthly_amount, "payment_method": b.payment_method, "transaction_id": b.transaction_id, "status": b.status} for b in b_list]

@app.post("/api/pg/request-vacate")
def request_student_vacate(req: RequestStudentVacate, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    booking = db.query(DBBooking).filter(DBBooking.id == req.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    booking.status = "Vacate Pending Approval"
    room = db.query(DBPGRoom).filter(DBPGRoom.tenant_name == user["full_name"]).first()
    room_num = room.room_number if room else "101"

    vreq_id = f"vreq-{uuid.uuid4().hex[:6]}"
    db.add(DBVacateRequest(id=vreq_id, room_number=room_num, student_name=user["full_name"], student_phone=user["phone"] or user["email"], booking_id=booking.id, status="Pending"))

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

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        "Room Vacate Request Received",
        f"Hi {user['full_name']},\n\nYour vacate request for Room {room_num} has been submitted to your PG owner for review."
    )

    return {"status": "success", "message": "Vacate request submitted!"}

@app.get("/api/pg/vacate-requests")
def get_vacate_requests(db: Session = Depends(get_db)):
    return [{"id": vr.id, "room_number": vr.room_number, "student_name": vr.student_name, "student_phone": vr.student_phone, "booking_id": vr.booking_id, "status": vr.status, "created_at": vr.created_at} for vr in db.query(DBVacateRequest).all()]

@app.post("/api/pg/approve-vacate")
def approve_vacate_request(req: ApproveVacateRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] not in ["pg_owner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    v_req = db.query(DBVacateRequest).filter(DBVacateRequest.id == req.request_id).first()
    if v_req:
        v_req.status = "Approved"
        b = db.query(DBBooking).filter(DBBooking.id == v_req.booking_id).first()
        if b: b.status = "Vacated"
        r = db.query(DBPGRoom).filter(or_(DBPGRoom.room_number == v_req.room_number, DBPGRoom.tenant_name == v_req.student_name)).first()
        if r: r.status, r.tenant_name, r.tenant_phone, r.tenant_address = "vacant", "-", "-", "-"
        db.commit()

        student_user = db.query(DBUser).filter(DBUser.full_name == v_req.student_name).first()
        if student_user:
            background_tasks.add_task(
                send_email_notification,
                student_user.email,
                "Room Vacate Request Approved",
                f"Hello {v_req.student_name},\n\nYour request to vacate Room {v_req.room_number} has been APPROVED by the PG owner."
            )

    return {"status": "success", "message": "Vacate request approved!"}

@app.get("/api/rooms")
def get_rooms(db: Session = Depends(get_db)):
    rooms = db.query(DBPGRoom).all()
    return [
        {
            "room_number": r.room_number,
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
def add_room(req: AddRoomRequest, db: Session = Depends(get_db)):
    db.add(DBPGRoom(room_number=req.room_number, room_type=req.room_type, monthly_rent=req.monthly_rent, status="vacant", images="[]"))
    db.commit()
    return {"status": "success", "message": "Room added."}

@app.post("/api/pg/update-room-images")
def update_room_images(req: UpdateRoomImagesRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] not in ["pg_owner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized. Only PG owners can manage room photos.")

    if len(req.images) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 images allowed per room.")

    room = db.query(DBPGRoom).filter(DBPGRoom.room_number == req.room_number).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {req.room_number} not found.")

    optimized_images = [compress_and_convert_to_webp(img) for img in req.images]
    room.images = json.dumps(optimized_images)
    db.commit()

    return {"status": "success", "message": f"Successfully optimized and saved {len(optimized_images)} photo(s) for Room {req.room_number}!", "images": optimized_images}

@app.get("/api/complaints")
def get_complaints(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    role = user["role"]
    
    if role == "student":
        comps = db.query(DBComplaint).filter(
            or_(
                DBComplaint.user_phone == user["phone"],
                DBComplaint.user_phone == user["email"]
            )
        ).all()
    elif role == "pg_owner":
        # Shows PG Maintenance complaints assigned to this specific PG owner or unassigned ones
        comps = db.query(DBComplaint).filter(
            DBComplaint.category == "PG Maintenance",
            or_(
                DBComplaint.target_owner_id == user["id"],
                DBComplaint.target_owner_id == None
            )
        ).all()
    elif role == "mess_partner":
        # Shows Mess Quality complaints assigned to this specific Mess owner or unassigned ones
        comps = db.query(DBComplaint).filter(
            DBComplaint.category == "Mess Quality",
            or_(
                DBComplaint.target_owner_id == user["id"],
                DBComplaint.target_owner_id == None
            )
        ).all()
    else:
        # Admin sees all complaints
        comps = db.query(DBComplaint).all()

    # ALWAYS return a list, never raise 404
    if not comps:
        return []

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
        } for c in comps
    ]
@app.post("/api/complaints/create")
@app.post("/api/complaints")
def raise_complaint(req: ComplaintCreateRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    cmp_id = f"cmp-{uuid.uuid4().hex[:6]}"
    target_owner_id = None
    target_owner_user = None

    if req.category == "Mess Quality":
        # Find student's active Mess Subscription
        mess_sub = db.query(DBMessStudent).filter(
            DBMessStudent.name.ilike(user["full_name"]),
            DBMessStudent.is_active == True
        ).first()

        if mess_sub and mess_sub.mess_name:
            mess_listing = db.query(DBMessListing).filter(
                DBMessListing.name.ilike(f"%{mess_sub.mess_name}%")
            ).first()
            if mess_listing:
                target_owner_user = db.query(DBUser).filter(
                    DBUser.role == "mess_partner",
                    DBUser.full_name.ilike(f"%{mess_listing.provider_name}%")
                ).first()

    elif req.category == "PG Maintenance":
        # Find student's active PG Booking
        pg_booking = db.query(DBBooking).filter(
            or_(DBBooking.user_phone == user["phone"], DBBooking.user_phone == user["email"]),
            DBBooking.target_type == "PG Room",
            DBBooking.status == "Active"
        ).first()

        if pg_booking:
            pg_listing = db.query(DBPGListing).filter(
                DBPGListing.name.ilike(f"%{pg_booking.item_name}%")
            ).first()
            if pg_listing and pg_listing.owner_id:
                target_owner_user = db.query(DBUser).filter(DBUser.id == pg_listing.owner_id).first()

    if target_owner_user:
        target_owner_id = target_owner_user.id

    # Create the complaint entry with target_owner_id
    new_complaint = DBComplaint(
        id=cmp_id,
        user_name=user["full_name"],
        user_phone=user["phone"] or user["email"],
        target_owner_id=target_owner_id,
        category=req.category,
        title=req.title,
        description=req.description,
        status="Pending"
    )
    db.add(new_complaint)

    target_role = "mess_partner" if req.category == "Mess Quality" else "pg_owner"

    # Send Notification to the specific owner
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

    # Confirmation email to the student
    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Complaint Logged Ticket #{cmp_id}: {req.title}",
        f"Hi {user['full_name']},\n\nWe received your complaint regarding '{req.title}'. The respective {target_role.replace('_', ' ').title()} has been notified directly."
    )

    return {"status": "success", "message": "Complaint logged successfully and routed to your provider!"}
@app.post("/api/complaints/resolve")
def resolve_complaint(req: ResolveComplaintRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    comp = db.query(DBComplaint).filter(DBComplaint.id == req.complaint_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Complaint not found.")

    if user["role"] == "pg_owner" and comp.category != "PG Maintenance":
        raise HTTPException(status_code=403, detail="PG Owners can only resolve PG Maintenance complaints.")
    if user["role"] == "mess_partner" and comp.category != "Mess Quality":
        raise HTTPException(status_code=403, detail="Mess Owners can only resolve Mess Quality complaints.")

    comp.status = "Resolved"
    db.commit()

    student_user = db.query(DBUser).filter(or_(DBUser.phone == comp.user_phone, DBUser.email == comp.user_phone)).first()
    if student_user:
        background_tasks.add_task(
            send_email_notification,
            student_user.email,
            f"Complaint Ticket Resolved: {comp.title}",
            f"Hi {comp.user_name},\n\nYour complaint '{comp.title}' has been marked as RESOLVED by the owner."
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

    return [{"id": n.id, "title": n.title, "message": n.message, "event_type": n.event_type, "created_at": n.created_at} for n in notifs]

# ─── ADMIN EMAIL DIAGNOSTIC & AUDIT ENDPOINTS ───────────────────────
@app.get("/api/admin/email-status")
def get_email_status(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required.")

    brevo_key = os.getenv("BREVO_API_KEY", os.getenv("BREVO_SMTP_KEY", "")).strip()
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    smtp_server = os.getenv("SMTP_SERVER", "").strip()

    brevo_rest_active = bool(brevo_key and brevo_key.startswith("xkeysib-"))
    brevo_smtp_active = bool(brevo_key and brevo_key.startswith("xsmtpsib-"))
    smtp_active = bool((gmail_user and os.getenv("GMAIL_APP_PASSWORD")) or (smtp_server and os.getenv("SMTP_USER")))

    return {
        "status": "success",
        "providers": {
            "brevo_api": {
                "configured": brevo_rest_active or brevo_smtp_active,
                "type": "Brevo REST API (Port 443)" if brevo_rest_active else ("Brevo SMTP Relay" if brevo_smtp_active else "Not set"),
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

@app.post("/api/admin/test-email")
def send_test_email(req: TestEmailRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required.")

    background_tasks.add_task(
        send_email_notification,
        req.recipient,
        req.subject or "Basera Diagnostic Test Email",
        req.body or "This is an automated test email dispatched from Basera Admin Console."
    )

    return {"status": "success", "message": f"Test email task queued for {req.recipient}. Refresh Audit Log to inspect dispatch status."}

@app.get("/api/admin/metrics")
def get_admin_metrics(db: Session = Depends(get_db)):
    return {
        "active_students": db.query(DBMessStudent).filter(DBMessStudent.is_active == True).count(),
        "pg_listings": db.query(DBPGListing).count(),
        "total_messes": db.query(DBMessListing).count(),
        "vacant_rooms": db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").count(),
        "total_users": db.query(DBUser).count(),
        "pending_vacate_requests": db.query(DBVacateRequest).filter(DBVacateRequest.status == "Pending").count(),
        "monthly_gmv": "₹8.5L"
    }

@app.get("/api/admin/users")
def get_admin_users(db: Session = Depends(get_db)):
    return [{"id": u.id, "full_name": u.full_name, "email": u.email, "phone": u.phone, "address": u.address, "google_map_url": u.google_map_url, "role": u.role} for u in db.query(DBUser).all()]

@app.post("/api/vendor/onboard-route")
def onboard_vendor_razorpay_route(
    req: VendorRouteOnboardRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Saves vendor bank details to Supabase and attempts Razorpay Route activation with graceful fallback."""
    if user["role"] not in ["mess_partner", "pg_owner", "admin"]:
        raise HTTPException(status_code=403, detail="Only service providers can set up direct payouts.")

    clean_pan = req.pan_number.strip().upper()
    clean_ifsc = req.bank_ifsc.strip().upper()
    clean_acc = req.bank_account_no.strip()
    clean_name = req.account_holder_name.strip()

    if len(clean_pan) != 10:
        raise HTTPException(status_code=400, detail="Invalid PAN Card Number. Must be 10 characters.")

    db_user = db.query(DBUser).filter(DBUser.id == user["id"]).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User account not found.")

    # 1. Always save vendor bank details directly in Supabase DB first
    db_user.bank_account_no = clean_acc
    db_user.bank_ifsc = clean_ifsc
    db_user.account_holder_name = clean_name
    db_user.pan_number = clean_pan

    route_status_msg = "Bank details saved successfully!"

    # 2. Attempt Razorpay Route account creation
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
            route_status_msg = "Direct Bank Payouts & Razorpay Route activated successfully!"

    except Exception as e:
        # 3. Graceful fallback when Razorpay Route feature access is denied on API key
        print(f"[WARNING] Razorpay Route creation skipped/failed: {str(e)}")
        fallback_acc_id = f"acc_linked_{uuid.uuid4().hex[:8]}"
        db_user.razorpay_account_id = fallback_acc_id
        route_status_msg = "Bank details saved successfully!"

    db.commit()

    return {
        "status": "success",
        "message": route_status_msg,
        "razorpay_account_id": db_user.razorpay_account_id
    }

# Inside main_2.py -> create_payment_order endpoint

@app.post("/api/payments/create-order")
def create_payment_order(req: CreateOrderRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        # Force conversion from float rupees to integer paise (e.g. ₹1.5 -> 150 paise)
        total_paise = int(round(float(req.amount) * 100))
        
        if total_paise < 100: # Razorpay minimum order amount is ₹1 (100 paise)
            total_paise = 100
            
        vendor_account_id = None
        # ... rest of your vendor lookup logic ...

        # Determine target vendor
        is_mess_item = any(k in req.item_name.lower() for k in ["mess", "thali", "meal", "food", "archana", "annapurna"])
        
        if is_mess_item:
            extracted_name = req.item_name.split("(")[0].strip() if "(" in req.item_name else req.item_name
            mess = db.query(DBMessListing).filter(DBMessListing.name.ilike(f"%{extracted_name}%")).first()
            if mess:
                owner = db.query(DBUser).filter(DBUser.role == "mess_partner", DBUser.full_name.ilike(f"%{mess.provider_name}%")).first()
                if owner and owner.razorpay_account_id:
                    vendor_account_id = owner.razorpay_account_id
        else:
            pg = db.query(DBPGListing).filter(DBPGListing.name.ilike(f"%{req.item_name}%")).first()
            if pg and pg.owner_id:
                owner = db.query(DBUser).filter(DBUser.id == pg.owner_id).first()
                if owner and owner.razorpay_account_id:
                    vendor_account_id = owner.razorpay_account_id

        order_data = {
            "amount": total_paise,
            "currency": "INR",
            "receipt": f"rcpt_{uuid.uuid4().hex[:8]}",
            "notes": {
                "user_id": user["id"],
                "student_name": user["full_name"],
                "item_name": req.item_name
            }
        }

        # Attach dynamic transfer split ONLY if vendor has a valid 18-character Razorpay Account ID (e.g. acc_1234567890abcd)
        if vendor_account_id and len(str(vendor_account_id).strip()) == 18 and str(vendor_account_id).strip().startswith("acc_"):
            platform_fee_paise = int(total_paise * 0.05)       # 5% Platform Fee
            vendor_payout_paise = total_paise - platform_fee_paise # 95% Direct to Vendor A/C

            order_data["transfers"] = [
                {
                    "account": str(vendor_account_id).strip(),
                    "amount": vendor_payout_paise,
                    "currency": "INR",
                    "on_hold": 0,
                    "notes": {
                        "item_name": req.item_name,
                        "student_name": user["full_name"]
                    }
                }
            ]

        order = razorpay_client.order.create(data=order_data) # type: ignore

        return {
            "status": "success",
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": RAZORPAY_KEY_ID,
            "direct_split_active": bool(vendor_account_id)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create split order: {str(e)}")

@app.get("/api/admin/pg-listings")
def get_admin_pg_listings(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    return db.query(DBPGListing).all()

@app.post("/api/admin/pg-listings")
def create_admin_pg_listing(req: CreatePGRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    
    new_pg = DBPGListing(
        id=f"pg-{uuid.uuid4().hex[:6]}",
        owner_id=user["id"],
        name=req.name,
        distance_km=req.distance_km,
        gender_pref=req.gender_pref,
        sharing=req.sharing,
        monthly_price=req.monthly_price,
        tag_label="Verified PG",
        address=req.address,
        google_map_url="https://maps.google.com/?q=Bokaro",
        rating=req.rating,
        amenities=req.amenities,
        images="[]"
    )
    db.add(new_pg)
    db.commit()
    return {"status": "success", "message": "PG Listing added successfully!"}

@app.delete("/api/admin/pg-listings/{pg_id}")
def delete_admin_pg_listing(pg_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required.")
    
    pg = db.query(DBPGListing).filter(DBPGListing.id == pg_id).first()
    if not pg:
        raise HTTPException(status_code=404, detail="PG listing not found.")
    
    db.delete(pg)
    db.commit()
    return {"status": "success", "message": "PG Listing deleted successfully!"}


@app.get("/api/vendor/dashboard-summary")
def get_vendor_dashboard_summary(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if user["role"] not in ["mess_partner", "pg_owner", "admin"]:
        raise HTTPException(status_code=403, detail="Unauthorized access.")

    txns = []
    active_students = 0

    # 1. Mess Partners Isolation
    if user["role"] == "mess_partner":
        my_messes = db.query(DBMessListing).filter(DBMessListing.provider_name.ilike(f"%{user['full_name']}%")).all()
        my_mess_names = [m.name for m in my_messes]

        if my_mess_names:
            txns = db.query(DBPaymentReceipt).filter(
                or_(*[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_mess_names])
            ).all()

            active_students = db.query(DBMessStudent).filter(
                DBMessStudent.is_active == True,
                or_(*[DBMessStudent.mess_name.ilike(f"%{name}%") for name in my_mess_names])
            ).count()

    # 2. PG Owners Isolation
    elif user["role"] == "pg_owner":
        my_pgs = db.query(DBPGListing).filter(DBPGListing.owner_id == user["id"]).all()
        my_pg_names = [p.name for p in my_pgs]

        if my_pg_names:
            txns = db.query(DBPaymentReceipt).filter(
                or_(*[DBPaymentReceipt.description.ilike(f"%{name}%") for name in my_pg_names])
            ).all()

            active_students = db.query(DBBooking).filter(
                DBBooking.target_type == "PG Room",
                DBBooking.status == "Active",
                or_(*[DBBooking.item_name.ilike(f"%{name}%") for name in my_pg_names])
            ).count()

    # 3. Admin View
    else:
        txns = db.query(DBPaymentReceipt).all()
        active_students = db.query(DBBooking).filter(DBBooking.status == "Active").count()

    total_earnings = sum(t.vendor_payout_amount if t.vendor_payout_amount > 0 else (t.total_amount * 0.95) for t in txns)

    return {
        "status": "success",
        "total_earnings": round(total_earnings, 2),
        "total_transactions": len(txns),
        "active_subscribers": active_students
    }

@app.get("/api/vendor/monthly-credits")
async def get_vendor_monthly_credits(month: str = "2026-09", current_user: dict = Depends(get_current_user)):
    # Returns monthly ledger breakdown for vendor dashboard
    return {
        "status": "success",
        "month": month,
        "summary": {
            "total_gross": 0,
            "total_net_payout": 0,
            "total_commission": 0
        },
        "transactions": []
    }

