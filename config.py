import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv(override=True)

class Config:
    API_ID = int(os.getenv("API_ID")) if os.getenv("API_ID") else None
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    MONGO_URI = os.getenv("MONGO_URI")
    ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
    ADMIN_UPI = os.getenv("ADMIN_UPI")
    AROLINKS_API_KEY = os.getenv("AROLINKS_API_KEY")
    VPLINK_API_KEY = os.getenv("VPLINK_API_KEY")
    WEB_APP_URL = os.getenv("WEB_APP_URL")
    PORT = int(os.getenv("PORT", 5000))
    REDIS_URI = os.getenv("REDIS_URI")
    BACKEND_URL = os.getenv("BACKEND_URL")
