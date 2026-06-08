from pyrogram import Client
from config import Config

# Initialize Pyrogram Bot Client
bot = Client(
    "hand_cricket_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)
