import uuid
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))
from config import Config
from database import tasks_col, task_stats_col, update_balance, get_user, to_ist

def get_backend_url():
    # If a backend URL is configured, use it, otherwise fall back to WEB_APP_URL replacing port if local
    # We can assume it is configured or we can dynamically build it
    # For local testing, we use http://localhost:5000
    # Let's read BACKEND_URL from config or environment, defaulting to http://localhost:5000
    import os
    return os.getenv("BACKEND_URL", "http://localhost:5000")

def get_bot_username():
    # We can get this from config, or use a default
    import os
    return os.getenv("BOT_USERNAME", "buckky_bot")

def check_and_update_expired_tasks(user_id):
    """
    Check all ongoing tasks for a user.
    If they are past their expires_at, mark them as 'expired'
    and increment the user's daily expired task count.
    """
    user_id = int(user_id)
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    
    # Retrieve or create user stats
    stats = task_stats_col.find_one({"_id": user_id})
    if not stats:
        stats = {
            "_id": user_id,
            "expired_count_today": 0,
            "last_expire_date": today_str
        }
        task_stats_col.insert_one(stats)
    elif stats.get("last_expire_date") != today_str:
        # Reset count for a new day
        stats["expired_count_today"] = 0
        stats["last_expire_date"] = today_str
        task_stats_col.update_one(
            {"_id": user_id},
            {"$set": {"expired_count_today": 0, "last_expire_date": today_str}}
        )

    # Find ongoing tasks that have expired
    expired_tasks = list(tasks_col.find({
        "user_id": user_id,
        "status": "ongoing",
        "expires_at": {"$lt": now}
    }))
    
    if expired_tasks:
        # Mark as expired in DB
        task_ids = [t["_id"] for t in expired_tasks]
        tasks_col.update_many(
            {"_id": {"$in": task_ids}},
            {"$set": {"status": "expired"}}
        )
        
        # Increment expired count
        new_count = stats["expired_count_today"] + len(expired_tasks)
        task_stats_col.update_one(
            {"_id": user_id},
            {"$set": {"expired_count_today": new_count}}
        )
        stats["expired_count_today"] = new_count
        
    return stats["expired_count_today"]

def create_or_get_task(user_id):
    """
    Create a new task, or return the ongoing task if it's still valid
    and has more than 10 minutes left in expiration.
    Returns: (task_dict, status_message)
    """
    user_id = int(user_id)
    
    # 1. Update and check expired tasks limit
    expired_today = check_and_update_expired_tasks(user_id)
    if expired_today >= 3:
        return None, "limit_reached"
        
    # 2. Check if there is an active ongoing task
    now = datetime.now(IST)
    ongoing_task = tasks_col.find_one({
        "user_id": user_id,
        "status": "ongoing",
        "expires_at": {"$gt": now}
    })
    
    if ongoing_task:
        remaining_time = to_ist(ongoing_task["expires_at"]) - now
        # If more than 10 minutes (600s) left, return the same task
        if remaining_time.total_seconds() > 600:
            return ongoing_task, "existing"
        else:
            # Force expiration of the current task to allow a new one
            tasks_col.update_one(
                {"_id": ongoing_task["_id"]},
                {"$set": {"status": "expired"}}
            )
            # Increment expired count
            stats = task_stats_col.find_one({"_id": user_id})
            new_count = stats["expired_count_today"] + 1
            task_stats_col.update_one(
                {"_id": user_id},
                {"$set": {"expired_count_today": new_count}}
            )
            
            # Check limit again after forced expiration
            if new_count >= 3:
                return None, "limit_reached"
                
    # 3. Create a new task
    task_id = str(uuid.uuid4())
    backend_url = get_backend_url()
    destination_url = f"{backend_url}/verify-task/{task_id}"
    
    # Call AroLinks API to shorten link
    # Endpoint: https://arolinks.com/api?api=TOKEN&url=DEST_URL
    api_key = Config.AROLINKS_API_KEY
    encoded_url = urllib.parse.quote(destination_url)
    api_url = f"https://arolinks.com/api?api={api_key}&url={encoded_url}"
    
    try:
        response = requests.get(api_url, timeout=10)
        res_json = response.json()
        if res_json.get("status") == "success" and res_json.get("shortenedUrl"):
            shortened_url = res_json["shortenedUrl"]
        else:
            # Fallback for testing/failure
            shortened_url = f"https://arolinks.com/mock-{task_id[:8]}"
    except Exception as e:
        print(f"AroLinks API Error: {e}")
        # Fallback shortened URL
        shortened_url = f"https://arolinks.com/mock-{task_id[:8]}"
        
    expires_at = now + timedelta(minutes=20)
    
    task_doc = {
        "_id": task_id,
        "user_id": user_id,
        "shortened_url": shortened_url,
        "status": "ongoing",
        "created_at": now,
        "expires_at": expires_at
    }
    
    tasks_col.insert_one(task_doc)
    return task_doc, "new"

def verify_and_reward_task(task_id):
    """
    Verify the completed task ID, reward user 0.60 Rs.
    Returns: (success_bool, user_id_or_error_msg)
    """
    now = datetime.now(IST)
    task = tasks_col.find_one({"_id": task_id})
    
    if not task:
        return False, "Task not found"
        
    if task["status"] == "completed":
        return False, "Task already completed"
        
    if task["status"] == "expired" or to_ist(task["expires_at"]) < now:
        if task["status"] == "ongoing":
            # Update to expired
            tasks_col.update_one({"_id": task_id}, {"$set": {"status": "expired"}})
            # Update user stats
            uid = task["user_id"]
            stats = task_stats_col.find_one({"_id": uid})
            if stats:
                task_stats_col.update_one(
                    {"_id": uid},
                    {"$inc": {"expired_count_today": 1}}
                )
        return False, "Task has expired"
        
    # Mark task as completed
    tasks_col.update_one(
        {"_id": task_id},
        {"$set": {"status": "completed"}}
    )
    
    # Reward user wallet
    user_id = task["user_id"]
    success, new_bal = update_balance(
        user_id=user_id,
        amount=0.60,
        tx_type="task_reward",
        details={"task_id": task_id}
    )
    
    if not success:
        return False, "Failed to update user wallet"
        
    return True, user_id
