"""Offline checks for score-event safety. No Telegram or paid API is used."""
import unittest
from unittest.mock import patch

import numpy as np

from engine import _local_score_timeline


class ScoreTimelineTests(unittest.TestCase):
    def frames(self, count):
        return [np.zeros((8, 8, 3), dtype=np.uint8) for _ in range(count)]

    def test_stable_one_zero_becomes_one_home_goal(self):
        observations = iter([(0, 0), (0, 0), (1, 0), (1, 0), (1, 0)])
        with patch("engine._score_from_frame", side_effect=lambda _: next(observations)):
            goals = _local_score_timeline(self.frames(5), 20)
        self.assertEqual([(goal.team, round(goal.second)) for goal in goals], [("home", 15)])

    def test_one_bad_ocr_frame_does_not_create_a_goal(self):
        observations = iter([(0, 0), (0, 0), (1, 0), (0, 0), (0, 0)])
        with patch("engine._score_from_frame", side_effect=lambda _: next(observations)):
            goals = _local_score_timeline(self.frames(5), 20)
        self.assertEqual(goals, [])

    def test_away_score_is_assigned_to_right_hand_team(self):
        observations = iter([(0, 0), (0, 0), (0, 1), (0, 1)])
        with patch("engine._score_from_frame", side_effect=lambda _: next(observations)):
            goals = _local_score_timeline(self.frames(4), 12)
        self.assertEqual([goal.team for goal in goals], ["away"])


if __name__ == "__main__":
    unittest.main()
