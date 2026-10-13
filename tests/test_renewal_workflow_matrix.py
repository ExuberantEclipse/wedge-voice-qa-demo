import itertools
import unittest

from voice_qa.renewal_workflow import RenewalWorkflowController


class RenewalWorkflowMatrixTests(unittest.TestCase):
    """Exhaustive text/tool checks that must pass before another live voice test."""

    COVERAGE_VALUES = ("active", "inactive", "changing", "uncertain")
    CLINIC_VALUES = ("keep_clinic", "change_clinic", "uncertain")
    PACKET_VALUES = ("yes", "no", "uncertain")
    HELP_VALUES = ("phone", "clinic", "self_service", "none", "uncertain")
    CONTACT_VALUES = ("no", "uncertain")

    def test_all_360_basic_sequence_combinations_are_non_repeating_and_terminal(self):
        combinations = itertools.product(
            self.COVERAGE_VALUES,
            self.CLINIC_VALUES,
            self.PACKET_VALUES,
            self.HELP_VALUES,
            self.CONTACT_VALUES,
        )
        count = 0
        for count, choices in enumerate(combinations, start=1):
            with self.subTest(choices=choices):
                steps = self._run_basic_scenario(*choices)
                self.assertEqual(len(steps), len(set(steps)), steps)
                self.assertEqual(steps[0:2], ["coverage", "clinic"])
                self.assertIn("packet", steps)
                self.assertIn("renewal_help", steps)
                self.assertIn("contact_changes", steps)
                self.assertEqual(steps[-1], "complete")
                self.assertLessEqual(len(steps), 8)
        self.assertEqual(count, 360)

    def test_completed_renewal_matrix_skips_coverage_packet_and_help(self):
        count = 0
        for count, (clinic, contact) in enumerate(
            itertools.product(self.CLINIC_VALUES, self.CONTACT_VALUES), start=1
        ):
            with self.subTest(clinic=clinic, contact=contact):
                workflow = RenewalWorkflowController()
                call_id = f"completed-{count}"
                workflow.start(call_id)
                workflow.record_renewal_status(call_id, "completed")
                steps = []

                self._append_unique(steps, workflow.next_step(call_id)["step"])
                self.assertEqual(steps[-1], "clinic")
                workflow.record_fact(call_id, "pcp_preference", clinic)
                if clinic == "change_clinic":
                    self._append_unique(steps, workflow.next_step(call_id)["step"])
                    self.assertEqual(steps[-1], "collect_clinic_update")
                    workflow.record_profile_update(call_id, "primary_care_clinic")

                self._append_unique(steps, workflow.next_step(call_id)["step"])
                self.assertEqual(steps[-1], "contact_changes")
                workflow.record_fact(call_id, "contact_information_changed", contact)
                self._append_unique(steps, workflow.next_step(call_id)["step"])

                self.assertEqual(steps[-1], "complete")
                self.assertNotIn("coverage", steps)
                self.assertNotIn("packet", steps)
                self.assertNotIn("renewal_help", steps)
        self.assertEqual(count, 6)

    def test_refusal_at_each_basic_question_closes_without_repeating(self):
        cases = ("coverage", "clinic", "contact_changes")
        for target in cases:
            with self.subTest(target=target):
                workflow = RenewalWorkflowController()
                call_id = f"decline-{target}"
                workflow.start(call_id)
                steps = [workflow.next_step(call_id)["step"]]

                if target == "coverage":
                    workflow.record_fact(call_id, "medi_cal_status", "declined")
                else:
                    workflow.record_fact(call_id, "medi_cal_status", "active")
                    steps.append(workflow.next_step(call_id)["step"])
                    if target == "clinic":
                        workflow.record_fact(call_id, "pcp_preference", "declined")
                    else:
                        workflow.record_fact(call_id, "pcp_preference", "keep_clinic")
                        steps.append(workflow.next_step(call_id)["step"])
                        workflow.record_fact(call_id, "packet_received", "yes")
                        steps.append(workflow.next_step(call_id)["step"])
                        workflow.record_fact(call_id, "help_preference", "none")
                        steps.append(workflow.next_step(call_id)["step"])
                        workflow.record_fact(
                            call_id, "contact_information_changed", "declined"
                        )

                steps.append(workflow.next_step(call_id)["step"])
                self.assertEqual(steps[-1], "close_declined")
                self.assertEqual(len(steps), len(set(steps)), steps)

    def _run_basic_scenario(
        self,
        coverage: str,
        clinic: str,
        packet: str,
        help_preference: str,
        contact: str,
    ) -> list[str]:
        workflow = RenewalWorkflowController()
        call_id = f"matrix-{coverage}-{clinic}-{packet}-{help_preference}-{contact}"
        workflow.start(call_id)
        steps = []

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        workflow.record_fact(call_id, "medi_cal_status", coverage)

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        workflow.record_fact(call_id, "pcp_preference", clinic)
        if clinic == "change_clinic":
            self._append_unique(steps, workflow.next_step(call_id)["step"])
            workflow.record_profile_update(call_id, "primary_care_clinic")

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        workflow.record_fact(call_id, "packet_received", packet)

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        workflow.record_fact(call_id, "help_preference", help_preference)

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        workflow.record_fact(call_id, "contact_information_changed", contact)

        if help_preference == "phone":
            self._append_unique(steps, workflow.next_step(call_id)["step"])
            workflow.record_contact_preference(
                call_id, "no_callback", "No, I do not want a callback"
            )

        self._append_unique(steps, workflow.next_step(call_id)["step"])
        return steps

    def _append_unique(self, steps: list[str], step: str) -> None:
        self.assertNotIn(step, steps, f"Repeated workflow step: {step}; history={steps}")
        steps.append(step)


if __name__ == "__main__":
    unittest.main()
