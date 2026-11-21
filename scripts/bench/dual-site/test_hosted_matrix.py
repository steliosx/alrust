"""Check the hosted runner's scope, scenario design and sensor boundary without starting any services."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hosted_lab
import hosted_matrix as matrix
from cache_replay_model import plan


class HostedMatrixTests(unittest.TestCase):
    def test_schedule_and_load(self):
        labels = set()
        for seed, case in matrix.schedule():
            name, profile, cadence, feedback, envs, newest, _, repeats = case
            self.assertNotIn((name, seed), labels)
            labels.add((name, seed))
            events = plan(profile, 4, 50, cadence, seed, newest)
            self.assertEqual(len(events), 200)
            self.assertEqual(sum(row["kind"] == "write" for row in events), 40)
            self.assertFalse(envs and (newest or not feedback))
            self.assertIn(repeats, (2, 4))
        self.assertEqual(len(labels), sum(case[-1] for case in matrix.CASES))

    def test_hosted_mode_does_not_admit_normal_host(self):
        fake = {"cpu.max": "200000 100000", "memory.max": str(4 * 1024**3), "memory.swap.max": "0"}
        with (
            patch.dict(os.environ, {"REPLAY_HOSTED": "1"}, clear=True),
            patch.object(hosted_lab.run_lab, "resource_caps", return_value=fake),
        ):
            with self.assertRaisesRegex(RuntimeError, "hosted|Hosted"):
                hosted_lab.replay_lab(None, None, None)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(hosted_lab.run_lab, "Lab", return_value="ordinary") as lab,
        ):
            self.assertEqual(hosted_lab.replay_lab(1, 2, 3), "ordinary")
            lab.assert_called_once_with(1, 2, 3)

    def test_missing_hosted_sensor_keeps_temperature_unknown(self):
        self.assertIsNone(hosted_lab.VirtualLab.read_temperature(SimpleNamespace()))
