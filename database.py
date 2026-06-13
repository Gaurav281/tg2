from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))

def to_ist(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)
from config import Config

# Initialize MongoDB client
client = MongoClient(Config.MONGO_URI)
db = client.get_database()

# Collections
users_col = db["users"]
transactions_col = db["transactions"]
matches_col = db["matches"]
tasks_col = db["tasks"]
task_stats_col = db["task_stats"]
feedbacks_col = db["feedbacks"]
car_event_cycles_col = db["car_event_cycles"]
free_fire_events_col = db["free_fire_events"]
cricket_event_cycles_col = db["cricket_event_cycles"]
import string
import random

def generate_unique_id():
    chars = string.ascii_uppercase + string.digits
    while True:
        uid = "BP" + "".join(random.choices(chars, k=6))
        # Ensure it's not already in use
        if not db["users"].find_one({"unique_id": uid}):
            return uid

def get_user(user_id):
    """Retrieve user details by Telegram user_id."""
    user = users_col.find_one({"_id": int(user_id)})
    if user:
        # Guarantee balance and unique_id fields exist for legacy compatibility
        changed = False
        if "unique_id" not in user:
            user["unique_id"] = generate_unique_id()
            changed = True
        if "deposit_balance" not in user:
            user["deposit_balance"] = round(user.get("balance", 0.0) - user.get("winning_balance", 0.0), 2)
            changed = True
        if "winning_balance" not in user:
            user["winning_balance"] = 0.0
            changed = True
        if changed:
            users_col.update_one(
                {"_id": int(user_id)},
                {"$set": {
                    "unique_id": user["unique_id"],
                    "deposit_balance": round(max(0.0, user["deposit_balance"]), 2),
                    "winning_balance": round(max(0.0, user["winning_balance"]), 2)
                }}
            )
    return user

def create_user(user_id, username, first_name, referred_by=None):
    """Create a new user with 0.5 signup bonus and process referral link if any."""
    user_id = int(user_id)
    if get_user(user_id):
        return None  # User already exists

    # Check if inviter exists
    inviter_id = None
    if referred_by:
        try:
            referred_by = int(referred_by)
            if referred_by != user_id and get_user(referred_by):
                inviter_id = referred_by
        except ValueError:
            pass

    user_doc = {
        "_id": user_id,
        "unique_id": generate_unique_id(),
        "username": username or f"User_{user_id}",
        "first_name": first_name or "",
        "balance": 0.0,
        "deposit_balance": 0.0,
        "winning_balance": 0.0,
        "free_fire_username": "",
        "free_fire_uid": "",
        "streak": 0,
        "last_streak_claim": None,
        "referred_by": inviter_id,
        "referrals_count": 0,
        "referral_claimed": [],  # list of strings: "1", "5", "10"
        "daily_missions": {
            "date": datetime.now(IST).strftime("%Y-%m-%d"),
            "matches_played": 0,
            "balance_added": 0.0,
            "max_score": 0,
            "claimed": {
                "matches_3": False,
                "add_balance": False
            }
        },
        "is_banned": False,
        "created_at": datetime.now(IST)
    }
    
    users_col.insert_one(user_doc)

    
    # Update referral count for inviter
    if inviter_id:
        users_col.update_one(
            {"_id": inviter_id},
            {"$inc": {"referrals_count": 1}}
        )
        # Log referral transaction (informational or referral bonuses can be claimed in rewards tab)
        
    return user_doc

def update_balance(user_id, amount, tx_type, details=None):
    """
    Atomically update user balance (total, deposit, winning) and log a transaction.
    If amount is negative, checks that the user has sufficient balance (no overdraft).
    Returns (success: bool, new_balance: float).
    """
    import time
    user_id = int(user_id)
    amount = round(float(amount), 2)
    
    if amount == 0:
        user = get_user(user_id)
        return True, user["balance"] if user else 0.0

    # Retrieve current user to perform calculations
    user = get_user(user_id)
    if not user:
        return False, 0.0

    deposit_bal = round(user.get("deposit_balance", 0.0), 2)
    winning_bal = round(user.get("winning_balance", 0.0), 2)
    total_bal = round(user.get("balance", 0.0), 2)

    # Check case by case
    if amount < 0:
        deduct_amt = abs(amount)
        if tx_type == "redeem":
            # For withdrawals, user can ONLY withdraw from winning_balance
            if winning_bal < deduct_amt:
                return False, 0.0
            
            # Atomically update by checking winning_balance in query
            new_winning = round(winning_bal - deduct_amt, 2)
            new_total = round(total_bal - deduct_amt, 2)
            
            query = {"_id": user_id, "winning_balance": {"$gte": deduct_amt}}
            update = {"$set": {
                "winning_balance": new_winning,
                "balance": new_total
            }}
        else:
            # For other fees (match_fee, car_event_fee, free_fire_fee, admin_remove)
            if total_bal < deduct_amt:
                return False, 0.0
                
            # Deduct from deposit_balance first, then winning_balance
            if deposit_bal >= deduct_amt:
                new_deposit = round(deposit_bal - deduct_amt, 2)
                new_winning = winning_bal
            else:
                remainder = round(deduct_amt - deposit_bal, 2)
                new_deposit = 0.0
                new_winning = round(winning_bal - remainder, 2)
                
            new_total = round(total_bal - deduct_amt, 2)
            
            query = {"_id": user_id, "balance": {"$gte": deduct_amt}}
            update = {"$set": {
                "deposit_balance": new_deposit,
                "winning_balance": new_winning,
                "balance": new_total
            }}
    else:
        # Addition (amount > 0)
        # Check transaction type:
        # If it is a deposit or admin manually adding: added to deposit_balance
        if tx_type in ["deposit", "admin_add", "match_refund", "car_event_refund", "free_fire_refund", "car_game_free_win"]:
            new_deposit = round(deposit_bal + amount, 2)
            new_winning = winning_bal
        else:
            # rewards: streak_reward, task_reward, referral_reward, match_win, car_event_win, free_fire_win
            new_deposit = deposit_bal
            new_winning = round(winning_bal + amount, 2)
            
        new_total = round(total_bal + amount, 2)
        
        query = {"_id": user_id}
        update = {"$set": {
            "deposit_balance": new_deposit,
            "winning_balance": new_winning,
            "balance": new_total
        }}

    # Perform atomic update
    updated_user = users_col.find_one_and_update(
        query,
        update,
        return_document=True
    )
    
    if not updated_user:
        return False, 0.0

    # Log transaction
    create_transaction(
        user_id=user_id,
        tx_type=tx_type,
        amount=amount,
        status="completed" if tx_type not in ["deposit", "redeem"] else "pending",
        details=details
    )
    
    return True, round(updated_user["balance"], 2)

def create_transaction(user_id, tx_type, amount, status, details=None):
    """Create a transaction log."""
    tx_doc = {
        "user_id": int(user_id),
        "type": tx_type,  # deposit, redeem, match_fee, match_win, task_reward, streak_reward, referral_reward, admin_add, admin_remove
        "amount": round(float(amount), 2),
        "status": status,  # pending, approved, rejected, completed
        "details": details or {},
        "created_at": datetime.now(IST),
        "updated_at": datetime.now(IST)
    }
    result = transactions_col.insert_one(tx_doc)
    return result.inserted_id

def approve_deposit(tx_id):
    """Admin approves a pending deposit, updating user wallet."""
    tx = transactions_col.find_one({"_id": ObjectId(tx_id), "status": "pending", "type": "deposit"})
    if not tx:
        return False, "Deposit transaction not found or already processed"
        
    user_id = tx["user_id"]
    amount = tx["amount"]
    
    # Calculate bonus if any: 10rs -> 10, 20rs -> 22, 50rs -> 54
    final_amount = amount
    if amount == 20:
        final_amount = 22
    elif amount == 50:
        final_amount = 54
        
    # Atomically add money to user
    users_col.update_one(
        {"_id": user_id},
        {"$inc": {"balance": final_amount, "deposit_balance": final_amount}}
    )
    
    # Mark transaction as approved
    transactions_col.update_one(
        {"_id": ObjectId(tx_id)},
        {
            "$set": {
                "status": "approved",
                "amount": final_amount, # store final credited amount
                "updated_at": datetime.now(IST)
            }
        }
    )
    
    # Track mission progress: update balance added if Rs 10 or more
    if amount >= 10:
        update_daily_mission_progress(user_id, balance_added=amount)
    
    return True, {"user_id": user_id, "amount": final_amount}

def reject_deposit(tx_id):
    """Admin rejects a pending deposit."""
    tx = transactions_col.find_one({"_id": ObjectId(tx_id), "status": "pending", "type": "deposit"})
    if not tx:
        return False, "Deposit transaction not found or already processed"
        
    transactions_col.update_one(
        {"_id": ObjectId(tx_id)},
        {"$set": {"status": "rejected", "updated_at": datetime.now(IST)}}
    )
    return True, tx["user_id"]

def approve_redeem(tx_id):
    """Admin approves a pending withdrawal."""
    tx = transactions_col.find_one({"_id": ObjectId(tx_id), "status": "pending", "type": "redeem"})
    if not tx:
        return False, "Redeem transaction not found or already processed"
        
    transactions_col.update_one(
        {"_id": ObjectId(tx_id)},
        {"$set": {"status": "approved", "updated_at": datetime.now(IST)}}
    )
    return True, tx["user_id"]

def reject_redeem(tx_id):
    """Admin rejects a pending withdrawal. Refunds the user's wallet."""
    tx = transactions_col.find_one({"_id": ObjectId(tx_id), "status": "pending", "type": "redeem"})
    if not tx:
        return False, "Redeem transaction not found or already processed"
        
    user_id = tx["user_id"]
    amount = abs(tx["amount"]) # Amount is logged as negative when requested
    
    # Refund user wallet
    users_col.update_one(
        {"_id": user_id},
        {"$inc": {"balance": amount, "winning_balance": amount}}
    )
    
    # Update transaction status
    transactions_col.update_one(
        {"_id": ObjectId(tx_id)},
        {"$set": {"status": "rejected", "updated_at": datetime.now(IST)}}
    )
    return True, user_id

def cancel_redeem_by_user(user_id):
    """Cancel latest pending redeem and refund money to user."""
    tx = transactions_col.find_one(
        {"user_id": int(user_id), "status": "pending", "type": "redeem"},
        sort=[("created_at", -1)]
    )
    if not tx:
        return False, "No pending withdrawal request found to cancel"
        
    # Refund user wallet
    users_col.update_one(
        {"_id": int(user_id)},
        {"$inc": {"balance": abs(tx["amount"]), "winning_balance": abs(tx["amount"])}}
    )
    
    # Mark transaction as rejected/cancelled
    transactions_col.update_one(
        {"_id": tx["_id"]},
        {"$set": {"status": "rejected", "details.reason": "Cancelled by user", "updated_at": datetime.now(IST)}}
    )
    return True, abs(tx["amount"])

def ban_user(user_id):
    users_col.update_one({"_id": int(user_id)}, {"$set": {"is_banned": True}})

def unban_user(user_id):
    users_col.update_one({"_id": int(user_id)}, {"$set": {"is_banned": False}})

def get_unbanned_users():
    return list(users_col.find({"is_banned": False}))

def get_banned_users():
    return list(users_col.find({"is_banned": True}))

def get_all_users():
    return list(users_col.find())

def get_transaction_history(user_id, limit=20):
    return list(transactions_col.find({"user_id": int(user_id)}).sort("created_at", -1).limit(limit))

def get_match_history(user_id, limit=20):
    return list(matches_col.find({
        "$or": [
            {"player_a.user_id": int(user_id)},
            {"player_b.user_id": int(user_id)}
        ],
        "type": {"$ne": "challenge"}
    }).sort("created_at", -1).limit(limit))

def save_match_result(match_id, player_a_data, player_b_data, match_type, winner_id, score_a, score_b):
    """Save completed match results to database."""
    match_doc = {
        "_id": match_id,
        "player_a": player_a_data,
        "player_b": player_b_data,
        "type": match_type,  # paid, free, challenge
        "winner_id": winner_id,  # user_id or 'bot' or 'draw'
        "score_a": score_a,
        "score_b": score_b,
        "created_at": datetime.now(IST)
    }
    matches_col.insert_one(match_doc)
    
    # Update daily missions for players (if paid match)
    if match_type == "paid":
        for p in [player_a_data, player_b_data]:
            if p and p.get("user_id") and p["user_id"] != "bot":
                uid = p["user_id"]
                score = p.get("score", 0)
                update_daily_mission_progress(uid, matches_played=1, max_score=score)

def get_leaderboard():
    """Get top 3 users based on win count and current user rank."""
    # We aggregate matches to count wins
    pipeline = [
        {"$match": {"winner_id": {"$exists": True, "$ne": "bot", "$type": "long"}}}, # exclude draws or bot wins
        {"$group": {"_id": "$winner_id", "wins": {"$sum": 1}}},
        {"$sort": {"wins": -1}},
        {"$limit": 100}
    ]
    rankings = list(matches_col.aggregate(pipeline))
    
    # Match rankings with usernames
    leaderboard = []
    user_wins_map = {}
    for idx, r in enumerate(rankings):
        uid = r["_id"]
        user_wins_map[uid] = r["wins"]
        if idx < 3: # get top 3
            user = get_user(uid)
            if user:
                leaderboard.append({
                    "rank": idx + 1,
                    "user_id": uid,
                    "username": user.get("username", "Unknown"),
                    "first_name": user.get("first_name", ""),
                    "wins": r["wins"]
                })
                
    # If less than 3, fill with placeholders or other active users
    if len(leaderboard) < 3:
        all_users = list(users_col.find({"is_banned": False}).limit(5))
        for u in all_users:
            if u["_id"] not in [l["user_id"] for l in leaderboard] and len(leaderboard) < 3:
                leaderboard.append({
                    "rank": len(leaderboard) + 1,
                    "user_id": u["_id"],
                    "username": u.get("username", "Player"),
                    "first_name": u.get("first_name", ""),
                    "wins": user_wins_map.get(u["_id"], 0)
                })
                
    return leaderboard

def get_user_rank(user_id):
    """Determine the specific rank of a user."""
    pipeline = [
        {"$match": {"winner_id": {"$exists": True, "$ne": "bot", "$type": "long"}}},
        {"$group": {"_id": "$winner_id", "wins": {"$sum": 1}}},
        {"$sort": {"wins": -1}}
    ]
    rankings = list(matches_col.aggregate(pipeline))
    for idx, r in enumerate(rankings):
        if r["_id"] == int(user_id):
            return idx + 1
    return 999  # Unranked

# --- Daily Missions & Streak Claim Helper ---

def update_daily_mission_progress(user_id, matches_played=0, balance_added=0.0, max_score=0):
    """Update progress metrics for daily missions."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return
        
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    
    # Initialize daily missions if date is outdated
    dm = user.get("daily_missions", {})
    if dm.get("date") != today_str:
        dm = {
            "date": today_str,
            "matches_played": 0,
            "balance_added": 0.0,
            "max_score": 0,
            "claimed": {
                "matches_3": False,
                "add_balance": False
            }
        }
        
    dm["matches_played"] += matches_played
    dm["balance_added"] += balance_added
    if max_score > dm["max_score"]:
        dm["max_score"] = max_score
        
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"daily_missions": dm}}
    )

def claim_daily_mission(user_id, mission_key):
    """Claim reward for a completed daily mission."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    dm = user.get("daily_missions", {})
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if dm.get("date") != today_str:
        return False, "Mission progress not found for today"
        
    if dm["claimed"].get(mission_key):
        return False, "Mission already claimed"
        
    # Rewards mapping
    rewards = {
        "matches_3": 0.50,
        "add_balance": 0.50
    }
    reward_amt = rewards.get(mission_key, 0.0)
    
    # Check eligibility
    eligible = False
    if mission_key == "matches_3" and dm["matches_played"] >= 3:
        eligible = True
    elif mission_key == "add_balance" and dm["balance_added"] >= 10:
        eligible = True
        
    if not eligible:
        return False, "Mission requirements not met"
        
    # Process reward
    dm["claimed"][mission_key] = True
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"daily_missions": dm}}
    )
    
    update_balance(user_id, reward_amt, "streak_reward", {"mission": mission_key})
    return True, reward_amt

def check_played_paid_match_today(user_id):
    """Checks if the user has played or registered for any paid match today (Hand Cricket, Car Game, or Free Fire)."""
    user_id = int(user_id)
    start_of_today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_today = start_of_today + timedelta(days=1)
    
    # 1. Check Hand Cricket paid matches today
    paid_match = matches_col.find_one({
        "type": "paid",
        "created_at": {"$gte": start_of_today, "$lt": end_of_today},
        "$or": [
            {"player_a.user_id": user_id},
            {"player_b.user_id": user_id}
        ]
    })
    if paid_match:
        return True
        
    # 2. Check Car Game paid events joined today (entry_fee > 0)
    paid_car = car_event_cycles_col.find_one({
        "entry_fee": {"$gt": 0},
        "participants": {
            "$elemMatch": {
                "user_id": user_id,
                "joined_at": {"$gte": start_of_today, "$lt": end_of_today}
            }
        }
    })
    if paid_car:
        return True
        
    # 3. Check Free Fire paid tournaments joined today (via transaction log for free_fire_fee)
    paid_ff = transactions_col.find_one({
        "user_id": user_id,
        "type": "free_fire_fee",
        "amount": {"$lt": 0},
        "created_at": {"$gte": start_of_today, "$lt": end_of_today}
    })
    if paid_ff:
        return True
        
    return False

def claim_daily_streak(user_id):
    """Claim daily streak reward."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    # Check if user has played at least 1 paid match today (IST)
    played_paid_today = check_played_paid_match_today(user_id)
    if not played_paid_today:
        return False, "You must play at least 1 paid match today to claim your daily streak reward."
        
    now = datetime.now(IST)
    last_claim = user.get("last_streak_claim")
    streak = user.get("streak", 0)
    
    if last_claim:
        last_claim_ist = to_ist(last_claim)
        time_diff = now - last_claim_ist
        
        # If claimed on the same calendar day (IST)
        if last_claim_ist.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d"):
            return False, "Already claimed today's reward"
            
        # If claimed yesterday (IST), increment streak. Else reset to 1
        yesterday = now - timedelta(days=1)
        if last_claim_ist.strftime("%Y-%m-%d") == yesterday.strftime("%Y-%m-%d"):
            streak = (streak % 7) + 1
        else:
            streak = 1
    else:
        streak = 1
        
    # Reward mapping
    rewards = {
        1: 0.10,
        2: 0.20,
        3: 0.30,
        4: 0.40,
        5: 0.50,
        6: 0.60,
        7: 1.00
    }
    reward_amt = rewards.get(streak, 0.10)
    
    # Update DB
    users_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "streak": streak,
                "last_streak_claim": now
            }
        }
    )
    
    update_balance(user_id, reward_amt, "streak_reward", {"day": streak})
    return True, {"streak": streak, "reward": reward_amt}

def claim_referral_reward(user_id, tier):
    """Claim reward for milestone invite referrals."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    claimed = user.get("referral_claimed", [])
    if tier in claimed:
        return False, "Referral tier reward already claimed"
        
    referral_count = user.get("referrals_count", 0)
    
    # Milestone requirements
    milestones = {
        "1": {"required": 1, "reward": 0.50},
        "5": {"required": 5, "reward": 2.00},
        "10": {"required": 10, "reward": 5.00}
    }
    
    if tier not in milestones:
        return False, "Invalid referral milestone tier"
        
    req = milestones[tier]
    if referral_count < req["required"]:
        return False, f"Requires at least {req['required']} referrals"
        
    # Add reward
    users_col.update_one(
        {"_id": user_id},
        {"$push": {"referral_claimed": tier}}
    )
    
    update_balance(user_id, req["reward"], "referral_reward", {"tier": tier})
    return True, req["reward"]

# --- Admin Panel Finance Stats ---
def get_finance_stats():
    """Calculate finance metrics to check profitability."""
    # 1. Total Deposits (Approved)
    dep_pipeline = [
        {"$match": {"type": "deposit", "status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    deposits = list(transactions_col.aggregate(dep_pipeline))
    total_deposits = deposits[0]["total"] if deposits else 0.0
    
    # 2. Total Redeems (Approved)
    red_pipeline = [
        {"$match": {"type": "redeem", "status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    redeems = list(transactions_col.aggregate(red_pipeline))
    total_redeems = abs(redeems[0]["total"]) if redeems else 0.0
    
    # 3. Total Match Fees (Paid) and Match Wins
    # Platform earns on paid match fees: 5.0 fee. Winner gets 8.5.
    # Total match fees collected = count of matches * 2 (or 1 if against bot)
    # Total match wins distributed = count of matches * 8.5 (if won by user)
    # If a match is against the bot:
    #   Fee: 5.0 collected.
    #   If user wins, user gets 8.5 (loss of 3.5 for platform).
    #   If bot wins, user gets 0 (profit of 5.0 for platform).
    # Let's count actual match fee / win transactions!
    fee_pipe = [
        {"$match": {"type": "match_fee", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    win_pipe = [
        {"$match": {"type": "match_win", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    fees = list(transactions_col.aggregate(fee_pipe))
    wins = list(transactions_col.aggregate(win_pipe))
    total_fees_collected = abs(fees[0]["total"]) if fees else 0.0
    total_wins_paid = wins[0]["total"] if wins else 0.0
    
    # 4. Total Task Rewards Paid
    task_pipe = [
        {"$match": {"type": "task_reward", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    task_rew = list(transactions_col.aggregate(task_pipe))
    total_task_rewards = task_rew[0]["total"] if task_rew else 0.0
    
    # 5. SignUp, Streak and Referral Rewards
    signup_pipe = [
        {"$match": {"type": "signup_bonus", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    streak_pipe = [
        {"$match": {"type": "streak_reward", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    ref_pipe = [
        {"$match": {"type": "referral_reward", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    
    signup = list(transactions_col.aggregate(signup_pipe))
    streak = list(transactions_col.aggregate(streak_pipe))
    ref = list(transactions_col.aggregate(ref_pipe))
    
    total_signup = signup[0]["total"] if signup else 0.0
    total_streak = streak[0]["total"] if streak else 0.0
    total_referral = ref[0]["total"] if ref else 0.0
    
    # Estimating ad revenue from tasks: 10$ per 1000 views = $0.01 per view.
    # At Rs 83 per dollar, it is approx 0.83 Rs per task.
    # User is paid 0.60 Rs, so platform earns 0.23 Rs per task.
    completed_tasks_count = tasks_col.count_documents({"status": "completed"})
    estimated_ad_revenue = completed_tasks_count * 0.83
    
    # Calculate Profit
    # Platform balance sheet:
    # Profit = Total Deposits + Est. Ad Revenue + Match Fees - Redeems - Wins - Task Rewards - Streak - Referral - Signup
    # In terms of wallet balances:
    # Current total wallets balance:
    total_user_balance = sum([u.get("balance", 0) for u in users_col.find()])
    
    net_profit = (total_deposits + estimated_ad_revenue + total_fees_collected) - (total_redeems + total_wins_paid + total_task_rewards + total_signup + total_streak + total_referral)
    
    return {
        "total_users": users_col.count_documents({}),
        "total_deposits": total_deposits,
        "total_redeems": total_redeems,
        "completed_tasks": completed_tasks_count,
        "estimated_ad_revenue": round(estimated_ad_revenue, 2),
        "total_task_rewards_paid": total_task_rewards,
        "match_fees_collected": total_fees_collected,
        "match_wins_paid": total_wins_paid,
        "total_giveaways": round(total_signup + total_streak + total_referral, 2),
        "total_user_wallets": round(total_user_balance, 2),
        "net_profit": round(net_profit, 2)
    }

def get_pending_deposits():
    return list(transactions_col.find({"type": "deposit", "status": "pending"}).sort("created_at", -1))

def get_pending_redeems():
    return list(transactions_col.find({"type": "redeem", "status": "pending"}).sort("created_at", -1))

def save_feedback(user_id, selected_games, other_game, likes_game):
    """Saves user feedback to the feedbacks collection."""
    feedback_doc = {
        "user_id": int(user_id),
        "selected_games": selected_games,
        "other_game": other_game,
        "likes_game": likes_game,
        "created_at": datetime.now(timezone.utc)
    }
    return feedbacks_col.insert_one(feedback_doc).inserted_id

# --- Profile Section Helpers ---
def update_free_fire_profile(user_id, ff_username, ff_uid):
    """Update user's Free Fire profile details."""
    users_col.update_one(
        {"_id": int(user_id)},
        {"$set": {
            "free_fire_username": ff_username,
            "free_fire_uid": ff_uid
        }}
    )
    return True

# --- Car Game Operations ---
def get_active_car_event_cycles(user_id):
    """Retrieves current active Car Game event cycles and formats for the user."""
    cycles = list(car_event_cycles_col.find({"status": "active"}))
    
    # Calculate total free event play count (lifetime)
    free_joined_count = car_event_cycles_col.count_documents({
        "event_id": 1,
        "participants": {
            "$elemMatch": {
                "user_id": int(user_id)
            }
        }
    })
    
    # Check if user has deposited min Rs 10
    has_deposit_10 = transactions_col.find_one({
        "user_id": int(user_id),
        "type": "deposit",
        "status": "approved",
        "amount": {"$gte": 10.0}
    }) is not None
    free_limit = 5 if has_deposit_10 else 3
    
    formatted = []
    for cyc in cycles:
        participants = cyc.get("participants", [])
        # Find user's unplayed or last played participation in this cycle
        unplayed = [p for p in participants if p["user_id"] == int(user_id) and not p["played"]]
        played_list = [p for p in participants if p["user_id"] == int(user_id) and p["played"]]
        
        user_joined = len(unplayed) > 0
        user_played = len(unplayed) == 0 and len(played_list) > 0
        
        # Calculate scores
        user_score = 0
        if unplayed:
            user_score = unplayed[0].get("score", 0)
        elif played_list:
            user_score = max(p.get("score", 0) for p in played_list)
            
        formatted.append({
            "id": str(cyc["_id"]),
            "event_id": cyc["event_id"],
            "entry_fee": cyc["entry_fee"],
            "max_participants": cyc["max_participants"],
            "prizes": cyc["prizes"],
            "joined_count": len(participants),
            "user_joined": user_joined,
            "user_played": user_played,
            "user_score": user_score,
            "status": cyc["status"],
            "free_joined_count": free_joined_count,
            "free_limit": free_limit
        })
    return formatted

def join_car_event(user_id, event_id):
    """Allows a user to join an active Car Game event cycle by paying the entry fee."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    cyc = car_event_cycles_col.find_one({"event_id": int(event_id), "status": "active"})
    if not cyc:
        return False, "Active event cycle not found"
        
    # Check if event is already full
    if len(cyc.get("participants", [])) >= cyc["max_participants"]:
        return False, "This event is currently full. Please wait for the next cycle."
        
    # Check if user already has joined in this cycle (either played or unplayed)
    joined_participant = next((p for p in cyc.get("participants", []) if p["user_id"] == user_id), None)
    if joined_participant:
        if not joined_participant["played"]:
            return True, {"message": "Already joined, resume play", "cycle_id": str(cyc["_id"])}
        else:
            return False, "You have already completed your game for this event cycle. Please wait for the next cycle to start."
            
    # Check total limit for free event (Event 1)
    if int(event_id) == 1:
        free_joined_count = car_event_cycles_col.count_documents({
            "event_id": 1,
            "participants": {
                "$elemMatch": {
                    "user_id": user_id
                }
            }
        })
        # Check if user has deposited min Rs 10
        has_deposit_10 = transactions_col.find_one({
            "user_id": user_id,
            "type": "deposit",
            "status": "approved",
            "amount": {"$gte": 10.0}
        }) is not None
        free_limit = 5 if has_deposit_10 else 3
        if free_joined_count >= free_limit:
            if free_limit == 3:
                return False, "You have reached your limit of 3 free games. Deposit min Rs 10 to get 2 more chances!"
            else:
                return False, "You have reached your maximum limit of 5 free games."
        
    # Deduct entry fee
    fee = cyc["entry_fee"]
    success, new_bal = update_balance(
        user_id=user_id,
        amount=-fee,
        tx_type="car_game_fee",
        details={"event_id": event_id, "cycle_id": str(cyc["_id"])}
    )
    if not success:
        return False, "Insufficient balance"
        
    # Add participant
    participant = {
        "user_id": user_id,
        "username": user.get("first_name") or user.get("username") or f"User {user_id}",
        "score": 0,
        "played": False,
        "joined_at": datetime.now(IST)
    }
    
    car_event_cycles_col.update_one(
        {"_id": cyc["_id"]},
        {"$push": {"participants": participant}}
    )
    
    return True, {"message": "Joined successfully", "cycle_id": str(cyc["_id"])}

def submit_car_score(user_id, event_id, score):
    """Submits a user's score for their unplayed participation in the active Car Game event."""
    user_id = int(user_id)
    score = int(score)
    
    cyc = car_event_cycles_col.find_one({"event_id": int(event_id), "status": "active"})
    if not cyc:
        return False, "Active cycle not found"
        
    # Find user's unplayed participation
    participants = cyc.get("participants", [])
    unplayed_idx = -1
    for idx, p in enumerate(participants):
        if p["user_id"] == user_id and not p["played"]:
            unplayed_idx = idx
            break
            
    if unplayed_idx == -1:
        return False, "No active unplayed participation found for this event"
        
    # Update score and mark as played
    car_event_cycles_col.update_one(
        {"_id": cyc["_id"], f"participants.{unplayed_idx}.user_id": user_id},
        {
            "$set": {
                f"participants.{unplayed_idx}.score": score,
                f"participants.{unplayed_idx}.played": True,
                f"participants.{unplayed_idx}.played_at": datetime.now(IST)
            }
        }
    )
    
    # Reload cycle to check if all participants have completed their game
    cyc = car_event_cycles_col.find_one({"_id": cyc["_id"]})
    
    # Increment daily mission progress if this is a paid event (entry_fee > 0)
    if cyc and cyc.get("entry_fee", 0.0) > 0.0:
        update_daily_mission_progress(user_id, matches_played=1)

    completed_participants = [p for p in cyc.get("participants", []) if p["played"]]
    
    if len(completed_participants) >= cyc["max_participants"]:
        # Resolve the cycle!
        resolve_car_event_cycle(cyc["_id"])
        
    return True, "Score submitted successfully"

def resolve_car_event_cycle(cycle_id):
    """Evaluates rankings, distributes prize pools, and restarts the event cycle."""
    cyc = car_event_cycles_col.find_one({"_id": ObjectId(cycle_id), "status": "active"})
    if not cyc:
        return
        
    participants = [p for p in cyc.get("participants", []) if p["played"]]
    # Sort by score descending, then played_at/joined_at ascending (earlier wins tie-breaker)
    participants.sort(key=lambda x: (-x["score"], x.get("played_at", x.get("joined_at", datetime.now(IST)))))
    
    # Prize mappings
    prizes = cyc["prizes"]
    
    # Process rank payouts
    for rank_idx, p in enumerate(participants):
        rank = rank_idx + 1
        prize = float(prizes.get(str(rank), 0.0))
        
        # Log rank results
        print(f"Resolving Event {cyc['event_id']}, Rank {rank}: User {p['user_id']} with score {p['score']} wins Rs {prize}")
        
        if prize > 0:
            tx_type = "car_game_free_win" if cyc["event_id"] == 1 else "match_win"
            # Credit account
            update_balance(
                user_id=p["user_id"],
                amount=prize,
                tx_type=tx_type,
                details={
                    "game": "car_game",
                    "event_id": cyc["event_id"],
                    "cycle_id": str(cycle_id),
                    "rank": rank,
                    "score": p["score"]
                }
            )
            
        # Send Pyrogram bot notification if client is running
        from bot.client import bot as bot_client
        try:
            notification_text = (
                f"🏆 **Car Game - Event {cyc['event_id']} Results!**\n\n"
                f"Congratulations! You ranked **#{rank}** out of {cyc['max_participants']} players with a score of **{p['score']}**.\n"
                f"💰 **Prize Won:** Rs {prize:.2f}\n\n"
                f"The event has restarted. Play again to win more!"
            ) if prize > 0 else (
                f"🎮 **Car Game - Event {cyc['event_id']} Results!**\n\n"
                f"You ranked **#{rank}** out of {cyc['max_participants']} players with a score of **{p['score']}**.\n"
                f"Better luck next time! The event has restarted, join now!"
            )
            # Schedule message sending in the bot's event loop
            import asyncio
            asyncio.run_coroutine_threadsafe(
                bot_client.send_message(p["user_id"], notification_text),
                asyncio.get_event_loop()
            )
        except Exception as e:
            print(f"Failed to send result notification to user {p['user_id']}: {e}")
            
    # Mark cycle as completed
    car_event_cycles_col.update_one(
        {"_id": ObjectId(cycle_id)},
        {"$set": {"status": "completed", "resolved_at": datetime.now(IST)}}
    )
    
    # Spawn a new active cycle
    car_event_cycles_col.insert_one({
        "event_id": cyc["event_id"],
        "entry_fee": cyc["entry_fee"],
        "max_participants": cyc["max_participants"],
        "prizes": cyc["prizes"],
        "status": "active",
        "participants": [],
        "created_at": datetime.now(IST)
    })

# --- Hand Cricket Cycle Operations ---
def get_active_cricket_cycle(user_id):
    """Retrieves current active Hand Cricket paid cycle and formats for the user."""
    cyc = cricket_event_cycles_col.find_one({"status": "active"})
    if not cyc:
        # Auto seed if missing
        cyc = {
            "event_id": 1,
            "entry_fee": 1.0,
            "max_participants": 2,
            "prizes": {"1": 1.8},
            "status": "active",
            "participants": [],
            "created_at": datetime.now(IST)
        }
        cricket_event_cycles_col.insert_one(cyc)
        
    participants = cyc.get("participants", [])
    unplayed = [p for p in participants if p["user_id"] == int(user_id) and not p["played"]]
    played_list = [p for p in participants if p["user_id"] == int(user_id) and p["played"]]
    
    user_joined = len(unplayed) > 0
    user_played = len(unplayed) == 0 and len(played_list) > 0
    
    user_score = 0
    if unplayed:
        user_score = unplayed[0].get("score", 0)
    elif played_list:
        user_score = max(p.get("score", 0) for p in played_list)
        
    return {
        "id": str(cyc["_id"]),
        "entry_fee": cyc["entry_fee"],
        "max_participants": cyc["max_participants"],
        "prizes": cyc["prizes"],
        "joined_count": len(participants),
        "user_joined": user_joined,
        "user_played": user_played,
        "user_score": user_score,
        "status": cyc["status"],
        "participants": [
            {
                "username": p["username"],
                "score": p["score"],
                "played": p["played"]
            } for p in participants
        ]
    }

def join_cricket_event(user_id):
    """Allows a user to join the active Hand Cricket cycle by paying the entry fee."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    cyc = cricket_event_cycles_col.find_one({"status": "active"})
    if not cyc:
        return False, "Active cricket cycle not found"
        
    # Check if full
    if len(cyc.get("participants", [])) >= cyc["max_participants"]:
        return False, "This match cycle is full. Please wait for the next cycle."
        
    # Check if user already joined
    joined_participant = next((p for p in cyc.get("participants", []) if p["user_id"] == user_id), None)
    if joined_participant:
        if not joined_participant["played"]:
            return True, "Already joined"
        else:
            return False, "You have already completed your game for this event cycle. Please wait for the next cycle to start."
            
    # Deduct fee
    fee = cyc["entry_fee"]
    success, new_bal = update_balance(
        user_id=user_id,
        amount=-fee,
        tx_type="match_fee",
        details={"status": "joined_cycle", "cycle_id": str(cyc["_id"])}
    )
    if not success:
        return False, "Insufficient balance"
        
    # Add participant
    participant = {
        "user_id": user_id,
        "username": user.get("first_name") or user.get("username") or f"User {user_id}",
        "score": 0,
        "played": False,
        "joined_at": datetime.now(IST)
    }
    cricket_event_cycles_col.update_one(
        {"_id": cyc["_id"]},
        {"$push": {"participants": participant}}
    )
    return True, "Joined successfully"

def submit_cricket_score(user_id, score):
    """Submits the score for the user's Hand Cricket paid match cycle."""
    user_id = int(user_id)
    score = int(score)
    cyc = cricket_event_cycles_col.find_one({"status": "active"})
    if not cyc:
        return False, "Active cricket cycle not found"
        
    participants = cyc.get("participants", [])
    unplayed_idx = -1
    for idx, p in enumerate(participants):
        if p["user_id"] == user_id and not p["played"]:
            unplayed_idx = idx
            break
            
    if unplayed_idx == -1:
        return False, "No active unplayed participation found for this cycle"
        
    cricket_event_cycles_col.update_one(
        {"_id": cyc["_id"], f"participants.{unplayed_idx}.user_id": user_id},
        {
            "$set": {
                f"participants.{unplayed_idx}.score": score,
                f"participants.{unplayed_idx}.played": True,
                f"participants.{unplayed_idx}.played_at": datetime.now(IST)
            }
        }
    )
    
    # Increment daily mission progress
    update_daily_mission_progress(user_id, matches_played=1)
    
    # Reload cycle to check if all participants have completed their game
    cyc = cricket_event_cycles_col.find_one({"_id": cyc["_id"]})
    completed_participants = [p for p in cyc.get("participants", []) if p["played"]]
    
    if len(completed_participants) >= cyc["max_participants"]:
        resolve_cricket_event_cycle(cyc["_id"])
        
    return True, "Score submitted successfully"

def resolve_cricket_event_cycle(cycle_id):
    """Evaluates rankings, distributes prize pools, and restarts the cricket cycle."""
    cyc = cricket_event_cycles_col.find_one({"_id": ObjectId(cycle_id), "status": "active"})
    if not cyc:
        return
        
    participants = [p for p in cyc.get("participants", []) if p["played"]]
    # Sort by score descending, then played_at/joined_at ascending (earlier wins tie-breaker)
    participants.sort(key=lambda x: (-x["score"], x.get("played_at", x.get("joined_at", datetime.now(IST)))))
    
    prizes = cyc["prizes"]
    
    for rank_idx, p in enumerate(participants):
        rank = rank_idx + 1
        prize = float(prizes.get(str(rank), 0.0))
        
        print(f"Resolving Cricket Cycle, Rank {rank}: User {p['user_id']} with score {p['score']} wins Rs {prize}")
        
        if prize > 0:
            update_balance(
                user_id=p["user_id"],
                amount=prize,
                tx_type="match_win",
                details={
                    "game": "hand_cricket",
                    "cycle_id": str(cycle_id),
                    "rank": rank,
                    "score": p["score"]
                }
            )
            
        # Send Pyrogram notification
        from bot.client import bot as bot_client
        try:
            notification_text = (
                f"🏏 **Hand Cricket Paid Match Results!**\n\n"
                f"Congratulations! You ranked **#{rank}** out of {cyc['max_participants']} players with a score of **{p['score']}**.\n"
                f"💰 **Prize Won:** Rs {prize:.2f}\n\n"
                f"The event has restarted. Play again to win more!"
            ) if prize > 0 else (
                f"🏏 **Hand Cricket Paid Match Results!**\n\n"
                f"You ranked **#{rank}** out of {cyc['max_participants']} players with a score of **{p['score']}**.\n"
                f"Better luck next time! Play again to win more!"
            )
            import asyncio
            asyncio.run_coroutine_threadsafe(
                bot_client.send_message(p["user_id"], notification_text),
                asyncio.get_event_loop()
            )
        except Exception as e:
            print(f"Failed to send cricket result notification to user {p['user_id']}: {e}")
            
    # Mark cycle completed
    cricket_event_cycles_col.update_one(
        {"_id": ObjectId(cycle_id)},
        {"$set": {"status": "completed", "resolved_at": datetime.now(IST)}}
    )
    
    # Spawn new cycle
    cricket_event_cycles_col.insert_one({
        "event_id": 1,
        "entry_fee": 1.0,
        "max_participants": 2,
        "prizes": {"1": 1.8},
        "status": "active",
        "participants": [],
        "created_at": datetime.now(IST)
    })

# --- Free Fire Event Operations ---
def get_free_fire_events():
    """Retrieve all available Free Fire events."""
    events = list(free_fire_events_col.find())
    formatted = []
    for ev in events:
        slots = ev.get("slots", {})
        joined_count = sum(1 for slot_val in slots.values() if slot_val is not None)
        formatted.append({
            "id": str(ev["_id"]),
            "mode": ev["mode"],
            "map": ev["map"],
            "entry_fee": ev["entry_fee"],
            "prize_per_kill": ev["prize_per_kill"],
            "max_participants": ev["max_participants"],
            "start_time": ev["start_time"],
            "end_time": ev["end_time"],
            "date": ev.get("date", "2026-06-12"),
            "room_id": ev.get("room_id", ""),
            "room_password": ev.get("room_password", ""),
            "joined_count": joined_count,
            "slots": slots
        })
    return formatted

def join_free_fire_event(user_id, event_id, slot_number):
    """Registers a user to a specific slot in a Free Fire event after validating balance."""
    user_id = int(user_id)
    slot_key = str(slot_number)
    
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    ff_username = user.get("free_fire_username", "").strip()
    ff_uid = user.get("free_fire_uid", "").strip()
    if not ff_username or not ff_uid:
        return False, "profile_incomplete"
        
    try:
        ev_id = ObjectId(event_id)
    except Exception:
        return False, "Invalid event ID"
        
    ev = free_fire_events_col.find_one({"_id": ev_id})
    if not ev:
        return False, "Event not found"
        
    slots = ev.get("slots", {})
    if slot_key not in slots:
        return False, "Invalid slot number"
        
    if slots[slot_key] is not None:
        return False, "Slot already occupied"
        
    # Check if user is already in another slot for this event
    for s_key, s_val in slots.items():
        if s_val and s_val.get("user_id") == user_id:
            return False, "You have already joined this event"
            
    # Deduct entry fee
    fee = ev["entry_fee"]
    success, new_bal = update_balance(
        user_id=user_id,
        amount=-fee,
        tx_type="free_fire_fee",
        details={"event_id": event_id, "slot": slot_number}
    )
    if not success:
        return False, "Insufficient balance"
        
    # Increment daily mission progress if this is a paid tournament
    if fee > 0.0:
        update_daily_mission_progress(user_id, matches_played=1)
        
    # Update slot details
    free_fire_events_col.update_one(
        {"_id": ev_id},
        {"$set": {
            f"slots.{slot_key}": {
                "user_id": user_id,
                "username": user.get("username", ""),
                "first_name": user.get("first_name", ""),
                "ff_username": ff_username,
                "ff_uid": ff_uid
            }
        }}
    )
    
    return True, "Successfully joined Free Fire tournament"

def declare_free_fire_results(event_id, kills_data):
    """
    Distributes rewards to registered Free Fire tournament participants based on kills.
    kills_data: dict of slot_number (str) -> kills (int)
    """
    try:
        ev_id = ObjectId(event_id)
    except Exception:
        return False, "Invalid event ID"
        
    ev = free_fire_events_col.find_one({"_id": ev_id})
    if not ev:
        return False, "Free Fire tournament not found"
        
    slots = ev.get("slots", {})
    prize_per_kill = ev.get("prize_per_kill", 4.0)
    
    from bot.client import bot as bot_client
    import asyncio
    
    # Process each reward
    for slot_key, kills in kills_data.items():
        slot_key = str(slot_key)
        occupant = slots.get(slot_key)
        if occupant:
            user_id = occupant["user_id"]
            prize_amount = float(kills) * prize_per_kill
            
            if prize_amount > 0:
                update_balance(
                    user_id=user_id,
                    amount=prize_amount,
                    tx_type="match_win",
                    details={
                        "game": "free_fire",
                        "event_id": event_id,
                        "slot": slot_key,
                        "kills": kills,
                        "prize_per_kill": prize_per_kill
                    }
                )
            
            # Send bot notification
            try:
                notification_text = (
                    f"🏆 **Free Fire Tournament Results declared!**\n\n"
                    f"Event: {ev['mode']} - {ev['map']} ({ev.get('date', 'N/A')})\n"
                    f"Character Slot: **#{slot_key}**\n"
                    f"Kills: **{kills}**\n"
                    f"💰 **Prize Won:** Rs {prize_amount:.2f} (Rs {prize_per_kill:.2f} per kill)\n\n"
                    f"Amount credited to your wallet balance."
                )
                asyncio.run_coroutine_threadsafe(
                    bot_client.send_message(user_id, notification_text),
                    asyncio.get_event_loop()
                )
            except Exception as e:
                print(f"Failed to notify user {user_id}: {e}")
                
    # Reset event slots and clear Room ID / Password so it restarts cycle
    reset_slots = {str(i): None for i in range(1, ev["max_participants"] + 1)}
    free_fire_events_col.update_one(
        {"_id": ev_id},
        {"$set": {
            "slots": reset_slots,
            "room_id": "",
            "room_password": ""
        }}
    )
    return True, "Results declared and rewards distributed successfully!"

# --- Seeding Routine ---
def seed_default_events():
    """Seed initial Car Game active cycles and Free Fire events if database is empty."""
    # Drop and re-seed if we detect old event configuration (e.g. Event 1 entry fee is not 0.0)
    e1 = car_event_cycles_col.find_one({"event_id": 1, "status": "active"})
    if e1 and e1.get("entry_fee") != 0.0:
        print("Old event configuration detected. Re-seeding Car Game cycles...")
        car_event_cycles_col.delete_many({})

    # Seed Car Game Event 1, 2, 3 cycles
    # Event 1: Free (Watch 3 Ads), Event 2: Rs 1.00, Event 3: Rs 3.00
    for eid, fee, participants, prizes in [
        (1, 0.0, 10, {"1": 0.5}),
        (2, 1.0, 10, {"1": 4.0, "2": 3.0, "3": 1.0}),
        (3, 3.0, 20, {"1": 12.0, "2": 9.0, "3": 7.0, "4": 6.0, "5": 5.0, "6": 4.0, "7": 3.0, "8": 2.0})
    ]:
        active = car_event_cycles_col.find_one({"event_id": eid, "status": "active"})
        if not active:
            car_event_cycles_col.insert_one({
                "event_id": eid,
                "entry_fee": fee,
                "max_participants": participants,
                "prizes": prizes,
                "status": "active",
                "participants": [],
                "created_at": datetime.now(IST)
            })
            
    # Seed Free Fire Event 1
    ff_exists = free_fire_events_col.find_one()
    if not ff_exists:
        free_fire_events_col.insert_one({
            "mode": "BR",
            "map": "Bermuda",
            "entry_fee": 5.0,
            "prize_per_kill": 4.0,
            "max_participants": 50,
            "start_time": "7:00 PM",
            "end_time": "8:00 PM",
            "date": "2026-06-12",
            "room_id": "",
            "room_password": "",
            "slots": {str(i): None for i in range(1, 51)},
            "created_at": datetime.now(IST)
        })
        
    # Seed Cricket Cycle
    cc1 = cricket_event_cycles_col.find_one({"status": "active"})
    if cc1 and (cc1.get("max_participants") != 2 or cc1.get("prizes", {}).get("1") != 1.8):
        print("Updating active cricket cycle parameters...")
        cricket_event_cycles_col.delete_many({"status": "active"})
        cc1 = None
        
    if not cc1:
        cricket_event_cycles_col.insert_one({
            "event_id": 1,
            "entry_fee": 1.0,
            "max_participants": 2,
            "prizes": {"1": 1.8},
            "status": "active",
            "participants": [],
            "created_at": datetime.now(IST)
        })

# Auto seed default events
try:
    seed_default_events()
except Exception as e:
    print(f"Error seeding events: {e}")
