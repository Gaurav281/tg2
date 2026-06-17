import random
import uuid

VALUE_MAP = {
    '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9, '10': 10,
    'J': 11, 'Q': 12, 'K': 13, 'A': 14
}

def get_new_shuffled_deck(num_decks=2):
    """Generate and shuffle standard playing card decks (no Jokers, ensuring pure sequences)."""
    suits = ['H', 'D', 'C', 'S']
    values = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    deck = []
    for _ in range(num_decks):
        for s in suits:
            for v in values:
                deck.append(v + s)
    random.shuffle(deck)
    return deck

def is_valid_sequence(cards):
    """Check if cards list forms a valid sequence (consecutive values of the same suit)."""
    if len(cards) < 3:
        return False
    
    # Check if all cards have the same suit
    suits = {c[-1] for c in cards}
    if len(suits) != 1:
        return False
        
    vals = [c[:-1] for c in cards]
    
    # Case 1: Ace as high (A = 14)
    int_vals_14 = [VALUE_MAP[v] for v in vals]
    int_vals_14.sort()
    
    is_consec_14 = True
    for i in range(len(int_vals_14) - 1):
        if int_vals_14[i+1] - int_vals_14[i] != 1:
            is_consec_14 = False
            break
    if is_consec_14:
        return True
        
    # Case 2: Ace as low (A = 1)
    int_vals_1 = [1 if v == 'A' else VALUE_MAP[v] for v in vals]
    int_vals_1.sort()
    
    is_consec_1 = True
    for i in range(len(int_vals_1) - 1):
        if int_vals_1[i+1] - int_vals_1[i] != 1:
            is_consec_1 = False
            break
    return is_consec_1

def is_valid_set(cards):
    """Check if cards list forms a valid set (same value, unique suits, 3 or 4 cards)."""
    if len(cards) < 3 or len(cards) > 4:
        return False
        
    vals = {c[:-1] for c in cards}
    if len(vals) != 1:
        return False
        
    suits = [c[-1] for c in cards]
    if len(suits) != len(set(suits)):
        return False
        
    return True

def validate_rummy_hand(groups):
    """Validate full 13-card hand declaration: min 2 sequences, all cards melded."""
    flat_cards = [card for g in groups for card in g]
    if len(flat_cards) != 13:
        return False, f"Hand must contain exactly 13 cards, but got {len(flat_cards)}."
        
    sequence_count = 0
    set_count = 0
    
    for idx, g in enumerate(groups):
        if len(g) < 3:
            return False, f"Group {idx+1} has {len(g)} cards; every group must contain at least 3 cards."
            
        if is_valid_sequence(g):
            sequence_count += 1
        elif is_valid_set(g):
            set_count += 1
        else:
            return False, f"Group {idx+1} ({', '.join(g)}) is not a valid sequence or set."
            
    if sequence_count < 2:
        return False, "You must form at least 2 valid sequences to declare."
        
    return True, "Valid declaration!"

class RummyMatch:
    def __init__(self, match_id=None, creator_id=None, creator_name=None, max_players=4):
        self.match_id = match_id or str(uuid.uuid4())
        self.room_code = None  # 6-digit lobby code
        self.creator_id = int(creator_id) if creator_id else None
        self.max_players = max_players  # 2, 3, or 4
        self.players = []  # dicts: {"user_id": int, "username": str, "hand": [], "groups": [], "is_host": bool, "is_offline": bool}
        self.deck = []
        self.discard_pile = []
        self.status = "waiting"  # waiting, playing, completed
        self.current_turn_index = 0
        self.turn_state = "draw"  # draw, discard
        self.winner_id = None
        self.declaration_error = None
        
        if creator_id:
            self.add_player(creator_id, creator_name, is_host=True)

    def add_player(self, user_id, username, is_host=False):
        user_id = int(user_id)
        for p in self.players:
            if p["user_id"] == user_id:
                p["is_offline"] = False
                return True
        if len(self.players) >= self.max_players:
            return False
            
        self.players.append({
            "user_id": user_id,
            "username": username or f"User_{user_id}",
            "hand": [],
            "groups": [],
            "is_host": is_host,
            "is_offline": False
        })
        return True

    def remove_player(self, user_id):
        user_id = int(user_id)
        self.players = [p for p in self.players if p["user_id"] != user_id]
        if not self.players:
            return True
        if not any(p["is_host"] for p in self.players):
            self.players[0]["is_host"] = True
            self.creator_id = self.players[0]["user_id"]
        return False

    def start_game(self):
        if len(self.players) < 2:
            return False, "A minimum of 2 players is required to start the match."
            
        # Shuffle 2 decks
        self.deck = get_new_shuffled_deck(num_decks=2)
        self.discard_pile = []
        
        # Deal 13 cards per player
        for p in self.players:
            p["hand"] = [self.deck.pop(0) for _ in range(13)]
            p["groups"] = [list(p["hand"])]
            
        # First open card
        self.discard_pile.append(self.deck.pop(0))
        
        self.status = "playing"
        self.current_turn_index = 0
        self.turn_state = "draw"
        self.winner_id = None
        self.declaration_error = None
        return True, "Game started."

    def draw_card(self, user_id, source):
        user_id = int(user_id)
        active_player = self.players[self.current_turn_index]
        if active_player["user_id"] != user_id:
            return False, "It is not your turn."
        if self.turn_state != "draw":
            return False, "You have already drawn a card for this turn."
            
        if source == "deck":
            if not self.deck:
                if len(self.discard_pile) > 1:
                    top_card = self.discard_pile.pop()
                    self.deck = list(self.discard_pile)
                    random.shuffle(self.deck)
                    self.discard_pile = [top_card]
                else:
                    return False, "The card deck is completely empty."
            card = self.deck.pop(0)
        elif source == "discard":
            if not self.discard_pile:
                return False, "The discard pile is empty."
            card = self.discard_pile.pop()
        else:
            return False, "Invalid draw source."
            
        active_player["hand"].append(card)
        if active_player["groups"]:
            active_player["groups"][0].append(card)
        else:
            active_player["groups"] = [[card]]
            
        self.turn_state = "discard"
        return True, card

    def discard_card(self, user_id, card):
        user_id = int(user_id)
        active_player = self.players[self.current_turn_index]
        if active_player["user_id"] != user_id:
            return False, "It is not your turn."
        if self.turn_state != "discard":
            return False, "You must draw a card before discarding."
        if card not in active_player["hand"]:
            return False, "Discard target card not found in your hand."
            
        active_player["hand"].remove(card)
        for g in active_player["groups"]:
            if card in g:
                g.remove(card)
                break
        active_player["groups"] = [g for g in active_player["groups"] if g]
        
        self.discard_pile.append(card)
        self.current_turn_index = (self.current_turn_index + 1) % len(self.players)
        self.turn_state = "draw"
        return True, "Discard completed."

    def update_groups(self, user_id, groups):
        user_id = int(user_id)
        for p in self.players:
            if p["user_id"] == user_id:
                flat_groups = [c for g in groups for c in g]
                if sorted(flat_groups) == sorted(p["hand"]):
                    p["groups"] = groups
                    return True
        return False

    def declare(self, user_id, groups):
        user_id = int(user_id)
        active_player = self.players[self.current_turn_index]
        if active_player["user_id"] != user_id:
            return False, "It is not your turn to declare."
            
        flat_groups = [c for g in groups for c in g]
        if len(flat_groups) != 13:
            return False, f"Declaration hand must contain exactly 13 cards (got {len(flat_groups)})."
            
        hand_copy = list(active_player["hand"])
        for c in flat_groups:
            if c in hand_copy:
                hand_copy.remove(c)
            else:
                return False, f"Card {c} in groups is not in your current hand."
                
        declared_discard = hand_copy[0]
        is_valid, msg = validate_rummy_hand(groups)
        if is_valid:
            active_player["hand"].remove(declared_discard)
            self.discard_pile.append(declared_discard)
            active_player["groups"] = groups
            self.status = "completed"
            self.winner_id = user_id
            return True, "Congratulations! Valid declaration."
        else:
            self.declaration_error = msg
            return False, msg

    def to_dict(self, self_user_id=None):
        players_data = []
        for p in self.players:
            is_self = (p["user_id"] == self_user_id)
            players_data.append({
                "user_id": p["user_id"],
                "username": p["username"],
                "is_host": p["is_host"],
                "is_offline": p["is_offline"],
                "card_count": len(p["hand"]),
                "groups": p["groups"] if (is_self or self.status == "completed") else [],
                "hand": p["hand"] if (is_self or self.status == "completed") else []
            })
            
        return {
            "match_id": self.match_id,
            "room_code": self.room_code,
            "creator_id": self.creator_id,
            "max_players": self.max_players,
            "players": players_data,
            "discard_top": self.discard_pile[-1] if self.discard_pile else None,
            "discard_pile": self.discard_pile if self.status == "completed" else [],
            "deck_count": len(self.deck),
            "status": self.status,
            "current_turn_index": self.current_turn_index,
            "active_turn_user_id": self.players[self.current_turn_index]["user_id"] if self.players and self.status == "playing" else None,
            "turn_state": self.turn_state,
            "winner_id": self.winner_id,
            "declaration_error": self.declaration_error
        }

# Global in-memory registry for Rummy matches
rummy_matches = {}
