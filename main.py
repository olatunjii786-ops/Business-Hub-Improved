import os
import json
import hmac
import hashlib
import httpx
import base64
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qsl
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, BigInteger, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# --- CONFIGURATION & ENVIRONMENT SETUP ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = os.getenv("ADMIN_TELEGRAM_ID")
APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
FLW_SECRET_HASH = os.getenv("FLW_SECRET_HASH", "BusinessHubSecureHash2026")
FLW_SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY") 

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: Environment variables TELEGRAM_BOT_TOKEN or DATABASE_URL are missing!")

BOT_USERNAME = "isaacbusinessbot"

app = FastAPI(title="Business Hub Central Engine")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE ENGINE CONFIGURATION ---
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- RELATIONAL DATA MODELS ---
class AdminConfig(Base):
    __tablename__ = "admin_configs"
    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Float, default=0.0)

class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BigInteger, primary_key=True)
    business_name = Column(String(255), nullable=False)
    bio = Column(Text, nullable=True)
    phone_number = Column(String(20), nullable=False)
    logo_url = Column(Text, nullable=True)
    is_approved = Column(Boolean, default=True)
    is_banned = Column(Boolean, default=False)  # Admin Security Gate Check
    bank_code = Column(String(50), nullable=True)
    account_number = Column(String(50), nullable=True)
    subaccount_id = Column(String(100), nullable=True)  

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(BigInteger, nullable=False)
    title = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    sizes = Column(String(255), nullable=True)
    category = Column(String(100), default="General")
    image_url = Column(Text, nullable=True)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(BigInteger, nullable=False)
    customer_id = Column(BigInteger, nullable=False)
    customer_name = Column(String(200), nullable=False)
    customer_phone = Column(String(20), nullable=False)
    delivery_address = Column(Text, nullable=False)
    items = Column(Text, default="[]")
    total_amount = Column(Float, nullable=False)
    order_code = Column(String(100), unique=True, nullable=False)
    status = Column(String(50), default="pending")

Base.metadata.create_all(bind=engine)

# --- PYDANTIC SCHEMAS ---
class CheckoutItem(BaseModel):
    product_id: int
    quantity: int
    size: str

class CheckoutRequest(BaseModel):
    customer_name: str
    customer_phone: str
    delivery_address: str
    items: List[CheckoutItem]

class PayoutUpgradePayload(BaseModel):
    bank_code: str
    account_number: str

class StatusPayload(BaseModel):
    status: str

class CommissionPayload(BaseModel):
    commission_percentage: float

class BanVendorPayload(BaseModel):
    vendor_id: int
    ban_status: bool

# --- TELEGRAM BOT UTILS ---
async def send_telegram_alert(chat_id: int, message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5.0)
    except Exception as e:
        print(f"Async Alert failure: {e}")

def validate_telegram_auth(init_data: str) -> Optional[dict]:
    if not init_data:
        return None
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_check = vals.pop('hash', None)
        data_check = '\n'.join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if h != hash_check:
            return None
        return json.loads(vals['user'])
    except Exception:
        return None

# --- AUTOMATED BANK REGISTRY DISCOVERY ENDPOINT ---
@app.get("/api/banks")
async def get_supported_banks():
    if not FLW_SECRET_KEY:
        return []
        
    url = "https://api.flutterwave.com/v3/banks/NG"
    headers = {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=8.0)
            if res.status_code == 200:
                return res.json().get("data", [])
            print(f"Failed fetching FLW banks list upstream: {res.text}")
            return []
    except Exception as e:
        print(f"Exception raised during bank list retrieval: {e}")
        return []

# --- FLUTTERWAVE CORE UTILS ---
async def generate_flw_subaccount(bank_code: str, account_number: str, business_name: str, phone: str, vendor_id: int, db: Session) -> Optional[str]:
    if not FLW_SECRET_KEY:
        print("Split implementation failed: FLUTTERWAVE_SECRET_KEY missing from environment properties context.")
        return None
        
    url = "https://api.flutterwave.com/v3/subaccounts"
    headers = {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    resolved_bank_code = None

    try:
        async with httpx.AsyncClient() as client:
            banks_res = await client.get("https://api.flutterwave.com/v3/banks/NG", headers=headers, timeout=8.0)
            if banks_res.status_code == 200:
                flw_banks = banks_res.json().get("data", [])
                
                for bank in flw_banks:
                    if bank.get("code") == bank_code:
                        resolved_bank_code = bank.get("code")
                        break
                
                if not resolved_bank_code:
                    search_terms = {
                        "100005": ["opay", "paycom"],
                        "090405": ["moniepoint"],
                        "090267": ["kuda"]
                    }
                    terms = search_terms.get(bank_code, [])
                    for bank in flw_banks:
                        bank_name_lower = bank.get("name", "").lower()
                        if any(term in bank_name_lower for term in terms):
                            resolved_bank_code = bank.get("code")
                            print(f"--> Dynamically mapped {bank_code} to Flutterwave code: {resolved_bank_code}")
                            break
    except Exception as fetch_err:
        print(f"Warning: Failed to perform live bank discovery lookup: {fetch_err}")

    if not resolved_bank_code:
        resolved_bank_code = bank_code

    fallback_email = f"vendor_{vendor_id}@businesshub.internal"
    
    # FETCH DYNAMIC COMMISSION TO CALCULATE CORRECT VENDOR FRACTIONAL SPLIT
    config_row = db.query(AdminConfig).filter(AdminConfig.config_key == "global_commission").first()
    admin_commission = config_row.config_value if config_row else 0.0
    vendor_share_fraction = (100.0 - admin_commission) / 100.0
    
    # REMAPPED TO PERCENTAGE SPLIT POOL SETUP FOR COMPATIBILITY WITH OPAY WALLETS
    payload = {
        "account_bank": resolved_bank_code,
        "account_number": account_number,
        "business_name": business_name,
        "business_mobile": phone,
        "business_email": fallback_email,
        "country": "NG",
        "split_type": "percentage",
        "split_value": vendor_share_fraction  
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if res.status_code in [200, 201]:
                return res.json().get("data", {}).get("subaccount_id")
            print(f"Upstream subaccount creation failure reason: {res.text}")
            return None
    except Exception as e:
        print(f"Gateway pipeline timeout/transport failure: {e}")
        return None

# --- ADMINISTRATIVE DECK SECURITY VERIFIER ---
def verify_admin_privileges(request: Request) -> bool:
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user or str(user.get('id')) != str(ADMIN_ID):
        raise HTTPException(status_code=403, detail="Access Denied: Administrative Rights Required.")
    return True

# --- ADMINISTRATIVE SYSTEM CONTROLS ---
@app.post("/api/admin/commission")
async def update_platform_commission(payload: CommissionPayload, request: Request, db: Session = Depends(get_db)):
    verify_admin_privileges(request)
    if payload.commission_percentage < 0 or payload.commission_percentage > 100:
        raise HTTPException(status_code=400, detail="Commission split configuration must sit between 0% and 100%")
        
    config = db.query(AdminConfig).filter(AdminConfig.config_key == "global_commission").first()
    if not config:
        config = AdminConfig(config_key="global_commission", config_value=payload.commission_percentage)
        db.add(config)
    else:
        config.config_value = payload.commission_percentage
    db.commit()
    return {"success": True, "message": f"Global infrastructure commission configured to {payload.commission_percentage}%"}

@app.get("/api/admin/overview")
async def get_admin_dashboard_metrics(request: Request, db: Session = Depends(get_db)):
    verify_admin_privileges(request)
    config = db.query(AdminConfig).filter(AdminConfig.config_key == "global_commission").first()
    current_commission = config.config_value if config else 0.0
    
    vendors = db.query(Vendor).all()
    vendor_list = [{
        "vendor_id": v.vendor_id,
        "business_name": v.business_name,
        "phone_number": v.phone_number,
        "is_banned": v.is_banned,
        "subaccount_id": v.subaccount_id
    } for v in vendors]
    
    return {"current_commission": current_commission, "vendors": vendor_list}

@app.post("/api/admin/vendors/ban")
async def toggle_vendor_ban_status(payload: BanVendorPayload, request: Request, db: Session = Depends(get_db)):
    verify_admin_privileges(request)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Target vendor profile missing.")
        
    vendor.is_banned = payload.ban_status
    db.commit()
    return {"success": True, "message": f"Vendor ban status updated to: {payload.ban_status}"}

@app.delete("/api/admin/vendors/{vendor_id}")
async def hard_delete_vendor_profile(vendor_id: int, request: Request, db: Session = Depends(get_db)):
    verify_admin_privileges(request)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor trace missing from database rows.")
        
    db.query(Product).filter(Product.vendor_id == vendor_id).delete()
    db.delete(vendor)
    db.commit()
    return {"success": True, "message": "Vendor and catalog metadata purged successfully."}

# --- ROUTE TEMPLATE ENDPOINTS ---
@app.get("/shop")
async def serve_shop(request: Request):
    return templates.TemplateResponse(request=request, name="shop.html")

@app.get("/vendor")
@app.get("/webapp/register")
async def serve_vendor(request: Request):
    return templates.TemplateResponse(request=request, name="vendor.html")

@app.get("/admin/dashboard")
async def serve_admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

# --- HEALTH ROUTE ENGINE FOR CRONJOB SERVICE PINGS ---
@app.get("/api/health")
async def cronjob_health_checkpoint():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# --- TELEGRAM BOT WEBHOOK ROUTER ---
# --- TELEGRAM BOT WEBHOOK ROUTER ---
@app.post("/webhook")
async def telegram_webhook_endpoint(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
        if "message" in payload:
            message = payload["message"]
            chat_id = message["chat"]["id"]
            user_text = message.get("text", "").strip()
            
            # 1. NEW COMMAND: /admin (Strictly for you)
            if user_text == "/admin":
                if str(chat_id) != str(ADMIN_ID):
                    await send_telegram_alert(chat_id, "❌ Prohibited: You do not have administrative access rights.")
                    return {"status": "ok"}
                    
                welcome_msg = (
                    f"⚡ *System Control Deck Activated.*\n\n"
                    f"Welcome back, Chief. Access your master admin panel below:"
                )
                keyboard = {
                    "inline_keyboard": [[
                        {"text": "📊 Open Control Deck", "web_app": {"url": f"{APP_URL}/admin/dashboard"}}
                    ]]
                }
                
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                async with httpx.AsyncClient() as client:
                    await client.post(url, json={
                        "chat_id": chat_id,
                        "text": welcome_msg,
                        "parse_mode": "Markdown",
                        "reply_markup": keyboard
                    }, timeout=5.0)
                return {"status": "ok"}

            # 2. COMMAND: /start
            if user_text.startswith("/start"):
                parts = user_text.split(" ", 1)
                
                # Deep-linking check for customers opening a specific shop
                if len(parts) > 1 and parts[1].isdigit():
                    target_vendor_id = int(parts[1])
                    
                    v_check = db.query(Vendor).filter(Vendor.vendor_id == target_vendor_id).first()
                    if v_check and v_check.is_banned:
                        await send_telegram_alert(chat_id, "⚠️ This boutique storefront is currently suspended.")
                        return {"status": "ok"}
                        
                    welcome_msg = (
                        f"🛍 *Welcome to Business Hub!*\n\n"
                        f"You have opened a direct merchant boutique page.\n\n"
                        f"👉 Tap the button below to view their active catalog elements!"
                    )
                    keyboard = {
                        "inline_keyboard": [[
                            {
                                "text": "🌐 Open Boutique Storefront",
                                "web_app": {"url": f"{APP_URL}/shop?startapp={target_vendor_id}"}
                            }
                        ]]
                    }
                
                # Base /start menu
                else:
                    # Tailored menu if the admin runs /start
                    if str(chat_id) == str(ADMIN_ID):
                        welcome_msg = (
                            f"⚡ *Welcome Back, Chief System Admin.*\n\n"
                            f"Ecosystem control parameters are healthy. Select your destination workspace below:"
                        )
                        keyboard = {
                            "inline_keyboard": [
                                [{"text": "📊 Open System Control Deck", "web_app": {"url": f"{APP_URL}/admin/dashboard"}}],
                                [{"text": "🛍 Open Global Marketplace", "web_app": {"url": f"{APP_URL}/shop"}}],
                                [{"text": "🛠 Open Vendor Workspace Console", "web_app": {"url": f"{APP_URL}/vendor"}}]
                            ]
                        }
                    # Standard menu for all other users/vendors
                    else:
                        welcome_msg = (
                            f"👋 *Welcome to the Business Hub Ecosystem!*\n\n"
                            f"Are you a customer ready to shop top-tier products, or a vendor looking to manage your boutique automation?\n\n"
                            f"Launch your workspace window instantly using the control deck below:"
                        )
                        keyboard = {
                            "inline_keyboard": [
                                [{"text": "🛍 Open Global Marketplace", "web_app": {"url": f"{APP_URL}/shop"}}],
                                [{"text": "🛠 Open Vendor Workspace Console", "web_app": {"url": f"{APP_URL}/vendor"}}]
                            ]
                        }
                
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                async with httpx.AsyncClient() as client:
                    await client.post(url, json={
                        "chat_id": chat_id,
                        "text": welcome_msg,
                        "parse_mode": "Markdown",
                        "reply_markup": keyboard
                    }, timeout=5.0)
                    
        return {"status": "ok"}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"status": "error", "detail": str(e)}


# --- ENDPOINTS CONFIGURATION ---
@app.get("/api/vendor/me")
async def verify_vendor_session(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Session check broken")
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor:
        return {"registered": False}
        
    if vendor.is_banned:
        raise HTTPException(status_code=403, detail="Your vendor console access has been suspended.")
    
    if vendor.bank_code and vendor.account_number and not vendor.subaccount_id:
        sub_id = await generate_flw_subaccount(
            bank_code=vendor.bank_code,
            account_number=vendor.account_number,
            business_name=vendor.business_name,
            phone=vendor.phone_number,
            vendor_id=vendor.vendor_id,
            db=db
        )
        if sub_id:
            vendor.subaccount_id = sub_id
            db.commit()
            db.refresh(vendor)
            
    has_payout = bool(vendor.bank_code and vendor.account_number and vendor.subaccount_id)
    
    return {
        "registered": True,
        "has_payout_setup": has_payout,
        "business_name": vendor.business_name,
        "bio": vendor.bio,
        "phone_number": vendor.phone_number,
        "logo_url": vendor.logo_url,
        "direct_link": f"https://t.me/{BOT_USERNAME}/app?startapp={vendor.vendor_id}"
    }

@app.post("/api/vendor/register")
async def register_or_edit_vendor(
    request: Request,
    business_name: str = Form(...),
    bio: str = Form(""),
    phone_number: str = Form(...),
    bank_code: Optional[str] = Form(None),
    account_number: Optional[str] = Form(None),
    logo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Security auth missing")
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if vendor and vendor.is_banned:
        raise HTTPException(status_code=403, detail="Action prohibited. Vendor workspace suspended.")

    logo_data_url = None
    if logo and logo.filename:
        file_contents = await logo.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        logo_data_url = f"data:{logo.content_type};base64,{encoded}"

    clean_phone = "".join(c for c in phone_number if c.isdigit())
    if clean_phone.startswith("0") and len(clean_phone) == 11:
        clean_phone = "234" + clean_phone[1:]

    target_subaccount_id = vendor.subaccount_id if vendor else None
    
    if bank_code and account_number:
        if len(account_number) != 10 or not account_number.isdigit():
            raise HTTPException(status_code=400, detail="Invalid NUBAN Account Number. Must be exactly 10 digits.")
            
        if not vendor or vendor.bank_code != bank_code or vendor.account_number != account_number:
            created_id = await generate_flw_subaccount(bank_code, account_number, business_name, clean_phone, user['id'], db=db)
            if not created_id and not target_subaccount_id:
                raise HTTPException(status_code=400, detail="Failed to register split settlement parameters with Flutterwave.")
            if created_id:
                target_subaccount_id = created_id

    if vendor:
        vendor.business_name = business_name
        vendor.bio = bio
        vendor.phone_number = clean_phone
        if bank_code:
            vendor.bank_code = bank_code
            vendor.account_number = account_number
            vendor.subaccount_id = target_subaccount_id
        if logo_data_url:
            vendor.logo_url = logo_data_url
    else:
        if not bank_code or not account_number:
            raise HTTPException(status_code=400, detail="Bank settlement properties required for registration.")
            
        vendor = Vendor(
            vendor_id=user['id'], 
            business_name=business_name, 
            bio=bio, 
            phone_number=clean_phone, 
            logo_url=logo_data_url,
            bank_code=bank_code,
            account_number=account_number,
            subaccount_id=target_subaccount_id,
            is_banned=False
        )
        db.add(vendor)
        
    db.commit()
    return {"success": True}

@app.post("/api/vendor/upgrade-payout")
async def upgrade_legacy_vendor_payout(payload: PayoutUpgradePayload, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Validation trace rejected.")
        
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor account missing.")
    if vendor.is_banned:
        raise HTTPException(status_code=403, detail="Account is suspended.")
        
    if len(payload.account_number) != 10 or not payload.account_number.isdigit():
        raise HTTPException(status_code=400, detail="Invalid account formatting sequence.")
        
    created_id = await generate_flw_subaccount(payload.bank_code, payload.account_number, vendor.business_name, vendor.phone_number, vendor.vendor_id, db=db)
    if not created_id:
        raise HTTPException(status_code=400, detail="Upstream banking verification trace failed on split initialization.")
        
    vendor.bank_code = payload.bank_code
    vendor.account_number = payload.account_number
    vendor.subaccount_id = created_id
    db.commit()
    return {"success": True}

@app.post("/api/products")
async def create_new_product(
    request: Request, title: str = Form(...), price: float = Form(...), quantity: int = Form(...),
    sizes: str = Form(""), category: str = Form("General"), image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid request parameters")
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if vendor and vendor.is_banned:
        raise HTTPException(status_code=403, detail="Banned vendors cannot spawn inventory items.")

    image_data_url = None
    if image and image.filename:
        file_contents = await image.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        image_data_url = f"data:{image.content_type};base64,{encoded}"

    new_product = Product(
        vendor_id=user['id'], title=title, price=price, quantity=quantity, 
        sizes=sizes, category=category.strip(), image_url=image_data_url
    )
    db.add(new_product)
    db.commit()
    return {"success": True}

@app.post("/api/products/{product_id}/edit")
async def modify_product_entry(
    product_id: int, request: Request, title: str = Form(...), price: float = Form(...), quantity: int = Form(...),
    sizes: str = Form(""), category: str = Form("General"), image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Auth trace invalid")
        
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if vendor and vendor.is_banned:
        raise HTTPException(status_code=403, detail="Suspended profile modifications blocked.")

    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product missing")
        
    product.title = title
    product.price = price
    product.quantity = quantity
    product.sizes = sizes
    product.category = category.strip()
    
    if image and image.filename:
        file_contents = await image.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        product.image_url = f"data:{image.content_type};base64,{encoded}"
        
    db.commit()
    return {"success": True}

@app.delete("/api/products/{product_id}")
async def remove_catalog_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Validation failed")
        
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(status_code=404, detail="Target missing")
        
    db.delete(product)
    db.commit()
    return {"success": True}

@app.get("/api/vendor/products")
async def list_vendor_products(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    products = db.query(Product).filter(Product.vendor_id == user['id']).order_by(Product.id.desc()).all()
    return [{"id": p.id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]

@app.get("/api/marketplace/configs")
async def load_storefront_configuration(vendor_id: Optional[int] = None, db: Session = Depends(get_db)):
    if vendor_id:
        v = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        if not v or v.is_banned:
            return {"mode": "marketplace", "vendors": [], "products": []}
            
        products = db.query(Product).filter(Product.vendor_id == vendor_id).order_by(Product.id.desc()).all()
        return {
            "mode": "store",
            "vendor": {"name": v.business_name, "bio": v.bio, "logo": v.logo_url, "id": v.vendor_id},
            "products": [{"id": p.id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]
        }
    
    # Marketplace View: Omit Banned Vendor Nodes
    active_vendors = db.query(Vendor).filter(Vendor.is_banned == False).all()
    active_vendor_ids = [v.vendor_id for v in active_vendors]
    
    products = db.query(Product).filter(Product.vendor_id.in_(active_vendor_ids)).order_by(Product.id.desc()).limit(150).all()
    return {
        "mode": "marketplace",
        "vendors": [{"id": ven.vendor_id, "name": ven.business_name, "logo": ven.logo_url} for ven in active_vendors],
        "products": [{"id": p.id, "vendor_id": p.vendor_id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]
    }

@app.post("/api/checkout")
async def run_checkout_pipeline(req: CheckoutRequest, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Session expired")
    if not req.items:
        raise HTTPException(status_code=400, detail="Cart manifest is empty")

    first_id = req.items[0].product_id
    sample = db.query(Product).filter(Product.id == first_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="Product not listed")
    
    target_vendor_id = sample.vendor_id
    vendor_profile = db.query(Vendor).filter(Vendor.vendor_id == target_vendor_id).first()
    if vendor_profile and vendor_profile.is_banned:
        raise HTTPException(status_code=400, detail="Checkout aborted: Merchant shop is inactive.")
        
    vendor_phone = vendor_profile.phone_number if vendor_profile else "2348000000000"

    calculated_total = 0.0
    items_summary = []

    for item in req.items:
        prod = db.query(Product).filter(Product.id == item.product_id).first()
        if not prod or prod.quantity < item.quantity:
            raise HTTPException(status_code=400, detail=f"Stock limit exceeded for {prod.title if prod else 'Item'}")
        
        calculated_total += (prod.price * item.quantity)
        items_summary.append({"product_id": prod.id, "title": prod.title, "price": prod.price, "quantity": item.quantity, "size": item.size})

    timestamp = int(datetime.now(timezone.utc).timestamp())
    generated_code = f"BH-{target_vendor_id}-{user['id']}-{timestamp}"

    new_order = Order(
        vendor_id=target_vendor_id, customer_id=user['id'], customer_name=req.customer_name,
        customer_phone=req.customer_phone, delivery_address=req.delivery_address,
        items=json.dumps(items_summary), total_amount=calculated_total, order_code=generated_code, status="pending"
    )
    db.add(new_order)
    db.commit()

    return {
        "success": True, 
        "order_code": generated_code, 
        "total_amount": calculated_total, 
        "vendor_phone": vendor_phone,
        "subaccount_id": vendor_profile.subaccount_id if vendor_profile else None
    }

@app.post("/api/v1/payments/webhook")
async def flutterwave_payment_webhook(request: Request, db: Session = Depends(get_db)):
    flw_signature = request.headers.get("verif-hash")
    if not flw_signature or flw_signature != FLW_SECRET_HASH:
        raise HTTPException(status_code=401, detail="Webhook signature check verification trace failed.")

    payload = await request.json()
    if payload.get("status") == "successful" or payload.get("data", {}).get("status") == "successful":
        tx_data = payload.get("data", {})
        target_order_code = tx_data.get("tx_ref")
        
        order = db.query(Order).filter(Order.order_code == target_order_code).first()
        if not order:
            return {"status": "ignored", "reason": "Order missing."}
            
        if order.status == "pending":
            try:
                loaded_items = json.loads(order.items)
                for item in loaded_items:
                    prod = db.query(Product).filter(Product.id == item["product_id"]).first()
                    if prod:
                        prod.quantity = max(0, prod.quantity - item["quantity"])
                
                order.status = "confirmed"
                db.commit()
                
                alert_message = (
                    f"💳 *PAYMENT SECURED & VERIFIED!*\n\n"
                    f"🛍 *Order Code:* `{order.order_code}`\n"
                    f"💰 *Total Value:* ₦{order.total_amount:,.2f}\n"
                    f"👤 *Customer:* {order.customer_name}\n\n"
                    f"Verification successfully completed via Flutterwave engine layers."
                )
                await send_telegram_alert(order.vendor_id, alert_message)
                
                buyer_alert = f"✅ *YOUR PAYMENT WAS CONFIRMED!*\n\nCode: `{order.order_code}`\nStatus: *CONFIRMED*"
                await send_telegram_alert(order.customer_id, buyer_alert)
                
            except Exception as e:
                db.rollback()
                print(f"Error inside processing pipeline context: {e}")
                raise HTTPException(status_code=500, detail="Internal transactional sync error.")
                
    return {"status": "processed"}

@app.get("/api/customer/orders")
async def view_customer_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")

    orders = db.query(Order).filter(Order.customer_id == user['id']).order_by(Order.id.desc()).all()
    return [{"id": o.id, "order_code": o.order_code, "total_amount": o.total_amount, "status": o.status, "items": json.loads(o.items)} for o in orders]

@app.post("/api/customer/orders/{order_id}/cancel")
async def user_cancel_pending_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid context")
        
    order = db.query(Order).filter(Order.id == order_id, Order.customer_id == user['id']).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order missing")
    if order.status != "pending":
        raise HTTPException(status_code=400, detail="Cannot cancel confirmed order")
        
    order.status = "cancelled"
    db.commit()
    
    cancel_alert = f"⚠️ *ORDER CANCELLED BY CUSTOMER*\n\nOrder Code: `{order.order_code}`"
    await send_telegram_alert(order.vendor_id, cancel_alert)
    return {"success": True}

@app.get("/api/vendor/orders")
async def view_incoming_vendor_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")

    orders = db.query(Order).filter(Order.vendor_id == user['id']).order_by(Order.id.desc()).all()
    return [{"id": o.id, "order_code": o.order_code, "customer_name": o.customer_name, "customer_phone": o.customer_phone, "delivery_address": o.delivery_address, "total_amount": o.total_amount, "status": o.status, "items": json.loads(o.items)} for o in orders]

@app.post("/api/orders/{order_id}/status")
async def adjust_order_lifecycle(order_id: int, status_payload: StatusPayload, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Credentials missing")

    order = db.query(Order).filter(Order.id == order_id, Order.vendor_id == user['id']).first()
    if not order:
         raise HTTPException(404, "Order not found")

    next_status = status_payload.status
    order.status = next_status
    db.commit()
    
    status_emoji = "✅" if next_status == "confirmed" else "🚚" if next_status == "delivered" else "❌"
    user_alert = f"{status_emoji} *YOUR ORDER HAS BEEN UPDATED!*\n\nCode: `{order.order_code}`\nNew Status: *{next_status.upper()}*"
    await send_telegram_alert(order.customer_id, user_alert)
    return {"success": True}
