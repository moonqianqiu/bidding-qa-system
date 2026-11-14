-- bidqa 数据库 schema（docs/01 §4.3 单一事实来源；客户端连接时幂等执行，不引 Alembic）。
-- 判重语义：UNIQUE(project_no, batch_id) + ingest 层 ON CONFLICT UPDATE——
-- 同批次重复导入=更新；跨批次=并存，查询侧按 batch 过滤。

CREATE TABLE IF NOT EXISTS bidding_procurement (
  id               BIGSERIAL PRIMARY KEY,
  project_no       TEXT        NOT NULL,       -- 业务键（UNIQUE(project_no, batch_id)）
  project_name     TEXT,                       -- 可空：取不到记 NULL 绝不硬造
  project_category TEXT,
  project_stage    TEXT,
  purchaser        TEXT,
  purchaser_contact TEXT,
  agency           TEXT,
  agency_contact   TEXT,
  winner           TEXT,
  winner_contact   TEXT,
  award_amount     BIGINT,                     -- 中标金额（元）；文本金额经 normalize_amount() 归一；取不到为 NULL 绝不写 0
  content          TEXT,
  location         TEXT,
  region           TEXT,                       -- 省+市两级，按省聚合起步
  subject_matter   TEXT,
  source_url       TEXT,
  publish_date     DATE,
  change_date      DATE,
  plan_date        DATE,
  award_date       DATE,
  batch_id         TEXT        NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_bp_project_batch UNIQUE (project_no, batch_id),
  CONSTRAINT ck_bp_amount_positive CHECK (award_amount IS NULL OR award_amount >= 0)
);
CREATE INDEX IF NOT EXISTS idx_bp_publish_date ON bidding_procurement (publish_date);
CREATE INDEX IF NOT EXISTS idx_bp_region       ON bidding_procurement (region);
CREATE INDEX IF NOT EXISTS idx_bp_subject      ON bidding_procurement (subject_matter);
CREATE INDEX IF NOT EXISTS idx_bp_winner       ON bidding_procurement (winner);

CREATE TABLE IF NOT EXISTS conversations (
  id         UUID PRIMARY KEY,
  title      TEXT        NOT NULL DEFAULT '',
  messages   JSONB       NOT NULL DEFAULT '{"schema_v": 1, "items": []}',  -- 内部带 schema_v（docs/01 §4.3）
  provider   TEXT        NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id UUID,
  question        TEXT        NOT NULL DEFAULT '',
  rating          TEXT        NOT NULL CHECK (rating IN ('up', 'down')),
  sources         JSONB       NOT NULL DEFAULT '[]',
  trace_id        TEXT        NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 用户体系（docs/01 §3.1 用户体系段；三角色 admin/user/guest）──
CREATE TABLE IF NOT EXISTS users (
  id            UUID        PRIMARY KEY,
  username      TEXT        NOT NULL UNIQUE,
  password_hash TEXT        NOT NULL,             -- scrypt$n$r$p$salt$dk（auth_core，零依赖）
  role          TEXT        NOT NULL CHECK (role IN ('admin', 'user')),
  disabled      BOOLEAN     NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT        PRIMARY KEY,              -- opaque token（可吊销/可踢人，比 JWT 诚实）
  user_id    UUID,                                 -- guest 会话为 NULL
  role       TEXT        NOT NULL CHECK (role IN ('admin', 'user', 'guest')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
  key   TEXT PRIMARY KEY,
  value TEXT        NOT NULL DEFAULT ''
);

-- 会话归属：NULL=机器/历史域（机器主 token 与人类 admin 可见，普通用户不可见）
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id UUID;
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id);
