import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv(override=True)

class Config:
    API_ID = int(os.getenv("API_ID", 26275561))
    API_HASH = os.getenv("API_HASH", "cec50cf5848bfe6794dc9a934b47cf62")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "6216446524:AAHUoHJV04sC6qPlr9EbFNFmVvbbjonPd1E")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://gauravpushpa28_db_user:c6o6bVkuzF9CdGCB@cluster0.iwh6rby.mongodb.net/hand_cricket_bot?appName=Cluster0")
    ADMIN_ID = int(os.getenv("ADMIN_ID", 5802486388))
    ADMIN_UPI = os.getenv("ADMIN_UPI", "gauravpushpa28@okaxis")
    AROLINKS_API_KEY = os.getenv("AROLINKS_API_KEY", "225febf7630f8333e09487597ed69d4fe0beeba0")
    WEB_APP_URL = os.getenv("WEB_APP_URL", "https://hcg1.netlify.app/")
    PORT = int(os.getenv("PORT", 5000))
    REDIS_URI = os.getenv("REDIS_URI", "")
