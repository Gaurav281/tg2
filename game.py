import random
import uuid
from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST)
IST = timezone(timedelta(hours=5, minutes=30))

class HandCricketMatch:
    def __init__(self, match_id=None, player_a_id=None, player_a_name=None, player_b_id=None, player_b_name=None, match_type="free"):
        self.match_id = match_id or str(uuid.uuid4())
        self.type = match_type  # paid, free, challenge
        self.challenge_code = None
        
        self.player_a = {
            "user_id": int(player_a_id) if (player_a_id is not None and player_a_id != "bot") else player_a_id,
            "username": player_a_name or "Player A",
            "score": 0,
            "choices": [],
            "status": None,  # batting, bowling
            "out": False,
            "is_offline": False,
            "current_choice": None
        }
        
        self.player_b = {
            "user_id": int(player_b_id) if (player_b_id is not None and player_b_id != "bot") else player_b_id,
            "username": player_b_name or ("Smart Bot" if player_b_id == "bot" else "Player B"),
            "score": 0,
            "choices": [],
            "status": None,
            "out": False,
            "is_offline": False,
            "current_choice": None
        }
        
        self.status = "toss"  # toss, batting_1, batting_2, completed, cancelled
        self.current_inning = 1
        self.current_ball = 0  # 0 to 6
        
        # Toss parameters
        self.toss_selector = random.choice([self.player_a["user_id"], self.player_b["user_id"]])
        # If selector is bot, bot picks automatically
        self.toss_coin_choice = None # head, tail
        self.toss_coin_result = None # head, tail
        self.toss_winner = None
        self.toss_choice_pending = True
        
        self.winner_id = None
        self.created_at = datetime.now(IST)
        self.rematch_requests = []
        self.ball_results_a = []  # visual score for each ball (e.g. '4', 'OUT', '')
        self.ball_results_b = []
        
        if self.is_bot_turn_for_toss():
            self.bot_select_toss()

    def get_player(self, user_id):
        if str(user_id) == "bot":
            return self.player_b if self.player_b["user_id"] == "bot" else self.player_a
        uid = int(user_id)
        if self.player_a["user_id"] == uid:
            return self.player_a
        if self.player_b["user_id"] == uid:
            return self.player_b
        return None

    def get_opponent(self, user_id):
        if str(user_id) == "bot":
            return self.player_a if self.player_b["user_id"] == "bot" else self.player_b
        uid = int(user_id)
        if self.player_a["user_id"] == uid:
            return self.player_b
        if self.player_b["user_id"] == uid:
            return self.player_a
        return None

    def is_bot_turn_for_toss(self):
        return self.toss_selector == "bot"

    def bot_select_toss(self):
        """If the bot is the toss selector, choose head or tail and spin."""
        choice = random.choice(["head", "tail"])
        self.select_toss_coin(self.player_b["user_id"], choice)

    def select_toss_coin(self, user_id, choice):
        """Player selects Head or Tail, then we spin and determine the winner."""
        if self.status != "toss" or not self.toss_choice_pending:
            return False, "Not in toss phase"
            
        if user_id != self.toss_selector:
            return False, "Not your turn to select toss"
            
        self.toss_coin_choice = choice
        self.toss_coin_result = random.choice(["head", "tail"])
        
        if self.toss_coin_choice == self.toss_coin_result:
            self.toss_winner = self.toss_selector
        else:
            self.toss_winner = self.player_b["user_id"] if self.toss_selector == self.player_a["user_id"] else self.player_a["user_id"]
            
        self.toss_choice_pending = False
        
        # If toss winner is bot, make decision automatically
        if self.toss_winner == "bot":
            bot_opt = random.choice(["batting", "bowling"])
            self.select_toss_option("bot", bot_opt)
            
        return True, {
            "toss_coin_choice": self.toss_coin_choice,
            "toss_coin_result": self.toss_coin_result,
            "toss_winner": self.toss_winner
        }

    def select_toss_option(self, user_id, option):
        """Toss winner selects batting or bowling to start Inning 1."""
        if self.status != "toss" or self.toss_choice_pending:
            return False, "Toss has not been spun yet"
            
        if user_id != self.toss_winner:
            return False, "Not your turn to choose batting or bowling"
            
        if option not in ["batting", "bowling"]:
            return False, "Invalid option (must be batting or bowling)"
            
        winner = self.get_player(self.toss_winner)
        loser = self.get_opponent(self.toss_winner)
        
        if option == "batting":
            winner["status"] = "batting"
            loser["status"] = "bowling"
        else:
            winner["status"] = "bowling"
            loser["status"] = "batting"
            
        self.status = "batting_1"
        self.current_inning = 1
        self.current_ball = 0
        
        # Initialize ball results trackers
        self.ball_results_a = ["" for _ in range(6)]
        self.ball_results_b = ["" for _ in range(6)]
        
        return True, {
            "player_a_status": self.player_a["status"],
            "player_b_status": self.player_b["status"],
            "status": self.status
        }

    def make_choice(self, user_id, choice):
        """Record choice (1-6) for a player. Plays bot choice if applicable."""
        if self.status not in ["batting_1", "batting_2"]:
            return False, "Match is not in active batting state"
            
        player = self.get_player(user_id)
        if not player:
            return False, "Player not found"
            
        if not (1 <= choice <= 6):
            return False, "Choice must be between 1 and 6"
            
        player["current_choice"] = choice
        
        # If playing against bot, bot makes its choice immediately
        if self.player_b["user_id"] == "bot" and self.player_b["current_choice"] is None:
            self.player_b["current_choice"] = self.get_smart_bot_choice()
            
        # Check if both choices are now ready
        if self.player_a["current_choice"] is not None and self.player_b["current_choice"] is not None:
            self.process_ball()
            
        return True, "Choice recorded"

    def get_smart_bot_choice(self):
        """
        Smart bot algorithm.
        Instead of completely random choices, it models typical human strategies:
        - If bot is batsman:
          - Plays standard distribution but leans towards 4, 6 and 3 when safe.
          - If the user (bowler) repeats choices, bot avoids that number.
        - If bot is bowler:
          - Tries to guess the batsman's next choice based on the user's history.
          - Humans rarely repeat the same number back-to-back.
        """
        opponent = self.player_a # since B is bot, A is user
        opp_choices = opponent["choices"]
        bot_choices = self.player_b["choices"]
        
        # Default distribution: 1-6
        weights = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        
        if self.player_b["status"] == "batting":
            # If bot is batting, try to score runs.
            # Lean towards 4 and 6, but slightly random.
            weights = [0.8, 1.2, 1.0, 1.4, 0.6, 1.5]
            
            # Avoid the last choice bowler did (bowler might repeat)
            if opp_choices:
                last_bowl = opp_choices[-1]
                weights[last_bowl - 1] *= 0.3
                
            # Avoid repeating its own last choice too much
            if bot_choices:
                last_bat = bot_choices[-1]
                weights[last_bat - 1] *= 0.5
                
        else: # bot is bowling
            # Try to guess what batsman (user) will play.
            # If user has history, check if they have a favorite number.
            if opp_choices:
                last_user_choice = opp_choices[-1]
                # Humans rarely repeat the same choice back to back
                weights[last_user_choice - 1] *= 0.3
                
                # Check user patterns (e.g. alternating or incrementing)
                if len(opp_choices) >= 2:
                    diff = opp_choices[-1] - opp_choices[-2]
                    next_predicted = (opp_choices[-1] + diff)
                    if 1 <= next_predicted <= 6:
                        weights[next_predicted - 1] *= 1.5
                        
                # Otherwise, guess favorite numbers: 4, 6, 3 are popular
                weights[3] *= 1.2 # number 4
                weights[5] *= 1.2 # number 6
                weights[2] *= 1.1 # number 3
                
        choices_list = [1, 2, 3, 4, 5, 6]
        return random.choices(choices_list, weights=weights)[0]

    def handle_ball_timeout(self, timed_out_user_id):
        """
        Processes a ball when a 6s timer expires.
        If user_id is passed, it means that player timed out.
        If both players timed out, we pass None.
        """
        if self.status not in ["batting_1", "batting_2"]:
            return False
            
        p_a = self.player_a
        p_b = self.player_b
        
        # Resolve choices
        if p_a["current_choice"] is None:
            p_a["current_choice"] = -1  # indicates timeout
        if p_b["current_choice"] is None:
            p_b["current_choice"] = -1
            
        self.process_ball()
        return True

    def process_ball(self):
        """Calculate runs/outs based on player choices for the current ball."""
        p_a = self.player_a
        p_b = self.player_b
        
        choice_a = p_a["current_choice"]
        choice_b = p_b["current_choice"]
        
        # Log choices in arrays (filter out timeout indicators)
        p_a["choices"].append(choice_a if choice_a > 0 else 0)
        p_b["choices"].append(choice_b if choice_b > 0 else 0)
        
        # Identify batsman and bowler
        bat = p_a if p_a["status"] == "batting" else p_b
        bowl = p_b if p_a["status"] == "batting" else p_a
        
        # Visual index for circles (0-5)
        ball_idx = self.current_ball
        self.current_ball += 1
        
        # Handle Timeout Scenarios
        # -1 represents timeout
        if choice_a == -1 or choice_b == -1:
            if choice_a == -1 and choice_b == -1:
                # Both timed out: batsman score += 0, bowl gets 0
                runs = 0
                ball_result = "TIMEOUT"
            elif bat["current_choice"] == -1:
                # Batsman timed out: 0 runs
                runs = 0
                ball_result = "T.O. (0)"
            else:
                # Bowler timed out: Batsman choice is added directly
                runs = bat["current_choice"]
                ball_result = f"+{runs} (TO)"
                bat["score"] += runs
        else:
            # Normal play
            if choice_a == choice_b:
                # OUT!
                bat["out"] = True
                runs = 0
                ball_result = "OUT"
            else:
                runs = bat["current_choice"]
                ball_result = f"+{runs}"
                bat["score"] += runs
                
        # Record ball visual result
        if bat == p_a:
            self.ball_results_a[ball_idx] = ball_result
        else:
            self.ball_results_b[ball_idx] = ball_result
            
        # Reset choices for next ball
        p_a["current_choice"] = None
        p_b["current_choice"] = None
        
        # Check inning transition or match completion
        self.check_inning_state()

    def check_inning_state(self):
        p_a = self.player_a
        p_b = self.player_b
        
        bat = p_a if p_a["status"] == "batting" else p_b
        bowl = p_b if p_a["status"] == "batting" else p_a
        
        if self.status == "batting_1":
            # Inning 1 Ends if: batsman is OUT or 6 balls are completed
            if bat["out"] or self.current_ball >= 6:
                # Inning Change!
                self.current_inning = 2
                self.status = "batting_2"
                self.current_ball = 0
                
                # Swap batting/bowling
                bat["status"] = "bowling"
                bowl["status"] = "batting"
                
                # If bot is now batting, automatically trigger choice for next ball
                if self.player_b["user_id"] == "bot" and self.player_b["status"] == "batting":
                    # Bot choice will be recorded when user submits choice
                    pass
        
        elif self.status == "batting_2":
            # Inning 2 Target check
            target = bowl["score"] + 1  # Bowl score was Inning 1 score
            
            # Inning 2 Ends if:
            # - Batsman (Inning 2) reaches target (Batsman wins!)
            # - Batsman is OUT
            # - 6 balls are completed
            
            if bat["score"] >= target:
                # Batsman wins!
                self.complete_match(winner_id=bat["user_id"])
            elif bat["out"] or self.current_ball >= 6:
                # Inning 2 ends without reaching target
                if bat["score"] == target - 1:
                    # Score is tied! DRAW
                    self.complete_match(winner_id="draw")
                else:
                    # Bowler wins (Inning 1 batsman)
                    self.complete_match(winner_id=bowl["user_id"])

    def complete_match(self, winner_id):
        self.status = "completed"
        self.winner_id = winner_id
        
    def handle_player_forfeit(self, forfeiting_user_id):
        """If a player forfeits, the other player wins immediately."""
        if self.status == "completed":
            return
            
        self.status = "completed"
        uid = int(forfeiting_user_id)
        
        if self.player_a["user_id"] == uid:
            self.winner_id = self.player_b["user_id"]
        else:
            self.winner_id = self.player_a["user_id"]

    def to_dict(self):
        """Return JSON-serializable representation of the match."""
        return {
            "match_id": self.match_id,
            "type": self.type,
            "challenge_code": self.challenge_code,
            "player_a": self.player_a,
            "player_b": self.player_b,
            "status": self.status,
            "current_inning": self.current_inning,
            "current_ball": self.current_ball,
            "toss_selector": self.toss_selector,
            "toss_coin_choice": self.toss_coin_choice,
            "toss_coin_result": self.toss_coin_result,
            "toss_winner": self.toss_winner,
            "toss_choice_pending": self.toss_choice_pending,
            "winner_id": self.winner_id,
            "created_at": self.created_at.isoformat(),
            "rematch_requests": self.rematch_requests,
            "ball_results_a": self.ball_results_a,
            "ball_results_b": self.ball_results_b
        }
