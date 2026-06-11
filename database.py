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

def get_user(user_id):
    """Retrieve user details by Telegram user_id."""
    return users_col.find_one({"_id": int(user_id)})

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
        "username": username or f"User_{user_id}",
        "first_name": first_name or "",
        "balance": 0.5,  # Signup bonus
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
    
    # Log the signup bonus transaction
    create_transaction(
        user_id=user_id,
        tx_type="signup_bonus",
        amount=0.5,
        status="completed",
        details={"message": "Welcome signup bonus"}
    )
    
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
    Atomically update user balance and log a transaction.
    If amount is negative, checks that the user has sufficient balance (no overdraft).
    Returns (success: bool, new_balance: float).
    """
    user_id = int(user_id)
    amount = round(float(amount), 2)
    
    if amount == 0:
        user = get_user(user_id)
        return True, user["balance"] if user else 0.0

    # Ensure no negative balance.
    # If deducting (amount < 0), balance must be >= abs(amount).
    query = {"_id": user_id}
    if amount < 0:
        query["balance"] = {"$gte": abs(amount)}
        
    # Atomically update balance
    user = users_col.find_one_and_update(
        query,
        {"$inc": {"balance": amount}},
        return_document=True
    )
    
    if not user:
        return False, 0.0
        
    # Log transaction
    create_transaction(
        user_id=user_id,
        tx_type=tx_type,
        amount=amount,
        status="completed" if tx_type not in ["deposit", "redeem"] else "pending",
        details=details
    )
    
    # If the transaction is a deposit or mission related, trigger missions check
    if tx_type == "deposit" and amount > 0:
        # Deposit is manually approved, so we update the missions when APPROVED, not here
        pass
        
    return True, round(user["balance"], 2)

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
        {"$inc": {"balance": final_amount}}
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
        {"$inc": {"balance": amount}}
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
        {"$inc": {"balance": abs(tx["amount"])}}
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
        "matches_3": 0.20,
        "add_balance": 0.30
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

def claim_daily_streak(user_id):
    """Claim daily streak reward."""
    user_id = int(user_id)
    user = get_user(user_id)
    if not user:
        return False, "User not found"
        
    # Check if user has played at least 1 paid match today (IST)
    start_of_today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_today = start_of_today + timedelta(days=1)
    
    played_paid_today = matches_col.find_one({
        "type": "paid",
        "created_at": {"$gte": start_of_today, "$lt": end_of_today},
        "$or": [
            {"player_a.user_id": user_id},
            {"player_b.user_id": user_id}
        ]
    })
    
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
