import asyncio
bot_loop = asyncio.new_event_loop()
asyncio.set_event_loop(bot_loop)

import os
import threading
import time
import random
import sys
import signal
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from database import (
    get_user, create_user, update_balance, get_transaction_history,
    get_match_history, get_leaderboard, get_user_rank, claim_daily_streak,
    claim_daily_mission, claim_referral_reward, save_match_result, tasks_col,
    save_feedback, update_free_fire_profile, get_active_car_event_cycles,
    join_car_event, submit_car_score, get_free_fire_events, join_free_fire_event
)
from matchmaking import matchmaker
from game import HandCricketMatch
from tasks.shortener import verify_and_reward_task, get_bot_username
from bot.client import bot as bot_client
import bot.handlers  # Register Telegram commands and callback query handlers at startup

# Signal handler for clean exit on Ctrl+C (SIGINT/SIGTERM) in the main thread
def signal_handler(sig, frame):
    print("\nShutting down Battle Play...")
    try:
        # Schedule the stop coroutine on the running background event loop cleanly
        future = asyncio.run_coroutine_threadsafe(bot_client.stop(), bot_loop)
        future.result(timeout=3.0)
    except Exception:
        pass
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Initialize Flask and SocketIO
from flask_cors import CORS
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "hand_cricket_secret_key_13579")

# Enable CORS for React Frontend HTTP requests & Socket.IO
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Track connected users: user_id (int) -> socket_id (str)
connected_users = {}
# Track active matchmaking timers: user_id -> Timer
matchmaking_timers = {}
# Track active ball timers: match_id -> Timer
ball_timers = {}
# Track active offline timers: user_id -> Timer
offline_timers = {}

def get_active_users_count():
    """Returns number of active human users connected via sockets."""
    return len(connected_users)

# --- HTTP ROUTING API ENDPOINTS ---

@app.route("/")
def home_index():
    return jsonify({"status": "healthy", "service": "Hand Cricket Backend"}), 200

@app.route("/verify-task/<task_id>", methods=["GET"])
def verify_task_route(task_id):
    """Callback route from AroLinks. Verifies task and rewards player."""
    success, result = verify_and_reward_task(task_id)
    bot_uname = get_bot_username()
    
    if success:
        # Notify user on Telegram about task reward
        try:
            bot_client.send_message(
                result,  # result is user_id
                "🎉 **Task Verified!**\n\n"
                "Your wallet has been credited with **Rs 0.50**."
            )
        except Exception:
            pass
        # Redirect back to bot start page with success flag
        return redirect(f"https://t.me/{bot_uname}?start=task_done")
    else:
        # Return elegant error page
        html = f"""
        <html>
            <head>
                <title>Task Verification Failed</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 50px 20px; }}
                    .card {{ background: #1e293b; border-radius: 12px; padding: 30px; max-width: 400px; margin: 0 auto; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); }}
                    h2 {{ color: #ef4444; }}
                    a {{ display: inline-block; margin-top: 20px; background: #3b82f6; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>❌ Task Failed</h2>
                    <p>{result}</p>
                    <p>The link might have expired or has already been used.</p>
                    <a href="https://t.me/{bot_uname}">Back to Bot</a>
                </div>
            </body>
        </html>
        """
        return render_template_string(html), 400

# Channel join check cache: user_id (int) -> {"joined": bool, "expires_at": float}
channel_membership_cache = {}

def is_user_member_of_channel(user_id):
    """Check if the user is a member of the Telegram channel free_fire_play_earn, with caching."""
    try:
        user_id_int = int(user_id)
    except ValueError:
        return False

    current_time = time.time()
    cached = channel_membership_cache.get(user_id_int)
    if cached and cached["expires_at"] > current_time:
        return cached["joined"]

    joined = False
    try:
        async def check_member():
            try:
                # bot_client is imported from bot.client
                member = await bot_client.get_chat_member("free_fire_play_earn", user_id_int)
                status = str(member.status).lower()
                if "left" in status or "kicked" in status or "banned" in status:
                    return False
                return True
            except Exception as e:
                # E.g., UserNotParticipant or generic Pyrogram error
                print(f"Pyrogram check_member error for {user_id_int}: {e}")
                return False

        # Schedule the check_member coroutine on the background event loop (bot_loop)
        future = asyncio.run_coroutine_threadsafe(check_member(), bot_loop)
        joined = future.result(timeout=4.0)
    except Exception as e:
        print(f"Error checking channel membership in thread for {user_id_int}: {e}")
        joined = False

    # Cache: 10 minutes if joined, 1 minute if not joined (so they can join and get updated quickly)
    duration = 600 if joined else 60
    channel_membership_cache[user_id_int] = {
        "joined": joined,
        "expires_at": current_time + duration
    }
    return joined

@app.route("/api/user/<user_id>", methods=["GET"])
def get_user_api(user_id):
    """Retrieve user details for React Web App store."""
    joined_channel = is_user_member_of_channel(user_id)
    user = get_user(user_id)
    if not user:
        username = request.args.get("username")
        first_name = request.args.get("first_name") or request.args.get("firstName")
        if username or first_name:
            try:
                user = create_user(user_id, username, first_name)
            except Exception as e:
                print(f"Error auto-registering user {user_id}: {e}")
        
        if not user:
            return jsonify({"error": "User not found"}), 404
    
    # Calculate active user count (online socket connections + some fake activity if needed)
    active_count = get_active_users_count()
    if active_count < 3:
        active_count += 5 # show baseline active users count for engagement
        
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = tasks_col.count_documents({
        "user_id": int(user_id),
        "status": "completed",
        "completed_at": {"$gte": start_of_today}
    })
        
    from database import transactions_col
    pending_deposits = transactions_col.count_documents({
        "user_id": int(user_id),
        "type": "deposit",
        "status": "pending"
    })
    pending_redeems = transactions_col.count_documents({
        "user_id": int(user_id),
        "type": "redeem",
        "status": "pending"
    })
    
    from database import free_fire_events_col
    active_ff_room = None
    try:
        for ev in free_fire_events_col.find():
            slots = ev.get("slots", {})
            for slot_key, slot_val in slots.items():
                if slot_val and slot_val.get("user_id") == int(user_id):
                    if ev.get("room_id") and ev.get("room_password"):
                        active_ff_room = {
                            "mode": ev["mode"],
                            "map": ev["map"],
                            "room_id": ev["room_id"],
                            "room_password": ev["room_password"],
                            "date": ev.get("date", ""),
                            "time": ev.get("start_time", "")
                        }
                    break
    except Exception as ex:
        print(f"Error checking active FF rooms: {ex}")

    from matchmaking import matchmaker
    paid_playing = matchmaker.get_paid_playing_count()
    match_id = matchmaker.user_to_match.get(int(user_id))
    active_match = None
    if match_id:
        m = matchmaker.get_match(match_id)
        if m and m.status not in ["completed", "cancelled"]:
            active_match = m.to_dict()
        
    return jsonify({
        "user": {
            "user_id": user.get("_id"),
            "username": user.get("username", ""),
            "first_name": user.get("first_name", ""),
            "balance": round(user.get("balance", 0.0), 2),
            "streak": user.get("streak", 0),
            "last_streak_claim": user.get("last_streak_claim").isoformat() if user.get("last_streak_claim") else None,
            "referrals_count": user.get("referrals_count", 0),
            "referral_claimed": user.get("referral_claimed", []),
            "daily_missions": user.get("daily_missions", {}),
            "is_banned": user.get("is_banned", False),
            "tasks_completed_today": completed_today,
            "pending_deposits": pending_deposits,
            "pending_redeems": pending_redeems,
            "free_fire_username": user.get("free_fire_username", ""),
            "free_fire_uid": user.get("free_fire_uid", ""),
            "active_ff_room": active_ff_room,
            "joined_channel": joined_channel
        },
        "active_users": active_count,
        "paid_playing": paid_playing,
        "active_match": active_match
    }), 200

@app.route("/api/leaderboard/<user_id>", methods=["GET"])
def get_leaderboard_api(user_id):
    """Retrieve top 3 and current user rank."""
    leaderboard = get_leaderboard()
    user_rank = get_user_rank(user_id)
    return jsonify({
        "leaderboard": leaderboard,
        "user_rank": user_rank
    }), 200

@app.route("/api/task/<user_id>", methods=["GET", "POST"])
def manage_task_api(user_id):
    user_id = int(user_id)
    from tasks.shortener import create_or_get_task, tasks_col, IST
    from datetime import datetime
    
    if request.method == "POST":
        task, status = create_or_get_task(user_id)
        if not task:
            if status == "limit_reached":
                return jsonify({"error": "Daily limit reached. 4 tasks will be available tomorrow."}), 400
            return jsonify({"error": "Failed to create task."}), 500
        return jsonify({
            "success": True, 
            "task": {
                "id": task["_id"],
                "shortened_url": task["shortened_url"],
                "status": task["status"]
            }
        }), 200
        
    # GET: check if there is an active ongoing task
    now = datetime.now(IST)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = tasks_col.count_documents({
        "user_id": user_id,
        "status": "completed",
        "completed_at": {"$gte": start_of_today}
    })
    
    ongoing_task = tasks_col.find_one({
        "user_id": user_id,
        "status": "ongoing"
    })
    
    task_data = None
    if ongoing_task:
        task_data = {
            "id": ongoing_task["_id"],
            "shortened_url": ongoing_task["shortened_url"],
            "status": ongoing_task["status"]
        }
        
    return jsonify({
        "completed_today": completed_today,
        "active_task": task_data
    }), 200

@app.route("/api/history/<user_id>", methods=["GET"])
def get_history_api(user_id):
    """Retrieve transaction and match logs."""
    tx_history = get_transaction_history(user_id)
    match_history = get_match_history(user_id)
    
    from database import to_ist
    # Format BSON ObjectId and datetime
    formatted_txs = []
    for tx in tx_history:
        formatted_txs.append({
            "id": str(tx["_id"]),
            "type": tx["type"],
            "amount": tx["amount"],
            "status": tx["status"],
            "details": tx.get("details", {}),
            "created_at": to_ist(tx["created_at"]).isoformat()
        })
        
    formatted_matches = []
    for m in match_history:
        formatted_matches.append({
            "id": m["_id"],
            "player_a": m["player_a"],
            "player_b": m["player_b"],
            "type": m["type"],
            "winner_id": m["winner_id"],
            "score_a": m["score_a"],
            "score_b": m["score_b"],
            "created_at": to_ist(m["created_at"]).isoformat()
        })
        
    return jsonify({
        "transactions": formatted_txs,
        "matches": formatted_matches
    }), 200

@app.route("/api/claim-streak/<user_id>", methods=["POST"])
def claim_streak_api(user_id):
    success, res = claim_daily_streak(user_id)
    if success:
        return jsonify({"success": True, "streak": res["streak"], "reward": res["reward"]}), 200
    else:
        return jsonify({"success": False, "error": res}), 400

@app.route("/api/claim-mission/<user_id>", methods=["POST"])
def claim_mission_api(user_id):
    data = request.json or {}
    mission_key = data.get("mission_key")
    if not mission_key:
        return jsonify({"error": "Missing mission_key"}), 400
        
    success, res = claim_daily_mission(user_id, mission_key)
    if success:
        return jsonify({"success": True, "reward": res}), 200
    else:
        return jsonify({"success": False, "error": res}), 400

@app.route("/api/claim-referral/<user_id>", methods=["POST"])
def claim_referral_api(user_id):
    data = request.json or {}
    tier = data.get("tier")
    if not tier:
        return jsonify({"error": "Missing tier"}), 400
        
    success, res = claim_referral_reward(user_id, str(tier))
    if success:
        return jsonify({"success": True, "reward": res}), 200
    else:
        return jsonify({"success": False, "error": res}), 400

@app.route("/api/feedback/<user_id>", methods=["POST"])
def submit_feedback_api(user_id):
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    data = request.json or {}
    selected_games = data.get("selected_games", [])
    other_game = data.get("other_game", "")
    likes_game = data.get("likes_game", "")

    # Save to database
    save_feedback(user_id, selected_games, other_game, likes_game)

    return jsonify({"success": True, "message": "Feedback submitted successfully"})

@app.route("/api/user/profile/<user_id>", methods=["POST"])
def update_profile_route(user_id):
    data = request.json or {}
    ff_username = data.get("free_fire_username", "").strip()
    ff_uid = data.get("free_fire_uid", "").strip()
    
    if not ff_username or not ff_uid:
        return jsonify({"success": False, "error": "Both Free Fire Username and UID are required."}), 400
        
    update_free_fire_profile(user_id, ff_username, ff_uid)
    return jsonify({"success": True, "message": "Profile updated successfully"}), 200

@app.route("/api/car-game/events", methods=["GET"])
def get_car_events_route():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    try:
        events = get_active_car_event_cycles(user_id)
        return jsonify({"success": True, "events": events}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/car-game/join", methods=["POST"])
def join_car_event_route():
    data = request.json or {}
    user_id = data.get("user_id")
    event_id = data.get("event_id")
    if not user_id or not event_id:
        return jsonify({"success": False, "error": "Missing user_id or event_id"}), 400
    
    success, res = join_car_event(user_id, event_id)
    if success:
        return jsonify({"success": True, "data": res}), 200
    else:
        return jsonify({"success": False, "error": res}), 400

@app.route("/api/car-game/submit-score", methods=["POST"])
def submit_car_score_route():
    data = request.json or {}
    user_id = data.get("user_id")
    event_id = data.get("event_id")
    score = data.get("score")
    if user_id is None or event_id is None or score is None:
        return jsonify({"success": False, "error": "Missing user_id, event_id, or score"}), 400
        
    success, msg = submit_car_score(user_id, event_id, score)
    if success:
        return jsonify({"success": True, "message": msg}), 200
    else:
        return jsonify({"success": False, "error": msg}), 400

@app.route("/api/free-fire/events", methods=["GET"])
def get_free_fire_events_route():
    try:
        events = get_free_fire_events()
        return jsonify({"success": True, "events": events}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/free-fire/join", methods=["POST"])
def join_free_fire_event_route():
    data = request.json or {}
    user_id = data.get("user_id")
    event_id = data.get("event_id")
    slot_number = data.get("slot_number")
    if not user_id or not event_id or not slot_number:
        return jsonify({"success": False, "error": "Missing user_id, event_id, or slot_number"}), 400
        
    success, res = join_free_fire_event(user_id, event_id, slot_number)
    if success:
        return jsonify({"success": True, "message": res}), 200
    else:
        if res == "profile_incomplete":
            return jsonify({"success": False, "error": "profile_incomplete", "message": "Please configure your Free Fire username and UID in your Profile to join."}), 400
        return jsonify({"success": False, "error": res, "message": res}), 400


# --- SOCKET.IO EVENT HANDLERS ---

@socketio.on("connect")
def handle_connect():
    # Pass user_id via query parameters: socket.connect({query: "userId=123"})
    user_id = request.args.get("userId")
    if user_id:
        try:
            uid = int(user_id)
            connected_users[uid] = request.sid
            
            # Cancel offline timer if user was in an active match and disconnected
            if uid in offline_timers:
                offline_timers[uid].cancel()
                offline_timers.pop(uid, None)
                
            # If user has an active match, join room automatically
            match_id = matchmaker.user_to_match.get(uid)
            if match_id:
                match = matchmaker.get_match(match_id)
                if match and match.status not in ["completed", "cancelled"]:
                    join_room(match_id)
                    # Reset offline status
                    player = match.get_player(uid)
                    if player:
                        player["is_offline"] = False
                    
                    # Notify room about rejoin
                    emit("player_rejoined", {"userId": uid}, to=match_id)
                    # Resend latest match status to the entire room to sync online state
                    socketio.emit("match_update", match.to_dict(), to=match_id)
                    # Also send directly to the connecting client's session ID to guarantee update
                    emit("match_update", match.to_dict(), to=request.sid)
        except ValueError:
            pass

@socketio.on("disconnect")
def handle_disconnect():
    # Find user_id from request.sid
    uid = None
    for k, v in list(connected_users.items()):
        if v == request.sid:
            uid = k
            break
            
    if uid:
        connected_users.pop(uid, None)
        
        # Remove from matchmaking queue
        matchmaker.remove_from_queue(uid)
        
        # If user was in an active match, start 20s forfeit timer
        match_id = matchmaker.user_to_match.get(uid)
        if match_id:
            match = matchmaker.get_match(match_id)
            if match and match.status not in ["completed", "cancelled"] and match.type != "free":
                player = match.get_player(uid)
                if player:
                    player["is_offline"] = True
                
                # Notify opponent and room about the offline state
                socketio.emit("match_update", match.to_dict(), to=match_id)
                emit("player_offline", {"userId": uid, "countdown": 20}, to=match_id)
                
                # Start timer
                t = threading.Timer(20.0, run_forfeit_timeout, args=[match_id, uid])
                offline_timers[uid] = t
                t.start()

def run_forfeit_timeout(match_id, user_id):
    """Triggered after 20s offline. Forfeits match in favor of opponent."""
    match = matchmaker.get_match(match_id)
    if match and match.status not in ["completed", "cancelled"]:
        match.handle_player_forfeit(user_id)
        
        # Resolve payout
        process_match_payout(match)
        
        # Emit update
        socketio.emit("match_update", match.to_dict(), to=match_id)
        matchmaker.clean_completed_match(match_id)

@socketio.on("join_matchmaking")
def handle_join_matchmaking(data):
    user_id = int(data["userId"])
    username = data["username"]
    
    # Verify user wallet balance
    user = get_user(user_id)
    if not user or user.get("is_banned"):
        emit("matchmaking_error", {"message": "User is banned or not registered."})
        return
        
    if user.get("balance", 0.0) < 5.0:
        emit("matchmaking_error", {"message": "Insufficient balance! Paid match requires Rs 5.00."})
        return
        
    # Deduct match fee (Rs 5.00) atomically
    success, new_bal = update_balance(user_id, -5.0, "match_fee", {"status": "matchmaking"})
    if not success:
        emit("matchmaking_error", {"message": "Failed to lock match fee."})
        return
        
    # Add to matchmaking
    res = matchmaker.add_to_queue(user_id, username, request.sid)
    
    if res["status"] == "already_in_match":
        # Refund fee
        update_balance(user_id, 5.0, "match_refund", {"reason": "Already in match"})
        emit("match_update", matchmaker.get_match(res["match_id"]).to_dict())
        return
        
    if res["status"] == "matched":
        match_id = res["match_id"]
        join_room(match_id)
        
        # Join opponent socket to room
        opp_sid = res["opponent_socket_id"]
        socketio.server.enter_room(opp_sid, match_id)
        
        # Update match status to matchmaking database if needed (keep in memory)
        match = matchmaker.get_match(match_id)
        emit("match_found", match.to_dict(), to=match_id)
        
        # Start timer for toss decision
        start_ball_timer(match_id, match.current_inning, match.current_ball)
        
    elif res["status"] == "queued":
        emit("matchmaking_queued")
        # Start a thread to check fallback to Bot after 6 seconds
        t = threading.Timer(6.0, trigger_bot_fallback, args=[user_id])
        matchmaking_timers[user_id] = t
        t.start()

def trigger_bot_fallback(user_id):
    """Triggered after 6s waiting in queue. Matches with bot."""
    matchmaker_timers = matchmaking_timers.pop(user_id, None)
    match = matchmaker.check_bot_fallback(user_id)
    if match:
        # Join user socket to room
        sid = connected_users.get(user_id)
        if sid:
            socketio.server.enter_room(sid, match.match_id)
            socketio.emit("match_found", match.to_dict(), to=sid)
            start_ball_timer(match.match_id, match.current_inning, match.current_ball)

@socketio.on("cancel_matchmaking")
def handle_cancel_matchmaking(data):
    user_id = int(data["userId"])
    
    # Cancel timer
    t = matchmaking_timers.pop(user_id, None)
    if t:
        t.cancel()
        
    # Remove from queue
    matchmaker.remove_from_queue(user_id)
    # Refund match fee
    update_balance(user_id, 5.0, "match_refund", {"reason": "Cancelled by user"})
    emit("matchmaking_cancelled")

@socketio.on("join_challenge")
def handle_join_challenge(data):
    user_id = int(data["userId"])
    match_id = data["matchId"]
    
    match = matchmaker.get_match(match_id)
    if match:
        join_room(match_id)
        emit("match_update", match.to_dict())

@socketio.on("create_challenge_match")
def handle_create_challenge_match(data):
    user_id = int(data["userId"])
    username = data["username"]
    
    match = matchmaker.create_challenge_match(user_id, username)
    join_room(match.match_id)
    emit("match_update", match.to_dict())

@socketio.on("cancel_challenge_match")
def handle_cancel_challenge_match(data):
    match_id = data["matchId"]
    match = matchmaker.get_match(match_id)
    if match and match.status == "waiting":
        match.status = "cancelled"
        socketio.emit("match_update", match.to_dict(), to=match_id)
        matchmaker.clean_completed_match(match_id)

@socketio.on("forfeit_match")
def handle_forfeit_match(data):
    match_id = data["matchId"]
    user_id = int(data["userId"])
    
    match = matchmaker.get_match(match_id)
    if match and match.status not in ["completed", "cancelled"]:
        cancel_ball_timer(match_id)
        match.handle_player_forfeit(user_id)
        process_match_payout(match)
        socketio.emit("match_update", match.to_dict(), to=match_id)
        matchmaker.clean_completed_match(match_id)

@socketio.on("choose_toss_side")
def handle_choose_toss_side(data):
    match_id = data["matchId"]
    user_id = int(data["userId"])
    choice = data["choice"] # head, tail
    
    match = matchmaker.get_match(match_id)
    if not match:
        return
        
    success, res = match.select_toss_coin(user_id, choice)
    if success:
        # Cancel current ball/toss timer
        cancel_ball_timer(match_id)
        emit("match_update", match.to_dict(), to=match_id)
        
        # If human won toss, start timer for option choice. If bot won toss, it automatically selected batting/bowling
        if match.status == "toss":
            # Wait for option selection from toss winner
            start_ball_timer(match_id, match.current_inning, match.current_ball)
        else:
            # Match transitioned to batting_1
            start_ball_timer(match_id, match.current_inning, match.current_ball)

@socketio.on("choose_toss_option")
def handle_choose_toss_option(data):
    match_id = data["matchId"]
    user_id = int(data["userId"])
    option = data["choice"] # batting, bowling
    
    match = matchmaker.get_match(match_id)
    if not match:
        return
        
    success, res = match.select_toss_option(user_id, option)
    if success:
        cancel_ball_timer(match_id)
        emit("match_update", match.to_dict(), to=match_id)
        start_ball_timer(match_id, match.current_inning, match.current_ball)

@socketio.on("submit_choice")
def handle_submit_choice(data):
    match_id = data["matchId"]
    user_id = int(data["userId"])
    choice = int(data["choice"]) # 1 to 6
    
    match = matchmaker.get_match(match_id)
    if not match:
        return
        
    success, res = match.make_choice(user_id, choice)
    if success:
        # Reset 6s timer since choices are resolved
        if match.player_a["current_choice"] is None and match.player_b["current_choice"] is None:
            cancel_ball_timer(match_id)
            
            emit("match_update", match.to_dict(), to=match_id)
            
            if match.status == "completed":
                # Handle payouts and database updates
                process_match_payout(match)
                emit("match_update", match.to_dict(), to=match_id)
                matchmaker.clean_completed_match(match_id)
            else:
                start_ball_timer(match_id, match.current_inning, match.current_ball)
        else:
            # Send status update just for choice indicator (user choice made, waiting for opponent)
            emit("match_update", match.to_dict(), to=match_id)

def process_match_payout(match):
    """Processes payouts and logs completed match details."""
    if match.type == "paid":
        winner_id = match.winner_id
        
        # Winner Payout (Rs 8.50)
        if winner_id not in ["draw", "bot"]:
            update_balance(winner_id, 8.50, "match_win", {"match_id": match.match_id})
        elif winner_id == "draw":
            # Refund both players Rs 5.00
            if match.player_a["user_id"] != "bot":
                update_balance(match.player_a["user_id"], 5.00, "match_refund", {"match_id": match.match_id, "reason": "Match Draw"})
            if match.player_b["user_id"] != "bot":
                update_balance(match.player_b["user_id"], 5.00, "match_refund", {"match_id": match.match_id, "reason": "Match Draw"})
                
        # Save results in Database
        save_match_result(
            match_id=match.match_id,
            player_a_data=match.player_a,
            player_b_data=match.player_b,
            match_type="paid",
            winner_id=winner_id,
            score_a=match.player_a["score"],
            score_b=match.player_b["score"]
        )
    else:
        # Free match / Challenge
        save_match_result(
            match_id=match.match_id,
            player_a_data=match.player_a,
            player_b_data=match.player_b,
            match_type="challenge",
            winner_id=match.winner_id,
            score_a=match.player_a["score"],
            score_b=match.player_b["score"]
        )

# --- TIMEOUT BALL TIMERS (10 SECONDS FOR CHOICE) ---

bot_choice_timers = {}

def start_ball_timer(match_id, inning, ball_num):
    """Initialize a 10-second timer for player choices plus 3s delay and grace."""
    cancel_ball_timer(match_id)
    t = threading.Timer(13.5, run_ball_timeout, args=[match_id, inning, ball_num])
    ball_timers[match_id] = t
    t.start()
    
    # Delayed bot choices
    match = matchmaker.get_match(match_id)
    if match and (match.player_a["user_id"] == "bot" or match.player_b["user_id"] == "bot"):
        if match.status in ["batting_1", "batting_2"]:
            bt = threading.Timer(5.0, run_bot_choice, args=[match_id, inning, ball_num])
            bot_choice_timers[match_id] = bt
            bt.start()

def cancel_ball_timer(match_id):
    t = ball_timers.pop(match_id, None)
    if t:
        t.cancel()
    bt = bot_choice_timers.pop(match_id, None)
    if bt:
        bt.cancel()

def run_bot_choice(match_id, inning, ball_num):
    match = matchmaker.get_match(match_id)
    if not match or match.status not in ["batting_1", "batting_2"] or match.winner_id:
        return
    if match.current_inning != inning or match.current_ball != ball_num:
        return
        
    bot_player = match.player_b if match.player_b["user_id"] == "bot" else match.player_a
    if bot_player["current_choice"] is None:
        bot_player["current_choice"] = match.get_smart_bot_choice()
        
        # Check if human player has already chosen
        human_player = match.player_a if match.player_b["user_id"] == "bot" else match.player_b
        if human_player["current_choice"] is not None:
            # Both choices are ready, process ball
            cancel_ball_timer(match_id)
            match.process_ball()
            socketio.emit("match_update", match.to_dict(), to=match_id)
            
            if match.status == "completed":
                process_match_payout(match)
                socketio.emit("match_update", match.to_dict(), to=match_id)
                matchmaker.clean_completed_match(match_id)
            else:
                start_ball_timer(match_id, match.current_inning, match.current_ball)
        else:
            # Emit update to show bot choice selected (Opp Choice: Selected)
            socketio.emit("match_update", match.to_dict(), to=match_id)

# Dynamic bindings to resolve circular imports
matchmaker.socketio = socketio
matchmaker.start_ball_timer = start_ball_timer

def run_ball_timeout(match_id, inning, ball_num):
    """Runs when 6s choice timer expires."""
    match = matchmaker.get_match(match_id)
    if not match or match.status not in ["toss", "batting_1", "batting_2"]:
        return
        
    # Ensure match has not progressed past this ball
    if match.current_inning != inning or match.current_ball != ball_num:
        return
        
    # Process choice timeout
    if match.status == "toss":
        # Toss timeout - auto select for the pending player
        if match.toss_choice_pending:
            # Toss selector timed out, pick randomly
            choice = "head" if match.is_bot_turn_for_toss() else random.choice(["head", "tail"])
            match.select_toss_coin(match.toss_selector, choice)
        else:
            # Toss option selection timed out
            match.select_toss_option(match.toss_winner, "batting")
            
        socketio.emit("match_update", match.to_dict(), to=match_id)
        start_ball_timer(match_id, match.current_inning, match.current_ball)
        
    else:
        # Batting choice timeout
        # Handle ball timeouts
        match.handle_ball_timeout(None)
        socketio.emit("match_update", match.to_dict(), to=match_id)
        
        if match.status == "completed":
            process_match_payout(match)
            socketio.emit("match_update", match.to_dict(), to=match_id)
            matchmaker.clean_completed_match(match_id)
        else:
            start_ball_timer(match_id, match.current_inning, match.current_ball)

# --- WEB APP TRIGGERED PORTALS TO BOT CHAT ---
@socketio.on("request_deposit_from_webapp")
def handle_request_deposit_from_webapp(data):
    user_id = int(data["userId"])
    amount = int(data["amount"])
    
    # Import locally to prevent potential import order issues
    from bot.handlers import user_states, clean_send as bot_clean_send
    
    # Store user state waiting for txn ID
    user_states[user_id] = {"action": "wait_deposit_txn", "amount": amount}
    
    # Build instructions & QR
    upi_uri = f"upi://pay?pa={Config.ADMIN_UPI}&pn=HandCricketAdmin&am={amount}&cu=INR"
    import urllib.parse
    encoded_upi = urllib.parse.quote(upi_uri)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_upi}"
    
    instruction_text = (
        f"🪙 **Deposit Package: Rs {amount}**\n\n"
        f"1. Scan the QR code shown below or pay directly to the UPI ID:\n"
        f"UPI ID: `{Config.ADMIN_UPI}`\n"
        f"Amount: **Rs {amount}**\n\n"
        f"2. Complete payment in your UPI app.\n"
        f"3. Copy the **Transaction ID / Ref No.** and send it here in the chat.\n\n"
        f"⚖️ *Admin approval is required to credit your account.*"
        f"<a href=\"{qr_url}\">&#8205;</a>"
    )
    
    # Send message using the Pyrogram loop safely
    import asyncio
    async def send_msg():
        await bot_clean_send(bot_client, user_id, instruction_text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="main_menu")
        ]]))
        
    asyncio.run_coroutine_threadsafe(send_msg(), bot_client.loop)

@socketio.on("request_redeem_from_webapp")
def handle_request_redeem_from_webapp(data):
    user_id = int(data["userId"])
    
    # Import locally
    from bot.handlers import clean_send as bot_clean_send
    from bot.keyboards import get_redeem_coin_keyboard
    
    user = get_user(user_id)
    balance = user.get("balance", 0.0) if user else 0.0
    
    redeem_text = (
        f"➖ **Redeem Coins**\n\n"
        f"Current Wallet Balance: **Rs {balance:.2f}**\n\n"
        f"Select a redemption package:"
    )
    
    import asyncio
    async def send_msg():
        await bot_clean_send(bot_client, user_id, redeem_text, reply_markup=get_redeem_coin_keyboard())
        
    asyncio.run_coroutine_threadsafe(send_msg(), bot_client.loop)

# --- WEB APP IN-APP DEPOSIT & REDEEM AND CHALLENGE BY CODE AND FREE BOT MATCH ---

@socketio.on("submit_deposit_from_webapp")
def handle_submit_deposit_from_webapp(data):
    user_id = int(data["userId"])
    amount = int(data["amount"])
    upi_txn_id = data["upiTxnId"].strip()
    
    if not upi_txn_id:
        return {"status": "error", "message": "UPI Transaction ID is required."}
        
    from database import create_transaction, get_user
    tx_id = create_transaction(
        user_id=user_id,
        tx_type="deposit",
        amount=amount,
        status="pending",
        details={"txn_id": upi_txn_id}
    )
    
    # Notify Admin
    user = get_user(user_id)
    first_name = user.get("first_name", "Web User") if user else "Web User"
    username = user.get("username", "") if user else ""
    username_str = f" (@{username})" if username else ""
    
    admin_msg = (
        f"📥 **New Deposit Request (via WebApp)!**\n\n"
        f"User: {first_name}{username_str}\n"
        f"User ID: `{user_id}`\n"
        f"Amount: **Rs {amount}**\n"
        f"Txn ID: `{upi_txn_id}`"
    )
    
    from bot.keyboards import get_admin_action_keyboard
    import asyncio
    async def send_admin_msg():
        try:
            await bot_client.send_message(
                Config.ADMIN_ID,
                admin_msg,
                reply_markup=get_admin_action_keyboard(str(tx_id), "deposit")
            )
        except Exception as e:
            print(f"Failed to notify admin: {e}")
            
    asyncio.run_coroutine_threadsafe(send_admin_msg(), bot_client.loop)
    
    async def send_user_msg():
        try:
            await bot_client.send_message(
                user_id,
                f"✅ **Deposit Submitted via WebApp!**\n\n"
                f"Deposit of **Rs {amount}** with Txn ID `{upi_txn_id}` is pending admin verification.\n"
                f"You will receive a notification as soon as it is approved."
            )
        except Exception as e:
            print(f"Failed to notify user: {e}")
            
    asyncio.run_coroutine_threadsafe(send_user_msg(), bot_client.loop)
    
    return {"status": "success", "message": "Deposit request submitted successfully!"}

@socketio.on("submit_redeem_from_webapp")
def handle_submit_redeem_from_webapp(data):
    user_id = int(data["userId"])
    amount = int(data["amount"])
    upi_or_mobile = data["upiOrMobile"].strip()
    
    if not upi_or_mobile:
        return {"status": "error", "message": "UPI ID or Mobile Number is required."}
        
    from database import get_user, update_balance, transactions_col
    user = get_user(user_id)
    if not user or user.get("balance", 0.0) < amount:
        return {"status": "error", "message": f"Insufficient balance. Available balance is Rs {user.get('balance', 0.0):.2f}"}
        
    success, new_bal = update_balance(user_id, -amount, "redeem", {"upi_or_mobile": upi_or_mobile})
    if not success:
        return {"status": "error", "message": "Failed to process transaction. Insufficient balance."}
        
    tx = transactions_col.find_one(
        {"user_id": user_id, "status": "pending", "type": "redeem"},
        sort=[("created_at", -1)]
    )
    tx_id = tx["_id"] if tx else "unknown"
    
    # Notify Admin
    first_name = user.get("first_name", "Web User")
    username = user.get("username", "")
    username_str = f" (@{username})" if username else ""
    
    admin_msg = (
        f"📤 **New Withdrawal Request (via WebApp)!**\n\n"
        f"User: {first_name}{username_str}\n"
        f"User ID: `{user_id}`\n"
        f"Redeem Pack: **Rs {amount}**\n"
        f"Withdrawal Destination: `{upi_or_mobile}`"
    )
    
    from bot.keyboards import get_admin_action_keyboard
    import asyncio
    async def send_admin_msg():
        try:
            await bot_client.send_message(
                Config.ADMIN_ID,
                admin_msg,
                reply_markup=get_admin_action_keyboard(str(tx_id), "redeem")
            )
        except Exception as e:
            print(f"Failed to notify admin: {e}")
            
    asyncio.run_coroutine_threadsafe(send_admin_msg(), bot_client.loop)
    
    async def send_user_msg():
        try:
            await bot_client.send_message(
                user_id,
                f"✅ **Withdrawal Request Registered via WebApp!**\n\n"
                f"Request to redeem **Rs {amount}** to `{upi_or_mobile}` is pending admin transfer.\n"
                f"Once processed, you will get a notification."
            )
        except Exception as e:
            print(f"Failed to notify user: {e}")
            
    asyncio.run_coroutine_threadsafe(send_user_msg(), bot_client.loop)
    
    return {"status": "success", "message": "Withdrawal request submitted successfully!"}

@socketio.on("play_with_bot_free")
def handle_play_with_bot_free(data):
    user_id = int(data["userId"])
    username = data["username"]
    
    if user_id in matchmaker.user_to_match:
        existing_match_id = matchmaker.user_to_match[user_id]
        match = matchmaker.get_match(existing_match_id)
        if match and match.status != "completed":
            join_room(existing_match_id)
            emit("match_update", match.to_dict())
            return
            
    match = HandCricketMatch(
        player_a_id=user_id,
        player_a_name=username,
        player_b_id="bot",
        player_b_name="Smart Bot",
        match_type="free"
    )
    match.status = "toss"
    match.toss_selector = user_id
    match.toss_choice_pending = True
    
    matchmaker.active_matches[match.match_id] = match
    matchmaker.user_to_match[user_id] = match.match_id
    
    join_room(match.match_id)
    emit("match_found", match.to_dict())
    start_ball_timer(match.match_id, match.current_inning, match.current_ball)

@socketio.on("join_challenge_by_code")
def handle_join_challenge_by_code(data):
    user_id = int(data["userId"])
    username = data["username"]
    code = data["code"].strip()
    
    match, err = matchmaker.join_challenge_by_code(code, user_id, username)
    if err:
        return {"status": "error", "message": err}
        
    join_room(match.match_id)
    socketio.emit("match_update", match.to_dict(), to=match.match_id)
    start_ball_timer(match.match_id, match.current_inning, match.current_ball)
    
    # Notify host in bot chat
    import asyncio
    async def send_host_msg():
        try:
            host_id = match.player_a["user_id"]
            await bot_client.send_message(
                host_id,
                f"🤝 {username} joined your challenge via code! The match is starting."
            )
        except Exception:
            pass
    asyncio.run_coroutine_threadsafe(send_host_msg(), bot_client.loop)
    
    return {"status": "success", "match": match.to_dict()}

# --- REMATCH MECHANISM ---
@socketio.on("request_rematch")
def handle_request_rematch(data):
    match_id = data["matchId"]
    user_id = int(data["userId"])
    
    match = matchmaker.get_match(match_id)
    if not match or match.status != "completed":
        return
        
    if user_id not in match.rematch_requests:
        match.rematch_requests.append(user_id)
        
    # Check if playing against bot (rematch starts immediately!)
    if match.player_b["user_id"] == "bot":
        # Create a new match fee deduction if it is paid
        if match.type == "paid":
            user = get_user(user_id)
            if not user or user["balance"] < 5.0:
                emit("matchmaking_error", {"message": "Insufficient balance for rematch!"})
                return
            update_balance(user_id, -5.0, "match_fee", {"details": "Rematch fee"})
            
        new_match = HandCricketMatch(
            player_a_id=user_id,
            player_a_name=match.player_a["username"] if match.player_a["user_id"] == user_id else match.player_b["username"],
            player_b_id="bot",
            player_b_name="Smart Bot",
            match_type=match.type
        )
        matchmaker.active_matches[new_match.match_id] = new_match
        matchmaker.user_to_match[user_id] = new_match.match_id
        
        # Join user to new room
        socketio.server.enter_room(request.sid, new_match.match_id)
        
        emit("rematch_started", new_match.to_dict(), to=request.sid)
        start_ball_timer(new_match.match_id, new_match.current_inning, new_match.current_ball)
        return

    # If two humans
    if len(match.rematch_requests) == 2:
        # Swap host and guest
        host_id = match.player_a["user_id"]
        guest_id = match.player_b["user_id"]
        
        # Deduct fees if paid match
        if match.type == "paid":
            for u_id in [host_id, guest_id]:
                user = get_user(u_id)
                if not user or user["balance"] < 5.0:
                    # Cancel rematch
                    socketio.emit("rematch_failed", {"message": "One of the players has insufficient balance."}, to=match_id)
                    return
            update_balance(host_id, -5.0, "match_fee", {"details": "Rematch fee"})
            update_balance(guest_id, -5.0, "match_fee", {"details": "Rematch fee"})

        new_match = HandCricketMatch(
            player_a_id=guest_id,  # Swap roles
            player_a_name=match.player_b["username"],
            player_b_id=host_id,
            player_b_name=match.player_a["username"],
            match_type=match.type
        )
        matchmaker.active_matches[new_match.match_id] = new_match
        matchmaker.user_to_match[host_id] = new_match.match_id
        matchmaker.user_to_match[guest_id] = new_match.match_id
        
        # Join both players to the new room
        for u_id in [host_id, guest_id]:
            sid = connected_users.get(u_id)
            if sid:
                socketio.server.enter_room(sid, new_match.match_id)
                
        socketio.emit("rematch_started", new_match.to_dict(), to=match_id)
        start_ball_timer(new_match.match_id, new_match.current_inning, new_match.current_ball)
    else:
        # Notify opponent of request
        opp = match.get_opponent(user_id)
        if opp and opp["user_id"] != "bot":
            opp_sid = connected_users.get(opp["user_id"])
            if opp_sid:
                socketio.emit("rematch_requested", {"userId": user_id}, to=opp_sid)


# --- LAUNCH BOT CLIENT IN BACKGROUND THREAD ---
def run_bot():
    """Start Pyrogram bot loop without signal registration in secondary threads."""
    import asyncio
    print("Starting Telegram Bot loop...")
    asyncio.set_event_loop(bot_loop)
    
    # Call start() directly. Pyrogram runs the coroutine internally on the set event loop.
    bot_client.start()
    
    try:
        bot_loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        bot_client.stop()

if __name__ == "__main__":
    # Start Telegram Bot in a separate daemon thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Start Flask Webserver + SocketIO
    port = Config.PORT
    print(f"Starting Flask-SocketIO server on port {port}...")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
