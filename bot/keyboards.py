from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import Config
import sys

def get_secure_web_url(user_id):
    """
    Constructs the secure Web App URL.
    Telegram WebAppInfo requires a secure public HTTPS URL. Local loopback addresses 
    (like localhost or 127.0.0.1) lack a Top-Level Domain (TLD) and are rejected 
    by Telegram's API with a BUTTON_URL_INVALID error. 
    This helper converts the URL and falls back to a public placeholder if localhost is used.
    """
    base_url = Config.WEB_APP_URL
    
    # Force HTTPS prefix
    if base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://", 1)
        
    # Guard against loopback addresses that crash Telegram API
    if "localhost" in base_url or "127.0.0.1" in base_url:
        print(
            "\n⚠️  WARNING: Config.WEB_APP_URL is set to a local loopback address (localhost).\n"
            "   Telegram WebAppInfo requires a secure public HTTPS URL (e.g., via ngrok).\n"
            "   Falling back to placeholder 'https://handcricketgame1.netlify.app' to prevent bot crash.\n", 
            file=sys.stderr
        )
        base_url = "https://handcricketgame1.netlify.app"
        
    return f"{base_url}?userId={user_id}"

def get_start_keyboard(user_id, is_admin=False):
    """Generate the main menu keyboard."""
    web_url = get_secure_web_url(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🏏 Play Match (Paid)", web_app=WebAppInfo(url=web_url))],
        [
            InlineKeyboardButton("👥 Challenge Friend", callback_data="btn_challenge"),
            InlineKeyboardButton("✉️ Invite Friend", callback_data="btn_invite")
        ],
        [
            InlineKeyboardButton("➕ Add Coin", callback_data="btn_add_coin"),
            InlineKeyboardButton("➖ Redeem Coin", callback_data="btn_redeem_coin")
        ],
        [InlineKeyboardButton("💰 Daily Task (Earn Free)", callback_data="btn_task")]
    ]
    
    if is_admin:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
        
    return InlineKeyboardMarkup(keyboard)

def get_add_coin_keyboard():
    """Deposit packages keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Pack 10 Rs", callback_data="deposit_pack_10")],
        [InlineKeyboardButton("🪙 Pack 20 Rs (Get 22)", callback_data="deposit_pack_20")],
        [InlineKeyboardButton("🪙 Pack 50 Rs (Get 54)", callback_data="deposit_pack_50")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])

def get_redeem_coin_keyboard():
    """Redemption packages keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Redeem 10 Rs", callback_data="redeem_pack_10")],
        [InlineKeyboardButton("💵 Redeem 20 Rs", callback_data="redeem_pack_20")],
        [InlineKeyboardButton("💵 Redeem 50 Rs", callback_data="redeem_pack_50")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])

def get_rejoin_keyboard(user_id):
    """Keyboard for users in an active match when sending bot messages."""
    web_url = get_secure_web_url(user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏏 Rejoin ongoing match", web_app=WebAppInfo(url=web_url))],
        [InlineKeyboardButton("❌ Forfeit/Cancel Match", callback_data="forfeit_match")]
    ])

def get_admin_keyboard():
    """Admin controls keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats & Analytics", callback_data="admin_stats")],
        [
            InlineKeyboardButton("➕ Add Coins", callback_data="admin_add_coins"),
            InlineKeyboardButton("➖ Remove Coins", callback_data="admin_remove_coins")
        ],
        [
            InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user"),
            InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban_user")
        ],
        [
            InlineKeyboardButton("📥 Pending Deposits", callback_data="admin_pending_dep"),
            InlineKeyboardButton("📤 Pending Redeems", callback_data="admin_pending_red")
        ],
        [InlineKeyboardButton("📣 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ])

def get_admin_action_keyboard(tx_id, action_type="deposit"):
    """Approve/Reject buttons for admin verification."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_{action_type}_{tx_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{action_type}_{tx_id}")
        ]
    ])

def get_cancel_redeem_keyboard():
    """Option to cancel ongoing redemption."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Withdrawal Request", callback_data="cancel_withdrawal")]
    ])
