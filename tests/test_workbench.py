"""工作台对交换资料的核心行为：变量角色、集合归属、范围与安全退回。"""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guideworkbench import (  # noqa: E402
    Approval,
    CopyRequest,
    CopyVariant,
    DEFAULT_SEMANTICS,
    RenderError,
    RenderPolicy,
    Scope,
    Semantics,
    VisitorRequest,
    Workbench,
    analyze_copy,
    assemble_copybook,
    bilingual_matches,
    check_fallback_text,
    coverage_gaps,
    cross_screen_view,
    inspect_copybook,
    load_workbench,
    make_route_semantic,
    plan_screen,
    render_text,
    resolve_delivery,
    review_copybook,
    survey_classes,
)
from guideworkbench.render import display_width, fill_template  # noqa: E402


DATA_DIR = ROOT / "data"


def bilingual_review(workbench):
    from guideworkbench import bilingual_matches

    return bilingual_matches(workbench, "guide-7")


def build_workbench(copybook=None, requests=None, screens=None):
    base = load_workbench(DATA_DIR)
    return Workbench(
        copybook=tuple(copybook) if copybook is not None else base.copybook,
        screens=screens if screens is not None else base.screens,
        requests=tuple(requests) if requests is not None else base.requests,
        fields=base.fields,
    )


class LoadingTest(unittest.TestCase):
    def test_reads_all_exchange_tables(self):
        workbench = load_workbench(DATA_DIR)
        self.assertEqual(len(workbench.copybook), 9)
        self.assertEqual(set(workbench.screens), {"S1", "S2"})
        self.assertEqual(len(workbench.requests), 4)
        self.assertIsNotNone(workbench.fields)

    def test_raw_text_is_preserved(self):
        workbench = load_workbench(DATA_DIR)
        v1 = workbench.copy_by_id()["V1"]
        self.assertIn("{group_count}", v1.text)

    def test_field_rules_are_not_screens(self):
        workbench = load_workbench(DATA_DIR)
        self.assertNotIn("S3", workbench.screens)


class VariableRoleTest(unittest.TestCase):
    def test_领队与来宾角色对调会被拦下(self):
        analysis = analyze_copy(load_workbench(DATA_DIR).copy_by_id()["V2"])
        codes = {flag.code for flag in analysis.errors}
        self.assertIn("P003", codes)
        role = analysis.check("role_positions")
        self.assertFalse(role.ok)

    def test_变量名齐全不代表集合归属正确(self):
        workbench = load_workbench(DATA_DIR)
        pair = next(
            p
            for p in bilingual_matches(workbench, "guide-7")
            if p.zh.id == "V1" and p.en.id == "V2"
        )
        self.assertEqual(pair.status, "mismatch")
        same_names = next(c for c in pair.checks if c.id == "same_placeholders")
        self.assertTrue(same_names.ok)

    def test_人数含本人会把同行者算错(self):
        analysis = analyze_copy(load_workbench(DATA_DIR).copy_by_id()["V3"])
        self.assertIn("G001", {flag.code for flag in analysis.errors})

    def test_其余来宾与其他人是不同补集(self):
        workbench = load_workbench(DATA_DIR)
        v1 = analyze_copy(workbench.copy_by_id()["V1"])
        v5 = analyze_copy(workbench.copy_by_id()["V5"])
        self.assertEqual(v1.split.complement_universe, "guests")
        self.assertEqual(v5.split.complement_universe, "people")

    def test_未决读法交给编辑并列出来宾类型(self):
        analysis = analyze_copy(load_workbench(DATA_DIR).copy_by_id()["V5"])
        self.assertTrue(analysis.pending)
        self.assertFalse(analysis.deliverable)
        self.assertEqual(analysis.serviceable_types, ("party", "closed-name"))

    def test_group_count_可以是只控制分流不显示的词位(self):
        analysis = analyze_copy(load_workbench(DATA_DIR).copy_by_id()["V8"])
        self.assertNotIn("group_count", analysis.variant.text.replace("{group_count}", ""))
        self.assertEqual(analysis.slot_use["group_count"], ("control",))
        self.assertEqual(analysis.scope, Scope.solo())

    def test_称呼词位既显示又受同意限制(self):
        analysis = analyze_copy(load_workbench(DATA_DIR).copy_by_id()["V9"])
        self.assertIn("display", analysis.slot_use["honorific"])
        self.assertTrue(analysis.scope.requires_consent)

    def test_冒号式称呼缺少同意不能显示(self):
        workbench = load_workbench(DATA_DIR)
        v9 = workbench.copy_by_id()["V9"]
        values = {
            "primary": "Dara",
            "guide": "Wei",
            "room": "北厅",
            "group_count": 2,
            "honorific": "女士",
        }
        rendered = render_text(v9.text, values)
        self.assertTrue(rendered.blocked)
        # 草稿只保留姓名，不显示未获同意的称呼。
        self.assertTrue(rendered.text.startswith("Dara and"))
        self.assertIn("没有记录本人同意", rendered.blocked[0])

    def test_未录称呼可用获准中性写法(self):
        policy = RenderPolicy(neutral_honorific="", neutral_honorific_approved=True)
        workbench = load_workbench(DATA_DIR)
        v9 = workbench.copy_by_id()["V9"]
        values = {
            "primary": "Arum",
            "guide": "Ivana",
            "room": "北厅",
            "group_count": 1,
            "honorific": None,
        }
        rendered = render_text(v9.text, values, policy)
        self.assertTrue(rendered.deliverable)
        self.assertTrue(rendered.used_neutral_honorific)
        self.assertTrue(rendered.text.startswith("Arum and"))

    def test_未获准的中性写法不能顶替称呼(self):
        workbench = load_workbench(DATA_DIR)
        v9 = workbench.copy_by_id()["V9"]
        values = {
            "primary": "Arum",
            "guide": "Ivana",
            "room": "北厅",
            "group_count": 1,
        }
        rendered = render_text(v9.text, values)
        self.assertTrue(rendered.blocked)


class ScopeAndPairTest(unittest.TestCase):
    def test_范围不同不能作为同一覆盖提交(self):
        workbench = load_workbench(DATA_DIR)
        pair = next(
            p
            for p in bilingual_matches(workbench, "guide-7")
            if p.zh.id == "V1" and p.en.id == "V8"
        )
        self.assertNotEqual(pair.zh_analysis.scope, pair.en_analysis.scope)
        self.assertNotEqual(pair.status, "match")

    def test_单独来宾短句与成组短句并存但不歧义(self):
        workbench = load_workbench(DATA_DIR)
        z = make_route_semantic(scope=Scope.party())
        pair_variants = [
            CopyVariant(
                "Z2",
                "guide-7",
                "zh",
                "r9",
                "{primary} 与 {group_count} 位同行者请随 {guide} 往 {room}。其余来宾请留在原处。",
            ),
            CopyVariant(
                "E9",
                "guide-7",
                "en",
                "r9",
                "{primary}, follow {guide} to {room} on your own. Other guests, stay here.",
            ),
            CopyVariant(
                "E2",
                "guide-7",
                "en",
                "r9",
                "{primary} and {group_count} companions, follow {guide} to {room}. All other guests, stay here.",
            ),
            CopyVariant(
                "Z9",
                "guide-7",
                "zh",
                "r9",
                "{primary} 请随 {guide} 往 {room}，其余来宾请留在原处。",
            ),
        ]
        semantics = Semantics(
            dict(
                DEFAULT_SEMANTICS.declarations,
                Z2=z,
                Z9=make_route_semantic(
                    scope=Scope.solo(),
                    party_displayed=False,
                    party_known=True,
                ),
                E9=DEFAULT_SEMANTICS.get("V8"),
                E2=z,
            )
        )
        wb = build_workbench(copybook=workbench.copybook + tuple(pair_variants))
        solo = resolve_delivery(
            wb, CopyRequest("A", "S2", "solo", dict(workbench.request("R2").data, group_count=0)),
            semantics=semantics,
        )
        party = resolve_delivery(
            wb, CopyRequest("B", "S2", "party", dict(workbench.request("R2").data, group_count=3)),
            semantics=semantics,
        )
        self.assertEqual(solo.copy_ids[0], "Z9")
        self.assertEqual(party.copy_ids[0], "Z2")
        self.assertEqual(party.status, "formal")

    def test_同一群体第二句范围不能变(self):
        workbench = load_workbench(DATA_DIR)
        pair = next(
            p
            for p in bilingual_matches(workbench, "guide-7")
            if p.zh.id == "V1" and p.en.id == "V4"
        )
        reference = next(c for c in pair.checks if c.id == "complement_reference")
        self.assertFalse(reference.ok)

    def test_两个互不可比的范围同时适用时交给编辑(self):
        workbench = load_workbench(DATA_DIR)
        odd_zh = make_route_semantic(scope=Scope(min_count=2))
        odd_en = make_route_semantic(scope=Scope(min_count=2))
        extra = [
            CopyVariant("ZA", "guide-7", "zh", "r20", "{primary}、{group_count} 人随 {guide} 往 {room}，其余来宾留下。"),
            CopyVariant("EA", "guide-7", "en", "r20", "{primary}, follow {guide} to {room} with {group_count} guests. Other guests, stay."),
        ]
        semantics = Semantics(
            dict(DEFAULT_SEMANTICS.declarations, ZA=odd_zh, EA=odd_en)
        )
        wb = build_workbench(copybook=workbench.copybook + tuple(extra))
        delivery = resolve_delivery(
            wb,
            CopyRequest("P", "S2", "party", dict(workbench.request("R2").data, group_count=3)),
            semantics=semantics,
        )
        self.assertIn(delivery.status, ("needs_editor", "fallback"))
        self.assertTrue(delivery.ambiguity)


class RenderingTest(unittest.TestCase):
    def test_宽度按东亚宽度与组合附标计算(self):
        self.assertEqual(display_width("北厅"), 4)
        self.assertEqual(display_width("Raden"), 5)
        self.assertEqual(display_width("é"), 1)
        self.assertEqual(display_width("a‍b"), 2)

    def test_缺少变量不会被空字符串顶替(self):
        with self.assertRaises(RenderError):
            fill_template("请 {primary} 往 {room}。", {"primary": "乌兰"})

    def test_退回消息必须是静态且不含具体值(self):
        columns, problems = check_fallback_text(
            "等候的来宾可向总台咨询。", 55, forbidden_values=("北厅",)
        )
        self.assertEqual(columns, 24)
        self.assertEqual(problems, ())
        _, problems = check_fallback_text(
            "请到 {room} 等候。", 55, forbidden_values=()
        )
        self.assertTrue(problems)
        _, problems = check_fallback_text(
            "请到北厅等候。", 55, forbidden_values=("北厅",)
        )
        self.assertTrue(problems)

    def test_空白规范化不改变中文标点(self):
        text = fill_template(
            "请 {primary} 和 {group_count} 位同行者跟 {guide} 往 {room}。",
            {"primary": "乌兰", "group_count": 0, "guide": "曾老师", "room": "北厅"},
        )
        self.assertEqual(text, "请 乌兰 和 0 位同行者跟 曾老师 往 北厅。")
        self.assertNotIn(" 。", text)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.workbench = load_workbench(DATA_DIR)

    def test_没有等价句对时只显示通用服务台(self):
        delivery = resolve_delivery(self.workbench, CopyRequest.from_visitor_request(self.workbench.request("R1")))
        self.assertIn(delivery.status, ("needs_editor", "fallback"))
        self.assertEqual(delivery.action, "ask_help_desk")
        self.assertEqual(set(delivery.messages), {"zh", "en"})
        for message in delivery.messages.values():
            self.assertEqual(message.status, "fallback")
        self.assertTrue(delivery.fallback_used)

    def test_超宽时不截断姓名也不换目的地(self):
        delivery = resolve_delivery(self.workbench, CopyRequest.from_visitor_request(self.workbench.request("R4")))
        self.assertEqual(delivery.action, "ask_help_desk")
        long_room = "地方城市口述文本资料展来宾活动入口"
        self.assertNotIn(long_room, " ".join(m.text for m in delivery.messages.values()))
        self.assertIn("超过", "；".join(delivery.issues))

    def test_可选身份为空时仍可预览但不冒用称呼(self):
        delivery = resolve_delivery(
            self.workbench,
            CopyRequest.from_visitor_request(self.workbench.request("R3"), shift="晚班"),
        )
        self.assertIn(delivery.status, ("needs_editor", "fallback"))
        text = " ".join(delivery.draft_text.values())
        self.assertIn("Arum", text)
        self.assertNotIn("Anggrek", text)
        for honorific in ("先生", "女士", "Mr", "Ms"):
            self.assertNotIn(honorific, text)

    def test_未到班次的请求不能预览(self):
        delivery = resolve_delivery(
            self.workbench, CopyRequest.from_visitor_request(self.workbench.request("R3"))
        )
        self.assertEqual(delivery.status, "not_yet")
        self.assertFalse(delivery.available)
        self.assertEqual(delivery.messages, {})

    def test_中英切换动作意图一致(self):
        for delivery in (resolve_delivery(self.workbench, CopyRequest.from_visitor_request(r)) for r in self.workbench.requests if r.id != "R3"):
            statuses = {message.status for message in delivery.messages.values()}
            self.assertEqual(len(statuses), 1, delivery.request_id)
            self.assertIn(delivery.action, {"ask_help_desk"})
        late = resolve_delivery(
            self.workbench,
            CopyRequest.from_visitor_request(self.workbench.request("R3"), shift="晚班"),
        )
        statuses = {message.status for message in late.messages.values()}
        self.assertEqual(statuses, {"fallback"})

    def test_缺目的地时不用备用提示中的地点冒充(self):
        values = dict(self.workbench.request("R2").data)
        values.pop("room")
        delivery = resolve_delivery(self.workbench, CopyRequest("X", "S2", "party", values))
        self.assertEqual(delivery.action, "ask_help_desk")
        for message in delivery.messages.values():
            self.assertNotIn("Water Garden", message.text)


class FormalSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.workbench = load_workbench(DATA_DIR)

    def test_正式提交只接受同一覆盖(self):
        plan = assemble_copybook(self.workbench)
        self.assertNotEqual(plan.status, "formal")
        for item in plan.submissions:
            self.assertNotEqual(item.status, "formal", item.label)

    def test_热换扩大人群不能进入正式提交(self):
        workbench = self.workbench
        # 给英文单独来宾句配一个中文“所有来宾”句：语言之间覆盖不同。
        extra = [
            CopyVariant(
                "ZX",
                "guide-7",
                "zh",
                "r9",
                "{primary} 请随 {guide} 往 {room}，其余来宾请留在这里。",
            ),
        ]
        semantics = Semantics(
            dict(
                DEFAULT_SEMANTICS.declarations,
                ZX=make_route_semantic(scope=Scope.all(), party_displayed=False, party_known=False, party_includes_primary=None),
            )
        )
        wb = build_workbench(copybook=workbench.copybook + tuple(extra))
        plan = assemble_copybook(wb, semantics=semantics)
        blocked = [
            item
            for item in plan.submissions
            if item.zh_id == "ZX" and item.en_id == "V8"
        ]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].status, "blocked")

    def test_缺同范围短句会列出待补译(self):
        gaps = coverage_gaps(self.workbench)
        self.assertTrue(any("单独来宾" in gap and "中文" in gap for gap in gaps))
        self.assertTrue(any("身份称呼" in gap for gap in gaps))

    def test_范围签署不能放行形态或宽度问题(self):
        approval = Approval("V1", "V4", "其余来宾即同行集合之外来宾")
        delivery = resolve_delivery(
            self.workbench,
            CopyRequest.from_visitor_request(self.workbench.request("R2")),
            approvals=[approval],
        )
        self.assertEqual(delivery.status, "needs_editor")
        self.assertEqual(delivery.action, "ask_help_desk")

    def test_交付前逐字核对姓名与目的地(self):
        from guideworkbench.bundle import _value_coverage_issues, LocaleMessage

        workbench = self.workbench
        request = CopyRequest.from_visitor_request(workbench.request("R2"))
        pair = next(
            p
            for p in bilingual_review(workbench)
            if p.zh.id == "V1" and p.en.id == "V4"
        )
        truncated = {
            locale: LocaleMessage(
                locale=locale,
                status="route",
                message_id="guide-7",
                copy_id="V1",
                revision="r1",
                # 名字被截成另一个词，地点仍在
                text=(
                    "请 Raden 和同组的 2 位同行者跟 Wei Lim 往 Water Garden。"
                    if locale == "zh"
                    else "Raden and 2 companions, follow Wei Lim to Water Garden."
                ),
                columns=50,
                limit=100,
            )
            for locale in ("zh", "en")
        }
        problems = _value_coverage_issues(pair, request, truncated)
        self.assertTrue(any("Raden Amarta de Lontar" in problem for problem in problems))
        self.assertTrue(any("截断" in problem for problem in problems))

    def test_范围签署只放行第二句指代(self):
        workbench = self.workbench
        party = make_route_semantic(scope=Scope.party(), kind="relative", source="addressee")
        extra = [
            CopyVariant(
                "ZP",
                "guide-7",
                "zh",
                "r9",
                "{primary} 与 {group_count} 位同行者请随 {guide} 往 {room}。其他来宾请留在原处。",
            ),
            CopyVariant(
                "EP",
                "guide-7",
                "en",
                "r9",
                "{primary} and {group_count} companions, follow {guide} to {room}. Other guests, stay here.",
            ),
        ]
        # 中文写“其他来宾”、英文写“Other guests”，两侧读法登记不同，
        # 需要编辑确认它们指同一剩余来宾群体。
        en_party = make_route_semantic(
            scope=Scope.party(), kind="remaining", source="sentence1_goers"
        )
        semantics = Semantics(
            dict(DEFAULT_SEMANTICS.declarations, ZP=party, EP=en_party)
        )
        wb = build_workbench(copybook=workbench.copybook + tuple(extra))
        request = CopyRequest.from_visitor_request(workbench.request("R2"))

        without = resolve_delivery(wb, request, semantics=semantics)
        self.assertEqual(without.status, "needs_editor")
        self.assertEqual(without.action, "ask_help_desk")

        approval = Approval("ZP", "EP", "其他来宾即同行集合之外来宾")
        with_note = resolve_delivery(
            wb, request, approvals=[approval], semantics=semantics
        )
        self.assertEqual(with_note.status, "approved")
        self.assertEqual(with_note.action, "route_to_room:Water Garden")
        self.assertIn("其他来宾", with_note.route_text["zh"])
        self.assertIn("Other guests", with_note.route_text["en"])

        # 同一签署不能把“范围不同”的 V4 放行。
        mismatched = resolve_delivery(
            workbench,
            request,
            approvals=[Approval("V1", "V4", "其他来宾即同行集合之外来宾")],
        )
        self.assertEqual(mismatched.status, "needs_editor")

    def test_交付前逐字核对姓名与目的地(self):
        zh_solo = make_route_semantic(
            scope=Scope.solo(), party_displayed=False, party_known=True
        )
        extra = [
            CopyVariant("ZS", "guide-7", "zh", "r9", "{primary} 请随 {guide} 前往 {room}。其余来宾请留在原处。"),
            CopyVariant("ES", "guide-7", "en", "r9", "{primary}, follow {guide} to {room} on your own. Other guests, stay here."),
        ]
        semantics = Semantics(
            dict(DEFAULT_SEMANTICS.declarations, ZS=zh_solo, ES=zh_solo)
        )
        wb = build_workbench(copybook=self.workbench.copybook + tuple(extra))
        approval = Approval("ZS", "ES", "其余来宾即同行集合之外来宾")
        delivery = resolve_delivery(
            wb,
            CopyRequest.from_visitor_request(self.workbench.request("R1")),
            approvals=[approval],
            semantics=semantics,
        )
        self.assertIn(delivery.status, ("formal", "approved"))
        self.assertIn("乌兰", delivery.route_text["zh"])
        self.assertIn("曾老师", delivery.route_text["zh"])
        self.assertIn("北厅", delivery.route_text["zh"])


class SurveyTest(unittest.TestCase):
    def setUp(self):
        self.workbench = load_workbench(DATA_DIR)

    def test_每类来宾都有可查看的预览(self):
        results = survey_classes(self.workbench)
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertTrue(result.renderable, result.request_id)
            self.assertTrue(result.consistent_action, result.request_id)

    def test_跨屏同类来宾动作不因屏幕切换而改变(self):
        view = cross_screen_view(self.workbench)
        for visit_class in ("solo", "party", "closed-name"):
            self.assertTrue(view.same_action(visit_class), visit_class)
        self.assertEqual(view.inconsistent_samples(), {})

    def test_屏幕视图列出来宾范围与缺口(self):
        view = plan_screen(self.workbench, "S1")
        self.assertEqual(set(view.assignments), {"solo", "party", "closed-name"})
        self.assertTrue(view.gaps)
        self.assertTrue(view.fallback_ok)


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.workbench = load_workbench(DATA_DIR)

    def test_原有请求仍可预览(self):
        report = inspect_copybook(self.workbench, shift="晚班")
        self.assertTrue(report.all_previewable)
        self.assertFalse(report.all_route_delivered)
        self.assertIn("S1", report.devices_to_notify)
        self.assertTrue(report.unsuitable)

    def test_未到班次的请求被标出(self):
        report = inspect_copybook(self.workbench)
        r3 = next(row for row in report.rows if row.request_id == "R3")
        self.assertEqual(r3.after_status, "not_yet")
        self.assertFalse(r3.preview_available)

    def test_翻译临时更正会重新选择短句并在必要时退回(self):
        # 更正版把英文成组短句改成与中文 V1 完全同范围、同补集的写法。
        corrected = [
            variant
            for variant in self.workbench.copybook
            if variant.id not in {"V4", "V9"}
        ] + [
            CopyVariant(
                "V4",
                "guide-7",
                "en",
                "r6",
                "{primary} and {group_count} companions, follow {guide} to {room}. All remaining guests, stay here.",
            ),
        ]
        # 更正后的英文与 V1 覆盖一致（所有已登记来宾），只是措辞改为
        # “All remaining guests”，与中文“其余来宾”同指一个剩余群体。
        semantics = Semantics(
            dict(
                DEFAULT_SEMANTICS.declarations,
                V4=make_route_semantic(scope=Scope.all()),
            )
        )
        report = inspect_copybook(
            self.workbench, proposed=corrected, semantics=semantics
        )
        self.assertTrue(report.rows)
        r2 = next(row for row in report.rows if row.request_id == "R2")
        self.assertTrue(r2.preview_available)
        self.assertNotEqual(r2.before_copy_ids, r2.after_copy_ids)

    def test_新增访客类别不沿用现有文案(self):
        extra = VisitorRequest(
            id="R9",
            visit_class="deaf-guest",
            device="S1",
            data={
                "primary": "Ada",
                "guide": "李老师",
                "room": "北厅",
                "group_count": 0,
                "honorific": None,
            },
        )
        report = inspect_copybook(self.workbench, extra_requests=[extra])
        row = next(row for row in report.rows if row.request_id == "R9")
        self.assertFalse(row.route_delivered)
        self.assertTrue(row.preview_available)
        self.assertIn("S1", report.devices_to_notify)

    def test_替换掉退回消息的更正会点出缺安全回退(self):
        proposed = [
            variant
            for variant in self.workbench.copybook
            if variant.message_id != "guide-8"
        ]
        report = inspect_copybook(self.workbench, proposed=proposed)
        self.assertFalse(report.fallback_ok_by_screen["S1"])
        self.assertIn("S1", report.devices_to_notify)


if __name__ == "__main__":
    unittest.main()
