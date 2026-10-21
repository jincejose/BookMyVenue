import hashlib
import hmac
import os
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from io import BytesIO
from secrets import token_hex
from zoneinfo import ZoneInfo

import requests
from flask import Flask, current_app, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity, jwt_required
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, text
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
VENUE_TZ = ZoneInfo("Asia/Kolkata")


def venue_today():
    return datetime.now(VENUE_TZ).date()


def slot_end(start):
    return f"{(int(start[:2]) + 3) % 24:02d}:00"


def payment_mode():
    safe_default = "razorpay" if os.getenv("APP_ENV") == "production" else "demo"
    return os.getenv("PAYMENT_MODE", safe_default).lower()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")
    phone = db.Column(db.String(30), nullable=True)
    sms_opt_in = db.Column(db.Boolean, nullable=False, default=False)


class Venue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(140), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    city = db.Column(db.String(80), nullable=False, index=True)
    address = db.Column(db.String(240), nullable=False)
    description = db.Column(db.Text, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    image = db.Column(db.String(500), nullable=False)
    amenities = db.Column(db.String(500), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(12), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    venue_id = db.Column(db.Integer, db.ForeignKey("venue.id"), nullable=False, index=True)
    event_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    guests = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    payment_status = db.Column(db.String(24), nullable=False, default="demo_paid")
    payment_order_id = db.Column(db.String(80), nullable=True, unique=True)
    payment_id = db.Column(db.String(80), nullable=True, unique=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (Index("uq_active_venue_slot", "venue_id", "event_date", "start_time", unique=True,
                            postgresql_where=text("status != 'cancelled'"), sqlite_where=text("status != 'cancelled'")),)


def venue_json(v):
    return {"id": v.id, "name": v.name, "category": v.category, "city": v.city,
            "address": v.address, "description": v.description, "capacity": v.capacity,
            "price": float(v.price), "image": v.image, "amenities": [x for x in v.amenities.split(",") if x],
            "owner_id": v.owner_id}


def booking_json(b):
    v = db.session.get(Venue, b.venue_id)
    u = db.session.get(User, b.user_id)
    return {"id": b.id, "reference": b.reference, "venue_id": b.venue_id,
            "venue": v.name if v else "Venue", "city": v.city if v else "", "image": v.image if v else "",
            "date": b.event_date.isoformat(), "start_time": b.start_time, "end_time": b.end_time,
            "guests": b.guests, "total": float(b.total), "status": b.status,
            "payment_status": b.payment_status, "customer": u.name if u else "Customer",
            "email": u.email if u else "", "phone": u.phone if u else ""}


def expire_unpaid_holds(venue_id, day):
    expired = Booking.query.filter_by(venue_id=venue_id, event_date=day, status="payment_pending").filter(
        Booking.expires_at <= datetime.utcnow()).all()
    for booking in expired:
        booking.status, booking.payment_status = "cancelled", "expired"
    if expired:
        db.session.commit()


def send_email(to_email, subject, body):
    host, sender = os.getenv("SMTP_HOST"), os.getenv("SMTP_FROM_EMAIL")
    if not host or not sender or not to_email:
        return False
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to_email, subject
    message.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=12) as smtp:
        smtp.starttls()
        username, password = os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD")
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)
    return True


def send_sms(phone, body):
    sid, token, sender = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"), os.getenv("TWILIO_FROM_NUMBER")
    if not sid or not token or not sender or not phone:
        return False
    response = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                             data={"To": phone, "From": sender, "Body": body}, auth=(sid, token), timeout=12)
    response.raise_for_status()
    return True


def notify_booking(booking, event):
    venue = db.session.get(Venue, booking.venue_id)
    customer = db.session.get(User, booking.user_id)
    owner = db.session.get(User, venue.owner_id) if venue else None
    if not venue or not customer or not owner:
        return
    event_date = booking.event_date.strftime("%d %b %Y")
    details = f"\nVenue: {venue.name}\nDate: {event_date}\nTime: {booking.start_time}–{booking.end_time}\nGuests: {booking.guests}\nReference: {booking.reference}\nAmount: ₹{float(booking.total):,.2f}"
    if event == "requested":
        payment_line = "Demo checkout is complete; no real payment was taken." if booking.payment_status == "demo_payment" else "Payment is received."
        customer_subject, customer_line = "Booking request received", f"{payment_line} Your request is waiting for the venue owner to confirm."
        owner_subject, owner_line = "A guest requested your venue", f"{customer.name} requested {venue.name}. Please sign in to confirm or decline."
    elif event == "confirmed":
        customer_subject, customer_line = "Your venue booking is confirmed", f"{venue.name} has confirmed your booking."
        owner_subject, owner_line = None, None
    elif event == "cancelled_by_customer":
        customer_subject, customer_line = "Your booking was cancelled", f"Your request for {venue.name} was cancelled and any eligible payment refund was initiated."
        owner_subject, owner_line = "A guest cancelled their request", f"{customer.name} cancelled their booking request for {venue.name}."
    else:
        customer_subject, customer_line = "Booking update", f"Your request for {venue.name} was declined. An eligible payment refund was initiated."
        owner_subject, owner_line = None, None
    outgoing = [(send_email, customer.email, customer_subject, f"Hi {customer.name},\n\n{customer_line}{details}\n\nBookMyVenue")]
    if customer.sms_opt_in:
        outgoing.append((send_sms, customer.phone, f"BookMyVenue: {customer_line} {venue.name}, {event_date}, {booking.start_time}. Ref {booking.reference}."))
    if event in ("requested", "cancelled_by_customer"):
        outgoing.append((send_email, owner.email, owner_subject, f"Hi {owner.name},\n\n{owner_line}{details}\n\nBookMyVenue"))
        if owner.sms_opt_in:
            outgoing.append((send_sms, owner.phone, f"BookMyVenue: {owner_line} Ref {booking.reference}."))
    for sender, *args in outgoing:
        try: sender(*args)
        except Exception: current_app.logger.exception("Booking notification delivery failed for %s", booking.reference)


def reserve_booking(data, user_id, *, status="pending", payment_status="demo_paid", expires_at=None):
    venue = db.session.get(Venue, data.get("venue_id"))
    if not venue or not venue.active:
        return None, ("Venue is no longer available.", 404)
    try:
        day = date.fromisoformat(data.get("date", ""))
    except (ValueError, TypeError):
        return None, ("Choose a valid date.", 400)
    slot, guests = data.get("start_time", ""), data.get("guests", 0)
    if day < venue_today():
        return None, ("Choose a date today or later.", 400)
    if slot not in ("09:00", "12:00", "15:00", "18:00", "21:00"):
        return None, ("Choose an available time slot.", 400)
    now = datetime.now(VENUE_TZ)
    if day == now.date() and slot <= now.strftime("%H:%M"):
        return None, ("That time has already passed.", 400)
    if not isinstance(guests, int) or guests < 1 or guests > venue.capacity:
        return None, (f"Guest count must be between 1 and {venue.capacity}.", 400)
    expire_unpaid_holds(venue.id, day)
    if Booking.query.filter_by(venue_id=venue.id, event_date=day, start_time=slot).filter(Booking.status != "cancelled").first():
        return None, ("That time was just booked. Please select another slot.", 409)
    booking = Booking(reference="BMV" + token_hex(4).upper(), user_id=user_id, venue_id=venue.id,
                      event_date=day, start_time=slot, end_time=slot_end(slot), guests=guests,
                      total=round(float(venue.price), 2), status=status,
                      payment_status=payment_status, expires_at=expires_at)
    db.session.add(booking)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return None, ("That time was just booked. Please select another slot.", 409)
    return booking, None


def refund_booking(booking):
    if booking.payment_status in ("demo_payment", "demo_paid"):
        booking.payment_status = "demo_refunded"
        return
    if booking.payment_status != "paid" or not booking.payment_id:
        return
    key, secret = os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")
    if not key or not secret:
        raise RuntimeError("Razorpay refund credentials are not configured.")
    response = requests.post(f"https://api.razorpay.com/v1/payments/{booking.payment_id}/refund",
                             json={"amount": round(float(booking.total) * 100)}, auth=(key, secret), timeout=15)
    response.raise_for_status()
    refund = response.json()
    booking.payment_status = "refunded" if refund.get("status") == "processed" else "refund_pending"


def current_user():
    return db.session.get(User, int(get_jwt_identity()))


def owner_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapped(*args, **kwargs):
        if current_user().role != "owner":
            return jsonify(error="Venue owner account required"), 403
        return fn(*args, **kwargs)
    return wrapped


def make_app():
    app = Flask(__name__)
    database_url = os.getenv("DATABASE_URL", "sqlite:///bookmyvenue.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config.update(SQLALCHEMY_DATABASE_URI=database_url, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
                      MAX_CONTENT_LENGTH=8 * 1024 * 1024,
                      UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
                      JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "dev-only-change-this-secret-before-deploy"),
                      JWT_ACCESS_TOKEN_EXPIRES=timedelta(days=7))
    db.init_app(app)
    JWTManager(app)
    CORS(app, origins=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").split(","),
         supports_credentials=False)

    @app.errorhandler(413)
    def upload_too_large(_error):
        return jsonify(error="Images must be 8 MB or smaller."), 413

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "BookMyVenue API"}

    @app.get("/api/config")
    def public_config():
        mode = payment_mode()
        checkout_enabled = mode == "demo" or (mode == "razorpay" and bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET")))
        cloud_ready = all(os.getenv(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))
        return jsonify(payment_mode=mode, checkout_enabled=checkout_enabled,
                       razorpay_key_id=os.getenv("RAZORPAY_KEY_ID", "") if checkout_enabled and mode == "razorpay" else "",
                       email_enabled=bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM_EMAIL")),
                       sms_enabled=bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_FROM_NUMBER")),
                       image_storage="cloudinary" if cloud_ready else ("local" if os.getenv("APP_ENV") != "production" else "not_configured"))

    @app.post("/api/auth/register")
    def register():
        data = request.get_json() or {}
        name, email, password = data.get("name", "").strip(), data.get("email", "").strip().lower(), data.get("password", "")
        role = data.get("role", "user")
        if not name or "@" not in email or len(password) < 8 or role not in ("user", "owner"):
            return jsonify(error="Enter a name, valid email, password of 8+ characters and valid account type."), 400
        if User.query.filter_by(email=email).first():
            return jsonify(error="An account with this email already exists."), 409
        user = User(name=name, email=email, password=generate_password_hash(password), role=role,
                    phone=(data.get("phone") or "").strip() or None, sms_opt_in=bool(data.get("sms_opt_in", False)))
        db.session.add(user)
        db.session.commit()
        return jsonify(token=create_access_token(identity=str(user.id)), user={"id": user.id, "name": user.name, "email": user.email, "role": user.role, "phone": user.phone, "sms_opt_in": user.sms_opt_in}), 201

    @app.post("/api/auth/login")
    def login():
        data = request.get_json() or {}
        user = User.query.filter_by(email=data.get("email", "").strip().lower()).first()
        if not user or not check_password_hash(user.password, data.get("password", "")):
            return jsonify(error="Email or password is incorrect."), 401
        return jsonify(token=create_access_token(identity=str(user.id)), user={"id": user.id, "name": user.name, "email": user.email, "role": user.role, "phone": user.phone, "sms_opt_in": user.sms_opt_in})

    @app.get("/api/venues")
    def venues():
        q = Venue.query.filter_by(active=True)
        city, category, search = request.args.get("city", "").strip(), request.args.get("category", "").strip(), request.args.get("q", "").strip()
        if city: q = q.filter(Venue.city.ilike(f"%{city}%"))
        if category and category != "All venues": q = q.filter_by(category=category)
        if search: q = q.filter(db.or_(Venue.name.ilike(f"%{search}%"), Venue.city.ilike(f"%{search}%"), Venue.description.ilike(f"%{search}%")))
        return jsonify([venue_json(v) for v in q.order_by(Venue.created_at.desc()).all()])

    @app.get("/api/venues/<int:venue_id>")
    def venue_detail(venue_id):
        v = db.session.get(Venue, venue_id)
        return (jsonify(venue_json(v)), 200) if v and v.active else (jsonify(error="Venue not found"), 404)

    @app.get("/api/venues/<int:venue_id>/availability")
    def availability(venue_id):
        v = db.session.get(Venue, venue_id)
        if not v: return jsonify(error="Venue not found"), 404
        try: day = date.fromisoformat(request.args.get("date", ""))
        except ValueError: return jsonify(error="Choose a valid date."), 400
        if day < venue_today(): return jsonify(error="Date must be today or later."), 400
        expire_unpaid_holds(venue_id, day)
        bookings = Booking.query.filter_by(venue_id=venue_id, event_date=day).filter(Booking.status != "cancelled").all()
        slots = ["09:00", "12:00", "15:00", "18:00", "21:00"]
        now = datetime.now(VENUE_TZ)
        return jsonify([{"start_time": s, "end_time": slot_end(s), "available": not any(b.start_time == s for b in bookings)
                         and not (day == now.date() and s <= now.strftime("%H:%M"))} for s in slots])

    @app.post("/api/bookings")
    @jwt_required()
    def create_booking():
        if payment_mode() != "demo":
            return jsonify(error="Use the secure checkout flow to create a paid booking."), 403
        b, error = reserve_booking(request.get_json() or {}, int(get_jwt_identity()), payment_status="demo_payment")
        if error: return jsonify(error=error[0]), error[1]
        notify_booking(b, "requested")
        return jsonify(booking_json(b)), 201

    @app.post("/api/checkout/order")
    @jwt_required()
    def checkout_order():
        mode = payment_mode()
        data = request.get_json() or {}
        if mode == "demo":
            booking, error = reserve_booking(data, int(get_jwt_identity()), payment_status="demo_payment")
            if error: return jsonify(error=error[0]), error[1]
            notify_booking(booking, "requested")
            return jsonify(mode="demo", booking=booking_json(booking)), 201
        if mode != "razorpay": return jsonify(error="Payment provider configuration is invalid."), 503
        key, secret = os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")
        if not key or not secret: return jsonify(error="Online payments are not configured yet."), 503
        hold_until = datetime.utcnow() + timedelta(minutes=15)
        booking, error = reserve_booking(data, int(get_jwt_identity()), status="payment_pending",
                                         payment_status="payment_pending", expires_at=hold_until)
        if error: return jsonify(error=error[0]), error[1]
        if float(booking.total) < 1:
            booking.status, booking.payment_status = "cancelled", "payment_failed"; db.session.commit()
            return jsonify(error="Online checkout requires a venue price of at least ₹1."), 400
        try:
            response = requests.post("https://api.razorpay.com/v1/orders",
                                     json={"amount": round(float(booking.total) * 100), "currency": "INR",
                                           "receipt": booking.reference, "notes": {"booking_reference": booking.reference}},
                                     auth=(key, secret), timeout=18)
            response.raise_for_status()
            order = response.json()
            booking.payment_order_id = order["id"]
            db.session.commit()
        except Exception:
            booking.status, booking.payment_status = "cancelled", "payment_failed"; db.session.commit()
            current_app.logger.exception("Could not create payment order for %s", booking.reference)
            return jsonify(error="Could not start checkout. Your time slot has been released."), 502
        customer = current_user()
        return jsonify(mode="razorpay", booking_id=booking.id, reference=booking.reference,
                       order_id=booking.payment_order_id, key_id=key, amount=order["amount"], currency="INR",
                       customer={"name": customer.name, "email": customer.email, "phone": customer.phone or ""}), 201

    @app.post("/api/checkout/verify")
    @jwt_required()
    def checkout_verify():
        data = request.get_json() or {}
        booking = db.session.get(Booking, data.get("booking_id"))
        payment_id, order_id, signature = data.get("razorpay_payment_id", ""), data.get("razorpay_order_id", ""), data.get("razorpay_signature", "")
        secret = os.getenv("RAZORPAY_KEY_SECRET", "")
        if not booking or booking.user_id != int(get_jwt_identity()): return jsonify(error="Checkout booking not found."), 404
        if booking.payment_status == "paid" and booking.payment_id == payment_id: return jsonify(booking_json(booking))
        if booking.status != "payment_pending" or booking.expires_at < datetime.utcnow():
            return jsonify(error="This checkout expired. Please choose the slot again."), 409
        expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        if not secret or order_id != booking.payment_order_id or not hmac.compare_digest(expected, signature):
            return jsonify(error="Payment verification failed."), 400
        booking.payment_id, booking.payment_status, booking.status, booking.expires_at = payment_id, "paid", "pending", None
        db.session.commit()
        notify_booking(booking, "requested")
        return jsonify(booking_json(booking))

    @app.post("/api/checkout/fail")
    @jwt_required()
    def checkout_fail():
        data = request.get_json() or {}
        booking = db.session.get(Booking, data.get("booking_id"))
        if booking and booking.user_id == int(get_jwt_identity()) and booking.status == "payment_pending":
            booking.status, booking.payment_status = "cancelled", "payment_failed"
            db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/webhooks/razorpay")
    def razorpay_webhook():
        webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
        signature = request.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(webhook_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not webhook_secret or not hmac.compare_digest(expected, signature): return jsonify(error="Invalid webhook signature."), 400
        payload = request.get_json(silent=True) or {}
        event = payload.get("event")
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id, payment_id = entity.get("order_id"), entity.get("id")
        booking = Booking.query.filter_by(payment_order_id=order_id).first() if order_id else None
        if not booking: return jsonify(ok=True)
        valid_amount = entity.get("amount") == round(float(booking.total) * 100) and entity.get("currency") == "INR"
        if event == "payment.captured" and valid_amount and booking.payment_status in ("payment_pending", "payment_failed", "expired"):
            if booking.status != "payment_pending" or (booking.expires_at and booking.expires_at < datetime.utcnow()):
                booking.payment_id, booking.payment_status = payment_id, "paid"; db.session.commit()
                try: refund_booking(booking); db.session.commit()
                except Exception:
                    db.session.rollback(); current_app.logger.exception("Late-payment refund failed for %s", booking.reference)
            else:
                booking.payment_id, booking.payment_status, booking.status, booking.expires_at = payment_id, "paid", "pending", None
                db.session.commit(); notify_booking(booking, "requested")
        elif event == "payment.failed" and booking.payment_status == "payment_pending":
            booking.status, booking.payment_status = "cancelled", "payment_failed"; db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/bookings/mine")
    @jwt_required()
    def my_bookings():
        return jsonify([booking_json(b) for b in Booking.query.filter_by(user_id=int(get_jwt_identity())).order_by(Booking.created_at.desc()).all()])

    @app.patch("/api/bookings/<int:booking_id>/cancel")
    @jwt_required()
    def cancel_booking(booking_id):
        b = db.session.get(Booking, booking_id)
        if not b or b.user_id != int(get_jwt_identity()): return jsonify(error="Booking not found."), 404
        if b.status == "cancelled": return jsonify(error="Booking is already cancelled."), 409
        try:
            refund_booking(b)
        except Exception:
            db.session.rollback(); current_app.logger.exception("Refund failed for %s", b.reference)
            return jsonify(error="Could not start the refund. Please contact support; the booking was left unchanged."), 502
        b.status = "cancelled"; db.session.commit(); notify_booking(b, "cancelled_by_customer")
        return jsonify(booking_json(b))

    @app.get("/api/owner/dashboard")
    @owner_required
    def owner_dashboard():
        uid = int(get_jwt_identity())
        venues = Venue.query.filter_by(owner_id=uid).all()
        ids = [v.id for v in venues]
        bookings = Booking.query.filter(Booking.venue_id.in_(ids), Booking.status != "payment_pending").order_by(Booking.event_date.asc()).all() if ids else []
        active = [b for b in bookings if b.status != "cancelled"]
        return jsonify(venues=[venue_json(v) for v in venues], bookings=[booking_json(b) for b in bookings],
                       stats={"venues": len(venues), "bookings": len(active), "revenue": sum(float(b.total) for b in active if b.status == "confirmed"), "upcoming": sum(1 for b in active if b.event_date >= venue_today())})

    @app.post("/api/owner/venues")
    @owner_required
    def create_venue():
        data = request.get_json() or {}
        required = ("name", "category", "city", "address", "description", "image")
        if any(not str(data.get(k, "")).strip() for k in required): return jsonify(error="Please complete every required venue field."), 400
        try:
            capacity, price = int(data.get("capacity", 0)), float(data.get("price", 0))
            if capacity < 1 or price < 0: raise ValueError()
        except (ValueError, TypeError): return jsonify(error="Enter a valid capacity and price."), 400
        v = Venue(owner_id=int(get_jwt_identity()), name=data["name"].strip(), category=data["category"], city=data["city"].strip(),
                  address=data["address"].strip(), description=data["description"].strip(), capacity=capacity, price=price,
                  image=data["image"].strip(), amenities=",".join(data.get("amenities", [])))
        db.session.add(v); db.session.commit()
        return jsonify(venue_json(v)), 201

    @app.patch("/api/owner/bookings/<int:booking_id>")
    @owner_required
    def update_booking(booking_id):
        uid = int(get_jwt_identity()); b = db.session.get(Booking, booking_id)
        if not b or db.session.get(Venue, b.venue_id).owner_id != uid: return jsonify(error="Booking not found."), 404
        status = (request.get_json() or {}).get("status")
        if status not in ("confirmed", "cancelled"): return jsonify(error="Invalid booking status."), 400
        if b.status in ("cancelled", "payment_pending"): return jsonify(error="This booking can no longer be changed."), 409
        if status == "cancelled":
            try: refund_booking(b)
            except Exception:
                db.session.rollback(); current_app.logger.exception("Refund failed for %s", b.reference)
                return jsonify(error="Refund could not be started, so the booking was left unchanged."), 502
        b.status = status; db.session.commit()
        notify_booking(b, "confirmed" if status == "confirmed" else "declined")
        return jsonify(booking_json(b))

    @app.post("/api/owner/uploads")
    @owner_required
    def upload_venue_image():
        image_file = request.files.get("image")
        if not image_file or not image_file.filename: return jsonify(error="Choose an image to upload."), 400
        try:
            image = Image.open(image_file.stream)
            if image.format not in ("JPEG", "PNG", "WEBP") or image.width * image.height > 24_000_000:
                return jsonify(error="Upload a JPEG, PNG, or WebP image up to 24 megapixels."), 400
            image.load()
            image.thumbnail((2400, 1600))
            output = BytesIO()
            image.convert("RGB").save(output, format="WEBP", quality=86, method=5)
            output.seek(0)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            return jsonify(error="The selected file is not a valid image."), 400
        cloud_name, cloud_key, cloud_secret = (os.getenv(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))
        if cloud_name and cloud_key and cloud_secret:
            try:
                response = requests.post(f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                                         files={"file": ("venue.webp", output.getvalue(), "image/webp")},
                                         data={"folder": "bookmyvenue/venues"}, auth=(cloud_key, cloud_secret), timeout=45)
                response.raise_for_status()
                return jsonify(url=response.json()["secure_url"], storage="cloudinary"), 201
            except Exception:
                current_app.logger.exception("Cloudinary venue photo upload failed")
                return jsonify(error="Photo upload failed. Please try again."), 502
        if os.getenv("APP_ENV") == "production":
            return jsonify(error="Image storage is not configured. Add Cloudinary credentials to enable uploads."), 503
        os.makedirs(current_app.config["UPLOAD_FOLDER"], exist_ok=True)
        filename = f"{token_hex(12)}.webp"
        with open(os.path.join(current_app.config["UPLOAD_FOLDER"], filename), "wb") as saved_file:
            saved_file.write(output.getvalue())
        return jsonify(url=f"/uploads/{filename}", storage="local"), 201

    @app.get("/uploads/<path:filename>")
    def local_upload(filename):
        if os.getenv("APP_ENV") == "production": return jsonify(error="Not found"), 404
        return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)

    @app.get("/")
    def frontend_index():
        return send_from_directory("static", "index.html")

    @app.get("/<path:path>")
    def frontend_assets(path):
        # API endpoints above take precedence; every other path supports React client-side routes.
        return send_from_directory("static", path) if os.path.isfile(os.path.join(app.root_path, "static", path)) else send_from_directory("static", "index.html")

    with app.app_context():
        db.create_all()
        legacy_columns = {
            "user": [("phone", "VARCHAR(30)"), ("sms_opt_in", "BOOLEAN NOT NULL DEFAULT FALSE")],
            "booking": [("payment_status", "VARCHAR(24) NOT NULL DEFAULT 'demo_paid'"),
                        ("payment_order_id", "VARCHAR(80)"), ("payment_id", "VARCHAR(80)"), ("expires_at", "TIMESTAMP")],
        }
        inspector = db.inspect(db.engine)
        for table, columns in legacy_columns.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column, column_type in columns:
                if column not in existing:
                    quoted_table = db.engine.dialect.identifier_preparer.quote(table)
                    db.session.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN {column} {column_type}"))
        db.session.commit()
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_payment_order ON booking (payment_order_id)"))
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_payment_id ON booking (payment_id)"))
        db.session.commit()
        if os.getenv("SEED_DEMO", "false").lower() == "true" and not Venue.query.first():
            owner = User(name="Demo Venue Owner", email="owner@bookmyvenue.in", password=generate_password_hash("Venue123!"), role="owner")
            db.session.add(owner); db.session.flush()
            demo = [
                ("The Palm Courtyard", "Garden", "Kochi", "Fort Kochi, Kerala", "A breezy tropical courtyard with old-world charm, warm lights and plenty of room to celebrate.", 180, 18000, "https://images.unsplash.com/photo-1519167758481-83f550bb49b3?auto=format&fit=crop&w=1400&q=85", "Garden,Outdoor,Parking,Catering"),
                ("Mango Tree Social", "Cafe", "Kochi", "Panampilly Nagar, Kochi", "A vibrant cafe and private dining space with a leafy terrace, thoughtful menus and a relaxed feel.", 65, 8500, "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=1400&q=85", "Terrace,Catering,WiFi,Parking"),
                ("The Glasshouse", "Banquet hall", "Kochi", "Kakkanad, Kochi", "A light-filled, contemporary event hall designed for weddings, launches and milestone gatherings.", 320, 32000, "https://images.unsplash.com/photo-1511795409834-ef04bbd61622?auto=format&fit=crop&w=1400&q=85", "Air conditioning,Stage,Parking,Sound system"),
                ("Backwater House", "Resort", "Alappuzha", "Mararikulam, Alappuzha", "Celebrate by the water in a private Kerala retreat with lush gardens and an unhurried coastal atmosphere.", 120, 26000, "https://images.unsplash.com/photo-1571896349842-33c89424de2d?auto=format&fit=crop&w=1400&q=85", "Waterfront,Accommodation,Catering,Pool"),
                ("Studio Malabar", "Meetup space", "Kochi", "Mattancherry, Kochi", "A flexible creative studio for workshops, intimate celebrations and community gatherings.", 50, 7000, "https://images.unsplash.com/photo-1497366754035-f200968a6e72?auto=format&fit=crop&w=1400&q=85", "Projector,WiFi,Air conditioning,Catering"),
                ("Hillview Pavilion", "Banquet hall", "Munnar", "Chithirapuram, Munnar", "A serene mountain pavilion framed by tea gardens, with sweeping views and space for your people.", 200, 24000, "https://images.unsplash.com/photo-1464366400600-7168b8af9bc3?auto=format&fit=crop&w=1400&q=85", "Mountain view,Outdoor,Parking,Catering")]
            for name, cat, city, address, desc, capacity, price, image, amenities in demo:
                db.session.add(Venue(owner_id=owner.id, name=name, category=cat, city=city, address=address, description=desc,
                                     capacity=capacity, price=price, image=image, amenities=amenities))
            db.session.commit()
    return app


app = make_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=os.getenv("FLASK_DEBUG") == "1")
