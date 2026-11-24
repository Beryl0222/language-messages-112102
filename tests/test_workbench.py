"""工作台的行为测试：变量角色、范围、宽度、选择与成对提交。"""

import json
import shutil
from pathlib import Path
import tempfile
import unittest

from workbench.model import (
    DataError,
    display_width,
    load_tables,
)
from workbench.selection import (
    BLOCKED,
    EDITOR_REVIEW,
    FALLBACK,
    READY,
    plan_request,
    plan_requests,
    render,
)
from workbench.semantics import (
    analyze_copy,
    assess_request,
    issues_for_request,
)
from workbench.submission import (
    audit_all_submissions,
    audit_submission,
    delivery_report,
    preview_regression,
    translation_gaps,
)


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class DataFixture(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables(DATA_DIR)

    def template(self, identifier):
        return self.tables.templates[identifier]

    def request(self, identifier):
        return self.tables.classes[identifier]


class WidthTest(unittest.TestCase):
    def test_cjk_counts_two_columns(self):
        self.assertEqual(display_width("北厅"), 4)
        self.assertEqual(display_width("ab"), 2)
        self.assertEqual(display_width("北厅ab"), 6)

    def test_combining_marks_and_zwj_are_zero(self):
        self.assertEqual(display_width("é"), 1)
        # 家庭 emoji：两个 W 字加零宽连接符。
        self.assertEqual(display_width("\U0001F468‍\U0001F469"), 4)

    def test_ambiguous_width_is_one(self):
        # “·”属东亚宽度 A，约定按一列。
        self.assertEqual(display_width("·"), 1)


class RoleReadingTest(DataFixture):
    def test_english_r1_makes_guide_the_addressee(self):
        analysis = analyze_copy(self.template("V2"))
        self.assertEqual(analysis.addressee, "guide")
        self.assertTrue(
            any(issue.code == "en_guide_as_addressee" for issue in analysis.issues)
        )
        self.assertEqual(analysis.slots["primary"].function, "led_guest")

    def test_chinese_r1_makes_primary_the_addressee(self):
        analysis = analyze_copy(self.template("V1"))
        self.assertEqual(analysis.addressee, "primary")
        self.assertEqual(analysis.rest_action, "stay")

    def test_honorific_only_modifies_primary(self):
        analysis = analyze_copy(self.template("V9"))
        self.assertFalse(analysis.slots["honorific"].affects_action)
        self.assertEqual(analysis.slots["honorific"].governs, ("primary",))
        self.assertEqual(analysis.slots["primary"].modified_by, ("honorific",))

    def test_party_of_n_is_flagged_as_including_primary(self):
        analysis = analyze_copy(self.template("V3"))
        self.assertTrue(analysis.companion_count_includes_primary)

    def test_line_yi_hang_is_ambiguous_not_silently_accepted(self):
        analysis = analyze_copy(self.template("V5"))
        self.assertTrue(
            any(issue.code == "zh_line_party" for issue in analysis.issues)
        )
        self.assertTrue(analysis.is_ambiguous)

    def test_plural_suffix_needs_value_specific_copy(self):
        analysis = analyze_copy(self.template("V4"))
        self.assertTrue(
            any(issue.code == "plural_suffix" for issue in analysis.issues)
        )

    def test_fallback_copy_has_no_named_slots(self):
        for identifier in ("V6", "V7"):
            self.assertTrue(self.template(identifier).is_fallback_copy())


class ConditionAndScopeTest(DataFixture):
    def test_solo_condition_is_recognised(self):
        analysis = analyze_copy(self.template("V8"))
        self.assertEqual(analysis.conditional_slots.get("group_count"), 0)

    def test_honorific_condition_is_recognised(self):
        analysis = analyze_copy(self.template("V9"))
        self.assertTrue(analysis.conditional_slots.get("honorific_required"))

    def test_zero_companions_cannot_use_generic_count_sentence(self):
        analysis = analyze_copy(self.template("V1"))
        issues = assess_request(self.template("V1"), analysis, self.request("R1"))
        self.assertTrue(
            any(issue.code == "zero_companions_stated" for issue in issues)
        )

    def test_article_mismatch_is_detected_per_value(self):
        template = self.template("V4")
        # 把模板临时改成 a {primary} 形式，检查按读音取冠词。
        from dataclasses import replace

        probe = replace(template, text="a {primary} should follow.", revision="rx")
        analysis = analyze_copy(probe)
        request = self.request("R3")
        issues = assess_request(probe, analysis, request)
        self.assertTrue(any(issue.code == "article_mismatch" for issue in issues))

    def test_rest_scope_required_for_guide_copy(self):
        analysis = analyze_copy(self.template("V1"))
        self.assertIsNotNone(analysis.rest_scope)
        self.assertEqual(analysis.rest_action, "stay")


class SelectionTest(DataFixture):
    def test_r4_on_narrow_screen_falls_back_whole_request(self):
        plan = plan_request(self.tables, self.request("R4"))
        self.assertEqual(plan.status, FALLBACK)
        self.assertTrue(plan.fallback)
        self.assertEqual(plan.action, "ask_help_desk")
        for choice in plan.choices.values():
            self.assertTrue(choice.fallback)
            self.assertEqual(choice.text, self.template(
                "V6" if choice.locale == "zh" else "V7"
            ).text)

    def test_fallback_never_truncates_the_name(self):
        plan = plan_request(self.tables, self.request("R4"))
        for choice in plan.choices.values():
            self.assertNotIn("Dara", choice.text)
            self.assertNotIn("地方城市", choice.text)

    def test_r3_uses_only_the_approved_public_name(self):
        plan = plan_request(self.tables, self.request("R3"), active_shift="晚班")
        self.assertNotEqual(plan.status, BLOCKED)
        for choice in plan.choices.values():
            self.assertNotIn("Anggrek", choice.text)
            self.assertIn("Arum", choice.text)

    def test_r3_is_blocked_before_its_shift(self):
        plan = plan_request(self.tables, self.request("R3"))
        self.assertEqual(plan.status, BLOCKED)
        self.assertTrue(
            any(issue.code == "class_not_active" for issue in plan.blocking_issues)
        )

    def test_r3_is_deliverable_on_the_late_shift(self):
        plan = plan_request(self.tables, self.request("R3"), active_shift="晚班")
        self.assertTrue(plan.deliverable)

    def test_solo_request_uses_conditioned_solo_copy(self):
        plan = plan_request(self.tables, self.request("R1"))
        self.assertEqual(plan.choices["en"].template.id, "V8")
        self.assertEqual(plan.action, "guide_led_guest_to_room")

    def test_selection_is_deterministic(self):
        first = plan_request(self.tables, self.request("R2"))
        second = plan_request(self.tables, self.request("R2"))
        self.assertEqual(first.summary(), second.summary())
        plans = plan_requests(self.tables)
        self.assertEqual([plan.request.id for plan in plans], ["R1", "R2", "R3", "R4"])

    def test_no_locale_ever_exceeds_the_screen_width(self):
        for request in self.tables.classes.values():
            plan = plan_request(self.tables, request, active_shift="晚班")
            for locale, choice in plan.choices.items():
                self.assertLessEqual(
                    choice.width,
                    plan.screen.max_width(locale),
                    f"{request.id}/{locale}: {choice.text}",
                )

    def test_language_switch_never_changes_the_action(self):
        for request in self.tables.classes.values():
            plan = plan_request(self.tables, request, active_shift="晚班")
            self.assertTrue(plan.action_consistent, request.id)
            self.assertIn(plan.action, {"guide_led_guest_to_room", "ask_help_desk"})

    def test_one_language_falling_back_forces_the_other(self):
        # 让中文刚好放不下，英文仍可用：两边都必须退回兜底。
        screen = self.tables.screens["S1"]
        screen.max_columns["zh"] = 40  # 两条中文导览句都放不下，兜底句 24 放得下
        plan = plan_request(self.tables, self.request("R1"))
        self.assertEqual(plan.status, FALLBACK)
        self.assertTrue(all(choice.fallback for choice in plan.choices.values()))

    def test_required_key_gap_is_reported_without_blocking(self):
        plan = plan_request(self.tables, self.request("R1"))
        codes = [issue.code for issue in plan.choices["zh"].issues]
        self.assertIn("required_key_not_stated", codes)
        self.assertTrue(plan.deliverable)

    def test_honorific_is_suppressed_when_declined(self):
        request = self.request("R2")
        request.data["honorific"] = "Mr"
        request.data["honorific_consent"] = False
        plan = plan_request(self.tables, request)
        for choice in plan.choices.values():
            self.assertNotIn("{honorific}", choice.text)
            self.assertNotIn("Mr", choice.text)

    def test_honorific_used_with_consent(self):
        request = self.request("R2")
        request.data["honorific"] = "Mx"
        request.data["honorific_consent"] = True
        plan = plan_request(self.tables, request)
        self.assertEqual(plan.choices["en"].template.id, "V9")
        self.assertTrue(plan.choices["en"].text.startswith("Mx "))

    def test_declined_honorific_does_not_enable_condition(self):
        request = self.request("R2")
        request.data["honorific"] = "Mr"
        request.data["honorific_consent"] = False
        self.assertIsNone(request.honorific)
        plan = plan_request(self.tables, request)
        self.assertNotEqual(plan.choices["en"].template.id, "V9")
        self.assertTrue(request.has_unconsented_honorific)

    def test_missing_honorific_uses_neutral_copy(self):
        request = self.request("R1")
        self.assertIsNone(request.honorific)
        plan = plan_request(self.tables, request)
        for choice in plan.choices.values():
            self.assertNotIn("{honorific}", choice.text)


class RenderTest(DataFixture):
    def test_render_refuses_honorific_without_value(self):
        from workbench.selection import TemplateRenderError

        with self.assertRaises(TemplateRenderError):
            render(self.template("V9"), self.request("R2"))

    def test_render_uses_request_values(self):
        text = render(self.template("V8"), self.request("R1"))
        self.assertEqual(
            text, "乌兰, follow 曾老师 to 北厅 on your own. Other guests, wait here."
        )


class PairAuditTest(DataFixture):
    def test_latest_pair_is_rejected(self):
        zh = self.tables.templates["V5"]
        en = self.tables.templates["V9"]
        audit = audit_submission(self.tables, "guide-7", zh.revision, en.revision)
        codes = {issue.code for issue in audit.issues}
        self.assertFalse(audit.acceptable)
        self.assertIn("coverage_divergence", codes)
        self.assertIn("asymmetric_exclusion", codes)

    def test_conditioned_copy_only_on_one_side_is_a_gap(self):
        gaps = translation_gaps(self.tables, "guide-7")
        self.assertFalse(gaps["symmetric"])
        self.assertIn("group_count==0", gaps["conditions_only_en"])
        self.assertIn("有经本人同意的身份称呼", gaps["conditions_only_en"])

    def test_waiting_message_pair_is_acceptable(self):
        audits = {row.message_id: row for row in audit_all_submissions(self.tables)}
        self.assertTrue(audits["guide-8"].acceptable)

    def test_english_r1_alone_is_rejected_as_submission(self):
        audit = audit_submission(self.tables, "guide-7", "r2", "r1")
        self.assertFalse(audit.acceptable)
        self.assertTrue(
            any(issue.code == "en_guide_as_addressee" for issue in audit.issues)
        )


class PreviewTest(DataFixture):
    def test_old_delivery_preview_differs_from_new(self):
        previews = {row.request.id: row for row in preview_regression(self.tables)}
        self.assertTrue(previews["R1"].changed)
        self.assertFalse(previews["R4"].changed)
        self.assertFalse(previews["R1"].old.action == "guide_led_guest_to_room")

    def test_report_lists_untranslated_and_devices(self):
        report = delivery_report(self.tables, active_shift="晚班")
        self.assertIn("party", report["untranslated"])
        self.assertEqual(report["missing_fallback"], [])
        for devices in report["notify_devices"].values():
            self.assertIn("入口窄屏", devices)
            self.assertIn("展厅大屏", devices)


class BadDataTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        for name in ("copybook.json", "screens.json", "classes.json"):
            shutil.copy(DATA_DIR / name, self.directory / name)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def _mutate(self, name, function):
        path = self.directory / name
        rows = json.loads(path.read_text(encoding="utf-8"))
        function(rows)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def test_unknown_placeholder_is_rejected(self):
        def change(rows):
            rows[0]["text"] = "请 {primary} 到 {lobby}。"

        self._mutate("copybook.json", change)
        with self.assertRaises(DataError):
            load_tables(self.directory)

    def test_fallback_with_a_room_is_rejected(self):
        def change(rows):
            for row in rows:
                if row["id"] == "V6":
                    row["text"] = "等候的来宾请到 {room}。"

        self._mutate("copybook.json", change)
        with self.assertRaises(DataError):
            load_tables(self.directory)

    def test_missing_required_key_is_rejected(self):
        def change(rows):
            rows[0]["data"].pop("room")

        self._mutate("classes.json", change)
        with self.assertRaises(DataError):
            load_tables(self.directory)

    def test_negative_group_count_is_rejected(self):
        def change(rows):
            rows[0]["data"]["group_count"] = -1

        self._mutate("classes.json", change)
        with self.assertRaises(DataError):
            load_tables(self.directory)

    def test_broken_waiting_ref_is_rejected(self):
        def change(rows):
            rows[0]["waiting_ref"] = "guide-99"

        self._mutate("screens.json", change)
        with self.assertRaises(DataError):
            load_tables(self.directory)


if __name__ == "__main__":
    unittest.main()
