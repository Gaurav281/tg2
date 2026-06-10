import time
import threading
import random
from game import HandCricketMatch

class Matchmaker:
    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []  # List of dicts: {"user_id": int, "username": str, "socket_id": str, "entered_at": float}
        self.active_matches = {}  # match_id -> HandCricketMatch object
        self.user_to_match = {}   # user_id -> match_id

    def add_to_queue(self, user_id, username, socket_id):
        """Add user to matchmaking queue. If another user is waiting, matches them."""
        user_id = int(user_id)
        with self.lock:
            # Check if user is already in a match
            if user_id in self.user_to_match:
                existing_match_id = self.user_to_match[user_id]
                match = self.active_matches.get(existing_match_id)
                if match and match.status != "completed":
                    return {"status": "already_in_match", "match_id": existing_match_id}

            # Remove from queue if already present
            self.queue = [item for item in self.queue if item["user_id"] != user_id]

            # If there's anyone else in the queue, match them!
            if self.queue:
                opponent = self.queue.pop(0)
                # Ensure they are not matching with themselves
                if opponent["user_id"] == user_id:
                    self.queue.append(opponent) # re-add
                else:
                    # Create match
                    match = HandCricketMatch(
                        player_a_id=opponent["user_id"],
                        player_a_name=opponent["username"],
                        player_b_id=user_id,
                        player_b_name=username,
                        match_type="paid"
                    )
                    self.active_matches[match.match_id] = match
                    self.user_to_match[opponent["user_id"]] = match.match_id
                    self.user_to_match[user_id] = match.match_id
                    return {
                        "status": "matched",
                        "match_id": match.match_id,
                        "player_a": match.player_a,
                        "player_b": match.player_b,
                        "opponent_socket_id": opponent["socket_id"]
                    }

            # Otherwise, add to queue
            self.queue.append({
                "user_id": user_id,
                "username": username,
                "socket_id": socket_id,
                "entered_at": time.time()
            })
            return {"status": "queued"}

    def remove_from_queue(self, user_id):
        """Remove user from queue if they cancel matchmaking."""
        user_id = int(user_id)
        with self.lock:
            self.queue = [item for item in self.queue if item["user_id"] != user_id]

    def check_bot_fallback(self, user_id):
        """
        Check if user is still in queue and has waited > 6 seconds.
        If so, remove them and start a match against the bot.
        """
        user_id = int(user_id)
        with self.lock:
            # Find user in queue
            user_item = None
            for item in self.queue:
                if item["user_id"] == user_id:
                    user_item = item
                    break
            
            if not user_item:
                return None  # Already matched or left queue
                
            # If waited 6 seconds
            if time.time() - user_item["entered_at"] >= 5.8: # slight buffer
                self.queue.remove(user_item)
                
                # Start bot match
                match = HandCricketMatch(
                    player_a_id=user_id,
                    player_a_name=user_item["username"],
                    player_b_id="bot",
                    player_b_name="Smart Bot",
                    match_type="paid"
                )
                self.active_matches[match.match_id] = match
                self.user_to_match[user_id] = match.match_id
                return match
                
            return None

    def create_challenge_match(self, host_id, host_name, match_type="challenge"):
        """Create a challenge match shell waiting for opponent to join."""
        host_id = int(host_id)
        with self.lock:
            # Create a unique match
            match = HandCricketMatch(
                player_a_id=host_id,
                player_a_name=host_name,
                player_b_id=None,
                player_b_name="Waiting...",
                match_type=match_type
            )
            # Generate unique 6-digit challenge code
            attempts = 0
            while attempts < 100:
                code = f"{random.randint(100000, 999999)}"
                code_exists = any(m.challenge_code == code and m.status == "waiting" for m in self.active_matches.values())
                if not code_exists:
                    match.challenge_code = code
                    break
                attempts += 1
                
            # Set status to waiting for opponent
            match.status = "waiting"
            self.active_matches[match.match_id] = match
            self.user_to_match[host_id] = match.match_id
            return match

    def join_challenge_by_code(self, challenge_code, guest_id, guest_name):
        """Guest joins a pending challenge match using 6-digit code."""
        guest_id = int(guest_id)
        challenge_code = str(challenge_code).strip()
        with self.lock:
             # Find the active waiting match with this code
            match = None
            for m in self.active_matches.values():
                if m.challenge_code == challenge_code and m.status == "waiting":
                    match = m
                    break
                    
            if not match:
                return None, "Challenge code not found or already started"
                
            if match.player_a["user_id"] == guest_id:
                return None, "You cannot challenge yourself"
                
            # Populate player_b details
            match.player_b["user_id"] = guest_id
            match.player_b["username"] = guest_name
            match.player_b["status"] = None
            
            # Change status to toss
            match.status = "toss"
            match.toss_selector = random.choice([match.player_a["user_id"], match.player_b["user_id"]])
            match.toss_choice_pending = True
            
            self.user_to_match[guest_id] = match.match_id
            return match, None

    def join_challenge_match(self, match_id, guest_id, guest_name):
        """Guest joins a pending challenge match."""
        guest_id = int(guest_id)
        with self.lock:
            match = self.active_matches.get(match_id)
            if not match:
                return None, "Challenge not found"
                
            if match.status != "waiting":
                return None, "Challenge already started or expired"
                
            if match.player_a["user_id"] == guest_id:
                return None, "You cannot challenge yourself"
                
            # Populate player_b details
            match.player_b["user_id"] = guest_id
            match.player_b["username"] = guest_name
            match.player_b["status"] = None
            
            # Change status to toss
            match.status = "toss"
            match.toss_selector = random.choice([match.player_a["user_id"], match.player_b["user_id"]])
            match.toss_choice_pending = True
            
            self.user_to_match[guest_id] = match_id
            return match, None

    def get_match(self, match_id):
        return self.active_matches.get(match_id)

    def clean_completed_match(self, match_id):
        """Remove completed match associations to allow new matches."""
        with self.lock:
            match = self.active_matches.get(match_id)
            if match:
                # Remove reverse mapping
                if match.player_a["user_id"] != "bot":
                    self.user_to_match.pop(match.player_a["user_id"], None)
                if match.player_b["user_id"] != "bot":
                    self.user_to_match.pop(match.player_b["user_id"], None)
                # Do not delete match object completely, as we might need it for statistics or Socket references temporarily
                # But we mark it finished

    def get_paid_playing_count(self):
        with self.lock:
            # count players in paid matchmaking queue
            queue_count = len(self.queue)
            # count players in active, non-completed paid matches
            active_count = 0
            for m in self.active_matches.values():
                if m.type == "paid" and m.status not in ["completed", "cancelled"]:
                    if m.player_a["user_id"] != "bot":
                        active_count += 1
                    if m.player_b["user_id"] != "bot":
                        active_count += 1
            total = queue_count + active_count
            if total < 2:
                # generate a baseline active players count like 2, 4, 6, 8
                import random
                # stable based on time minutes
                minute_seed = int(time.time() / 60)
                random.seed(minute_seed)
                total = random.choice([2, 4, 6, 8])
                random.seed()
            return total

# Global Matchmaker Instance
matchmaker = Matchmaker()
