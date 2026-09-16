import unittest

from navila_agno.contracts import MidLevelAction
from navila_agno.robot import action_to_go2_command
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


if __name__ == "__main__":
    unittest.main()
