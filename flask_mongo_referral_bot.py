import asyncio
import os
import re
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, jsonify, request
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UTC = timezone.utc


def load_env_file(filename: Str = ".env") -> None:
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def clean_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def first_valid_env(*names: str, default: str = "") -> str:
    bad_parts = ("{", "}", "your_", "SHU_YERGA", "<", ">")
    for name in names:
        value = clean_env(name)
        if value and not any(part in value for part in bad_parts):
            return value
    return default


def render_base_url() -> str:
    url = first_valid_env("WEBHOOK_URL", "PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL")
    if url:
        return url.rstrip("/")
    host = first_valid_env("RENDER_EXTERNAL_HOSTNAME")
    return f"https://{host}" if host else ""


def masked(value: str) -> str:
    if not value:
        return "yo'q"
    return "***" if len(value) <= 12 else f"{value[:6]}...{value[-4:]}"


load_env_file()
BOT_TOKEN = first_valid_env("BOT_TOKEN", "8831278254:AAHdL4in2whlp76ZOGkw0tNimW5XeCQQOyc", "TOKEN")
OWNER_ID = int(clean_env("OWNER_ID", "6968399046") or "6968399046")
ADMIN_IDS_TEXT = clean_env("ADMIN_IDS", str(OWNER_ID))
MONGO_URI = first_valid_env("MONGO_URI", "mongodb+srv://tojiyevjavohir67_db_user:<qeJ3nSLEMc1LH1jf>@cluster0.pysrg0q.mongodb.net/?appName=Cluster0")
MONGO_DB = first_valid_env("MONGODB_DB", "MONGO_DB", default="referral_coin_bot")
WEBHOOK_URL = render_base_url()
WEBHOOK_SECRET = clean_env("WEBHOOK_SECRET", "referral-secret") or "referral-secret"
PORT = int(clean_env("PORT", "5000") or "5000")
REFERRAL_REWARD = int(clean_env("REFERRAL_REWARD", "1000") or "1000")
ADMIN_IDS = {OWNER_ID, *{int(x.strip()) for x in ADMIN_IDS_TEXT.split(",") if x.strip().isdigit()}}

print("ENV check:", f"BOT_TOKEN={masked(BOT_TOKEN)}", f"MONGO_URI={masked(MONGO_URI)}", f"WEBHOOK_URL={WEBHOOK_URL or 'yoq'}", file=sys.stderr)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan. Render Environment Variables joyida KEY=BOT_TOKEN qilib kiriting.")
if not MONGO_URI:
    raise RuntimeError("MONGODB_URI kiritilmagan. Render Environment Variables joyida KEY=MONGODB_URI qilib kiriting.")

mongo = MongoClient(MONGO_URI)
db = mongo[MONGO_DB]
users = db.users
referrals = db.referrals
requirements = db.requirements
withdrawals = db.withdrawals
admins = db.admins
promo_codes = db.promo_codes
promo_redemptions = db.promo_redemptions
coin_transfers = db.coin_transfers
broadcasts = db.broadcasts

flask_app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_loop = asyncio.new_event_loop()
telegram_started = False
telegram_start_lock = threading.Lock()
telegram_loop_thread: threading.Thread | None = None
webhook_configured = False
webhook_lock = threading.Lock()

LANGS = ("uz", "ru", "en")
LANG_NAMES = {"uz": "O'zbek", "ru": "Русский", "en": "English"}
TR = {
    "uz": {
        "choose_lang": "Tilni tanlang:", "lang_saved": "Til saqlandi.", "welcome": "Assalomu alaykum! Kerakli bo'limni tanlang.",
        "get_coin": "Yashil - Tekin coin olish", "account": "Ko'k - Mening hisobim", "withdraw": "Qizil - Coinni yechish", "promo": "Binafsha - Promo kod", "top": "Ko'k - Top referallar", "language": "Sariq - Til almashtirish", "admin_panel": "To'q sariq - Admin panel",
        "ref_link": "Sizning referal linkingiz:\n{link}\n\nHar bir tasdiqlangan do'st uchun {reward} coin beriladi.",
        "account_text": "ID: {id}\nCoin: {coins}\nReferallar: {refs}\nYechilgan: {withdrawn}",
        "withdraw_text": "Qaysi mukofotni yechmoqchisiz?", "not_enough": "Coin yetarli emas. Kerak: {price}, sizda: {coins}", "withdraw_ok": "So'rov qabul qilindi. Admin tez orada ko'rib chiqadi.",
        "sub_required": "Botdan foydalanish uchun quyidagilarga obuna bo'ling, keyin tekshiring.", "check_sub": "Obunani tekshirish", "sub_ok": "Obuna tasdiqlandi.", "sub_bad": "Hali hammasiga obuna bo'lmagansiz.",
        "promo_ask": "Promo kodni yuboring:", "promo_bad": "Promo kod xato yoki muddati tugagan.", "promo_used": "Siz bu promo koddan oldin foydalangansiz.", "promo_ok": "Promo kod qabul qilindi! Hisobingizga {coins} coin qo'shildi.",
        "admin_only": "Bu bo'lim faqat admin uchun.", "owner_only": "Bu amal faqat bot egasi uchun.", "admin_title": "Admin panel", "stats": "Statistika\nFoydalanuvchilar: {users}\nJami coin: {coins}\nMajburiy obuna: {reqs}\nPromo kodlar: {promos}",
        "add_req": "Majburiy obuna qo'shish", "remove_req": "Majburiy obunani o'chirish", "list_req": "Obunalar ro'yxati", "broadcast": "Hammaga xabar", "add_admin": "Admin qo'shish", "remove_admin": "Admin o'chirish", "add_coin": "Coin tashlash", "add_promo": "Promo kod qo'shish", "list_promo": "Promo kodlar",
        "cancel": "Bekor qilish", "cancelled": "Bekor qilindi.", "send_user": "Foydalanuvchi ID yoki @username yuboring:", "send_amount": "Coin miqdorini yuboring:", "coin_sent": "{user} foydalanuvchiga {coins} coin yuborildi.", "coin_received": "Admin hisobingizga {coins} coin qo'shdi.",
        "promo_code_step": "Promo kod nomini yuboring. Masalan: BONUS100", "promo_coins_step": "Bu promo kod nechta coin bersin?", "promo_hours_step": "Promo kod muddati necha soat bo'lsin? 0 yuborsangiz muddatsiz bo'ladi.", "promo_limit_step": "Nechta odam ishlata olsin?", "promo_created": "Promo kod yaratildi: {code}\nCoin: {coins}\nMuddat: {expires}\nLimit: {limit}",
        "not_found": "Topilmadi.", "bad_number": "Musbat son yuboring.", "send_broadcast": "Hammaga yuboriladigan xabarni yuboring:", "broadcast_done": "Xabar yuborildi. Yetdi: {sent}, xato: {failed}", "req_type": "Majburiy obuna turini tanlang:", "req_title": "Nomini yuboring:", "req_target": "Kanal/chat username yoki ID yuboring. Instagram/bot uchun link yuboring:", "req_added": "Majburiy obuna qo'shildi.", "empty": "Bo'sh.", "ok": "OK", "top_title": "Top referallar", "top_empty": "Hozircha referal yo'q.", "top_7": "1 haftalik top", "top_20": "20 kunlik top", "top_365": "1 yillik top"
    },
    "ru": {
        "choose_lang": "Выберите язык:", "lang_saved": "Язык сохранен.", "welcome": "Здравствуйте! Выберите раздел.",
        "get_coin": "Зеленая - Получить монеты", "account": "Синяя - Мой счет", "withdraw": "Красная - Вывести монеты", "promo": "Фиолетовая - Промокод", "top": "Синяя - Топ рефералов", "language": "Желтая - Сменить язык", "admin_panel": "Оранжевая - Админ панель",
        "ref_link": "Ваша реферальная ссылка:\n{link}\n\nЗа каждого подтвержденного друга вы получите {reward} монет.",
        "account_text": "ID: {id}\nМонеты: {coins}\nРефералы: {refs}\nВыведено: {withdrawn}",
        "withdraw_text": "Какую награду вывести?", "not_enough": "Недостаточно монет. Нужно: {price}, у вас: {coins}", "withdraw_ok": "Заявка принята. Админ скоро проверит.",
        "sub_required": "Чтобы пользоваться ботом, подпишитесь и нажмите проверку.", "check_sub": "Проверить подписку", "sub_ok": "Подписка подтверждена.", "sub_bad": "Вы еще не подписались на все.",
        "promo_ask": "Отправьте промокод:", "promo_bad": "Промокод неверный или истек.", "promo_used": "Вы уже использовали этот промокод.", "promo_ok": "Промокод принят! Вам добавлено {coins} монет.",
        "admin_only": "Только для админа.", "owner_only": "Только владелец бота может это сделать.", "admin_title": "Админ панель", "stats": "Статистика\nПользователи: {users}\nВсего монет: {coins}\nОбязательные подписки: {reqs}\nПромокоды: {promos}",
        "add_req": "Добавить подписку", "remove_req": "Удалить подписку", "list_req": "Список подписок", "broadcast": "Рассылка", "add_admin": "Добавить админа", "remove_admin": "Удалить админа", "add_coin": "Отправить монеты", "add_promo": "Добавить промокод", "list_promo": "Промокоды",
        "cancel": "Отмена", "cancelled": "Отменено.", "send_user": "Отправьте ID пользователя или @username:", "send_amount": "Отправьте количество монет:", "coin_sent": "Пользователю {user} отправлено {coins} монет.", "coin_received": "Админ добавил вам {coins} монет.",
        "promo_code_step": "Отправьте код. Например: BONUS100", "promo_coins_step": "Сколько монет дает промокод?", "promo_hours_step": "Срок промокода в часах? 0 - без срока.", "promo_limit_step": "Сколько людей могут использовать?", "promo_created": "Промокод создан: {code}\nМонеты: {coins}\nСрок: {expires}\nЛимит: {limit}",
        "not_found": "Не найдено.", "bad_number": "Отправьте положительное число.", "send_broadcast": "Отправьте сообщение для рассылки:", "broadcast_done": "Рассылка готова. Успешно: {sent}, ошибок: {failed}", "req_type": "Выберите тип подписки:", "req_title": "Отправьте название:", "req_target": "Отправьте username/ID канала или ссылку для Instagram/бота:", "req_added": "Подписка добавлена.", "empty": "Пусто.", "ok": "OK", "top_title": "Топ рефералов", "top_empty": "Пока нет рефералов.", "top_7": "Топ за неделю", "top_20": "Топ за 20 дней", "top_365": "Топ за год"
    },
    "en": {
        "choose_lang": "Choose language:", "lang_saved": "Language saved.", "welcome": "Hello! Choose a section.",
        "get_coin": "Green - Get free coins", "account": "Blue - My account", "withdraw": "Red - Withdraw coins", "promo": "Purple - Promo code", "top": "Blue - Top referrals", "language": "Yellow - Change language", "admin_panel": "Orange - Admin panel",
        "ref_link": "Your referral link:\n{link}\n\nYou get {reward} coins for every confirmed friend.",
        "account_text": "ID: {id}\nCoins: {coins}\nReferrals: {refs}\nWithdrawn: {withdrawn}",
        "withdraw_text": "Which reward do you want to withdraw?", "not_enough": "Not enough coins. Need: {price}, you have: {coins}", "withdraw_ok": "Request accepted. Admin will review it soon.",
        "sub_required": "Subscribe to the required pages, then check again.", "check_sub": "Check subscription", "sub_ok": "Subscription confirmed.", "sub_bad": "You are not subscribed to everything yet.",
        "promo_ask": "Send promo code:", "promo_bad": "Promo code is wrong or expired.", "promo_used": "You have already used this promo code.", "promo_ok": "Promo accepted! {coins} coins added.",
        "admin_only": "Admin only.", "owner_only": "Bot owner only.", "admin_title": "Admin panel", "stats": "Statistics\nUsers: {users}\nTotal coins: {coins}\nRequired subscriptions: {reqs}\nPromo codes: {promos}",
        "add_req": "Add required subscription", "remove_req": "Remove required subscription", "list_req": "Subscription list", "broadcast": "Broadcast", "add_admin": "Add admin", "remove_admin": "Remove admin", "add_coin": "Send coins", "add_promo": "Add promo code", "list_promo": "Promo codes",
        "cancel": "Cancel", "cancelled": "Cancelled.", "send_user": "Send user ID or @username:", "send_amount": "Send coin amount:", "coin_sent": "Sent {coins} coins to {user}.", "coin_received": "Admin added {coins} coins to your account.",
        "promo_code_step": "Send promo code. Example: BONUS100", "promo_coins_step": "How many coins should it give?", "promo_hours_step": "How many hours should it work? Send 0 for no expiry.", "promo_limit_step": "How many people can use it?", "promo_created": "Promo code created: {code}\nCoins: {coins}\nExpires: {expires}\nLimit: {limit}",
        "not_found": "Not found.", "bad_number": "Send a positive number.", "send_broadcast": "Send broadcast message:", "broadcast_done": "Broadcast done. Sent: {sent}, failed: {failed}", "req_type": "Choose subscription type:", "req_title": "Send title:", "req_target": "Send channel/chat username or ID. For Instagram/bot send link:", "req_added": "Required subscription added.", "empty": "Empty.", "ok": "OK", "top_title": "Top referrals", "top_empty": "No referrals yet.", "top_7": "Weekly top", "top_20": "20-day top", "top_365": "Yearly top"
    },
}
WITHDRAW_ITEMS = [("king", "King", 5000), ("hp300", "300 HP", 4000), ("game_money", "O'yin puli", 6000), ("coin30000", "30.000 coin", 10000), ("chrome", "Xrom", 10000)]
REQ_TYPES = [("channel", "Telegram kanal"), ("chat", "Telegram chat"), ("request", "Zayafka kanal"), ("instagram", "Instagram"), ("bot", "Telegram bot")]


def now() -> datetime:
    return datetime.now(UTC)


def t(lang: str, key: str, **kwargs: Any) -> str:
    text = TR.get(lang, TR["uz"]).get(key, TR["uz"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def get_lang(user_id: int | None) -> str:
    if not user_id:
        return "uz"
    doc = users.find_one({"_id": user_id}, {"lang": 1})
    lang = doc.get("lang") if doc else "uz"
    return lang if lang in LANGS else "uz"


def button_texts(key: str) -> set[str]:
    return {TR[lang][key] for lang in LANGS}


def setup_indexes() -> None:
    users.create_index([("_id", ASCENDING)])
    users.create_index([("username", ASCENDING)])
    users.create_index([("referral_count", DESCENDING)])
    referrals.create_index([("referred_id", ASCENDING)], unique=True)
    requirements.create_index([("active", ASCENDING), ("created_at", ASCENDING)])
    admins.create_index([("_id", ASCENDING)])
    promo_codes.create_index([("code", ASCENDING)], unique=True)
    promo_redemptions.create_index([("code", ASCENDING), ("user_id", ASCENDING)], unique=True)


def bootstrap_owner() -> None:
    admins.update_one({"_id": OWNER_ID}, {"$set": {"role": "owner", "updated_at": now()}, "$setOnInsert": {"created_at": now()}}, upsert=True)
    for admin_id in ADMIN_IDS:
        admins.update_one({"_id": admin_id}, {"$setOnInsert": {"role": "admin", "created_at": now()}}, upsert=True)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID or (admins.find_one({"_id": user_id}) or {}).get("role") == "owner"


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or admins.find_one({"_id": user_id}) is not None


def main_menu(lang: str, admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(t(lang, "get_coin")), KeyboardButton(t(lang, "account"))], [KeyboardButton(t(lang, "withdraw")), KeyboardButton(t(lang, "promo"))], [KeyboardButton(t(lang, "top")), KeyboardButton(t(lang, "language"))]]
    if admin:
        rows[-1].append(KeyboardButton(t(lang, "admin_panel")))
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"lang:{code}")] for code, name in LANG_NAMES.items()])


def admin_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "stats").split("\n", 1)[0], callback_data="admin:stats"), InlineKeyboardButton(t(lang, "add_req"), callback_data="admin:add_req")],
        [InlineKeyboardButton(t(lang, "remove_req"), callback_data="admin:remove_req"), InlineKeyboardButton(t(lang, "list_req"), callback_data="admin:list_req")],
        [InlineKeyboardButton(t(lang, "add_coin"), callback_data="admin:add_coin"), InlineKeyboardButton(t(lang, "add_promo"), callback_data="admin:add_promo")],
        [InlineKeyboardButton(t(lang, "list_promo"), callback_data="admin:list_promo"), InlineKeyboardButton(t(lang, "broadcast"), callback_data="admin:broadcast")],
        [InlineKeyboardButton(t(lang, "add_admin"), callback_data="admin:add_admin"), InlineKeyboardButton(t(lang, "remove_admin"), callback_data="admin:remove_admin")],
    ])


def subscription_keyboard(lang: str, missing: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for req in missing:
        title = req.get("title") or req.get("target") or "Link"
        url = req.get("url") or req.get("target")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            rows.append([InlineKeyboardButton(f"{title}", url=url)])
        else:
            rows.append([InlineKeyboardButton(f"{title}", callback_data="noop")])
    rows.append([InlineKeyboardButton(t(lang, "check_sub"), callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


def withdraw_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{label} - {price} coin", callback_data=f"withdraw:{code}")] for code, label, price in WITHDRAW_ITEMS])


def req_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"reqtype:{code}")] for code, label in REQ_TYPES])


def remove_req_keyboard(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"O'chirish: {doc.get('title', doc.get('target'))}", callback_data=f"reqdel:{doc['_id']}")] for doc in requirements.find({"active": True}).sort("created_at", ASCENDING)]
    return InlineKeyboardMarkup(rows or [[InlineKeyboardButton(t(lang, "empty"), callback_data="noop")]])


def top_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "top_7"), callback_data="top:7")],
        [InlineKeyboardButton(t(lang, "top_20"), callback_data="top:20")],
        [InlineKeyboardButton(t(lang, "top_365"), callback_data="top:365")],
    ])


def upsert_user(tg_user, referrer_id: int | None = None) -> dict[str, Any]:
    existing = users.find_one({"_id": tg_user.id})
    base = {"username": tg_user.username, "first_name": tg_user.first_name, "last_name": tg_user.last_name, "updated_at": now()}
    if existing:
        users.update_one({"_id": tg_user.id}, {"$set": base})
        return users.find_one({"_id": tg_user.id})
    pending = referrer_id if referrer_id and referrer_id != tg_user.id and users.find_one({"_id": referrer_id}) else None
    doc = {"_id": tg_user.id, **base, "coins": 0, "withdrawn_coins": 0, "referral_count": 0, "pending_referrer_id": pending, "referrer_id": None, "is_referral_counted": False, "lang": "uz", "state": None, "created_at": now()}
    users.insert_one(doc)
    return doc


def parse_user_ref(text: str) -> dict[str, Any] | None:
    value = text.strip()
    if value.startswith("@"):
        return users.find_one({"username": {"$regex": f"^{re.escape(value[1:])}$", "$options": "i"}})
    if value.isdigit():
        return users.find_one({"_id": int(value)})
    return users.find_one({"username": {"$regex": f"^{re.escape(value)}$", "$options": "i"}})


def user_title(user: dict[str, Any] | None) -> str:
    if not user:
        return "unknown"
    if user.get("username"):
        return "@" + user["username"]
    name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part).strip()
    return name or str(user.get("_id"))


def positive_int(text: str) -> int | None:
    try:
        number = int(text.strip())
        return number if number > 0 else None
    except ValueError:
        return None


def promo_code(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).upper()


def is_promo_expired(doc: dict[str, Any]) -> bool:
    expires_at = doc.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now()


def normalize_requirement_target(req_type: str, value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("https://t.me/"):
        username = value.rsplit("/", 1)[-1].strip()
        return f"@{username}" if username and not username.startswith("+") else value, value
    if req_type in {"channel", "chat", "request"}:
        if value.startswith("@") or value.startswith("-100") or value.startswith("-"):
            target = value
        else:
            target = f"@{value}"
        url = value if value.startswith("http") else (f"https://t.me/{target.lstrip('@')}" if not target.startswith("-") else "")
        return target, url
    if req_type == "bot":
        url = value if value.startswith("http") else f"https://t.me/{value.lstrip('@')}"
        return value, url
    return value, value if value.startswith("http") else ""


async def user_is_member(user_id: int, req: dict[str, Any]) -> bool:
    req_type = req.get("type")
    target = str(req.get("target", "")).strip()
    if req_type in {"instagram", "bot"}:
        return True
    try:
        member = await telegram_app.bot.get_chat_member(target, user_id)
        return member.status in {"creator", "administrator", "member", "restricted"}
    except (BadRequest, Forbidden, TelegramError):
        return False


async def missing_requirements(user_id: int) -> list[dict[str, Any]]:
    missing = []
    for req in requirements.find({"active": True}).sort("created_at", ASCENDING):
        if not await user_is_member(user_id, req):
            missing.append(req)
    return missing


async def count_referral_if_ready(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = users.find_one({"_id": user_id})
    if not user or user.get("is_referral_counted") or user.get("referrer_id"):
        return
    referrer_id = user.get("pending_referrer_id")
    if not referrer_id or referrer_id == user_id or not users.find_one({"_id": referrer_id}):
        return
    try:
        referrals.insert_one({"referrer_id": referrer_id, "referred_id": user_id, "created_at": now()})
    except DuplicateKeyError:
        users.update_one({"_id": user_id}, {"$set": {"is_referral_counted": True, "pending_referrer_id": None}})
        return
    users.update_one({"_id": user_id}, {"$set": {"is_referral_counted": True, "referrer_id": referrer_id, "pending_referrer_id": None}})
    users.update_one({"_id": referrer_id}, {"$inc": {"coins": REFERRAL_REWARD, "referral_count": 1}})


async def require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    lang = get_lang(user.id)
    missing = await missing_requirements(user.id)
    if not missing:
        await count_referral_if_ready(user.id, context)
        return True
    msg = update.callback_query.message if update.callback_query else update.effective_message
    await msg.reply_text(t(lang, "sub_required"), reply_markup=subscription_keyboard(lang, missing))
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    referrer_id = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    upsert_user(update.effective_user, referrer_id)
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))
    await require_subscription(update, context)


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(t(get_lang(update.effective_user.id), "choose_lang"), reply_markup=lang_keyboard())


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    code = code if code in LANGS else "uz"
    users.update_one({"_id": query.from_user.id}, {"$set": {"lang": code, "state": None}}, upsert=True)
    await query.message.reply_text(t(code, "lang_saved"), reply_markup=main_menu(code, is_admin(query.from_user.id)))


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = get_lang(query.from_user.id)
    missing = await missing_requirements(query.from_user.id)
    if missing:
        await query.message.reply_text(t(lang, "sub_bad"), reply_markup=subscription_keyboard(lang, missing))
        return
    await count_referral_if_ready(query.from_user.id, context)
    await query.message.reply_text(t(lang, "sub_ok"), reply_markup=main_menu(lang, is_admin(query.from_user.id)))


async def show_ref_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_subscription(update, context):
        return
    lang = get_lang(update.effective_user.id)
    bot_user = await context.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start={update.effective_user.id}"
    await update.message.reply_text(t(lang, "ref_link", link=link, reward=REFERRAL_REWARD), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_subscription(update, context):
        return
    lang = get_lang(update.effective_user.id)
    user = users.find_one({"_id": update.effective_user.id}) or {}
    await update.message.reply_text(t(lang, "account_text", id=update.effective_user.id, coins=user.get("coins", 0), refs=user.get("referral_count", 0), withdrawn=user.get("withdrawn_coins", 0)), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))


async def withdraw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_subscription(update, context):
        await update.message.reply_text(t(get_lang(update.effective_user.id), "withdraw_text"), reply_markup=withdraw_keyboard())


async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = get_lang(query.from_user.id)
    code = query.data.split(":", 1)[1]
    item = next((x for x in WITHDRAW_ITEMS if x[0] == code), None)
    if not item:
        return
    _, label, price = item
    user = users.find_one({"_id": query.from_user.id}) or {"coins": 0}
    if user.get("coins", 0) < price:
        await query.message.reply_text(t(lang, "not_enough", price=price, coins=user.get("coins", 0)))
        return
    users.update_one({"_id": query.from_user.id}, {"$inc": {"coins": -price, "withdrawn_coins": price}})
    withdrawals.insert_one({"user_id": query.from_user.id, "reward": label, "price": price, "status": "new", "created_at": now()})
    await query.message.reply_text(t(lang, "withdraw_ok"), reply_markup=main_menu(lang, is_admin(query.from_user.id)))


async def promo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_subscription(update, context):
        users.update_one({"_id": update.effective_user.id}, {"$set": {"state": "promo_redeem"}})
        await update.message.reply_text(t(get_lang(update.effective_user.id), "promo_ask"))


async def top_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_subscription(update, context):
        lang = get_lang(update.effective_user.id)
        await update.message.reply_text(t(lang, "top_title"), reply_markup=top_keyboard(lang))


def top_referrers_text(lang: str, days: int) -> str:
    since = now() - timedelta(days=days)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$referrer_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    rows = list(referrals.aggregate(pipeline))
    title = t(lang, "top_title")
    if not rows:
        return f"{title}\n\n{t(lang, 'top_empty')}"
    lines = [title, ""]
    for index, row in enumerate(rows, 1):
        user = users.find_one({"_id": row["_id"]})
        lines.append(f"{index}. {user_title(user)} - {row['count']}")
    return "\n".join(lines)


async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = get_lang(query.from_user.id)
    days = int(query.data.split(":", 1)[1])
    await query.message.reply_text(top_referrers_text(lang, days))


async def redeem_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_user.id)
    code = promo_code(update.message.text)
    if promo_redemptions.find_one({"code": code, "user_id": update.effective_user.id}):
        users.update_one({"_id": update.effective_user.id}, {"$set": {"state": None}})
        await update.message.reply_text(t(lang, "promo_used"), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))
        return
    doc = promo_codes.find_one({"code": code, "active": True})
    if not doc or is_promo_expired(doc) or int(doc.get("used", 0)) >= int(doc.get("limit", 0)):
        users.update_one({"_id": update.effective_user.id}, {"$set": {"state": None}})
        await update.message.reply_text(t(lang, "promo_bad"), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))
        return
    try:
        updated = promo_codes.find_one_and_update({"code": code, "active": True, "used": {"$lt": int(doc.get("limit", 0))}}, {"$inc": {"used": 1}}, return_document=ReturnDocument.AFTER)
        if not updated or is_promo_expired(updated):
            raise ValueError("expired")
        promo_redemptions.insert_one({"code": code, "user_id": update.effective_user.id, "coins": int(updated["coins"]), "created_at": now()})
        users.update_one({"_id": update.effective_user.id}, {"$inc": {"coins": int(updated["coins"])}, "$set": {"state": None}})
    except (DuplicateKeyError, ValueError, PyMongoError):
        await update.message.reply_text(t(lang, "promo_bad"), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))
        return
    await update.message.reply_text(t(lang, "promo_ok", coins=int(doc["coins"])), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_user.id)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(t(lang, "admin_only"))
        return
    await update.message.reply_text(t(lang, "admin_title"), reply_markup=admin_keyboard(lang))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = get_lang(user_id)
    if not is_admin(user_id):
        await query.message.reply_text(t(lang, "admin_only"))
        return
    action = query.data.split(":", 1)[1]
    if action == "stats":
        total_users = users.count_documents({})
        total_coins = next((x.get("total", 0) for x in users.aggregate([{"$group": {"_id": None, "total": {"$sum": "$coins"}}}])), 0)
        await query.message.reply_text(t(lang, "stats", users=total_users, coins=total_coins, reqs=requirements.count_documents({"active": True}), promos=promo_codes.count_documents({"active": True})))
    elif action == "add_req":
        if not is_owner(user_id):
            await query.message.reply_text(t(lang, "owner_only"))
            return
        users.update_one({"_id": user_id}, {"$set": {"state": "req_type"}})
        await query.message.reply_text(t(lang, "req_type"), reply_markup=req_type_keyboard())
    elif action == "remove_req":
        if not is_owner(user_id):
            await query.message.reply_text(t(lang, "owner_only"))
            return
        await query.message.reply_text(t(lang, "remove_req"), reply_markup=remove_req_keyboard(lang))
    elif action == "list_req":
        lines = [f"{i}. {d.get('title')} - {d.get('type')} - {d.get('target')}" for i, d in enumerate(requirements.find({"active": True}).sort("created_at", ASCENDING), 1)]
        await query.message.reply_text("\n".join(lines) if lines else t(lang, "empty"))
    elif action == "add_coin":
        users.update_one({"_id": user_id}, {"$set": {"state": "coin_user"}})
        await query.message.reply_text(t(lang, "send_user"))
    elif action == "add_promo":
        users.update_one({"_id": user_id}, {"$set": {"state": "promo_code"}, "$unset": {"draft_promo": ""}})
        await query.message.reply_text(t(lang, "promo_code_step"))
    elif action == "list_promo":
        lines = []
        for i, p in enumerate(promo_codes.find({"active": True}).sort("created_at", DESCENDING).limit(20), 1):
            exp = p.get("expires_at") or "no limit"
            lines.append(f"{i}. {p['code']} | {p['coins']} coin | {p.get('used', 0)}/{p.get('limit', 0)} | {exp}")
        await query.message.reply_text("\n".join(lines) if lines else t(lang, "empty"))
    elif action == "broadcast":
        users.update_one({"_id": user_id}, {"$set": {"state": "broadcast"}})
        await query.message.reply_text(t(lang, "send_broadcast"))
    elif action in {"add_admin", "remove_admin"}:
        if not is_owner(user_id):
            await query.message.reply_text(t(lang, "owner_only"))
            return
        users.update_one({"_id": user_id}, {"$set": {"state": "admin_add" if action == "add_admin" else "admin_remove"}})
        await query.message.reply_text(t(lang, "send_user"))


async def req_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = get_lang(query.from_user.id)
    if not is_owner(query.from_user.id):
        await query.message.reply_text(t(lang, "owner_only"))
        return
    req_type = query.data.split(":", 1)[1]
    users.update_one({"_id": query.from_user.id}, {"$set": {"state": "req_title", "draft_req": {"type": req_type}}})
    await query.message.reply_text(t(lang, "req_title"))


async def req_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = get_lang(query.from_user.id)
    if not is_owner(query.from_user.id):
        await query.message.reply_text(t(lang, "owner_only"))
        return
    from bson import ObjectId
    try:
        requirements.update_one({"_id": ObjectId(query.data.split(":", 1)[1])}, {"$set": {"active": False, "updated_at": now()}})
        await query.message.reply_text(t(lang, "ok"))
    except Exception:
        await query.message.reply_text(t(lang, "not_found"))


async def admin_state_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = get_lang(user_id)
    user = users.find_one({"_id": user_id}) or {}
    state = user.get("state")
    text = update.message.text.strip()
    if state == "promo_redeem":
        await redeem_promo(update, context)
        return
    if not is_admin(user_id):
        return
    if state == "coin_user":
        target = parse_user_ref(text)
        if not target:
            await update.message.reply_text(t(lang, "not_found")); return
        users.update_one({"_id": user_id}, {"$set": {"state": "coin_amount", "draft_coin": {"user_id": target["_id"]}}})
        await update.message.reply_text(t(lang, "send_amount"))
    elif state == "coin_amount":
        amount = positive_int(text)
        if not amount:
            await update.message.reply_text(t(lang, "bad_number")); return
        target_id = int(user.get("draft_coin", {}).get("user_id"))
        users.update_one({"_id": target_id}, {"$inc": {"coins": amount}})
        coin_transfers.insert_one({"admin_id": user_id, "user_id": target_id, "coins": amount, "created_at": now()})
        users.update_one({"_id": user_id}, {"$set": {"state": None}, "$unset": {"draft_coin": ""}})
        await update.message.reply_text(t(lang, "coin_sent", user=target_id, coins=amount), reply_markup=main_menu(lang, True))
        try:
            await context.bot.send_message(target_id, t(get_lang(target_id), "coin_received", coins=amount))
        except TelegramError:
            pass
    elif state == "promo_code":
        code = promo_code(text)
        if not code:
            await update.message.reply_text(t(lang, "promo_code_step")); return
        users.update_one({"_id": user_id}, {"$set": {"state": "promo_coins", "draft_promo": {"code": code}}})
        await update.message.reply_text(t(lang, "promo_coins_step"))
    elif state == "promo_coins":
        amount = positive_int(text)
        if not amount:
            await update.message.reply_text(t(lang, "bad_number")); return
        draft = user.get("draft_promo", {}); draft["coins"] = amount
        users.update_one({"_id": user_id}, {"$set": {"state": "promo_hours", "draft_promo": draft}})
        await update.message.reply_text(t(lang, "promo_hours_step"))
    elif state == "promo_hours":
        try:
            hours = int(text)
            if hours < 0: raise ValueError
        except ValueError:
            await update.message.reply_text(t(lang, "bad_number")); return
        draft = user.get("draft_promo", {}); draft["hours"] = hours
        users.update_one({"_id": user_id}, {"$set": {"state": "promo_limit", "draft_promo": draft}})
        await update.message.reply_text(t(lang, "promo_limit_step"))
    elif state == "promo_limit":
        limit = positive_int(text)
        if not limit:
            await update.message.reply_text(t(lang, "bad_number")); return
        draft = user.get("draft_promo", {})
        expires_at = now() + timedelta(hours=int(draft.get("hours", 0))) if int(draft.get("hours", 0)) else None
        promo_codes.update_one({"code": draft["code"]}, {"$set": {"code": draft["code"], "coins": int(draft["coins"]), "limit": limit, "used": 0, "expires_at": expires_at, "active": True, "created_by": user_id, "updated_at": now()}, "$setOnInsert": {"created_at": now()}}, upsert=True)
        users.update_one({"_id": user_id}, {"$set": {"state": None}, "$unset": {"draft_promo": ""}})
        exp = expires_at.strftime("%Y-%m-%d %H:%M UTC") if expires_at else "no limit"
        await update.message.reply_text(t(lang, "promo_created", code=draft["code"], coins=draft["coins"], expires=exp, limit=limit), reply_markup=admin_keyboard(lang))
    elif state == "broadcast":
        sent = failed = 0
        for doc in users.find({}, {"_id": 1}):
            try:
                await context.bot.send_message(doc["_id"], text); sent += 1
            except TelegramError:
                failed += 1
        broadcasts.insert_one({"admin_id": user_id, "text": text, "sent": sent, "failed": failed, "created_at": now()})
        users.update_one({"_id": user_id}, {"$set": {"state": None}})
        await update.message.reply_text(t(lang, "broadcast_done", sent=sent, failed=failed), reply_markup=main_menu(lang, True))
    elif state in {"admin_add", "admin_remove"}:
        if not is_owner(user_id):
            await update.message.reply_text(t(lang, "owner_only")); return
        target = parse_user_ref(text)
        if not target:
            await update.message.reply_text(t(lang, "not_found")); return
        if state == "admin_add":
            admins.update_one({"_id": target["_id"]}, {"$set": {"role": "admin", "updated_at": now()}, "$setOnInsert": {"created_at": now()}}, upsert=True)
            await update.message.reply_text("Admin qo'shildi.")
        else:
            if int(target["_id"]) == OWNER_ID:
                await update.message.reply_text(t(lang, "owner_only")); return
            admins.delete_one({"_id": target["_id"]})
            await update.message.reply_text("Admin o'chirildi.")
        users.update_one({"_id": user_id}, {"$set": {"state": None}})
    elif state == "req_title":
        if not is_owner(user_id):
            await update.message.reply_text(t(lang, "owner_only")); return
        draft = user.get("draft_req", {}); draft["title"] = text
        users.update_one({"_id": user_id}, {"$set": {"state": "req_target", "draft_req": draft}})
        await update.message.reply_text(t(lang, "req_target"))
    elif state == "req_target":
        if not is_owner(user_id):
            await update.message.reply_text(t(lang, "owner_only")); return
        draft = user.get("draft_req", {})
        req_type = draft.get("type", "channel")
        target, url = normalize_requirement_target(req_type, text)
        requirements.insert_one({"type": req_type, "title": draft.get("title", target), "target": target, "url": url, "active": True, "created_by": user_id, "created_at": now()})
        users.update_one({"_id": user_id}, {"$set": {"state": None}, "$unset": {"draft_req": ""}})
        await update.message.reply_text(t(lang, "req_added"), reply_markup=admin_keyboard(lang))


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text in button_texts("get_coin"):
        await show_ref_link(update, context)
    elif text in button_texts("account"):
        await show_account(update, context)
    elif text in button_texts("withdraw"):
        await withdraw_menu(update, context)
    elif text in button_texts("promo"):
        await promo_menu(update, context)
    elif text in button_texts("top"):
        await top_menu(update, context)
    elif text in button_texts("language"):
        await language_cmd(update, context)
    elif text in button_texts("admin_panel"):
        await admin_panel(update, context)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "welcome"), reply_markup=main_menu(lang, is_admin(update.effective_user.id)))


def register_handlers() -> None:
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("language", language_cmd))
    telegram_app.add_handler(CallbackQueryHandler(language_callback, pattern=r"^lang:"))
    telegram_app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub$"))
    telegram_app.add_handler(CallbackQueryHandler(top_callback, pattern=r"^top:"))
    telegram_app.add_handler(CallbackQueryHandler(withdraw_callback, pattern=r"^withdraw:"))
    telegram_app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    telegram_app.add_handler(CallbackQueryHandler(req_type_callback, pattern=r"^reqtype:"))
    telegram_app.add_handler(CallbackQueryHandler(req_delete_callback, pattern=r"^reqdel:"))
    telegram_app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_state_message), group=1)
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler), group=2)


def start_loop() -> None:
    asyncio.set_event_loop(telegram_loop)
    telegram_loop.run_forever()


def ensure_loop_running() -> None:
    global telegram_loop_thread
    if telegram_loop.is_running():
        return
    if telegram_loop_thread and telegram_loop_thread.is_alive():
        return
    telegram_loop_thread = threading.Thread(target=start_loop, daemon=True)
    telegram_loop_thread.start()


def run_coroutine(coro, timeout: int = 30):
    ensure_loop_running()
    future = asyncio.run_coroutine_threadsafe(coro, telegram_loop)
    return future.result(timeout=timeout)


async def initialize_telegram() -> None:
    global telegram_started
    if telegram_started:
        return
    await telegram_app.initialize()
    await telegram_app.start()
    telegram_started = True


def ensure_telegram_started() -> None:
    with telegram_start_lock:
        if not telegram_started:
            run_coroutine(initialize_telegram())


async def configure_webhook(url: str) -> None:
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    await telegram_app.bot.set_webhook(f"{url.rstrip('/')}/webhook/{WEBHOOK_SECRET}", allowed_updates=Update.ALL_TYPES)


def configure_webhook_once(url: str | None = None) -> None:
    global webhook_configured
    public_url = (url or WEBHOOK_URL or "").rstrip("/")
    if not public_url:
        return
    with webhook_lock:
        if webhook_configured:
            return
        ensure_telegram_started()
        run_coroutine(configure_webhook(public_url), timeout=45)
        webhook_configured = True
        print(f"Webhook configured: {public_url}/webhook/{WEBHOOK_SECRET}", file=sys.stderr)


async def process_update_json(data: dict[str, Any]) -> None:
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)


@flask_app.get("/")
def health():
    host = request.headers.get("X-Forwarded-Host") or request.host
    scheme = request.headers.get("X-Forwarded-Proto") or "https"
    if not webhook_configured and host and "localhost" not in host and "127.0.0.1" not in host:
        try:
            configure_webhook_once(f"{scheme}://{host}")
        except Exception as exc:
            print(f"Webhook setup error: {exc}", file=sys.stderr)
    return jsonify({"ok": True, "bot": "running", "webhook": webhook_configured})


@flask_app.post("/webhook")
@flask_app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    ensure_telegram_started()
    data = request.get_json(force=True, silent=True) or {}
    try:
        run_coroutine(process_update_json(data), timeout=25)
    except FutureTimeoutError:
        return jsonify({"ok": False, "error": "timeout"}), 504
    except Exception as exc:
        print(f"Webhook process error: {exc}", file=sys.stderr)
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


def main() -> None:
    if WEBHOOK_URL:
        ensure_telegram_started()
        configure_webhook_once(WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Bot polling rejimida ishga tushdi.", file=sys.stderr)
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)


setup_indexes()
bootstrap_owner()
register_handlers()
if WEBHOOK_URL:
    try:
        configure_webhook_once(WEBHOOK_URL)
    except Exception as exc:
        print(f"Webhook initial setup error: {exc}", file=sys.stderr)

if __name__ == "__main__":
    main()
