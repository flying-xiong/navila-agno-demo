import unittest

from navila_agno.contracts import EventType
from navila_agno.robot import MockRobot
from navila_agno.runtime import MissionRuntime
from navila_agno.supervisor import MockSupervisor
from navila_agno.vla import MockVLA, VLAExecutor, parse_action


class ParseActionTests(unittest.TestCase):
    def test_parse_stop(self) -> None:
        action = parse_action("The next action is stop.")
        self.assertEqual(action.action_type, "stop")

    def test_parse_forward_cm(self) -> None:
        action = parse_action("move forward 25 cm.")
        self.assertEqual(action.action_type, "move_forward")
        self.assertAlmostEqual(action.distance_m, 0.25)

    def test_parse_turn_left(self) -> None:
        action = parse_action("turn left 30 degrees.")
        self.assertEqual(action.action_type, "turn_left")
        self.assertAlmostEqual(action.angle_deg, 30.0)


class MissionRuntimeTests(unittest.TestCase):
    def test_mock_mission_completes(self) -> None:
        robot = MockRobot()
        executor = VLAExecutor(policy=MockVLA(), robot=robot)
        runtime = MissionRuntime(supervisor=MockSupervisor(), executor=executor)

        report = runtime.run("去 5 楼会议室 A")

        self.assertTrue(report.success)
        self.assertEqual(report.completed_subgoals, 3)
        self.assertEqual(
            [e.type for e in report.events],
            [EventType.SUBGOAL_COMPLETED] * 3,
        )

    def test_blocked_subgoal_triggers_replan(self) -> None:
        robot = MockRobot(fail_subgoal_id="go-to-elevator", fail_after_steps=1)
        executor = VLAExecutor(policy=MockVLA(), robot=robot)
        runtime = MissionRuntime(supervisor=MockSupervisor(), executor=executor)

        report = runtime.run("去 5 楼会议室 A")

        self.assertTrue(report.success)
        self.assertEqual(report.completed_subgoals, 3)
        self.assertIn(EventType.BLOCKED, [e.type for e in report.events])
        self.assertTrue(any(e.subgoal_id == "reroute-via-stairs" for e in report.events))


if __name__ == "__main__":
    unittest.main()
