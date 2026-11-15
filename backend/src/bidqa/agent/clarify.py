"""C5 澄清闸门——限定条件判据（docs/02 C5；量化验收 §0.1：无限定条件数字题 100% 触发）。

判据：限定条件=地区/时间/标的物，任一存在即放行（不追问）。
启发式（确定性、无模型调用，golden 集验收）：
- 地区：34 省级行政区名词表 + 「X省/X市/X区/X县/X自治区」后缀模式；
- 时间：年份/月份/日/今年/去年/最近/季度/上半年 等模式；
- 标的物：剥掉领域停用词（采购/中标/招标…）后仍留有 ≥2 连续汉字的实义名词。
数字意图 = 多少/排名/金额/均价/几家/TopN 类词。
触发 = 有数字意图 且 三类限定条件全无。
"""

from __future__ import annotations

import re

# 省级行政区词表单一来源 = ingest/regions.PROVINCES（C1 检查点拍板收口：
# agent→ingest 分层允许；此前本地副本与 ingest 各一份会漂移）
from bidqa.ingest.regions import PROVINCES

_REGION_RE = re.compile("|".join(PROVINCES) + r"|[\u4e00-\u9fa5]{1,4}(?:省|市|自治区)")
_TIME_RE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日|今年|去年|前年|本月|上月|"
    r"最近|近期|今年以来|上半年|下半年|第[一二三四]季度|[春夏秋冬]季|同比|环比"
)
_NUMBER_INTENT_RE = re.compile(
    r"多少|排名|排行|top\s*\d*|几家|几个|数量|金额|均价|平均|合计|总额|总计|规模|份额",
    re.IGNORECASE,
)
# 领域停用词：剥离后剩余实义词视为标的物
_DOMAIN_STOPWORDS = re.compile(
    r"采购|招标|投标|中标|竞标|开标|评标|项目|工程|货物|服务|供应商|投标人|招标人|"
    r"采购人|代理机构|公告|结果|主要|哪些|什么|多少|排名|金额|数量|情况|信息|的|了|吗|呢|"
    r"有|是|在|和|与|及|请|问|我|想|了解|一下|关于|进行|可以|能够|是否|如何|怎样"
)
# 数字/程度副词类填充词（不构成标的物）+ 单字动词
_FILLER_RE = re.compile(
    r"总共|共计|一共|大概|大约|以上|以下|之前|之后|前\s*\d+|前十|前五|前几|排行|"
    r"均价|平均|合计|总额|总计|规模|份额|几家|几个|查询|查一下|查查|看看|统计|汇总|"
    r"列出|告诉|帮忙|帮我|请问|目前|现在|分别|各|全部|所有|帮|查|找|看|列|搜"
)
_HAS_SUBJECT_RE = re.compile(r"[\u4e00-\u9fa5]{2,}")


def has_qualifiers(question: str) -> dict[str, bool]:
    stripped = _FILLER_RE.sub("", _DOMAIN_STOPWORDS.sub("", question))
    return {
        "region": bool(_REGION_RE.search(question)),
        "time": bool(_TIME_RE.search(question)),
        "subject": bool(_HAS_SUBJECT_RE.search(stripped)),
    }


def is_underqualified(question: str) -> bool:
    """True=应触发澄清（有数字意图且地区/时间/标的物全无）。"""
    if not _NUMBER_INTENT_RE.search(question):
        return False
    q = has_qualifiers(question)
    return not (q["region"] or q["time"] or q["subject"])
