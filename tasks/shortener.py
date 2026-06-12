import uuid
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))
from config import Config
from database import tasks_col, update_balance, to_ist

def get_backend_url():
    import os
    return os.getenv("BACKEND_URL", "https://tg2-w82m.onrender.com")

def get_bot_username():
    import os
    return os.getenv("BOT_USERNAME", "battleplay_bot")

def create_or_get_task(user_id):
    """
    Create a new task, or return the ongoing task if it exists.
    Returns: (task_dict, status_message)
    """
    user_id = int(user_id)
    now = datetime.now(IST)
    
    # Calculate start of today in IST
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 1. Check if user already completed 4 tasks today
    completed_today = tasks_col.count_documents({
        "user_id": user_id,
        "status": "completed",
        "completed_at": {"$gte": start_of_today}
    })
    
    if completed_today >= 4:
        return None, "limit_reached"
        
    # 2. Check if there is an active ongoing task
    ongoing_task = tasks_col.find_one({
        "user_id": user_id,
        "status": "ongoing"
    })
    
    if ongoing_task:
        if "mock-" in ongoing_task.get("shortened_url", ""):
            tasks_col.update_one({"_id": ongoing_task["_id"]}, {"$set": {"status": "expired"}})
        else:
            return ongoing_task, "existing"
        
    # 3. Create a new task.
    # First 2 links: arolinks
    # Next 2 links: vplink
    task_id = str(uuid.uuid4())
    bot_uname = get_bot_username()
    destination_url = f"https://t.me/{bot_uname}?start=verify_{task_id}"
    encoded_url = urllib.parse.quote(destination_url)
    
    shortened_url = None
    
    if completed_today < 2:
        # Use AroLinks API
        api_key = Config.AROLINKS_API_KEY
        api_url = f"https://arolinks.com/api?api={api_key}&url={encoded_url}"
        try:
            response = requests.get(api_url, timeout=10)
            res_json = response.json()
            if res_json.get("status") == "success" and res_json.get("shortenedUrl"):
                shortened_url = res_json["shortenedUrl"]
            else:
                print(f"AroLinks API success false or missing shortenedUrl: {res_json}")
        except Exception as e:
            print(f"AroLinks API Call Error: {e}")
    else:
        # Use VPLink API
        api_key = Config.VPLINK_API_KEY
        api_url = f"https://vplink.in/api?api={api_key}&url={encoded_url}"
        try:
            response = requests.get(api_url, timeout=10)
            res_json = response.json()
            if res_json.get("status") == "success" and res_json.get("shortenedUrl"):
                shortened_url = res_json["shortenedUrl"]
            else:
                print(f"VPLink API success false or missing shortenedUrl: {res_json}")
        except Exception as e:
            print(f"VPLink API Call Error: {e}")
            
    # Fallback to direct verification url if APIs fail
    if not shortened_url:
        shortened_url = destination_url
        
    task_doc = {
        "_id": task_id,
        "user_id": user_id,
        "shortened_url": shortened_url,
        "status": "ongoing",
        "created_at": now
    }
    
    tasks_col.insert_one(task_doc)
    return task_doc, "new"

def verify_and_reward_task(task_id):
    """
    Verify the completed task ID, reward user 0.50 Rs.
    Returns: (success_bool, user_id_or_error_msg)
    """
    now = datetime.now(IST)
    task = tasks_col.find_one({"_id": task_id})
    
    if not task:
        return False, "Task not found"
        
    if task["status"] == "completed":
        return False, "Task already completed"
        
    # Mark task as completed
    tasks_col.update_one(
        {"_id": task_id},
        {"$set": {
            "status": "completed",
            "completed_at": now
        }}
    )
    
    # Reward user wallet (0.50 Rs)
    user_id = task["user_id"]
    success, new_bal = update_balance(
        user_id=user_id,
        amount=0.50,
        tx_type="task_reward",
        details={"task_id": task_id}
    )
    
    if not success:
        return False, "Failed to update user wallet"
        
    return True, user_id
