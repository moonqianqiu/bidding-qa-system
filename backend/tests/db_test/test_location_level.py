"""NIGHT-008 Location.level 判定：按名称分省/市两级（docs/01 §4.2 `:Location {name, level}`）。

原建图硬编码 `SET l.level = '省'`，40 个存量节点全错标（多数实为「省+市」名）。
纯函数单测无需 Neo4j；接线单测用 stub driver 验证 upsert 确实把判定结果传给 Cypher。
"""

from __future__ import annotations

import pytest

from bidqa.database.neo4j import NeoClient, location_level


@pytest.mark.parametrize("name,level", [
    # 纯省名 → 省
    ("安徽省", "省"),
    ("江苏省", "省"),
    # 自治区/特别行政区全名与惯用简称 → 省
    ("广西壮族自治区", "省"),
    ("新疆维吾尔自治区", "省"),
    ("内蒙古", "省"),
    # 省+市拼接 → 市
    ("安徽省合肥市", "市"),
    ("江苏省南京市", "市"),
    # 直辖市 → 市（省/市两级模型中直辖市本身即市级实体）
    ("北京市", "市"),
    ("北京市北京市", "市"),
    ("重庆市渝中区", "市"),
    # 自治州/地区/盟 → 市
    ("云南省大理白族自治州", "市"),
    ("湖北省恩施土家族苗族自治州", "市"),
    ("兴安盟", "市"),
    ("喀什地区", "市"),
])
def test_location_level_by_name(name: str, level: str) -> None:
    assert location_level(name) == level


def test_location_level_defensive() -> None:
    """空名不会建节点（upsert FOREACH 空值守卫），None/空白防御性归 '市'。"""
    assert location_level("") == "市"
    assert location_level(None) == "市"
    assert location_level("  ") == "市"


def test_all_province_level_divisions_classified() -> None:
    """34 省级行政区完备性：23 省 + 5 自治区 + 2 特别行政区 → 省；4 直辖市 → 市。"""
    provinces = ["河北省", "山西省", "辽宁省", "吉林省", "黑龙江省", "江苏省", "浙江省",
                 "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省",
                 "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省", "甘肃省",
                 "青海省", "台湾省", "内蒙古自治区", "广西壮族自治区", "西藏自治区",
                 "宁夏回族自治区", "新疆维吾尔自治区", "香港特别行政区", "澳门特别行政区"]
    assert {location_level(n) for n in provinces} == {"省"}
    assert {location_level(n) for n in ("北京市", "上海市", "天津市", "重庆市")} == {"市"}


def test_upsert_passes_computed_level(monkeypatch) -> None:
    """建图接线：upsert 必须把 location_level(region) 作为 $level 传给 Cypher，
    且语句不再硬编码 '省'（NIGHT-008 根因）。"""
    runs: list[tuple[str, dict]] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, cypher, **params):
            runs.append((cypher, params))
            return []

    class FakeDriver:
        def session(self, *args, **kwargs):
            return FakeSession()

        def close(self):
            return None

    class FakeGraphDatabase:  # 形状同 neo4j.GraphDatabase 模块：.driver() 工厂
        @staticmethod
        def driver(*args, **kwargs):
            return FakeDriver()

    monkeypatch.setattr("bidqa.database.neo4j.GraphDatabase", FakeGraphDatabase)
    client = NeoClient("bolt://stub", "u", "p")
    client.upsert_project({"project_no": "U1", "region": "安徽省合肥市"})
    client.upsert_project({"project_no": "U2", "region": "安徽省"})
    client.upsert_project({"project_no": "U3", "region": "  "})  # 空地区不建 Location

    assert "SET l.level = $level" in runs[0][0]
    assert "SET l.level = '省'" not in runs[0][0]
    assert runs[0][1]["level"] == "市" and runs[0][1]["region"] == "安徽省合肥市"
    assert runs[1][1]["level"] == "省"
    assert runs[2][1]["level"] == "市" and runs[2][1]["region"] == ""
