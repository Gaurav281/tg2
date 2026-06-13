import re
import urllib.parse
from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))

def to_ist(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from config import Config
from database import (
    get_user, create_user, update_balance, ban_user, unban_user,
    get_unbanned_users, get_banned_users, get_all_users, get_finance_stats,
    get_pending_deposits, get_pending_redeems, approve_deposit,
    reject_deposit, approve_redeem, reject_redeem, cancel_redeem_by_user,
    create_transaction
)
from bot.keyboards import (
    get_start_keyboard, get_add_coin_keyboard, get_redeem_coin_keyboard,
    get_rejoin_keyboard, get_admin_keyboard, get_admin_action_keyboard,
    get_cancel_redeem_keyboard
)
from tasks.shortener import create_or_get_task, get_bot_username
from matchmaking import matchmaker
from bot.client import bot

# Store last bot message ID for each user to maintain a single-message interface
# Keys: user_id (int) -> Value: message_id (int)
last_bot_messages = {}

# Store last user message ID to clear chat history of old commands
last_user_messages = {}

async def clean_user_history(client: Client, user_id, current_message_id):
    """Deletes the previous user command/message from chat history."""
    user_id = int(user_id)
    if user_id in last_user_messages:
        try:
            await client.delete_messages(user_id, last_user_messages[user_id])
        except Exception:
            pass
    last_user_messages[user_id] = current_message_id

# Store admin states (e.g. waiting for broadcast, waiting for user/coin)
# Keys: admin_id (int) -> Value: dict with "action", "data"
admin_states = {}

# Store user states (e.g. waiting for deposit txn_id, waiting for redeem upi)
# Keys: user_id (int) -> Value: dict with "action", "data"
user_states = {}

def get_clean_text(text):
    """Remove HTML tags for logging/parsing."""
    return re.sub(r'<[^>]+>', '', text)

async def clean_send(client: Client, chat_id, text, reply_markup=None):
    """
    Ensures that only ONE bot message exists for a user at any time.
    Deletes the previous bot message before sending a new one.
    """
    chat_id = int(chat_id)
    # Try deleting previous bot message
    if chat_id in last_bot_messages:
        try:
            await client.delete_messages(chat_id, last_bot_messages[chat_id])
        except Exception:
            pass
            
    # Send new message
    msg = await client.send_message(chat_id, text, reply_markup=reply_markup)
    last_bot_messages[chat_id] = msg.id
    return msg

# --- START COMMAND ---
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    await clean_user_history(client, user_id, message.id)
        
    # Check if banned
    user = get_user(user_id)
    if user and user.get("is_banned"):
        await clean_send(client, user_id, "❌ You have been banned from using this bot.")
        return
        
    # Check if in active match
    match_id = matchmaker.user_to_match.get(user_id)
    if match_id:
        match = matchmaker.get_match(match_id)
        if match and match.status not in ["completed", "cancelled"]:
            # Prompt user to rejoin or forfeit
            await clean_send(
                client,
                user_id,
                "🏏 You are currently in an active match!\n"
                "Please rejoin to continue playing or cancel/forfeit the match.",
                reply_markup=get_rejoin_keyboard(user_id)
            )
            return

    # Handle invite parameter
    referred_by = None
    challenge_match_id = None
    verify_task_id = None
    if message.command and len(message.command) > 1:
        param = message.command[1]
        if param.startswith("invite_"):
            referred_by = param.split("_")[1]
        elif param.startswith("challenge_"):
            challenge_match_id = param.split("_")[1]
        elif param.startswith("verify_"):
            verify_task_id = param.split("_")[1]
            
    # Create or fetch user
    if not user:
        user = create_user(user_id, username, first_name, referred_by=referred_by)
        is_new = True
    else:
        is_new = False

    # Handle task verification parameter
    if verify_task_id:
        from tasks.shortener import verify_and_reward_task
        success, msg = verify_and_reward_task(verify_task_id)
        if success:
            await clean_send(
                client, 
                user_id, 
                f"✅ **Task Verified Successfully!**\n\n"
                f"Reward of **Rs 0.50** has been credited to your wallet.", 
                reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID)
            )
        else:
            await clean_send(
                client, 
                user_id, 
                f"❌ **Verification Failed:**\n{msg}", 
                reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID)
            )
        return
        
    # If starting via a challenge match link
    if challenge_match_id:
        match = matchmaker.get_match(challenge_match_id)
        if match:
            if match.status == "waiting":
                if match.player_a["user_id"] == user_id:
                    await clean_send(client, user_id, "⏳ Waiting for a friend to join your challenge...")
                    return
                # Join match and direct to web app
                match, err = matchmaker.join_challenge_match(challenge_match_id, user_id, first_name)
                if err:
                    await clean_send(client, user_id, f"❌ Challenge Error: {err}")
                else:
                    # Notify Socket.IO room and start timer
                    try:
                        if hasattr(matchmaker, "socketio") and matchmaker.socketio:
                            matchmaker.socketio.emit("match_update", match.to_dict(), to=match.match_id)
                        if hasattr(matchmaker, "start_ball_timer") and matchmaker.start_ball_timer:
                            matchmaker.start_ball_timer(match.match_id, match.current_inning, match.current_ball)
                    except Exception as e:
                        print(f"Error starting challenge via matchmaker: {e}")
                        
                    # Notify host
                    try:
                        host_id = match.player_a["user_id"]
                        web_url = f"{Config.WEB_APP_URL}?userId={host_id}"
                        # In private chat, we can update host
                        await clean_send(
                            client,
                            host_id,
                            f"🤝 {first_name} joined your challenge! The match is starting.",
                            reply_markup=get_start_keyboard(host_id, host_id == Config.ADMIN_ID)
                        )
                    except Exception:
                        pass
                        
                    # Let user join via Web App
                    web_url = f"{Config.WEB_APP_URL}?userId={user_id}"
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏏 Enter Challenge Match", web_app=WebAppInfo(url=web_url))
                    ]])
                    await clean_send(
                        client,
                        user_id,
                        f"🏏 You joined the challenge against {match.player_a['username']}!\n"
                        "Click below to enter the match.",
                        reply_markup=keyboard
                    )
                    return
            else:
                await clean_send(client, user_id, "❌ This challenge link has already expired or is active.")
                return

    # Standard /start welcome
    balance = user.get("balance", 0.0) if user else 0.0
    is_admin = (user_id == Config.ADMIN_ID)
    
    welcome_text = (
        f"👋 **Welcome to Battle Play, {first_name}!**\n\n"
        f"Play games right inside Telegram and earn real money!\n\n"
        f"🆔 **Your Login ID:** `{user.get('unique_id', '')}`\n"
        f"💰 **Wallet Balance:** Rs {balance:.2f}\n\n"
        f"Select an option below to get started:"
    )
    
    await clean_send(client, user_id, welcome_text, reply_markup=get_start_keyboard(user_id, is_admin))

@bot.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    help_text = (
        "❓ **Battle Play Bot Help**\n\n"
        "Here are the available commands:\n"
        "🔹 /start - Launch the bot and view main menu\n"
        "🔹 /help - View help guidelines\n\n"
        "You can play games and earn cash rewards directly in our Web App."
    )
    await clean_send(client, user_id, help_text, reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID))

@bot.on_callback_query(filters.regex("btn_join_tg"))
async def btn_join_tg_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    text = (
        "📢 **Official Telegram Channels & Groups**\n\n"
        "Join our official spaces to stay updated and get support:\n\n"
        "🟢 **Official Bot:** [Battle Play Bot](https://t.me/battleplay_bot)\n"
        "🔵 **Official Channel:** @free_fire_play_earn\n\n"
        "Click the buttons below to open and join:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Official Channel", url="https://t.me/free_fire_play_earn")],
        [InlineKeyboardButton("🤖 Battle Play Bot", url="https://t.me/battleplay_bot")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard, disable_web_page_preview=True)

# --- REJOIN & FORFEIT HANDLERS ---
@bot.on_callback_query(filters.regex("forfeit_match"))
async def forfeit_match_handler(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    match_id = matchmaker.user_to_match.get(user_id)
    
    if match_id:
        match = matchmaker.get_match(match_id)
        if match:
            match.handle_player_forfeit(user_id)
            # Notify opponent
            opp = match.get_opponent(user_id)
            if opp and opp["user_id"] != "bot":
                try:
                    await client.send_message(
                        opp["user_id"],
                        "🎉 Opponent forfeited the match! You won! Balance updated."
                    )
                except Exception:
                    pass
            # Clean matchmaker maps
            matchmaker.clean_completed_match(match_id)
            
    await query.answer("Match forfeited.")
    # Show main menu
    user = get_user(user_id)
    is_admin = (user_id == Config.ADMIN_ID)
    balance = user.get("balance", 0.0) if user else 0.0
    await query.edit_message_text(
        f"🏠 **Main Menu**\n\n💰 **Wallet Balance:** Rs {balance:.2f}\n\nSelect an option:",
        reply_markup=get_start_keyboard(user_id, is_admin)
    )

# --- BACK TO MAIN MENU CALLBACK ---
@bot.on_callback_query(filters.regex("main_menu"))
async def main_menu_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    
    # Cancel any state
    user_states.pop(user_id, None)
    admin_states.pop(user_id, None)
    
    user = get_user(user_id)
    if user and user.get("is_banned"):
        await query.answer("Banned.", show_alert=True)
        return
        
    balance = user.get("balance", 0.0) if user else 0.0
    is_admin = (user_id == Config.ADMIN_ID)
    
    await query.edit_message_text(
        f"🏠 **Main Menu**\n\n"
        f"💰 **Wallet Balance:** Rs {balance:.2f}\n\n"
        f"Select an option to play, task, or manage wallet:",
        reply_markup=get_start_keyboard(user_id, is_admin)
    )

# --- ADD COIN FLOW ---
@bot.on_callback_query(filters.regex("btn_add_coin"))
async def btn_add_coin_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    user = get_user(user_id)
    balance = user.get("balance", 0.0) if user else 0.0
    
    text = (
        f"➕ **Add Coin to Wallet**\n\n"
        f"Current Wallet Balance: **Rs {balance:.2f}**\n\n"
        f"Select a pack to buy. Payment is done via UPI. "
        f"After selection, we will provide a QR code and Admin UPI ID:"
    )
    await query.edit_message_text(text, reply_markup=get_add_coin_keyboard())

@bot.on_callback_query(filters.regex(r"deposit_pack_(\d+)"))
async def deposit_pack_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    amount = int(query.matches[0].group(1))
    
    # Generate UPI QR code link
    # upi://pay?pa=UPI_ID&pn=Name&am=AMOUNT&cu=INR
    upi_uri = f"upi://pay?pa={Config.ADMIN_UPI}&pn=HandCricketAdmin&am={amount}&cu=INR"
    encoded_upi = urllib.parse.quote(upi_uri)
    
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_upi}"
    
    # Store user state waiting for transaction ID
    user_states[user_id] = {"action": "wait_deposit_txn", "amount": amount}
    
    instruction_text = (
        f"🪙 **Deposit Package: Rs {amount}**\n\n"
        f"1. Scan the QR code shown below or copy the Admin UPI ID:\n"
        f"UPI ID: `{Config.ADMIN_UPI}`\n"
        f"Amount to Pay: **Rs {amount}**\n\n"
        f"2. Complete the payment on your UPI app.\n"
        f"3. Copy the **Transaction ID / Ref No.** and send it here in the chat.\n\n"
        f"⚖️ *Once the admin verifies the txn ID, the coins will be added instantly.*"
        f"<a href=\"{qr_url}\">&#8205;</a>" # Invisible image link for preview
    )
    
    await query.edit_message_text(
        instruction_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
        disable_web_page_preview=False
    )

# --- REDEEM COIN FLOW ---
@bot.on_callback_query(filters.regex("btn_redeem_coin"))
async def btn_redeem_coin_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    user = get_user(user_id)
    balance = user.get("balance", 0.0) if user else 0.0
    
    text = (
        f"➖ **Redeem Coins**\n\n"
        f"Current Wallet Balance: **Rs {balance:.2f}**\n\n"
        f"Select a redemption package:"
    )
    await query.edit_message_text(text, reply_markup=get_redeem_coin_keyboard())

@bot.on_callback_query(filters.regex(r"redeem_pack_(\d+)"))
async def redeem_pack_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    amount = int(query.matches[0].group(1))
    
    # 1. Deduct amount atomically
    # Amount is negative for deduction
    success, new_bal = update_balance(user_id, -amount, "redeem", {"status": "pending_detail"})
    
    if not success:
        await query.answer("❌ Insufficient balance to redeem this pack!", show_alert=True)
        return
        
    # Store state waiting for withdrawal detail (UPI/Mobile)
    user_states[user_id] = {"action": "wait_redeem_upi", "amount": amount}
    
    text = (
        f"💵 **Withdrawal: Rs {amount}**\n\n"
        f"Amount has been locked from your wallet.\n\n"
        f"👉 Please send the **UPI ID** or **Mobile Number** (for GPay/PhonePe) where you want to receive the withdrawal.\n\n"
        f"⚠️ *If you wish to cancel this withdrawal and refund your balance, click the cancel button below.*"
    )
    
    await query.edit_message_text(text, reply_markup=get_cancel_redeem_keyboard())

@bot.on_callback_query(filters.regex("cancel_withdrawal"))
async def cancel_withdrawal_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    
    # Process cancel
    success, refunded_amount = cancel_redeem_by_user(user_id)
    # Remove state
    user_states.pop(user_id, None)
    
    if success:
        await query.answer(f"✅ Withdrawal cancelled. Rs {refunded_amount} refunded to wallet.", show_alert=True)
    else:
        await query.answer("❌ No pending withdrawal to cancel.", show_alert=True)
        
    user = get_user(user_id)
    balance = user.get("balance", 0.0) if user else 0.0
    is_admin = (user_id == Config.ADMIN_ID)
    await query.edit_message_text(
        f"🏠 **Main Menu**\n\n💰 **Wallet Balance:** Rs {balance:.2f}\n\nSelect an option:",
        reply_markup=get_start_keyboard(user_id, is_admin)
    )

# --- TASK FLOW ---
@bot.on_message(filters.command("task") & filters.private)
async def task_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    await trigger_task(client, user_id)

@bot.on_callback_query(filters.regex("btn_task"))
async def btn_task_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await trigger_task(client, user_id, callback_query=query)

async def trigger_task(client: Client, user_id, callback_query: CallbackQuery = None):
    # Retrieve user
    user = get_user(user_id)
    if not user:
        return
        
    # Check/Create task
    task, msg_status = create_or_get_task(user_id)
    
    if msg_status == "limit_reached":
        err_msg = (
            "❌ **0 task remaining**\n\n"
            "4 tasks will be available tomorrow."
        )
        if callback_query:
            await callback_query.edit_message_text(
                err_msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="main_menu")]])
            )
        else:
            await clean_send(
                client,
                user_id,
                err_msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="main_menu")]])
            )
        return
        
    instructions = (
        f"💰 **Earn Rs 0.50 per Task!**\n\n"
        f"Complete shortener tasks to earn wallet coins! You can do up to 4 tasks daily.\n\n"
        f"📝 **Instructions:**\n"
        f"1. Click the **Open Task** button below.\n"
        f"2. You will be redirected to the shortener link. Complete the validation steps.\n"
        f"3. Once finished, you will automatically be redirected back to the bot, and Rs 0.50 will be credited.\n"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open Task", url=task["shortened_url"])],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])
    
    if callback_query:
        await callback_query.edit_message_text(instructions, reply_markup=keyboard)
    else:
        await clean_send(client, user_id, instructions, reply_markup=keyboard)

# --- REFERRAL & CHALLENGE CALLBACKS ---
@bot.on_callback_query(filters.regex("btn_invite"))
async def btn_invite_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    user = get_user(user_id)
    bot_uname = get_bot_username()
    
    referral_link = f"https://t.me/{bot_uname}?start=invite_{user_id}"
    ref_count = user.get("referrals_count", 0) if user else 0
    
    text = (
        f"✉️ **Invite Friends & Earn!**\n\n"
        f"Invite your friends to play. You will earn rewards for milestones which you can claim in the Web App Rewards Tab:\n\n"
        f"• Invite 1 Friend: **Rs 0.50**\n"
        f"• Invite 5 Friends: **Rs 2.00**\n"
        f"• Invite 10 Friends: **Rs 5.00**\n\n"
        f"📊 **Your Referral Stats:**\n"
        f"Total Referrals: **{ref_count}**\n\n"
        f"🔗 **Your Invite Link:**\n`{referral_link}`"
    )
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(referral_link)}&text={urllib.parse.quote('Join Battle Play and play games! 🎮')}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Invite Link", url=share_url)],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard)

@bot.on_callback_query(filters.regex("btn_challenge"))
async def btn_challenge_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    user = get_user(user_id)
    bot_uname = get_bot_username()
    first_name = user.get("first_name", "Player") if user else "Player"
    
    # Create challenge match in matchmaker
    match = matchmaker.create_challenge_match(user_id, first_name)
    challenge_link = f"https://t.me/{bot_uname}?start=challenge_{match.match_id}"
    
    text = (
        f"⚔️ **Challenge Friend**\n\n"
        f"You can challenge your friends to a Hand Cricket match!\n\n"
        f"Share the challenge code or link below with your friend. When they join, the match will start instantly:\n\n"
        f"🔑 **Challenge Code:** `{match.challenge_code}`\n"
        f"🔗 **Challenge Link:**\n`{challenge_link}`\n\n"
        f"⌛ *Waiting for friend to join...*"
    )
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(challenge_link)}&text={urllib.parse.quote('I challenge you to a Hand Cricket match! Click to join! ⚔️')}"
    web_url = f"{Config.WEB_APP_URL}?userId={user_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Open Match", web_app=WebAppInfo(url=web_url))],
        [InlineKeyboardButton("⚔️ Share Challenge Link", url=share_url)],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard)

# --- CAPTURE USER TEXT MESSAGES (States: deposits, withdrawals, etc.) ---
@bot.on_message(filters.text & filters.private)
async def user_text_handler(client: Client, message: Message):
    if message.outgoing or (message.text and message.text.startswith("/")):
        return
        
    user_id = message.from_user.id
    text = message.text.strip()
    
    await clean_user_history(client, user_id, message.id)

    # Check admin states
    if user_id == Config.ADMIN_ID and user_id in admin_states:
        state = admin_states[user_id]
        
        # 1. Admin Broadcast
        if state["action"] == "wait_broadcast":
            admin_states.pop(user_id, None)
            all_users = get_all_users()
            sent_count = 0
            for u in all_users:
                try:
                    await client.send_message(u["_id"], f"📣 **Announcement**\n\n{text}")
                    sent_count += 1
                except Exception:
                    pass
            await clean_send(
                client,
                user_id,
                f"✅ Broadcast sent successfully to {sent_count} users.",
                reply_markup=get_admin_keyboard()
            )
            return
            
        # 2. Admin Balance Manipulation
        elif state["action"] in ["wait_addbalance", "wait_removebalance"]:
            action = state["action"]
            admin_states.pop(user_id, None)
            
            # Parse username/userid and amount
            match = re.match(r"^@?(\w+)\s+(\d+(?:\.\d+)?)$", text)
            if not match:
                await clean_send(client, user_id, "❌ Invalid format. Please write in `<username_or_userid> <amount>` format.", reply_markup=get_admin_keyboard())
                return
                
            user_identifier = match.group(1)
            amount = float(match.group(2))
            is_add = (action == "wait_addbalance")
            await perform_balance_change(client, user_id, user_identifier, amount, is_add=is_add)
            return

        # 3. Admin Ban User
        elif state["action"] == "wait_ban":
            admin_states.pop(user_id, None)
            await perform_ban(client, user_id, text)
            return

        # 4. Admin Unban User
        elif state["action"] == "wait_unban":
            admin_states.pop(user_id, None)
            await perform_unban(client, user_id, text)
            return

        # 5. Free Fire Event Create
        elif state["action"] == "wait_ff_create":
            admin_states.pop(user_id, None)
            parts = [p.strip() for p in text.split("|")]
            if len(parts) != 8:
                await clean_send(
                    client,
                    user_id,
                    "❌ Invalid format. Please write exactly in the `Mode | Map | Entry Fee | Prize Per Kill | Total Participants | Start Time | End Time | Date` format.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
                )
                return
            try:
                mode = parts[0]
                map_name = parts[1]
                entry_fee = float(parts[2])
                prize_per_kill = float(parts[3])
                max_participants = int(parts[4])
                start_time = parts[5]
                end_time = parts[6]
                date_val = parts[7]
                
                from database import free_fire_events_col, IST
                free_fire_events_col.insert_one({
                    "mode": mode,
                    "map": map_name,
                    "entry_fee": entry_fee,
                    "prize_per_kill": prize_per_kill,
                    "max_participants": max_participants,
                    "start_time": start_time,
                    "end_time": end_time,
                    "date": date_val,
                    "room_id": "",
                    "room_password": "",
                    "slots": {str(i): None for i in range(1, max_participants + 1)},
                    "created_at": datetime.now(IST)
                })
                await clean_send(client, user_id, "✅ Free Fire event created successfully!", reply_markup=get_admin_keyboard())
            except Exception as e:
                await clean_send(client, user_id, f"❌ Error saving event: {e}", reply_markup=get_admin_keyboard())
            return

        # 6. Free Fire Event Edit
        elif state["action"] == "wait_ff_edit":
            event_id = state["event_id"]
            admin_states.pop(user_id, None)
            parts = [p.strip() for p in text.split("|")]
            if len(parts) != 8:
                await clean_send(
                    client,
                    user_id,
                    "❌ Invalid format. Please write exactly in the `Mode | Map | Entry Fee | Prize Per Kill | Total Participants | Start Time | End Time | Date` format.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
                )
                return
            try:
                mode = parts[0]
                map_name = parts[1]
                entry_fee = float(parts[2])
                prize_per_kill = float(parts[3])
                max_participants = int(parts[4])
                start_time = parts[5]
                end_time = parts[6]
                date_val = parts[7]
                
                from database import free_fire_events_col
                from bson.objectid import ObjectId
                
                ev = free_fire_events_col.find_one({"_id": ObjectId(event_id)})
                existing_slots = ev.get("slots", {}) if ev else {}
                
                new_slots = {}
                for i in range(1, max_participants + 1):
                    slot_key = str(i)
                    new_slots[slot_key] = existing_slots.get(slot_key, None)
                    
                free_fire_events_col.update_one(
                    {"_id": ObjectId(event_id)},
                    {"$set": {
                        "mode": mode,
                        "map": map_name,
                        "entry_fee": entry_fee,
                        "prize_per_kill": prize_per_kill,
                        "max_participants": max_participants,
                        "start_time": start_time,
                        "end_time": end_time,
                        "date": date_val,
                        "slots": new_slots
                    }}
                )
                await clean_send(client, user_id, "✅ Free Fire event updated successfully!", reply_markup=get_admin_keyboard())
            except Exception as e:
                await clean_send(client, user_id, f"❌ Error updating event: {e}", reply_markup=get_admin_keyboard())
            return

        # 7. Free Fire Set Room Details
        elif state["action"] == "wait_ff_room":
            event_id = state["event_id"]
            admin_states.pop(user_id, None)
            parts = [p.strip() for p in text.split("|")]
            if len(parts) != 2:
                await clean_send(
                    client,
                    user_id,
                    "❌ Invalid format. Please write exactly in the `Room ID | Password` format.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
                )
                return
            try:
                room_id = parts[0]
                room_password = parts[1]
                
                from database import free_fire_events_col
                from bson.objectid import ObjectId
                free_fire_events_col.update_one(
                    {"_id": ObjectId(event_id)},
                    {"$set": {
                        "room_id": room_id,
                        "room_password": room_password
                    }}
                )
                await clean_send(client, user_id, "✅ Free Fire Room ID and Password set successfully!", reply_markup=get_admin_keyboard())
            except Exception as e:
                await clean_send(client, user_id, f"❌ Error setting room details: {e}", reply_markup=get_admin_keyboard())
            return

        # 8. Free Fire Declare Results
        elif state["action"] == "wait_ff_result":
            event_id = state["event_id"]
            admin_states.pop(user_id, None)
            try:
                normalized_text = text.replace("\n", ",")
                kills_data = {}
                pairs = [p.strip() for p in normalized_text.split(",") if p.strip()]
                for pair in pairs:
                    sub_parts = pair.split(":")
                    if len(sub_parts) != 2:
                        continue
                    slot_num = sub_parts[0].strip()
                    kills_val = int(sub_parts[1].strip())
                    kills_data[slot_num] = kills_val
                
                from database import declare_free_fire_results
                success, msg = declare_free_fire_results(event_id, kills_data)
                if success:
                    await clean_send(client, user_id, f"✅ {msg}", reply_markup=get_admin_keyboard())
                else:
                    await clean_send(client, user_id, f"❌ {msg}", reply_markup=get_admin_keyboard())
            except Exception as e:
                await clean_send(client, user_id, f"❌ Error declaring results: {e}", reply_markup=get_admin_keyboard())
            return
            
    # Check user states
    if user_id in user_states:
        state = user_states[user_id]
        
        # 1. User providing deposit transaction ID
        if state["action"] == "wait_deposit_txn":
            user_states.pop(user_id, None)
            amount = state["amount"]
            
            # Create a pending deposit transaction in DB
            tx_id = create_transaction(
                user_id=user_id,
                tx_type="deposit",
                amount=amount,
                status="pending",
                details={"txn_id": text}
            )
            
            # Notify Admin
            admin_msg = (
                f"📥 **New Deposit Request!**\n\n"
                f"User: {message.from_user.first_name} (@{message.from_user.username})\n"
                f"User ID: `{user_id}`\n"
                f"Amount: **Rs {amount}**\n"
                f"Txn ID: `{text}`"
            )
            try:
                await client.send_message(
                    Config.ADMIN_ID,
                    admin_msg,
                    reply_markup=get_admin_action_keyboard(str(tx_id), "deposit")
                )
            except Exception as e:
                print(f"Failed to notify admin: {e}")
                
            # Update user screen
            await clean_send(
                client,
                user_id,
                f"✅ **Deposit Submitted!**\n\n"
                f"Deposit of **Rs {amount}** with Txn ID `{text}` is pending admin verification.\n"
                f"You will receive a notification as soon as it is approved.",
                reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID)
            )
            return
            
        # 2. User providing redeem details (UPI ID or mobile number)
        elif state["action"] == "wait_redeem_upi":
            user_states.pop(user_id, None)
            amount = state["amount"]
            
            # Fetch latest pending redeem transaction to update details
            from database import transactions_col
            tx = transactions_col.find_one_and_update(
                {"user_id": user_id, "status": "pending", "type": "redeem", "details.upi_or_mobile": {"$exists": False}},
                {"$set": {"details.upi_or_mobile": text, "updated_at": datetime.now(IST)}},
                sort=[("created_at", -1)]
            )
            
            if not tx:
                await clean_send(client, user_id, "❌ Error saving withdrawal details. Please try again.", reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID))
                return
                
            # Notify Admin
            admin_msg = (
                f"📤 **New Withdrawal Request!**\n\n"
                f"User: {message.from_user.first_name} (@{message.from_user.username})\n"
                f"User ID: `{user_id}`\n"
                f"Redeem Pack: **Rs {amount}**\n"
                f"Withdrawal Destination: `{text}`"
            )
            try:
                await client.send_message(
                    Config.ADMIN_ID,
                    admin_msg,
                    reply_markup=get_admin_action_keyboard(str(tx["_id"]), "redeem")
                )
            except Exception as e:
                print(f"Failed to notify admin: {e}")
                
            # Update user screen
            await clean_send(
                client,
                user_id,
                f"✅ **Withdrawal Request Registered!**\n\n"
                f"Request to redeem **Rs {amount}** to `{text}` is pending admin transfer.\n"
                f"Once processed, you will get a notification.",
                reply_markup=get_start_keyboard(user_id, user_id == Config.ADMIN_ID)
            )
            return

    # Default fallback - if no state, just treat as /start
    await start_handler(client, message)

# --- ADMIN PANEL COMMANDS & CALLBACKS ---
@bot.on_message(filters.command("admin") & filters.private)
async def admin_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    if user_id != Config.ADMIN_ID:
        await clean_send(client, user_id, "❌ Access Denied: Admin only.")
        return
    await clean_send(client, user_id, "⚙️ **Admin Panel**\n\nSelect control action:", reply_markup=get_admin_keyboard())

@bot.on_callback_query(filters.regex("admin_panel"))
async def admin_panel_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
    await query.edit_message_text("⚙️ **Admin Panel**\n\nSelect control action:", reply_markup=get_admin_keyboard())

@bot.on_callback_query(filters.regex("admin_stats"))
async def admin_stats_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
        
    stats = get_finance_stats()
    
    text = (
        f"📊 **Financial Stats & Analytics**\n\n"
        f"👥 Total Registered Users: **{stats['total_users']}**\n\n"
        f"📥 Total Deposits Approved: **Rs {stats['total_deposits']:.2f}**\n"
        f"📤 Total Redeems Paid: **Rs {stats['total_redeems']:.2f}**\n\n"
        f"🔗 Tasks Completed: **{stats['completed_tasks']}**\n"
        f"💰 Estimated Ad Earnings ($10 CPM): **Rs {stats['estimated_ad_revenue']:.2f}**\n"
        f"🎁 Task Rewards Paid: **Rs {stats['total_task_rewards_paid']:.2f}**\n\n"
        f"🏏 Total Match Fees Collected: **Rs {stats['match_fees_collected']:.2f}**\n"
        f"🏆 Total Match Wins Paid: **Rs {stats['match_wins_paid']:.2f}**\n\n"
        f"🎗️ Total Free Giveaways (Referrals, Streaks, Signup): **Rs {stats['total_giveaways']:.2f}**\n"
        f"🏦 Total Wallet Liabilities (Current Balance): **Rs {stats['total_user_wallets']:.2f}**\n\n"
        f"📈 **Net Platform Profit/Loss:** **Rs {stats['net_profit']:.2f}**"
    )
    await query.edit_message_text(text, reply_markup=get_admin_keyboard())

@bot.on_callback_query(filters.regex("admin_pending_dep"))
async def admin_pending_dep_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
        
    deps = get_pending_deposits()
    if not deps:
        await query.answer("No pending deposits.", show_alert=True)
        return
        
    # Show first pending deposit
    tx = deps[0]
    user = get_user(tx["user_id"])
    u_name = user.get("username", "Unknown") if user else "Unknown"
    
    text = (
        f"📥 **Pending Deposit (1 of {len(deps)})**\n\n"
        f"User: {user.get('first_name') if user else 'N/A'} (@{u_name})\n"
        f"User ID: `{tx['user_id']}`\n"
        f"Amount: **Rs {tx['amount']}**\n"
        f"Txn ID: `{tx['details'].get('txn_id')}`\n"
        f"Requested: {to_ist(tx['created_at']).strftime('%Y-%m-%d %I:%M:%S %p')}"
    )
    await query.edit_message_text(text, reply_markup=get_admin_action_keyboard(str(tx["_id"]), "deposit"))

@bot.on_callback_query(filters.regex("admin_pending_red"))
async def admin_pending_red_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
        
    reds = get_pending_redeems()
    if not reds:
        await query.answer("No pending redeems.", show_alert=True)
        return
        
    # Show first pending redeem
    tx = reds[0]
    user = get_user(tx["user_id"])
    u_name = user.get("username", "Unknown") if user else "Unknown"
    
    text = (
        f"📤 **Pending Withdrawal (1 of {len(reds)})**\n\n"
        f"User: {user.get('first_name') if user else 'N/A'} (@{u_name})\n"
        f"User ID: `{tx['user_id']}`\n"
        f"Pack Amount: **Rs {abs(tx['amount'])}**\n"
        f"UPI ID / Mobile: `{tx['details'].get('upi_or_mobile')}`\n"
        f"Requested: {to_ist(tx['created_at']).strftime('%Y-%m-%d %I:%M:%S %p')}"
    )
    await query.edit_message_text(text, reply_markup=get_admin_action_keyboard(str(tx["_id"]), "redeem"))

@bot.on_callback_query(filters.regex("admin_broadcast"))
async def admin_broadcast_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
    admin_states[user_id] = {"action": "wait_broadcast"}
    await query.edit_message_text(
        "📣 **Admin Broadcast**\n\n"
        "Please type the message you want to broadcast to all registered users.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]])
    )

# --- APPROVE/REJECT HANDLERS (ADMIN ACTION) ---
@bot.on_callback_query(filters.regex(r"adm_(app|rej)_(deposit|redeem)_(.+)"))
async def admin_action_callback(client: Client, query: CallbackQuery):
    admin_id = query.from_user.id
    if admin_id != Config.ADMIN_ID:
        return
        
    action = query.matches[0].group(1)   # app, rej
    tx_type = query.matches[0].group(2)  # deposit, redeem
    tx_id = query.matches[0].group(3)     # ObjectId string
    
    if tx_type == "deposit":
        if action == "app":
            success, res = approve_deposit(tx_id)
            if success:
                # Notify User
                try:
                    await client.send_message(
                        res["user_id"],
                        f"🎉 **Deposit Approved!**\n\n"
                        f"Your wallet has been credited with **Rs {res['amount']:.2f}**."
                    )
                except Exception:
                    pass
                await query.answer("Deposit approved & credited.")
            else:
                await query.answer(f"Failed: {res}", show_alert=True)
        else:
            success, uid = reject_deposit(tx_id)
            if success:
                try:
                    await client.send_message(
                        uid,
                        f"❌ **Deposit Rejected!**\n\n"
                        f"Your deposit submission has been rejected by the admin. Please verify your transaction details."
                    )
                except Exception:
                    pass
                await query.answer("Deposit rejected.")
            else:
                await query.answer("Failed to reject.", show_alert=True)
                
    elif tx_type == "redeem":
        if action == "app":
            success, uid = approve_redeem(tx_id)
            if success:
                try:
                    await client.send_message(
                        uid,
                        f"🎉 **Withdrawal Successful!**\n\n"
                        f"Admin has processed and transferred your withdrawal request."
                    )
                except Exception:
                    pass
                await query.answer("Withdrawal marked approved.")
            else:
                await query.answer("Failed to approve.", show_alert=True)
        else:
            success, uid = reject_redeem(tx_id)
            if success:
                try:
                    await client.send_message(
                        uid,
                        f"❌ **Withdrawal Rejected!**\n\n"
                        f"Your withdrawal request was rejected. The locked amount has been refunded back to your wallet."
                    )
                except Exception:
                    pass
                await query.answer("Withdrawal rejected & amount refunded.")
            else:
                await query.answer("Failed to reject.", show_alert=True)
                
    # Go back to admin panel
    await query.edit_message_text("⚙️ **Admin Panel**\n\nSelect control action:", reply_markup=get_admin_keyboard())

# --- ADMIN HELPERS (PROMPTS AND ACTIONS) ---

def get_add_coins_prompt():
    unbanned = get_unbanned_users()
    user_list = "\n".join([f"• `{u['_id']}` - {u['first_name']} (@{u.get('username', 'N/A')})" for u in unbanned[:30]])
    text = (
        f"➕ **Add Coins**\n\n"
        f"**Active Unbanned Users:**\n{user_list}\n\n"
        f"👉 Please send `<username> <amount>` to add to user wallet."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]])
    return text, keyboard

def get_remove_coins_prompt():
    unbanned = get_unbanned_users()
    user_list = "\n".join([f"• `{u['_id']}` - {u['first_name']} (@{u.get('username', 'N/A')})" for u in unbanned[:30]])
    text = (
        f"➖ **Remove Coins**\n\n"
        f"**Active Unbanned Users:**\n{user_list}\n\n"
        f"👉 Please send `<username> <amount>` to remove from user wallet."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]])
    return text, keyboard

def get_ban_user_prompt():
    unbanned = get_unbanned_users()
    user_list = "\n".join([f"• `{u['_id']}` - {u['first_name']} (@{u.get('username', 'N/A')})" for u in unbanned[:30]])
    text = (
        f"🚫 **Ban User**\n\n"
        f"**Active Unbanned Users:**\n{user_list}\n\n"
        f"👉 Please send `<username>` to ban that user."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]])
    return text, keyboard

def get_unban_user_prompt():
    banned = get_banned_users()
    if not banned:
        user_list = "*No banned users found.*"
    else:
        user_list = "\n".join([f"• `{u['_id']}` - {u['first_name']} (@{u.get('username', 'N/A')})" for u in banned[:30]])
    text = (
        f"🔓 **Unban User**\n\n"
        f"**Banned Users:**\n{user_list}\n\n"
        f"👉 Please send `<username>` to unban that user."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]])
    return text, keyboard

async def perform_balance_change(client: Client, admin_id: int, user_identifier: str, amount: float, is_add: bool):
    if user_identifier.startswith("@"):
        user_identifier = user_identifier[1:]
        
    # Find user
    target_user = None
    try:
        target_user = get_user(int(user_identifier))
    except ValueError:
        all_users = get_all_users()
        for u in all_users:
            if u.get("username") == user_identifier:
                target_user = u
                break
                
    if not target_user:
        await clean_send(client, admin_id, f"❌ User '{user_identifier}' not found in database.", reply_markup=get_admin_keyboard())
        return False
        
    tx_type = "admin_add" if is_add else "admin_remove"
    actual_amount = amount if is_add else -amount
    
    success, new_bal = update_balance(target_user["_id"], actual_amount, tx_type, {"admin_id": admin_id})
    
    if success:
        # Notify target user
        try:
            msg = "credited" if is_add else "deducted"
            await client.send_message(
                target_user["_id"],
                f"🔔 Admin has {msg} **Rs {amount:.2f}** in your wallet. New Balance: **Rs {new_bal:.2f}**"
            )
        except Exception:
            pass
            
        await clean_send(
            client,
            admin_id,
            f"✅ Balance updated for {target_user['first_name']} (@{target_user.get('username')}).\n"
            f"New Balance: **Rs {new_bal:.2f}**",
            reply_markup=get_admin_keyboard()
        )
        return True
    else:
        await clean_send(client, admin_id, "❌ Action failed (e.g. insufficient user balance or database error).", reply_markup=get_admin_keyboard())
        return False

async def perform_ban(client: Client, admin_id: int, user_identifier: str):
    if user_identifier.startswith("@"):
        user_identifier = user_identifier[1:]
        
    # Find user
    target_user = None
    try:
        target_user = get_user(int(user_identifier))
    except ValueError:
        all_users = get_all_users()
        for u in all_users:
            if u.get("username") == user_identifier:
                target_user = u
                break
                
    if not target_user:
        await clean_send(client, admin_id, f"❌ User '{user_identifier}' not found in database.", reply_markup=get_admin_keyboard())
        return False
        
    ban_user(target_user["_id"])
    await clean_send(
        client,
        admin_id,
        f"✅ User {target_user['first_name']} (@{target_user.get('username')}) has been BANNED.",
        reply_markup=get_admin_keyboard()
    )
    return True

async def perform_unban(client: Client, admin_id: int, user_identifier: str):
    if user_identifier.startswith("@"):
        user_identifier = user_identifier[1:]
        
    # Find user
    target_user = None
    try:
        target_user = get_user(int(user_identifier))
    except ValueError:
        all_users = get_all_users()
        for u in all_users:
            if u.get("username") == user_identifier:
                target_user = u
                break
                
    if not target_user:
        await clean_send(client, admin_id, f"❌ User '{user_identifier}' not found in database.", reply_markup=get_admin_keyboard())
        return False
        
    unban_user(target_user["_id"])
    await clean_send(
        client,
        admin_id,
        f"✅ User {target_user['first_name']} (@{target_user.get('username')}) has been UNBANNED.",
        reply_markup=get_admin_keyboard()
    )
    return True

# --- BALANCE MANIPULATION VIA DIRECT COMMANDS ---
@bot.on_message(filters.command(["addbalance", "addcoins"]) & filters.private)
async def addbalance_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    if user_id != Config.ADMIN_ID:
        return
        
    if message.command and len(message.command) >= 3:
        user_identifier = message.command[1]
        try:
            amount = float(message.command[2])
            await perform_balance_change(client, user_id, user_identifier, amount, is_add=True)
        except ValueError:
            admin_states[user_id] = {"action": "wait_addbalance"}
            text, markup = get_add_coins_prompt()
            await clean_send(client, user_id, f"❌ Invalid amount. Showing menu:\n\n{text}", reply_markup=markup)
    else:
        admin_states[user_id] = {"action": "wait_addbalance"}
        text, markup = get_add_coins_prompt()
        await clean_send(client, user_id, text, reply_markup=markup)

@bot.on_message(filters.command(["removebalance", "removecoins"]) & filters.private)
async def removebalance_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    if user_id != Config.ADMIN_ID:
        return
        
    if message.command and len(message.command) >= 3:
        user_identifier = message.command[1]
        try:
            amount = float(message.command[2])
            await perform_balance_change(client, user_id, user_identifier, amount, is_add=False)
        except ValueError:
            admin_states[user_id] = {"action": "wait_removebalance"}
            text, markup = get_remove_coins_prompt()
            await clean_send(client, user_id, f"❌ Invalid amount. Showing menu:\n\n{text}", reply_markup=markup)
    else:
        admin_states[user_id] = {"action": "wait_removebalance"}
        text, markup = get_remove_coins_prompt()
        await clean_send(client, user_id, text, reply_markup=markup)

# --- BAN / UNBAN DIRECT COMMANDS ---
@bot.on_message(filters.command("ban") & filters.private)
async def ban_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    if user_id != Config.ADMIN_ID:
        return
        
    if message.command and len(message.command) >= 2:
        user_identifier = message.command[1]
        await perform_ban(client, user_id, user_identifier)
    else:
        admin_states[user_id] = {"action": "wait_ban"}
        text, markup = get_ban_user_prompt()
        await clean_send(client, user_id, text, reply_markup=markup)

@bot.on_message(filters.command("unban") & filters.private)
async def unban_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clean_user_history(client, user_id, message.id)
    if user_id != Config.ADMIN_ID:
        return
        
    if message.command and len(message.command) >= 2:
        user_identifier = message.command[1]
        await perform_unban(client, user_id, user_identifier)
    else:
        admin_states[user_id] = {"action": "wait_unban"}
        text, markup = get_unban_user_prompt()
        await clean_send(client, user_id, text, reply_markup=markup)

# --- ADMIN BUTTONS CALLBACK HANDLERS ---
@bot.on_callback_query(filters.regex("admin_add_coins"))
async def admin_add_coins_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
    admin_states[user_id] = {"action": "wait_addbalance"}
    text, markup = get_add_coins_prompt()
    await query.edit_message_text(text, reply_markup=markup)

@bot.on_callback_query(filters.regex("admin_remove_coins"))
async def admin_remove_coins_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
    admin_states[user_id] = {"action": "wait_removebalance"}
    text, markup = get_remove_coins_prompt()
    await query.edit_message_text(text, reply_markup=markup)

@bot.on_callback_query(filters.regex("admin_ban_user"))
async def admin_ban_user_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
    admin_states[user_id] = {"action": "wait_ban"}
    text, markup = get_ban_user_prompt()
    await query.edit_message_text(text, reply_markup=markup)

@bot.on_callback_query(filters.regex("admin_unban_user"))
async def admin_unban_user_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        return
    admin_states[user_id] = {"action": "wait_unban"}
    text, markup = get_unban_user_prompt()
    await query.edit_message_text(text, reply_markup=markup)

@bot.on_callback_query(filters.regex("admin_ff_events"))
async def admin_ff_events_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
    
    from database import get_free_fire_events
    events = get_free_fire_events()
    
    text = "🏆 **Free Fire Tournaments**\n\n"
    if not events:
        text += "No active tournaments."
    else:
        for idx, ev in enumerate(events):
            text += (
                f"**{idx+1}. {ev['mode']} - {ev['map']}**\n"
                f"📅 Date: {ev['date']} | Time: {ev['start_time']} to {ev['end_time']}\n"
                f"💰 Entry: Rs {ev['entry_fee']} | Kill: Rs {ev['prize_per_kill']}\n"
                f"🔑 Room: ID `{ev['room_id'] or '-'}` | Pass `{ev['room_password'] or '-'}`\n"
                f"👥 Slots: {ev['joined_count']}/{ev['max_participants']}\n"
                f"ID: `{ev['id']}`\n\n"
            )
            
    keyboard_rows = []
    for ev in events:
        keyboard_rows.append([
            InlineKeyboardButton(f"📝 Edit", callback_data=f"adm_ffedit_{ev['id']}"),
            InlineKeyboardButton(f"🔑 Room", callback_data=f"adm_ffroom_{ev['id']}"),
            InlineKeyboardButton(f"🏆 Declare", callback_data=f"adm_ffres_{ev['id']}"),
            InlineKeyboardButton(f"❌ Del", callback_data=f"adm_ffdel_{ev['id']}")
        ])
    keyboard_rows.append([InlineKeyboardButton("➕ Create FF Event", callback_data="admin_ff_create")])
    keyboard_rows.append([InlineKeyboardButton("↩️ Back to Admin Panel", callback_data="admin_panel")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))

@bot.on_callback_query(filters.regex(r"^adm_ffdel_(.+)"))
async def admin_ff_delete_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
        
    event_id = query.data.split("_")[2]
    from database import free_fire_events_col
    from bson.objectid import ObjectId
    try:
        free_fire_events_col.delete_one({"_id": ObjectId(event_id)})
        await query.answer("Event deleted successfully!", show_alert=True)
    except Exception as e:
        await query.answer(f"Error: {e}", show_alert=True)
        
    # Refresh events list
    from database import get_free_fire_events
    events = get_free_fire_events()
    text = "🏆 **Free Fire Tournaments**\n\n"
    if not events:
        text += "No active tournaments."
    else:
        for idx, ev in enumerate(events):
            text += (
                f"**{idx+1}. {ev['mode']} - {ev['map']}**\n"
                f"📅 Date: {ev['date']} | Time: {ev['start_time']} to {ev['end_time']}\n"
                f"💰 Entry: Rs {ev['entry_fee']} | Kill: Rs {ev['prize_per_kill']}\n"
                f"🔑 Room: ID `{ev['room_id'] or '-'}` | Pass `{ev['room_password'] or '-'}`\n"
                f"👥 Slots: {ev['joined_count']}/{ev['max_participants']}\n"
                f"ID: `{ev['id']}`\n\n"
            )
    keyboard_rows = []
    for ev in events:
        keyboard_rows.append([
            InlineKeyboardButton(f"📝 Edit", callback_data=f"adm_ffedit_{ev['id']}"),
            InlineKeyboardButton(f"🔑 Room", callback_data=f"adm_ffroom_{ev['id']}"),
            InlineKeyboardButton(f"🏆 Declare", callback_data=f"adm_ffres_{ev['id']}"),
            InlineKeyboardButton(f"❌ Del", callback_data=f"adm_ffdel_{ev['id']}")
        ])
    keyboard_rows.append([InlineKeyboardButton("➕ Create FF Event", callback_data="admin_ff_create")])
    keyboard_rows.append([InlineKeyboardButton("↩️ Back to Admin Panel", callback_data="admin_panel")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))

@bot.on_callback_query(filters.regex("admin_ff_create"))
async def admin_ff_create_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
        
    admin_states[user_id] = {"action": "wait_ff_create"}
    
    instruction = (
        "➕ **Create Free Fire Event**\n\n"
        "Please send the event details in the exact format shown below:\n\n"
        "`Mode | Map | Entry Fee | Prize Per Kill | Total Participants | Start Time | End Time | Date`\n\n"
        "**Example:**\n"
        "`BR | Bermuda | 5 | 4 | 50 | 7:00 PM | 8:00 PM | 2026-06-12`"
    )
    await query.edit_message_text(
        instruction, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
    )

@bot.on_callback_query(filters.regex(r"^adm_ffedit_(.+)"))
async def admin_ff_edit_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
        
    event_id = query.data.split("_")[2]
    admin_states[user_id] = {"action": "wait_ff_edit", "event_id": event_id}
    
    from database import free_fire_events_col
    from bson.objectid import ObjectId
    ev = free_fire_events_col.find_one({"_id": ObjectId(event_id)})
    current_details = ""
    if ev:
        current_details = f"Current: `{ev['mode']} | {ev['map']} | {ev['entry_fee']} | {ev['prize_per_kill']} | {ev['max_participants']} | {ev['start_time']} | {ev['end_time']} | {ev.get('date', '2026-06-12')}`\n\n"
        
    instruction = (
        "📝 **Edit Free Fire Event**\n\n"
        f"{current_details}"
        "Please send the new event details in the exact format shown below:\n\n"
        "`Mode | Map | Entry Fee | Prize Per Kill | Total Participants | Start Time | End Time | Date`\n\n"
        "**Example:**\n"
        "`BR | Bermuda | 5 | 4 | 50 | 7:00 PM | 8:00 PM | 2026-06-12`"
    )
    await query.edit_message_text(
        instruction, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
    )

@bot.on_callback_query(filters.regex(r"^adm_ffroom_(.+)"))
async def admin_ff_room_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
        
    event_id = query.data.split("_")[2]
    admin_states[user_id] = {"action": "wait_ff_room", "event_id": event_id}
    
    from database import free_fire_events_col
    from bson.objectid import ObjectId
    ev = free_fire_events_col.find_one({"_id": ObjectId(event_id)})
    current_details = ""
    if ev:
        current_details = f"Current Room: `{ev.get('room_id') or '-'}` | Pass: `{ev.get('room_password') or '-'}`\n\n"
        
    instruction = (
        "🔑 **Set Room Details**\n\n"
        f"{current_details}"
        "Please send the Room ID and Password in this exact format:\n\n"
        "`Room ID | Password`\n\n"
        "**Example:**\n"
        "`8390291 | pass123`"
    )
    await query.edit_message_text(
        instruction, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
    )

@bot.on_callback_query(filters.regex(r"^adm_ffres_(.+)"))
async def admin_ff_result_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id != Config.ADMIN_ID:
        await query.answer("Access Denied.", show_alert=True)
        return
        
    event_id = query.data.split("_")[2]
    admin_states[user_id] = {"action": "wait_ff_result", "event_id": event_id}
    
    from database import free_fire_events_col
    from bson.objectid import ObjectId
    ev = free_fire_events_col.find_one({"_id": ObjectId(event_id)})
    
    # List registered players to help admin reward
    registered_players = ""
    copy_template = ""
    if ev:
        slots = ev.get("slots", {})
        players_list = []
        template_parts = []
        for s_key, s_val in slots.items():
            if s_val:
                players_list.append(f"• Slot **#{s_key}**: {s_val['ff_username']} ({s_val['ff_uid']}) - User: @{s_val.get('username') or 'N/A'}")
                template_parts.append(f"{s_key}:0")
        if players_list:
            registered_players = "\n".join(players_list) + "\n\n"
            copy_template = ", ".join(template_parts)
        else:
            registered_players = "(No players registered in this event yet)\n\n"
            
    instruction = (
        "🏆 **Declare Results & Send Rewards**\n\n"
        f"Registered Players:\n{registered_players}"
    )
    if copy_template:
        instruction += (
            "📋 **Copy-Paste Template** (Copy, edit the kills and send back):\n"
            f"`{copy_template}`\n\n"
        )
    instruction += (
        "Please send the player kills in this exact format:\n\n"
        "`Slot_Number:Kills` (separated by commas or newlines)\n\n"
        "**Example:**\n"
        "`1:5, 5:2, 12:0` (means Slot 1 got 5 kills, Slot 5 got 2, etc.)"
    )
    await query.edit_message_text(
        instruction, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_ff_events")]])
    )
