"""C5 澄清闸门判据（docs/02 §0.1 量化验收：无限定条件数字题 100% 触发 + 任一限定放行）。"""

from __future__ import annotations

import pytest

from bidqa.agent.clarify import has_qualifiers, is_underqualified


@pytest.mark.parametrize("question", [
    "有多少个中标项目？",          # 数字意图，无任何限定
    "中标金额总共多少？",          # 数字意图，无任何限定
    "采购项目的均价是多少？",      # 数字意图，无任何限定
    "帮我查一下排名前五的",        # 数字意图，无任何限定
])
def test_underqualified_number_question_triggers(question: str) -> None:
    assert is_underqualified(question) is True


@pytest.mark.parametrize("question", [
    "空调采购的主要供应商有哪些？",        # 标的物=空调
    "安徽省软件采购金额排名？",            # 地区=安徽
    "最近三个月电梯采购多少家中标？",      # 时间=最近三个月
    "2025年计算机设备采购总额是多少？",    # 时间=2025年
    "合肥市医疗设备采购的中标人是谁？",    # 地区=合肥
    "你好",                                # 无数字意图，不触发
    "招标公告在哪里查看？",                # 无数字意图（虽然也无标的物）
])
def test_qualified_question_passes_gate(question: str) -> None:
    assert is_underqualified(question) is False


def test_qualifier_breakdown() -> None:
    q = has_qualifiers("安徽省2025年空调采购")
    assert q == {"region": True, "time": True, "subject": True}
    q2 = has_qualifiers("有多少个中标项目？")
    assert not any(q2.values())
