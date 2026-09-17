"""Tests for machine-checkable subgoal stop conditions."""
from __future__ import annotations

import json
import unittest

from navila_agno.contracts import (
    MAX_SUBGOAL_STEPS,
    EventType,
    MidLevelAction,
    RobotState,
    StopCondition,
    SubGoal,
    merge_subgoals,
)
from navila_agno.robot import MockRobot
from navila_agno.supervisor import AgnoSupervisor, _merge_unjustified
from navila_agno.vla import VLAExecutor


class ConstantPolicy:
    """Always emits the same NaVILA-style answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def predict(self, instruction, image_paths, state=None) -> str:
        return self.answer


class ParseTests(unittest.TestCase):
    def test_accepts_canonical_kinds(self) -> None:
        condition = StopCondition.from_value(
            {"kind": "vla_stop_after_distance", "distance_m": 3.0, "description": "到门口"}
        )
        assert condition is not None
        self.assertEqual(condition.kind, "vla_stop_after_distance")
        self.assertAlmostEqual(condition.distance_m, 3.0)
        self.assertEqual(condition.description, "到门口")

    def test_accepts_a_bare_kind_string(self) -> None:
        condition = StopCondition.from_value("steps", None)
        self.assertIsNone(condition)  # steps without a count is not judgeable

    def test_normalises_aliases(self) -> None:
        condition = StopCondition.from_value({"kind": "stop_after_distance", "distance": 2})
        assert condition is not None
        self.assertEqual(condition.kind, "vla_stop_after_distance")
        self.assertAlmostEqual(condition.distance_m, 2.0)

    def test_rejects_unknown_kind(self) -> None:
        self.assertIsNone(StopCondition.from_value({"kind": "looks_pretty_done"}, 3.0))
        self.assertIsNone(StopCondition.from_value(None, 3.0))

    def test_distance_kind_defaults_to_the_estimate(self) -> None:
        condition = StopCondition.from_value({"kind": "distance"}, 4.0)
        assert condition is not None
        self.assertAlmostEqual(condition.distance_m, 4.0)


class DefaultTests(unittest.TestCase):
    def test_turn_only_subgoal_does_not_use_distance(self) -> None:
        condition = StopCondition.default_for("turn left 90 degrees and then continue", 0.5)
        self.assertEqual(condition.kind, "vla_stop")

    def test_navigation_subgoal_requires_covered_distance(self) -> None:
        condition = StopCondition.default_for("walk toward the fire hydrant and stop", 3.0)
        self.assertEqual(condition.kind, "vla_stop_after_distance")
        self.assertAlmostEqual(condition.threshold_m(3.0), 2.4)

    def test_without_estimate_falls_back_to_vla_stop(self) -> None:
        self.assertEqual(StopCondition.default_for("walk forward", None).kind, "vla_stop")


class EvaluateTests(unittest.TestCase):
    def test_hand_back_budget_only_for_distance_guarded_stops(self) -> None:
        guarded = StopCondition(kind="vla_stop_after_distance", distance_m=6.0)
        self.assertAlmostEqual(guarded.hand_back_after_m(None), 7.8)
        self.assertIsNone(StopCondition(kind="vla_stop").hand_back_after_m(3.0))
        self.assertIsNone(StopCondition(kind="distance", distance_m=3.0).hand_back_after_m(3.0))
        # Falls back to the supervisor's estimate when no distance was given.
        self.assertAlmostEqual(
            StopCondition(kind="vla_stop_after_distance").hand_back_after_m(4.0), 5.2
        )

    def test_vla_stop(self) -> None:
        condition = StopCondition(kind="vla_stop")
        self.assertFalse(condition.evaluate(action_type="move_forward", traveled_m=5.0, step=1).met)
        self.assertTrue(condition.evaluate(action_type="stop", traveled_m=0.0, step=1).met)

    def test_stop_before_the_distance_is_premature(self) -> None:
        condition = StopCondition(kind="vla_stop_after_distance", distance_m=3.0)
        decision = condition.evaluate(action_type="stop", traveled_m=0.4, step=1)
        self.assertFalse(decision.met)
        self.assertTrue(decision.premature)
        self.assertIn("0.40m", decision.reason)

    def test_stop_after_the_distance_completes(self) -> None:
        condition = StopCondition(kind="vla_stop_after_distance", distance_m=3.0)
        decision = condition.evaluate(action_type="stop", traveled_m=2.5, step=6)
        self.assertTrue(decision.met)
        self.assertFalse(decision.premature)

    def test_stop_exactly_on_the_tolerance_boundary(self) -> None:
        condition = StopCondition(kind="vla_stop_after_distance", distance_m=3.0)
        threshold = condition.threshold_m(3.0)
        self.assertAlmostEqual(threshold, 2.4, places=6)
        self.assertFalse(
            condition.evaluate(action_type="stop", traveled_m=threshold - 0.01, step=6).met
        )
        self.assertTrue(
            condition.evaluate(action_type="stop", traveled_m=threshold, step=6).met
        )

    def test_distance_kind_ignores_the_vla(self) -> None:
        condition = StopCondition(kind="distance", distance_m=5.0)
        self.assertTrue(
            condition.evaluate(action_type="move_forward", traveled_m=4.1, step=9).met
        )

    def test_steps_kind_counts_steps(self) -> None:
        condition = StopCondition(kind="steps", steps=3)
        self.assertFalse(condition.evaluate(action_type="move_forward", traveled_m=0.0, step=2).met)
        self.assertTrue(condition.evaluate(action_type="move_forward", traveled_m=0.0, step=3).met)

    def test_pre_action_check_never_waits_on_the_vla(self) -> None:
        condition = StopCondition(kind="vla_stop")
        self.assertFalse(condition.evaluate(action_type=None, traveled_m=9.0, step=9).met)


class ExecutorTests(unittest.TestCase):
    def _run(self, policy_answer: str, subgoal: SubGoal):
        robot = MockRobot()
        robot.begin_mission()
        executor = VLAExecutor(policy=ConstantPolicy(policy_answer), robot=robot)
        return executor.execute(subgoal)

    def test_early_stop_replans_instead_of_completing(self) -> None:
        outcome = self._run(
            "stop",
            SubGoal(
                id="sg-1",
                instruction="walk to the fire hydrant and stop",
                estimated_distance_m=3.0,
                max_steps=10,
                stop_condition=StopCondition(
                    kind="vla_stop_after_distance", distance_m=3.0
                ),
            ),
        )
        self.assertEqual(outcome.event.type, EventType.UNCERTAIN)
        self.assertIn("提前返回 stop", outcome.event.message)

    def test_short_target_stop_completes(self) -> None:
        outcome = self._run(
            "stop",
            SubGoal(
                id="sg-1",
                instruction="walk to the fire hydrant and stop",
                estimated_distance_m=0.5,
                max_steps=10,
                stop_condition=StopCondition(kind="vla_stop"),
            ),
        )
        self.assertEqual(outcome.event.type, EventType.SUBGOAL_COMPLETED)

    def test_first_step_stop_with_ground_left_replans(self) -> None:
        """A `vla_stop` subgoal must not be declared done before it moved."""
        records = []
        robot = MockRobot()
        robot.begin_mission()
        executor = VLAExecutor(policy=ConstantPolicy("stop"), robot=robot)
        executor.on_step = records.append
        outcome = executor.execute(
            SubGoal(
                id="sg-1",
                instruction="walk down the corridor to the fire hydrant",
                estimated_distance_m=10.0,
                max_steps=10,
                stop_condition=StopCondition(kind="vla_stop"),
            )
        )
        self.assertEqual(outcome.event.type, EventType.UNCERTAIN)
        self.assertIn("第一步", outcome.event.message)
        self.assertEqual(outcome.event.step_count, 1)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].stop_reason)

    def test_distance_condition_finishes_without_a_vla_stop(self) -> None:
        outcome = self._run(
            "move forward 0.5 m.",
            SubGoal(
                id="sg-1",
                instruction="walk straight down the corridor",
                estimated_distance_m=2.0,
                max_steps=10,
                stop_condition=StopCondition(kind="distance", distance_m=1.0),
            ),
        )
        self.assertEqual(outcome.event.type, EventType.SUBGOAL_COMPLETED)
        self.assertIn("已前进", outcome.event.message)
        # Threshold is 1.0 * 0.8, reached after two 0.5 m steps.
        self.assertEqual(outcome.event.step_count, 2)

    def test_steps_condition_ends_the_subgoal(self) -> None:
        outcome = self._run(
            "turn left 90 degrees",
            SubGoal(
                id="sg-1",
                instruction="turn left and then continue",
                estimated_distance_m=1.0,
                max_steps=10,
                stop_condition=StopCondition(kind="steps", steps=3),
            ),
        )
        self.assertEqual(outcome.event.type, EventType.SUBGOAL_COMPLETED)
        self.assertIn("3 步", outcome.event.message)

    def test_unconfirmed_segment_is_handed_back_at_its_distance_budget(self) -> None:
        """Walking forever without a VLA `stop` must not eat the step budget."""
        outcome = self._run(
            "move forward 0.75 m.",
            SubGoal(
                id="sg-1",
                instruction="walk along the corridor to the fire hydrant and stop",
                estimated_distance_m=6.0,
                max_steps=30,
                stop_condition=StopCondition(
                    kind="vla_stop_after_distance", distance_m=6.0
                ),
            ),
        )
        self.assertEqual(outcome.event.type, EventType.UNCERTAIN)
        self.assertIn("距离预算", outcome.event.message)
        # Budget is 6.0 * 1.3 = 7.8 m, i.e. 11 steps of 0.75 m.
        self.assertEqual(outcome.event.step_count, 11)

    def test_missing_camera_frames_replan_instead_of_arriving(self) -> None:
        """A blind policy would answer `stop`, which must not count as arrival."""

        class BlindRobot(MockRobot):
            def mission_frames(self):
                return []

            def subgoal_frames(self):
                return []

            def recent_frames(self, n):
                return []

        robot = BlindRobot()
        robot.begin_mission()
        policy = ConstantPolicy("stop")
        executor = VLAExecutor(policy=policy, robot=robot)
        outcome = executor.execute(
            SubGoal(
                id="sg-1",
                instruction="walk to the fire hydrant and stop",
                estimated_distance_m=1.0,
                max_steps=5,
                stop_condition=StopCondition(kind="vla_stop"),
            )
        )
        self.assertEqual(outcome.event.type, EventType.UNCERTAIN)
        self.assertIn("相机", outcome.event.message)

    def test_subgoal_completion_is_recorded_with_its_reason(self) -> None:
        records = []
        robot = MockRobot()
        robot.begin_mission()
        executor = VLAExecutor(policy=ConstantPolicy("stop"), robot=robot)
        executor.on_step = records.append
        outcome = executor.execute(
            SubGoal(
                id="sg-1",
                instruction="walk and stop",
                estimated_distance_m=1.0,
                max_steps=5,
                stop_condition=StopCondition(kind="vla_stop"),
            )
        )
        self.assertEqual(outcome.event.type, EventType.SUBGOAL_COMPLETED)
        self.assertEqual(len(records), 1)
        self.assertIn("VLA 返回 stop", records[0].stop_reason)


class MergeTests(unittest.TestCase):
    def _subgoal(self, sid: str, instruction: str, distance: float, steps: int = 8) -> SubGoal:
        return SubGoal(
            id=sid,
            instruction=instruction,
            estimated_distance_m=distance,
            max_steps=steps,
            note_zh=f"说明{sid}",
            stop_condition=StopCondition(kind="vla_stop"),
        )

    def test_merge_joins_instruction_and_distance(self) -> None:
        first = self._subgoal("a", "Walk through the gate.", 3.0)
        second = self._subgoal("b", "turn right and continue", 2.0)
        second.stop_condition = StopCondition(kind="distance", distance_m=2.0)
        merged = merge_subgoals(first, second)
        self.assertEqual(
            merged.instruction, "Walk through the gate, then turn right and continue"
        )
        self.assertAlmostEqual(merged.estimated_distance_m, 5.0)
        self.assertEqual(merged.id, "a")
        self.assertEqual(merged.note_zh, "说明a，然后说明b")
        # The combined segment ends where the second one ended.
        self.assertEqual(merged.stop_condition.kind, "distance")

    def test_merge_caps_the_step_budget(self) -> None:
        first = self._subgoal("a", "walk", 3.0, steps=20)
        second = self._subgoal("b", "walk on", 3.0, steps=20)
        self.assertEqual(merge_subgoals(first, second).max_steps, MAX_SUBGOAL_STEPS)

    def test_unjustified_subgoal_is_folded_into_the_previous_one(self) -> None:
        first = self._subgoal("a", "Walk through the gate and stop.", 3.0)
        vague = self._subgoal("b", "keep going for a bit", 2.0)
        result = _merge_unjustified([first, vague], {1})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "a")
        self.assertIn("then keep going for a bit", result[0].instruction)

    def test_consecutive_unjustified_subgoals_collapse_to_one(self) -> None:
        justified = self._subgoal("a", "Walk to the gate.", 3.0)
        vague = [self._subgoal(f"v{i}", f"vague segment {i}", 1.0) for i in range(4)]
        result = _merge_unjustified([justified] + vague, {1, 2, 3, 4})
        self.assertEqual(len(result), 1)
        self.assertIn("vague segment 3", result[0].instruction)

    def test_a_leading_unjustified_subgoal_gets_a_derived_condition(self) -> None:
        vague = SubGoal(id="v", instruction="walk forward", estimated_distance_m=2.0)
        vague.stop_condition = StopCondition()
        result = _merge_unjustified([vague], {0})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].stop_condition.kind, "vla_stop_after_distance")

    def test_justified_plan_is_untouched(self) -> None:
        plan = [self._subgoal("a", "walk", 2.0), self._subgoal("b", "walk more", 2.0)]
        result = _merge_unjustified(plan, set())
        self.assertEqual(result, plan)


class ParsedPlanMergeTests(unittest.TestCase):
    """End-to-end check that a vague model answer collapses into one subgoal."""

    def test_turn_judged_by_a_step_count_merges_into_the_previous_segment(self) -> None:
        raw = json.dumps(
            [
                {
                    "id": "1",
                    "instruction": "Walk forward through the gate.",
                    "estimated_distance_m": 3,
                    "max_steps": 6,
                    "stop_condition": {"kind": "vla_stop_after_distance", "distance_m": 3},
                },
                {
                    "id": "2",
                    "instruction": "Turn right to face down the corridor.",
                    "estimated_distance_m": 0.5,
                    "max_steps": 3,
                    "stop_condition": {"kind": "steps", "steps": 3},
                },
            ]
        )
        subgoals = AgnoSupervisor._parse_subgoals(raw)
        assert subgoals is not None
        self.assertEqual(len(subgoals), 1)
        self.assertIn("Turn right", subgoals[0].instruction)
        # The step count is replaced by something the executor can verify.
        self.assertEqual(subgoals[0].stop_condition.kind, "vla_stop_after_distance")

    def test_a_plain_steps_condition_survives(self) -> None:
        raw = json.dumps(
            [
                {
                    "id": "1",
                    "instruction": "Back away from the wall slowly.",
                    "estimated_distance_m": 1.0,
                    "max_steps": 4,
                    "stop_condition": {"kind": "steps", "steps": 3},
                }
            ]
        )
        subgoals = AgnoSupervisor._parse_subgoals(raw)
        assert subgoals is not None
        self.assertEqual(subgoals[0].stop_condition.kind, "steps")

    def test_model_answer_without_conditions_merges(self) -> None:
        raw = json.dumps(
            [
                {"id": "1", "instruction": "Walk through the gate.", "estimated_distance_m": 3, "max_steps": 6},
                {"id": "2", "instruction": "turn right and continue", "estimated_distance_m": 1, "max_steps": 4},
                {"id": "3", "instruction": "walk to the end and stop", "estimated_distance_m": 5, "max_steps": 10},
            ]
        )
        subgoals = AgnoSupervisor._parse_subgoals(raw)
        assert subgoals is not None
        self.assertEqual(len(subgoals), 1)
        self.assertIn("Walk through the gate", subgoals[0].instruction)
        self.assertIn("walk to the end and stop", subgoals[0].instruction)
        self.assertAlmostEqual(subgoals[0].estimated_distance_m, 9.0)

    def test_explicit_conditions_keep_the_plan_split(self) -> None:
        raw = json.dumps(
            [
                {
                    "id": "1",
                    "instruction": "Walk through the gate.",
                    "estimated_distance_m": 3,
                    "stop_condition": {"kind": "vla_stop_after_distance", "distance_m": 3},
                },
                {
                    "id": "2",
                    "instruction": "walk to the end and stop",
                    "estimated_distance_m": 5,
                    "stop_condition": {"kind": "vla_stop_after_distance", "distance_m": 5},
                },
            ]
        )
        subgoals = AgnoSupervisor._parse_subgoals(raw)
        assert subgoals is not None
        self.assertEqual(len(subgoals), 2)


if __name__ == "__main__":
    unittest.main()
