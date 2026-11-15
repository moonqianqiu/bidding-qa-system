"""PG 集成测试（真实服务；CI 无 PG 自动跳过）。

覆盖：ensure_schema 幂等、upsert 判重语义（同批次=更新/跨批次=并存）、
六类查询、D3 工具端到端、conversations/feedback 存储。
C2 起存储按职责分模块（PgConn + BpRepo/ConversationStore/FeedbackStore），
本文件断言语义与拆分前逐字一致，仅构造方式随新形状更新。
"""

from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest
from psycopg import sql

from bidqa.config import Settings
from bidqa.database.bp_repo import BpRepo
from bidqa.database.connection import PgConn
from bidqa.database.conversations import ConversationStore
from bidqa.database.feedback import FeedbackStore
from bidqa.ingest.db_import import import_db
from bidqa.tools.db_tool import SearchDatabaseTool


def _stores(conn: PgConn) -> tuple[BpRepo, ConversationStore, FeedbackStore]:
    return BpRepo(conn), ConversationStore(conn), FeedbackStore(conn)


@pytest.fixture()
def clean_pg(pg_client: PgConn):
    yield pg_client
    pg_client.connection().execute("DELETE FROM bidding_procurement WHERE batch_id LIKE 'it-%'")
    pg_client.connection().execute("DELETE FROM feedback WHERE trace_id LIKE 'it-%'")
    pg_client.commit()


@pytest.fixture()
def isolated_pg(pg_client: PgConn):
    """一次性隔离库（P0-7 收口）：真实服务器上临时建 bidqa_it_<8hex> 库，
    破坏性用例（全局 purge / 游客开关等无 scoped 条件的 SQL）在其上执行，
    结束整库 DROP——真实开发库零接触（AGENTS：测试不得污染真实仓库）。
    建库挂在 pg_client 同一套连接参数上；pg_client 先行 skip 时本夹具连带跳过。"""
    s = Settings(_env_file=".env")  # cwd=backend 时读真实 .env
    name = f"bidqa_it_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(host=s.POSTGRES_HOST, port=s.POSTGRES_PORT,
                            dbname=s.POSTGRES_DB, user=s.POSTGRES_USER,
                            password=s.POSTGRES_PASSWORD, autocommit=True)
    created = False
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created = True
        client = PgConn(s.POSTGRES_HOST, s.POSTGRES_PORT, name,
                        s.POSTGRES_USER, s.POSTGRES_PASSWORD)
        client.ensure_schema()
        yield client
        client.close()
    finally:
        if created:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)")
                          .format(sql.Identifier(name)))
        admin.close()


def test_schema_idempotent(pg_client: PgConn) -> None:
    pg_client.ensure_schema()
    pg_client.ensure_schema()  # 二次执行不炸 = 幂等


ROWS = [
    {"project_no": "IT-001", "project_name": "空调采购项目", "winner": "甲公司",
     "award_amount": 660_000, "region": "安徽", "subject_matter": "空调",
     "publish_date": date(2025, 1, 15)},
    {"project_no": "IT-002", "project_name": "电梯采购项目", "winner": "乙公司",
     "award_amount": 1_200_000, "region": "安徽", "subject_matter": "电梯",
     "publish_date": date(2025, 2, 20)},
    {"project_no": "IT-003", "project_name": "软件采购项目", "winner": "甲公司",
     "award_amount": None, "region": "江苏", "subject_matter": "软件",
     "publish_date": date(2025, 2, 28)},
]


def test_upsert_and_queries(clean_pg: PgConn) -> None:
    bp, _, _ = _stores(clean_pg)
    bp.upsert_bp([dict(r) for r in ROWS], "it-1")
    # 同批次重复导入 = 更新（不产生重复行）
    updated = [dict(ROWS[0])]
    updated[0]["award_amount"] = 700_000
    bp.upsert_bp(updated, "it-1")
    rows, _ = bp.query_bp({"query_type": "detail_search", "keyword": "IT-001",
                           "top_n": 5})
    assert len(rows) == 1 and rows[0]["award_amount"] == 700_000

    # 跨批次 = 并存
    bp.upsert_bp([dict(ROWS[0])], "it-2")
    rows, _ = bp.query_bp({"query_type": "detail_search", "keyword": "IT-001",
                           "top_n": 5})
    assert len(rows) == 2

    # 聚合：标的物（金额 NULL 不计入 SUM，行数计入 COUNT）；按批次过滤——共享开发库
    # 已有真实数据（subject_matter 回填后维度 2,188 个），无过滤时种子行进不了真实 top10
    # （2026-11-08 实证：HEAD 上即挂，e022a7b 时点已潜伏；修复同 test_six_query_types 口径）
    rows, _ = bp.query_bp(
        {"query_type": "subject_agg", "top_n": 10, "filters": {"batch_id": "it-1"}})
    soft = next(r for r in rows if r["label"] == "软件")
    assert soft["projects"] == 1 and soft["total_amount"] is None  # 取不到=NULL 非 0


def test_six_query_types(clean_pg: PgConn) -> None:
    bp, _, _ = _stores(clean_pg)
    bp.upsert_bp([dict(r) for r in ROWS], "it-1")
    batch = {"filters": {"batch_id": "it-1"}}  # 开发库可能已有真实数据，按批次隔离断言
    for qt, expect_label in [("region_agg", "安徽"), ("supplier_topn", "乙公司"),
                             ("purchaser_topn", None), ("time_trend", "2025-02"),
                             ("subject_agg", "电梯")]:
        rows, _ = bp.query_bp({"query_type": qt, "top_n": 10, **batch})
        assert rows, f"{qt} 无结果"
        if expect_label:
            assert any(r["label"] == expect_label for r in rows)


def test_region_filter_contains_city_granularity(clean_pg: PgConn) -> None:
    """真实库 region 是省+市（安徽省合肥市），按省过滤必须命中（包含匹配）。"""
    bp, _, _ = _stores(clean_pg)
    bp.upsert_bp([{"project_no": "IT-R1", "project_name": "地区过滤验证",
                   "region": "安徽省合肥市", "award_amount": 100}], "it-r")
    for filters in ({"地区": "安徽省"}, {"region": "合肥"}, {"省份": "安徽"}):
        rows, _ = bp.query_bp({"query_type": "region_agg", "filters": filters, "top_n": 10})
        assert "安徽省合肥市" in [r["label"] for r in rows], filters


def test_d3_tool_end_to_end(clean_pg: PgConn) -> None:
    bp, _, _ = _stores(clean_pg)
    bp.upsert_bp([dict(r) for r in ROWS], "it-1")
    tool = SearchDatabaseTool(bp)
    out = tool.run(tool.args_model.model_validate(
        {"query_type": "supplier_topn", "top_n": 5,
         "filters": {"batch_id": "it-1"}}))  # 开发库有真实数据，按批次隔离
    assert out["empty"] is False
    assert out["charts"][0]["kind"] == "bar"
    assert "乙公司" in out["charts"][0]["items"][0]["label"] or \
        any("乙公司" in it["label"] for it in out["charts"][0]["items"])
    # 聚合查询附 1 条统计口径来源（数据类答案不再零出处；方案 A 2026-11-15）
    assert len(out["sources"]) == 1
    assert out["sources"][0]["id"].startswith("db:agg:")
    assert "batch_id" in out["sources"][0]["title"] or "全库" in out["sources"][0]["title"]

    detail = tool.run(tool.args_model.model_validate(
        {"query_type": "detail_search", "keyword": "空调"}))
    assert "空调采购项目" in detail["text"]
    assert detail["sources"][0]["id"].startswith("db:IT-001")

    empty = tool.run(tool.args_model.model_validate(
        {"query_type": "detail_search", "keyword": "不存在的关键词XYZ"}))
    assert empty["empty"] is True and "放宽限定条件" in empty["text"]


def test_aggregate_keyword_filters(clean_pg: PgConn) -> None:
    """keyword 对聚合类生效（2026-11-15 修复）：TopN 只统计关键词命中行。"""
    bp, _, _ = _stores(clean_pg)
    bp.upsert_bp([dict(r) for r in ROWS], "it-1")
    tool = SearchDatabaseTool(bp)
    batch = {"filters": {"batch_id": "it-1"}}  # 开发库真实数据按批次隔离
    all_rows, _ = bp.query_bp({"query_type": "supplier_topn", "top_n": 10, **batch})
    ac_rows, _ = bp.query_bp({"query_type": "supplier_topn", "keyword": "空调",
                              "top_n": 10, **batch})
    assert len(ac_rows) < len(all_rows)  # 未含关键词的乙公司（电梯）被过滤
    assert [r["label"] for r in ac_rows] == ["甲公司"]  # 只剩空调采购项目的中标人
    assert ac_rows[0]["projects"] == 1
    empty = tool.run(tool.args_model.model_validate(
        {"query_type": "supplier_topn", "keyword": "不存在的关键词XYZ", **batch}))
    assert empty["empty"] is True and "放宽限定条件" in empty["text"]  # 诚实空结果


def test_excel_import_dry_run(clean_pg: PgConn, tmp_path) -> None:
    """F6 统一试运行约定：--dry-run 打条数+抽样+不写库。"""
    from openpyxl import Workbook

    bp, _, _ = _stores(clean_pg)
    wb = Workbook()
    ws = wb.active
    ws.append(["项目编号", "项目名称", "中标人", "中标金额", "地区", "发布时间"])
    ws.append(["IT-101", "示例空调采购", "丙公司", "66万", "安徽省合肥市", "2025-03-01"])
    ws.append(["IT-102", "示例电梯采购", "丁公司", "120万元", "江苏", "2025/03/05"])
    ws.append([None, None, None, None, None, None])  # 空行跳过
    path = tmp_path / "sample.xlsx"
    wb.save(path)

    result = import_db(path, bp, batch_id="it-dry", dry_run=True)
    assert result["dry_run"] is True and result["rows"] == 2
    rows, _ = bp.query_bp({"query_type": "detail_search", "keyword": "IT-101"})
    assert rows == []  # dry-run 不写库

    result = import_db(path, bp, batch_id="it-dry")
    assert result["written"] == 2
    rows, _ = bp.query_bp({"query_type": "detail_search", "keyword": "IT-101"})
    assert rows[0]["award_amount"] == 660_000  # 「66万」归一
    assert rows[0]["region"] == "安徽"          # 地区归一


def test_conversations_and_feedback(clean_pg: PgConn) -> None:
    _, convs, fb = _stores(clean_pg)
    conv_id = str(uuid.uuid4())
    convs.upsert_conversation(conv_id, "测试会话",
                              {"schema_v": 1, "items": [{"role": "user", "content": "hi"}]})
    got = convs.get_conversation(conv_id)
    assert got["title"] == "测试会话" and got["messages"]["schema_v"] == 1
    assert conv_id in [c["id"] for c in convs.list_conversations()]
    fid = fb.insert_feedback(conv_id, "空调采购有哪些？", "down",
                             sources=[{"id": "chunk-1"}], trace_id="it-fb-1")
    assert fid > 0
    with pytest.raises(ValueError):
        fb.insert_feedback(conv_id, "q", "middle", sources=[])
    convs.delete_conversation(conv_id)
    assert convs.get_conversation(conv_id) is None


# ── 反向匹配兜底（参考吸收 #4b：正向落空 + 关键词≥3 字 → 关键词包含字段值）──
# 注意：跑在真实开发库上（8,489 行），断言一律用成员包含，不假设表里只有测试行。
def test_query_bp_reverse_matching(clean_pg: PgConn) -> None:
    bp, _, _ = _stores(clean_pg)
    rows = [
        {"project_no": "IT-RM-001", "project_name": "空调采购项目", "winner": "甲公司",
         "award_amount": 100, "region": "安徽", "publish_date": date(2025, 1, 1)},
        {"project_no": "IT-RM-002", "project_name": "电梯采购项目", "winner": "乙公司",
         "award_amount": 200, "region": "芜湖", "publish_date": date(2025, 1, 2)},
    ]
    bp.upsert_bp([dict(r, batch_id="it-rm") for r in rows], "it-rm")
    # 关键词是实际名称的超集（带「及监理服务招标公告」后缀）→ 正向 0 行，反向命中
    got, sql = bp.query_bp(
        {"query_type": "detail_search", "keyword": "空调采购项目及监理服务招标公告"})
    assert "IT-RM-001" in [r["project_no"] for r in got]
    assert "ILIKE" in sql  # 返回的 sql 是实际生效的反向变体
    # 反向变体里 filters 原样保留：region 过滤芜湖 → 测试行（region 安徽）被筛掉
    got2, _ = bp.query_bp({"query_type": "detail_search",
                           "keyword": "空调采购项目及监理服务招标公告",
                           "filters": {"region": "芜湖"}})
    assert "IT-RM-001" not in [r["project_no"] for r in got2]
    # 正向直接命中时不触发反向（sql 无 ILIKE）
    got3, sql3 = bp.query_bp({"query_type": "detail_search", "keyword": "电梯"})
    assert "IT-RM-002" in [r["project_no"] for r in got3]
    assert "ILIKE" not in sql3


# ── 用户体系（docs/01 §3.1）：UsersStore 真实 PG 集成；IT-auth 前缀 try/finally 清理 ──
def test_users_store_seed_login_resolve_roundtrip(pg_client: PgConn) -> None:
    """空表种子幂等 → create_user → login 正/误 → resolve → disable 即刻失效 → logout。"""
    from bidqa.auth.store import UsersStore

    store = UsersStore(pg_client)
    name = "IT-auth-alice"
    try:
        store.seed_if_empty("IT-auth-admin", "pw-admin")
        assert store.seed_if_empty("IT-auth-admin", "pw-admin") is False  # 非空即跳过
        assert store.count_users() >= 1

        uid = store.create_user(name, "pw-alice", "user")
        assert uid is not None
        assert store.create_user(name, "x", "user") is None  # 重名拒绝

        ok = store.login(name, "pw-alice", ttl_hours=72)
        assert ok and ok["role"] == "user" and ok["username"] == name
        assert store.login(name, "wrong", ttl_hours=72) is None  # 凭据错统一 None（防枚举）

        ident = store.resolve(ok["token"])
        assert ident is not None and ident.user_id == uid and ident.role == "user"

        # 停用 → 会话即刻失效（resolve 复核 disabled）
        assert store.set_disabled(name, True) is True
        assert store.resolve(ok["token"]) is None
        assert store.set_disabled(name, False) is True

        # 注销 → resolve 不回
        assert store.logout(ok["token"]) is True
        assert store.resolve(ok["token"]) is None
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM sessions WHERE user_id IN "
                     "(SELECT id FROM users WHERE username LIKE 'IT-auth-%')")
        conn.execute("DELETE FROM users WHERE username LIKE 'IT-auth-%'")
        pg_client.commit()


def test_guest_toggle_and_visibility(isolated_pg: PgConn) -> None:
    """游客开关运行时切换 + 会话可见域（user 仅本人 / admin 含机器域 / 机器仅 NULL）。

    P0-7：set_guest_enabled 会写真实 app_settings（旧实现 finally 直接 DELETE
    种子行=污染运行时开关），迁隔离库执行，不再需要任何清理（整库即弃）。"""
    from bidqa.auth.store import UsersStore

    store = UsersStore(isolated_pg)
    convs = ConversationStore(isolated_pg)
    store.set_guest_enabled(False)
    assert store.login_guest(1) is None  # 关=不可进
    store.set_guest_enabled(True)
    guest = store.login_guest(1)
    assert guest and guest["role"] == "guest"
    ident = store.resolve(guest["token"])
    assert ident is not None and ident.user_id is None  # 游客无 users 行

    # 可见域三元组（_visibility）
    import uuid as _uuid

    mine, other, machine = (str(_uuid.uuid4()) for _ in range(3))
    uid = store.create_user("IT-auth-bob", "pw", "user")
    admin_uid = store.create_user("IT-auth-root", "pw", "admin")
    convs.upsert_conversation(mine, "mine", {"schema_v": 1, "items": []},
                              user_id=uid)
    convs.upsert_conversation(other, "other", {"schema_v": 1, "items": []},
                              user_id=admin_uid)
    convs.upsert_conversation(machine, "machine", {"schema_v": 1, "items": []})

    assert store.reset_password("IT-auth-bob", "pw2") is not None  # 新契约：成功=吊销会话数
    got = [c["id"] for c in convs.list_conversations(
        viewer_user_id=uid, machine_domain=False)]
    assert mine in got and other not in got and machine not in got
    got_admin = [c["id"] for c in convs.list_conversations(
        viewer_user_id=admin_uid, machine_domain=True)]
    assert {other, machine} <= set(got_admin)  # admin=本人 + 机器域
    assert mine not in got_admin  # 普通用户的会话对 admin 不可见（隔离）
    got_machine = [c["id"] for c in convs.list_conversations(
        viewer_user_id=None, machine_domain=True)]
    assert machine in got_machine and mine not in got_machine
    assert convs.get_conversation(other, viewer_user_id=uid,
                                  machine_domain=False) is None  # 越权=不可见
    assert convs.delete_conversation(other, viewer_user_id=uid,
                                     machine_domain=False) is False


# ── B12 控制台全库会话管理（真实 PG 集成；IT-console- 前缀 try/finally 清理）──
def test_list_all_conversations_owner_join_and_q_filter(pg_client: PgConn) -> None:
    """全库列表旁路：LEFT JOIN users 取属主名（机器域=owner None）；q 过滤 title。"""
    from bidqa.auth.store import UsersStore

    store = UsersStore(pg_client)
    convs = ConversationStore(pg_client)
    uid = store.create_user("IT-console-owner", "pw", "user")
    ids = {k: str(uuid.uuid4()) for k in ("bobs", "machine")}
    try:
        convs.upsert_conversation(ids["bobs"], "IT-console-bobs问答",
                                  {"schema_v": 1, "items": [{"role": "user", "content": "q"}]},
                                  user_id=uid)
        convs.upsert_conversation(ids["machine"], "IT-console-机器域",
                                  {"schema_v": 1, "items": [{"role": "user", "content": "q"}]})
        rows = convs.list_all_conversations(limit=500)
        by_id = {r["id"]: r for r in rows}
        assert by_id[ids["bobs"]]["owner"] == "IT-console-owner"
        assert by_id[ids["bobs"]]["user_id"] == uid
        assert by_id[ids["machine"]]["owner"] is None and \
            by_id[ids["machine"]]["user_id"] is None  # 机器域
        assert by_id[ids["bobs"]]["has_reply"] is False  # 无 assistant 回复

        hit = [r["id"] for r in convs.list_all_conversations(limit=500, q="bobs")]
        assert ids["bobs"] in hit and ids["machine"] not in hit
        miss = [r["id"] for r in convs.list_all_conversations(limit=500,
                                                              q="不存在的标题")]
        assert ids["bobs"] not in miss
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM sessions WHERE user_id IN "
                     "(SELECT id FROM users WHERE username LIKE 'IT-console-%')")
        conn.execute("DELETE FROM users WHERE username LIKE 'IT-console-%'")
        conn.execute("DELETE FROM conversations WHERE title LIKE 'IT-console-%'")
        pg_client.commit()


def test_purge_empty_protection_window_and_shell_rule(isolated_pg: PgConn) -> None:
    """空壳判定（无 assistant 回复）+ 10 分钟保护窗：刚更新过的空壳不被盘点/清删。

    P0-7：purge 的 DELETE 无任何前缀条件=全局删除是**生产行为**（审计红实证：
    开发库上跑会吞掉非测试空壳行），故用例迁一次性隔离库执行；并保留
    「非 IT- 前缀陈年空壳同样被删」的镜像断言钉死全局语义，清理随整库即弃。"""
    convs = ConversationStore(isolated_pg)
    fresh, aged, with_reply, aged_raw = (str(uuid.uuid4()) for _ in range(4))
    convs.upsert_conversation(fresh, "IT-console-新鲜空壳",
                              {"schema_v": 1,
                               "items": [{"role": "user", "content": "q"}]})
    convs.upsert_conversation(aged, "IT-console-陈年空壳",
                              {"schema_v": 1, "items": []})
    convs.upsert_conversation(aged_raw, "遗留-非前缀空壳",
                              {"schema_v": 1, "items": []})
    convs.upsert_conversation(with_reply, "IT-console-有回复",
                              {"schema_v": 1,
                               "items": [{"role": "user", "content": "q"},
                                         {"role": "assistant", "content": "a"}]})
    # 两个陈年空壳的 updated_at 拨回 11 分钟前（保护窗外）
    isolated_pg.connection().execute(
        "UPDATE conversations SET updated_at = now() - interval '11 minutes' "
        "WHERE id IN (%s, %s)", (aged, aged_raw))

    stats = convs.empty_conversations()
    assert "IT-console-陈年空壳" in stats["samples"] and stats["count"] >= 2
    # 单行条件直查（判定矩阵精确断言）
    titles = [r["title"] for r in isolated_pg.connection().execute(
        "SELECT title FROM conversations WHERE "
        f"{ConversationStore._empty_shell_cond()} AND updated_at < "
        "now() - make_interval(mins => 10)").fetchall()]
    assert "IT-console-陈年空壳" in titles
    assert "遗留-非前缀空壳" in titles  # purge 的判定不看前缀（全局语义）
    assert "IT-console-新鲜空壳" not in titles  # 保护窗内不动
    assert "IT-console-有回复" not in titles  # 有 assistant 回复不算空壳

    deleted = convs.purge_empty_conversations()
    assert deleted >= 2
    assert convs.get_conversation(aged) is None
    assert convs.get_conversation(aged_raw) is None  # 非前缀空壳一并删除
    assert convs.get_conversation(fresh) is not None  # 保护窗内幸存
    assert convs.get_conversation(with_reply) is not None  # 有回复幸存


def test_reset_password_revokes_sessions(pg_client: PgConn) -> None:
    """改密吊销会话（store 层单一入口）：登录 → 改密 → 旧 token resolve 不回。"""
    from bidqa.auth.store import UsersStore

    store = UsersStore(pg_client)
    try:
        store.create_user("IT-console-reset", "pw-old", "user")
        session = store.login("IT-console-reset", "pw-old", ttl_hours=1)
        assert session and store.resolve(session["token"]) is not None

        revoked = store.reset_password("IT-console-reset", "pw-new")
        assert revoked == 1  # 恰好吊销刚才那一个
        assert store.resolve(session["token"]) is None  # 旧令牌即刻失效
        assert store.login("IT-console-reset", "pw-new", ttl_hours=1) is not None
        assert store.login("IT-console-reset", "pw-old", ttl_hours=1) is None
        assert store.reset_password("IT-console-ghost", "x") is None  # 不存在=None
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM sessions WHERE user_id IN "
                     "(SELECT id FROM users WHERE username LIKE 'IT-console-%')")
        conn.execute("DELETE FROM users WHERE username LIKE 'IT-console-%'")
        pg_client.commit()


def test_resolve_uses_live_role(pg_client: PgConn) -> None:
    """role 实时取库（审计 P1-3）：登录后角色被降级，旧会话即刻失权——
    resolve 此前读 sessions.role 登录时快照，降权窗口长达会话 TTL（72h）。"""
    from bidqa.auth.store import UsersStore

    store = UsersStore(pg_client)
    try:
        store.create_user("IT-auth-role", "pw", "admin")
        session = store.login("IT-auth-role", "pw", ttl_hours=1)
        ident = store.resolve(session["token"])
        assert ident is not None and ident.role == "admin"
        pg_client.connection().execute(
            "UPDATE users SET role = 'user' WHERE username = 'IT-auth-role'")
        pg_client.commit()
        ident2 = store.resolve(session["token"])
        assert ident2 is not None and ident2.role == "user"  # 降权即刻生效
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM sessions WHERE user_id IN "
                     "(SELECT id FROM users WHERE username LIKE 'IT-auth-%')")
        conn.execute("DELETE FROM users WHERE username LIKE 'IT-auth-%'")
        pg_client.commit()


def test_upsert_conflict_owner_guard(pg_client: PgConn) -> None:
    """upsert ON CONFLICT 归属守卫（审计 P1-1 存储层）：owner 不同的更新整条拒绝
    （title/messages/provider 均不动）——回答不得写进他人会话。同 owner 与机器域
    （NULL）照常更新。"""
    conv = str(uuid.uuid4())
    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        store = ConversationStore(pg_client)
        store.upsert_conversation(conv, "A的", {"schema_v": 1, "items": []},
                                  provider="p1", user_id=uid_a)
        # 他人 upsert：整条拒绝（不带 WHERE 修正前会覆盖 title/messages/provider）
        store.upsert_conversation(conv, "抢夺", {"schema_v": 2, "items": []},
                                  provider="p2", user_id=uid_b)
        row = store.get_conversation(conv)
        assert row["title"] == "A的" and row["provider"] == "p1"
        assert row["messages"]["schema_v"] == 1
        # 同 owner 更新照常
        store.upsert_conversation(conv, "A改", {"schema_v": 2, "items": []},
                                  provider="p1", user_id=uid_a)
        assert store.get_conversation(conv)["title"] == "A改"
        # 机器域（NULL）：NULL owner 行可被机器域更新；机器域同样不得动他人行
        machine_conv = str(uuid.uuid4())
        store.upsert_conversation(machine_conv, "机器的", {"schema_v": 1, "items": []},
                                  provider="", user_id=None)
        store.upsert_conversation(machine_conv, "机器改", {"schema_v": 2, "items": []},
                                  provider="", user_id=None)
        assert store.get_conversation(machine_conv)["title"] == "机器改"
        store.upsert_conversation(machine_conv, "A抢机器", {"schema_v": 3, "items": []},
                                  provider="", user_id=uid_a)
        assert store.get_conversation(machine_conv)["title"] == "机器改"  # 互为越权
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM conversations WHERE id = ANY(%s)", ([conv, machine_conv],))
        pg_client.commit()


def test_build_pg_failure_not_cached_forever(monkeypatch) -> None:
    """PG 首次构建失败不得永久缓存 None（审计 P1-2）：lru_cache 曾把「PG 暂时
    不可达」钉死成「进程内永远不可用」，PG 恢复后不重启仍全量降级。负缓存只在
    短窗口内抑制重试，窗口过后重连。"""
    from bidqa.database import factory as db_factory

    class FakeSettings:
        has_postgres = True
        POSTGRES_HOST = "127.0.0.1"
        POSTGRES_PORT = 1
        POSTGRES_DB = "db"
        POSTGRES_USER = "u"
        POSTGRES_PASSWORD = "p"

    calls: list[int] = []

    class FakeConn:
        def __init__(self, *_a, **_k) -> None:
            calls.append(1)

        def ensure_schema(self) -> None:
            raise RuntimeError("PG down")

    monkeypatch.setattr(db_factory, "get_settings", lambda: FakeSettings)
    monkeypatch.setattr(db_factory, "PgConn", FakeConn)
    monkeypatch.setattr(db_factory, "_pg_cache", None)
    monkeypatch.setattr(db_factory, "_pg_failed_at", 0.0)
    assert db_factory.build_pg() is None
    db_factory._pg_failed_at = 0.0  # 负缓存窗口已过
    assert db_factory.build_pg() is None
    assert len(calls) == 2  # 失败后窗口过仍会重试（lru_cache 一次失败永久 None=红）
