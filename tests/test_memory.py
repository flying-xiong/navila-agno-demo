"""Tests for NaVILA's history-frame memory and the executor's frame sources."""
from __future__ import annotations

import unittest

from navila_agno.contracts import MidLevelAction, SubGoal
from navila_agno.navila_memory import (
    DEFAULT_NUM_VIDEO_FRAMES,
    plan_frame_selection,
    sample_and_pad_paths,
)
from navila_agno.robot import MockRobot
from navila_agno.vla import VLAExecutor, MockVLA


class SelectionTests(unittest.TestCase):
    def test_short_history_pads_front_and_keeps_everything(self) -> None:
        self.assertEqual(
            sample_and_pad_paths(["a", "b", "c"], 8),
            [None, None, None, None, None, "a", "b", "c"],
        )

    def test_exact_length_is_identity(self) -> None:
        frames = [f"f{i}" for i in range(8)]
        self.assertEqual(sample_and_pad_paths(frames, 8), frames)

    def test_long_history_spans_the_whole_trajectory(self) -> None:
        selection = sample_and_pad_paths([f"f{i}" for i in range(100)], 8)
        self.assertEqual(selection[0], "f0")  # start of the mission is kept
        self.assertEqual(selection[-1], "f99")  # current frame is always last
        self.assertEqual(len(selection), 8)
        picked = [int(str(item)[1:]) for item in selection]
        self.assertEqual(picked, sorted(picked))
        # Far more spread out than a sliding window of the last 8 frames.
        self.assertGreater(picked[1], 8)

    def test_single_frame_history_is_all_padding_plus_current(self) -> None:
        self.assertEqual(sample_and_pad_paths(["only"], 8), [None] * 7 + ["only"])

    def test_num_frames_one_returns_current(self) -> None:
        self.assertEqual(sample_and_pad_paths(["a", "b", "c"], 1), ["c"])

    def test_rejects_empty_history(self) -> None:
        with self.assertRaises(ValueError):
            plan_frame_selection(0, 8)

    def test_matches_official_numpy_sampling(self) -> None:
        try:
            import numpy as np
        except ImportError:  # pragma: no cover - numpy is optional here
            self.skipTest("numpy 未安装")

        for length in range(1, 130):
            for num_frames in (1, 2, 3, 8, 16):
                frames = list(range(length))
                if len(frames) < num_frames:
                    while len(frames) < num_frames:
                        frames.insert(0, None)
                    expected = frames
                else:
                    latest = frames[-1]
                    idx = np.linspace(
                        0, len(frames) - 1, num=num_frames - 1, endpoint=False, dtype=int
                    )
                    expected = [frames[i] for i in idx] + [latest]
                with self.subTest(length=length, num_frames=num_frames):
                    self.assertEqual(plan_frame_selection(length, num_frames), expected)


class RobotHistoryTests(unittest.TestCase):
    def test_history_survives_subgoal_boundaries(self) -> None:
        robot = MockRobot()
        robot.begin_mission()
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        after_first = len(robot.mission_frames())

        robot.begin_subgoal(SubGoal(id="sg-2", instruction="go on", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))

        # Mission history keeps growing, the recent window was reset.
        self.assertGreater(len(robot.mission_frames()), after_first)
        self.assertLess(len(robot.subgoal_frames()), len(robot.mission_frames()))

    def test_begin_mission_clears_history(self) -> None:
        robot = MockRobot()
        robot.begin_mission()
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        self.assertGreater(len(robot.mission_frames()), 1)
        robot.begin_mission()
        self.assertEqual(len(robot.mission_frames()), 1)


class ExecutorFrameSourceTests(unittest.TestCase):
    def _executor(self, mode: str) -> tuple[VLAExecutor, MockRobot]:
        robot = MockRobot()
        robot.begin_mission()
        executor = VLAExecutor(policy=MockVLA(), robot=robot, frame_memory=mode)
        return executor, robot

    def test_episode_mode_uses_mission_history(self) -> None:
        executor, robot = self._executor("episode")
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        robot.begin_subgoal(SubGoal(id="sg-2", instruction="go", estimated_distance_m=1.0))
        self.assertEqual(executor._observation_frames(), robot.mission_frames())

    def test_recent_mode_uses_sliding_window(self) -> None:
        executor, robot = self._executor("recent")
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        for _ in range(12):
            robot.execute(MidLevelAction("move_forward", distance_m=0.1))
        self.assertEqual(len(executor._observation_frames()), 8)

    def test_subgoal_mode_excludes_earlier_subgoals(self) -> None:
        executor, robot = self._executor("subgoal")
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        robot.begin_subgoal(SubGoal(id="sg-2", instruction="go", estimated_distance_m=1.0))
        robot.execute(MidLevelAction("move_forward", distance_m=0.5))
        first = robot.mission_frames()[0]
        self.assertNotIn(first, executor._observation_frames())

    def test_step_record_reports_frame_counts(self) -> None:
        records = []
        executor, robot = self._executor("episode")
        executor.on_step = records.append
        robot.begin_subgoal(SubGoal(id="sg-1", instruction="go", estimated_distance_m=1.0))
        executor.execute(SubGoal(id="sg-1", instruction="go", estimated_distance_m=0.5, max_steps=2))
        self.assertTrue(records)
        # MockRobot.executor runs off the mock frame strings, but the counts
        # must reflect what the VLA was handed.
        self.assertGreaterEqual(records[0].history_frames, 1)
        self.assertLessEqual(records[0].video_frames, DEFAULT_NUM_VIDEO_FRAMES)


if __name__ == "__main__":
    unittest.main()
