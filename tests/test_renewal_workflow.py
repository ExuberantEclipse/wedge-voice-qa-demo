import unittest

from voice_qa.renewal_workflow import RenewalWorkflowController, WorkflowError


class RenewalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = RenewalWorkflowController()
        self.workflow.start("call-1")

    def test_busy_is_not_callback_consent(self):
        result = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "I'm busy right now"
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(self.workflow.session("call-1").contact_preference, "unknown")

    def test_server_directed_happy_path_sequence(self):
        self.assertEqual(self.workflow.next_step("call-1")["step"], "coverage")
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "clinic")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "packet")
        self.workflow.record_fact("call-1", "packet_received", "yes")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "renewal_help")
        self.workflow.record_fact("call-1", "help_preference", "none")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "contact_changes")
        self.workflow.record_fact("call-1", "contact_information_changed", "no")
        completion = self.workflow.next_step("call-1")
        self.assertEqual(completion["step"], "complete")
        self.assertIn("all done", completion["say"])
        self.assertIn("hang up", completion["say"])

    def test_completed_renewal_skips_coverage_packet_and_help(self):
        self.workflow.record_renewal_status("call-1", "completed")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "clinic")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "contact_changes")

    def test_legacy_packet_status_permanently_closes_packet_question(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.workflow.record_renewal_status("call-1", "packet_received")
        self.assertEqual(
            self.workflow.session("call-1").facts["packet_received"], "yes"
        )
        self.assertEqual(self.workflow.next_step("call-1")["step"], "renewal_help")

    def test_clinic_change_is_collected_before_packet_question(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.workflow.record_fact("call-1", "pcp_preference", "change_clinic")
        self.assertEqual(
            self.workflow.next_step("call-1")["step"], "collect_clinic_update"
        )
        self.workflow.record_profile_update("call-1", "primary_care_clinic")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "packet")

    def test_clinic_question_uses_current_profile_value(self):
        self.workflow.set_current_clinic("call-1", "South Park Health Center")
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        next_step = self.workflow.next_step("call-1")
        self.assertEqual(next_step["step"], "clinic")
        self.assertIn("South Park Health Center", next_step["say"])
        self.assertNotIn("Universal Community", next_step["say"])

    def test_contact_update_loops_only_for_another_change(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.workflow.record_fact("call-1", "packet_received", "yes")
        self.workflow.record_fact("call-1", "help_preference", "none")
        self.workflow.record_fact("call-1", "contact_information_changed", "yes")
        self.assertEqual(
            self.workflow.next_step("call-1")["step"], "collect_profile_update"
        )
        self.workflow.record_profile_update("call-1", "mailing_address")
        self.assertEqual(
            self.workflow.next_step("call-1")["step"], "more_profile_updates"
        )
        self.workflow.record_fact("call-1", "contact_information_changed", "no")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")

    def test_phone_help_leads_to_single_callback_offer(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.workflow.record_fact("call-1", "packet_received", "yes")
        self.workflow.record_fact("call-1", "help_preference", "phone")
        self.workflow.record_fact("call-1", "contact_information_changed", "no")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "callback_offer")

    def test_callback_decline_closes_branch_and_stays_complete(self):
        self._advance_to_callback_offer()
        declined = self.workflow.record_contact_preference(
            "call-1", "no_callback", "No thanks, I don't want a callback"
        )
        self.assertEqual(declined["decision"], "recorded")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")

        retried = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "Please call me tomorrow"
        )
        self.assertEqual(retried["decision"], "blocked")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")

    def test_completed_callback_request_closes_branch(self):
        self._advance_to_callback_offer()
        recorded = self.workflow.record_contact_preference(
            "call-1",
            "callback_requested",
            "Please call me tomorrow afternoon",
            requested_time="tomorrow afternoon",
            channel="phone",
        )
        self.assertEqual(recorded["decision"], "recorded")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")

    def test_callback_revocation_after_consent_closes_branch(self):
        self._advance_to_callback_offer()
        self.workflow.record_contact_preference(
            "call-1", "callback_requested", "Yes, please call me"
        )
        revoked = self.workflow.record_contact_preference(
            "call-1", "callback_revoked", "Actually, don't call me"
        )
        self.assertEqual(revoked["preference"], "callback_revoked")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "complete")

    def test_declined_answer_closes_review(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "declined")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "close_declined")

    def test_explicit_callback_request_is_recorded(self):
        result = self.workflow.record_contact_preference(
            "call-1",
            "callback_requested",
            "Please call me tomorrow afternoon",
            requested_time="tomorrow afternoon",
            purpose="renewal packet help",
            timezone="Pacific",
            language="English",
        )
        self.assertEqual(result["decision"], "recorded")
        self.assertEqual(result["requested_time"], "tomorrow afternoon")
        self.assertEqual(
            self.workflow.session("call-1").callback_request,
            {
                "requested_time": "tomorrow afternoon",
                "purpose": "renewal packet help",
                "timezone": "Pacific",
                "language": "English",
            },
        )

    def test_spanish_callback_request_is_recorded(self):
        result = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "Puede llamarme mañana"
        )
        self.assertEqual(result["decision"], "recorded")

    def test_yes_only_counts_after_a_direct_callback_question(self):
        blocked = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "Yes"
        )
        self.assertEqual(blocked["decision"], "blocked")

        recorded = self.workflow.record_contact_preference(
            "call-1",
            "callback_requested",
            "Yes",
            agent_prompt="Would you like me to record a callback request?",
        )
        self.assertEqual(recorded["decision"], "recorded")

    def test_condition_is_required_and_preserved(self):
        blocked = self.workflow.record_contact_preference(
            "call-1", "conditional_callback", "Call me only if the packet is late"
        )
        self.assertEqual(blocked["decision"], "blocked")

        recorded = self.workflow.record_contact_preference(
            "call-1",
            "conditional_callback",
            "Call me only if the packet is late",
            condition="only if the packet is late",
        )
        self.assertEqual(recorded["condition"], "only if the packet is late")

    def test_refusal_locks_out_later_callback_attempt(self):
        self.workflow.record_contact_preference(
            "call-1",
            "callback_requested",
            "Please call me tomorrow",
            requested_time="tomorrow afternoon",
        )
        refused = self.workflow.record_contact_preference(
            "call-1", "no_callback", "Do not call me"
        )
        self.assertEqual(refused["decision"], "recorded")
        self.assertEqual(self.workflow.session("call-1").callback_request, {})

        retried = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "Please call me tomorrow"
        )
        self.assertEqual(retried["decision"], "blocked")
        self.assertEqual(self.workflow.session("call-1").contact_preference, "no_callback")

    def test_refusal_inside_callback_request_is_blocked(self):
        result = self.workflow.record_contact_preference(
            "call-1", "callback_requested", "I did not ask for a callback"
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(
            self.workflow.session("call-1").contact_preference, "callback_revoked"
        )

    def test_completed_status_and_fact_are_persisted_without_raw_words(self):
        self.workflow.record_renewal_status("call-1", "completed")
        self.workflow.record_fact("call-1", "language", "spanish")
        self.workflow.record_fact("call-1", "medi_cal_status", "changing")
        self.workflow.record_fact("call-1", "contact_information_changed", "yes")
        session = self.workflow.session("call-1")
        self.assertEqual(session.renewal_status, "completed")
        self.assertEqual(session.facts["language"], "spanish")
        self.assertEqual(session.facts["medi_cal_status"], "changing")
        self.assertEqual(session.facts["contact_information_changed"], "yes")
        self.assertNotIn("caller_words", str(session.audit_events))

    def test_rejects_invalid_fact(self):
        with self.assertRaisesRegex(WorkflowError, "Unsupported workflow fact"):
            self.workflow.record_fact("call-1", "language", "klingon")

    def _advance_to_callback_offer(self):
        self.workflow.record_fact("call-1", "medi_cal_status", "active")
        self.workflow.record_fact("call-1", "pcp_preference", "keep_clinic")
        self.workflow.record_fact("call-1", "packet_received", "yes")
        self.workflow.record_fact("call-1", "help_preference", "phone")
        self.workflow.record_fact("call-1", "contact_information_changed", "no")
        self.assertEqual(self.workflow.next_step("call-1")["step"], "callback_offer")


if __name__ == "__main__":
    unittest.main()
