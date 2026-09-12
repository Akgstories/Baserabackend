import os
import uuid
import json
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy import create_engine, String, Integer, Boolean, Float, Text, DateTime, or_, and_
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, Mapped, mapped_column

# ─── DATABASE CONFIGURATION ─────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./basera.db")

# Convert legacy postgres:// to postgresql:// for SQLAlchemy
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass


# ─── BREVO SMTP EMAIL CONFIGURATION ──────────────────────────────────
SMTP_SERVER = os.getenv("BREVO_SMTP_SERVER", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("BREVO_SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "no-reply@baseras.in")
SENDER_PASSWORD = os.getenv("BREVO_SMTP_KEY", "")

def send_email_notification(recipient_email: str, subject: str, body_text: str):
    """Sends transactional email alerts using Brevo SMTP."""
    if not recipient_email or "@" not in recipient_email:
        return

    # Fallback log if environment SMTP key is not set
    if not SENDER_PASSWORD:
        print(f"[BREVO MOCK ALERT] To: {recipient_email} | Subject: {subject} | Body: {body_text[:70]}...")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = f"Basera Platform <{SENDER_EMAIL}>"
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"[BREVO EMAIL SUCCESS] Sent to {recipient_email}")
    except Exception as e:
        print(f"[BREVO EMAIL ERROR] Failed to send to {recipient_email}: {e}")


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DBPGListing(Base):
    __tablename__ = "pg_listings"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
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

class DBPGRoom(Base):
    __tablename__ = "pg_rooms"
    room_number: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    room_type: Mapped[str] = mapped_column(String, nullable=False)
    tenant_name: Mapped[str] = mapped_column(String, default="-")
    tenant_phone: Mapped[str] = mapped_column(String, default="-")
    tenant_address: Mapped[str] = mapped_column(String, default="-")
    monthly_rent: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="vacant")

class DBVacateRequest(Base):
    __tablename__ = "vacate_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    room_number: Mapped[str] = mapped_column(String, nullable=False)
    student_name: Mapped[str] = mapped_column(String, nullable=False)
    student_phone: Mapped[str] = mapped_column(String, nullable=False)
    booking_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBMessStudent(Base):
    __tablename__ = "mess_students"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    google_map_url: Mapped[str] = mapped_column(Text, default="https://maps.google.com/?q=GEC+Bokaro+Hostel")
    plan: Mapped[str] = mapped_column(String, nullable=False)
    diet: Mapped[str] = mapped_column(String, nullable=False)
    base_price: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expiry_date: Mapped[str] = mapped_column(String, default=(datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"))

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
    payment_method: Mapped[str] = mapped_column(String, default="UPI")
    transaction_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="Active")

class DBPaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    transaction_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    payer_name: Mapped[str] = mapped_column(String, nullable=False)
    payer_phone: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_method: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)

class DBComplaint(Base):
    __tablename__ = "complaints"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    user_phone: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="Pending")
    created_at: Mapped[str] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M"))

class DBNotification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    recipient_role: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String, default="general")
    created_at: Mapped[str] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M"))


def seed_database():
    try:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        admin_user = db.query(DBUser).filter(DBUser.role == "admin").first()
        if not admin_user:
            db.add(DBUser(
                id="usr-admin01", full_name="Platform Admin", email="akgstories02@gmail.com",
                password="admin@2026", phone="9155118661", address="Admin Office, GEC Bokaro",
                google_map_url="https://maps.google.com/?q=GEC+Bokaro", role="admin", token="tok-admin123"
            ))

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
                    rating="4.9 (28)", amenities=json.dumps(["Wi-Fi", "Homely Mess", "Power Backup"])
                ),
                DBPGListing(
                    id="pg-2", name="Chandankiyari Comfort Girls PG", distance_km=0.2,
                    gender_pref="Girls PG", sharing="Single Room", has_ac=True, monthly_price=4800,
                    tag_label="1 left", address="📍 P.O-Kherabera, Chandankiyari, Bokaro",
                    google_map_url="https://maps.google.com/?q=23.5780,86.3520",
                    rating="4.8 (19)", amenities=json.dumps(["CCTV", "3-Time Food", "Geyser"])
                )
            ])

        if not db.query(DBMessListing).first():
            db.add_all([
                DBMessListing(
                    id="mess-1", name="Annapurna Homely Mess", provider_name="Ramesh Sharma",
                    monthly_price=3000, diet_type="Veg & Non-Veg", meals_per_day="3-Time (Breakfast, Lunch, Dinner)",
                    rating="4.9 (42 reviews)", address="📍 Near GEC Bokaro Main Gate",
                    google_map_url="https://maps.google.com/?q=GEC+Bokaro+Main+Gate",
                    description="Freshly prepared hygienic meals tailored for engineering students."
                ),
                DBMessListing(
                    id="mess-2", name="Shuddha Shakahari Mess", provider_name="Geeta Devi",
                    monthly_price=2600, diet_type="Pure Veg", meals_per_day="3-Time (Breakfast, Lunch, Dinner)",
                    rating="4.8 (31 reviews)", address="📍 Vill-Ghoragara, Chandankiyari",
                    google_map_url="https://maps.google.com/?q=Chandankiyari+Bokaro",
                    description="100% Pure Vegetarian North & South Indian meals cooked with pure desi ghee."
                )
            ])

        if not db.query(DBPGRoom).first():
            db.add_all([
                DBPGRoom(room_number="101", room_type="Single AC", tenant_name="Rahul Kumar", tenant_phone="9876543210", tenant_address="GEC Bokaro Hostel Block A", monthly_rent=8000, status="occupied"),
                DBPGRoom(room_number="102", room_type="Double Non-AC", tenant_name="-", tenant_phone="-", tenant_address="-", monthly_rent=5200, status="vacant")
            ])

        if not db.query(DBMessStudent).first():
            db.add_all([
                DBMessStudent(id="ms-101", name="Aditya Kumar", phone="9155118661", address="GEC Bokaro Hostel, Room 101", google_map_url="https://maps.google.com/?q=GEC+Bokaro+Hostel", plan="3-Time Daily Mess Plan", diet="Non-Veg", base_price=3000, is_active=True),
                DBMessStudent(id="ms-102", name="Tushar Das", phone="9876542170", address="Power Grid Boys PG, Room 204", google_map_url="https://maps.google.com/?q=23.5750,86.3500", plan="2-Time Standard Plan", diet="Veg", base_price=2500, is_active=True)
            ])

        db.commit()
        db.close()
        print("[DATABASE] Connection and seeding successful!")
    except Exception as e:
        print(f"[DATABASE NOTICE] Seed skipped or DB offline: {e}")


app = FastAPI(title="Basera Multi-Portal API", version="12.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    seed_database()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def notify_owner(db: Session, recipient_role: str, title: str, message: str, event_type: str = "general"):
    db.add(DBNotification(
        id=f"notif-{uuid.uuid4().hex[:8]}", recipient_role=recipient_role,
        title=title, message=message, event_type=event_type,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M")
    ))


# ─── SCHEMAS & AUTH ───────────────────────────────────────────────
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

class MealCancelRequest(BaseModel):
    meal_type: str
    date: Optional[str] = None

class WeeklyMenuUpdateRequest(BaseModel):
    day_name: str
    breakfast: str
    lunch: str
    dinner: str

class BookingCreateWithPaymentRequest(BaseModel):
    target_type: str
    item_name: str
    move_in_date: str
    special_requests: Optional[str] = ""
    monthly_amount: int
    payment_method: str

class RequestStudentVacate(BaseModel):
    booking_id: str

class ApproveVacateRequest(BaseModel):
    request_id: str

class AddRoomRequest(BaseModel):
    room_number: str
    room_type: str
    monthly_rent: int

class ComplaintCreateRequest(BaseModel):
    category: str
    title: str
    description: str

class ResolveComplaintRequest(BaseModel):
    complaint_id: str

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
    return {"status": "online", "platform": "Basera Engine", "database": "Supabase PostgreSQL Active"}

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
            plan="3-Time Standard Daily Plan", diet="Non-Veg", base_price=3000, is_active=True
        ))

    db.commit()

    background_tasks.add_task(
        send_email_notification,
        req.email,
        "Welcome to Basera Platform!",
        f"Hello {req.full_name},\n\nYour account has been registered as a {role.upper()} on Basera.\n\nThank you!"
    )

    return {"status": "success", "token": token, "user": {"id": new_user.id, "full_name": new_user.full_name, "email": new_user.email, "phone": new_user.phone, "address": new_user.address, "google_map_url": new_user.google_map_url, "role": new_user.role}}

@app.post("/api/auth/login")
def login_user(req: LoginRequest, db: Session = Depends(get_db)):
    query = db.query(DBUser).filter(DBUser.email == req.email, DBUser.password == req.password)
    if req.target_role:
        query = query.filter(DBUser.role == req.target_role)

    user = query.first() or db.query(DBUser).filter(DBUser.email == req.email, DBUser.password == req.password).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email, password, or role combination.")

    user.token = f"tok-{uuid.uuid4().hex}"
    db.commit()
    return {"status": "success", "token": user.token, "user": {"id": user.id, "full_name": user.full_name, "email": user.email, "phone": user.phone, "address": user.address, "google_map_url": user.google_map_url, "role": user.role}}

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
        "Your Basera account password has been updated successfully. If you did not request this, please contact support."
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

@app.get("/api/pgs")
def get_pgs(gender_pref: Optional[str] = Query(None), search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(DBPGListing)

    if gender_pref and gender_pref != "All":
        query = query.filter(DBPGListing.gender_pref.ilike(f"%{gender_pref}%"))

    if search:
        q = f"%{search.lower().strip()}%"
        query = query.filter(or_(DBPGListing.name.ilike(q), DBPGListing.address.ilike(q)))

    return [
        {
            "id": l.id, "name": l.name, "distance_km": l.distance_km, "gender_pref": l.gender_pref,
            "sharing": l.sharing, "has_ac": l.has_ac, "monthly_price": l.monthly_price,
            "tag_label": l.tag_label, "address": l.address, "google_map_url": l.google_map_url, "rating": l.rating,
            "amenities": json.loads(l.amenities) if l.amenities else []
        }
        for l in query.all()
    ]

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

    notify_owner(db, "mess_partner", f"Meal Canceled: {req.meal_type}", f"{user['full_name']} ({user['phone']}) canceled {req.meal_type} for {target_date}.", "meal_cancel")
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Cancellation Confirmed: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour request to cancel {req.meal_type} for {target_date} is confirmed. ₹{refund_coins} has been credited to your bill."
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

    notify_owner(db, "mess_partner", f"Meal Restored: {req.meal_type}", f"{user['full_name']} restored {req.meal_type} for {target_date}.", "meal_restore")
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Meal Restored: {req.meal_type} ({target_date})",
        f"Hi {user['full_name']},\n\nYour {req.meal_type} for {target_date} has been restored successfully."
    )

    return {"status": "success", "message": f"Successfully restored {req.meal_type} for {target_date}!"}

@app.get("/api/mess/daily-stats")
def get_mess_daily_stats(date: str = Query(default=datetime.now().strftime("%Y-%m-%d")), db: Session = Depends(get_db)):
    cancels = db.query(DBMealCancellation).filter(DBMealCancellation.date == date).all()
    active_students = db.query(DBMessStudent).filter(DBMessStudent.is_active == True).all()

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
def get_owner_monthly_billing(month: str = Query(default="2026-09"), db: Session = Depends(get_db)):
    subscribers = db.query(DBMessStudent).all()
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

@app.post("/api/bookings")
def create_booking_with_payment(req: BookingCreateWithPaymentRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    booking_id = f"b-{uuid.uuid4().hex[:8]}"
    txn_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"

    db.add(DBPaymentReceipt(transaction_id=txn_id, payer_name=user["full_name"], payer_phone=user["phone"] or user["email"], amount=req.monthly_amount, payment_method=req.payment_method, description=f"Booking - {req.item_name}", date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    new_b = DBBooking(id=booking_id, user_phone=user["phone"] or user["email"], target_type=req.target_type, item_name=req.item_name, move_in_date=req.move_in_date, special_requests=req.special_requests or "", monthly_amount=req.monthly_amount, payment_method=req.payment_method, transaction_id=txn_id, status="Active")
    db.add(new_b)

    vacant_room = db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").first()
    if vacant_room:
        vacant_room.status, vacant_room.tenant_name, vacant_room.tenant_phone, vacant_room.tenant_address = "occupied", user["full_name"], user["phone"], user["address"]

    notify_owner(db, "pg_owner", "New Room Booking Confirmed", f"{user['full_name']} booked {req.item_name}.", "booking")
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Booking Confirmation: {req.item_name}",
        f"Hi {user['full_name']},\n\nYour room booking for {req.item_name} is confirmed!\nTransaction ID: {txn_id}\nMonthly Amount: ₹{req.monthly_amount}\nMove-in Date: {req.move_in_date}"
    )

    return {"status": "success", "message": "Payment verified and booking confirmed!"}

@app.post("/api/pg/request-vacate")
def request_student_vacate(req: RequestStudentVacate, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    booking = db.query(DBBooking).filter(DBBooking.id == req.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    booking.status = "Vacate Pending Approval"
    room = db.query(DBPGRoom).filter(DBPGRoom.tenant_name == user["full_name"]).first()
    room_num = room.room_number if room else "101"

    db.add(DBVacateRequest(id=f"vreq-{uuid.uuid4().hex[:6]}", room_number=room_num, student_name=user["full_name"], student_phone=user["phone"] or user["email"], booking_id=booking.id, status="Pending"))
    notify_owner(db, "pg_owner", "Room Vacate Request Submitted", f"{user['full_name']} requested to vacate Room {room_num}.", "vacate_request")
    db.commit()
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
                f"Hello {v_req.student_name},\n\nYour request to vacate Room {v_req.room_number} has been approved."
            )

    return {"status": "success", "message": "Vacate request approved!"}

@app.get("/api/rooms")
def get_rooms(db: Session = Depends(get_db)):
    return [{"room_number": r.room_number, "room_type": r.room_type, "tenant_name": r.tenant_name, "tenant_phone": r.tenant_phone, "tenant_address": r.tenant_address, "monthly_rent": r.monthly_rent, "status": r.status} for r in db.query(DBPGRoom).all()]

@app.post("/api/pg/add-room")
def add_room(req: AddRoomRequest, db: Session = Depends(get_db)):
    db.add(DBPGRoom(room_number=req.room_number, room_type=req.room_type, monthly_rent=req.monthly_rent, status="vacant"))
    db.commit()
    return {"status": "success", "message": "Room added."}

@app.get("/api/complaints")
def get_complaints(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    role = user["role"]
    if role == "student":
        comps = db.query(DBComplaint).filter(DBComplaint.user_phone == user["phone"]).all()
    elif role == "pg_owner":
        comps = db.query(DBComplaint).filter(DBComplaint.category == "PG Maintenance").all()
    elif role == "mess_partner":
        comps = db.query(DBComplaint).filter(DBComplaint.category == "Mess Quality").all()
    else:
        comps = db.query(DBComplaint).all()

    return [{"id": c.id, "user_name": c.user_name, "user_phone": c.user_phone, "category": c.category, "title": c.title, "description": c.description, "status": c.status, "created_at": c.created_at} for c in comps]

@app.post("/api/complaints")
def raise_complaint(req: ComplaintCreateRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    db.add(DBComplaint(id=f"cmp-{uuid.uuid4().hex[:6]}", user_name=user["full_name"], user_phone=user["phone"] or user["email"], category=req.category, title=req.title, description=req.description, status="Pending"))

    target_role = "pg_owner" if req.category == "PG Maintenance" else "mess_partner"
    notify_owner(db, target_role, f"New Complaint: {req.category}", f"{user['full_name']} logged complaint: '{req.title}'", "complaint")
    db.commit()

    background_tasks.add_task(
        send_email_notification,
        user["email"],
        f"Complaint Logged: {req.title}",
        f"Hi {user['full_name']},\n\nWe received your complaint regarding '{req.title}'. The respective owner has been notified."
    )

    return {"status": "success", "message": "Complaint logged successfully!"}

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
            f"Complaint Resolved: {comp.title}",
            f"Hi {comp.user_name},\n\nYour complaint '{comp.title}' has been marked as RESOLVED by the owner."
        )

    return {"status": "success", "message": f"Complaint '{comp.title}' marked as RESOLVED!"}

@app.get("/api/notifications")
def get_notifications(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    notifs = db.query(DBNotification).filter(
        or_(DBNotification.recipient_role == user["role"], DBNotification.recipient_role == "all")
    ).order_by(DBNotification.created_at.desc()).all()

    return [{"id": n.id, "title": n.title, "message": n.message, "event_type": n.event_type, "created_at": n.created_at} for n in notifs]

@app.get("/api/admin/metrics")
def get_admin_metrics(db: Session = Depends(get_db)):
    return {
        "active_students": db.query(DBMessStudent).filter(DBMessStudent.is_active == True).count(),
        "pg_listings": db.query(DBPGListing).count(),
        "vacant_rooms": db.query(DBPGRoom).filter(DBPGRoom.status == "vacant").count(),
        "total_users": db.query(DBUser).count(),
        "pending_vacate_requests": db.query(DBVacateRequest).filter(DBVacateRequest.status == "Pending").count(),
        "monthly_gmv": "₹8.5L"
    }

@app.get("/api/admin/users")
def get_admin_users(db: Session = Depends(get_db)):
    return [{"id": u.id, "full_name": u.full_name, "email": u.email, "phone": u.phone, "address": u.address, "google_map_url": u.google_map_url, "role": u.role} for u in db.query(DBUser).all()]