import unittest
import tempfile

from navila_agno.contracts import MidLevelAction, RobotState, SubGoal
from navila_agno.robot import Go2HttpRobot, MockRobot, action_to_go2_command
from navila_agno.vla import (
    HttpVLAClient,
    LightNavWSClient,
    MockVLA,
    NaVILAHttpClient,
    build_navila_payload,
    lightnav_waypoint_to_action,
    make_vla,
)


class MakeVLATests(unittest.TestCase):
    def test_mock(self) -> None:
        self.assertIsInstance(make_vla("mock"), MockVLA)

    def test_navila_aliases(self) -> None:
        self.assertIsInstance(make_vla("http"), NaVILAHttpClient)
        self.assertIsInstance(make_vla("navila"), NaVILAHttpClient)
        self.assertIsInstance(make_vla("navila"), HttpVLAClient)

    def test_lightnav(self) -> None:
        self.assertIsInstance(make_vla("lightnav"), LightNavWSClient)

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ValueError):
            make_vla("unknown")


class NavilaPayloadTests(unittest.TestCase):
    def test_payload(self) -> None:
        payload = build_navila_payload(
            "go forward", ["a.jpg", "b.jpg"], None
        )
        self.assertEqual(payload["instruction"], "go forward")
        self.assertEqual(payload["num_video_frames"], 2)


class LightNavActionTests(unittest.TestCase):
    def test_stop(self) -> None:
        action = lightnav_waypoint_to_action({"stop": True, "rc": 0})
        self.assertEqual(action.action_type, "stop")

    def test_turn(self) -> None:
        action = lightnav_waypoint_to_action(
            {"rc": 0, "actions": {"actions": [[0.0, 0.0, 0.35]]}}
        )
        self.assertEqual(action.action_type, "turn_left")
        self.assertAlmostEqual(action.angle_deg, 20.0535, places=3)

    def test_waypoint(self) -> None:
        action = lightnav_waypoint_to_action(
            {"rc": 0, "actions": {"actions": [[0.31, 0.02, 0.02]]}}
        )
        self.assertEqual(action.action_type, "move")
        self.assertAlmostEqual(action.distance_m, 0.31)


class Go2CommandTests(unittest.TestCase):
    def test_move_forward(self) -> None:
        cmd = action_to_go2_command(
            MidLevelAction("move_forward", distance_m=0.3)
        )
        self.assertAlmostEqual(cmd["vx"], 0.3)
        self.assertAlmostEqual(cmd["duration_sec"], 1.0)

    def test_turn_left_sign(self) -> None:
        cmd = action_to_go2_command(
            MidLevelAction("turn_left", angle_deg=40.0)
        )
        self.assertLess(cmd["vyaw"], 0.0)
        self.assertLessEqual(abs(cmd["vyaw"]), 5.0)

    def test_waypoint_clamping(self) -> None:
        cmd = action_to_go2_command(
            MidLevelAction(
                "move",
                distance_m=0.0,
                lateral_m=0.0,
                angle_deg=0.0,
            )
        )
        self.assertAlmostEqual(cmd["vx"], 0.0)
        self.assertAlmostEqual(cmd["vyaw"], 0.0)


class RobotLifecycleTests(unittest.TestCase):
    def test_mock_begin_subgoal(self) -> None:
        robot = MockRobot()
        robot.begin_subgoal(SubGoal(id="s", instruction="go", estimated_distance_m=2.0))
        self.assertAlmostEqual(robot.remaining_distance_m, 2.0)

    def test_go2_observe_before_begin_subgoal(self) -> None:
        with tempfile.TemporaryDirectory() as frame_dir:
            robot = Go2HttpRobot(
                endpoint="http://127.0.0.1:1",
                dry_run=True,
                timeout_s=0.2,
                max_retries=1,
                connect_timeout_s=0.2,
                frame_dir=frame_dir,
            )
            state = robot.observe()
        self.assertIsInstance(state, RobotState)


if __name__ == "__main__":
    unittest.main()
