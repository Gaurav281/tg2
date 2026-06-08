import unittest
from game import HandCricketMatch

class TestHandCricketGame(unittest.TestCase):

    def test_toss_and_choices(self):
        match = HandCricketMatch(
            player_a_id=11111,
            player_a_name="Player 1",
            player_b_id=22222,
            player_b_name="Player 2"
        )
        
        self.assertEqual(match.status, "toss")
        self.assertTrue(match.toss_choice_pending)
        
        # Determine who should spin the toss
        selector = match.toss_selector
        self.assertIn(selector, [11111, 22222])
        
        # Selector spins toss
        success, res = match.select_toss_coin(selector, "head")
        self.assertTrue(success)
        self.assertFalse(match.toss_choice_pending)
        self.assertIn(match.toss_winner, [11111, 22222])
        
        # Toss winner selects batting or bowling
        toss_winner = match.toss_winner
        success, res = match.select_toss_option(toss_winner, "batting")
        self.assertTrue(success)
        self.assertEqual(match.status, "batting_1")
        self.assertEqual(match.current_inning, 1)
        self.assertEqual(match.current_ball, 0)
        
        # Get batsman and bowler
        bat = match.player_a if match.player_a["status"] == "batting" else match.player_b
        bowl = match.player_b if match.player_a["status"] == "batting" else match.player_a
        
        # Play ball 1 (No out)
        # Batsman plays 4, Bowler plays 2
        success, res = match.make_choice(bat["user_id"], 4)
        self.assertTrue(success)
        success, res = match.make_choice(bowl["user_id"], 2)
        self.assertTrue(success)
        
        self.assertEqual(bat["score"], 4)
        self.assertEqual(match.current_ball, 1)
        self.assertFalse(bat["out"])
        
        # Play ball 2 (Out!)
        # Batsman plays 6, Bowler plays 6
        success, res = match.make_choice(bat["user_id"], 6)
        self.assertTrue(success)
        success, res = match.make_choice(bowl["user_id"], 6)
        self.assertTrue(success)
        
        # Since batsman got out, inning change should happen automatically
        self.assertEqual(match.current_inning, 2)
        self.assertEqual(match.status, "batting_2")
        self.assertEqual(match.current_ball, 0)
        self.assertTrue(bat["out"])
        
        # In second inning, roles swap
        # The previous bowler is now batting, trying to chase target of 4 + 1 = 5
        self.assertEqual(bowl["status"], "batting")
        self.assertEqual(bat["status"], "bowling")
        
        # Second inning batsman plays 4, bowler plays 3 (score = 4)
        success, res = match.make_choice(bowl["user_id"], 4)
        self.assertTrue(success)
        success, res = match.make_choice(bat["user_id"], 3)
        self.assertTrue(success)
        
        self.assertEqual(bowl["score"], 4)
        self.assertEqual(match.status, "batting_2")
        
        # Second inning batsman plays 2, bowler plays 1 (score = 6, exceeds target of 5, wins!)
        success, res = match.make_choice(bowl["user_id"], 2)
        self.assertTrue(success)
        success, res = match.make_choice(bat["user_id"], 1)
        self.assertTrue(success)
        
        self.assertEqual(bowl["score"], 6)
        self.assertEqual(match.status, "completed")
        self.assertEqual(match.winner_id, bowl["user_id"])

    def test_bot_match(self):
        match = HandCricketMatch(
            player_a_id=11111,
            player_a_name="Player 1",
            player_b_id="bot"
        )
        
        # Since bot might be selector, if bot selector, it spins automatically
        if match.toss_choice_pending:
            success, res = match.select_toss_coin(11111, "head")
            self.assertTrue(success)
            
        if match.status == "toss":
            # Player 1 won, choose batting
            success, res = match.select_toss_option(11111, "batting")
            self.assertTrue(success)
            
        self.assertEqual(match.status, "batting_1")
        
        # Play a turn. User plays 5. Bot choice should be auto-generated.
        success, res = match.make_choice(11111, 5)
        self.assertTrue(success)
        
        # Verify that bot's choice was populated and ball was processed
        self.assertIsNone(match.player_a["current_choice"])
        self.assertIsNone(match.player_b["current_choice"])
        
        # It could either progress to ball 1 of inning 1, or if they got OUT, transition to inning 2 ball 0
        self.assertTrue(
            (match.current_inning == 1 and match.current_ball == 1) or
            (match.current_inning == 2 and match.current_ball == 0)
        )

if __name__ == "__main__":
    unittest.main()
