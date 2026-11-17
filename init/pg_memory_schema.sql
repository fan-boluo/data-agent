-- ============================================================
-- 记忆域 + 交互记录域（PostgreSQL，schema=agent）
-- 定位：记忆与聊天记录的唯一真源。pgvector 取代 Qdrant，消灭「双写 + 对账」
--       补偿子系统：记忆行与向量同表同事务，天然一致，无需同步队列。
-- 依赖：PostgreSQL 13+ / pgvector / pg_trgm
-- 使用：psql -f init/pg_memory_schema.sql
--
-- 与 MySQL 的分工：
--   PostgreSQL  -> 交互记录（agent.chat_message）+ 记忆（agent.*）+ 语义层
--   MySQL       -> 平台业务表（app / app_member / 用户等）
--
-- 约定：
--   1. embedding 维度必须与 backend/config.json 的 embedding.dimensions 一致（默认 512）
--   2. chat_message 按月 RANGE 分区：过期数据 DROP PARTITION 秒级完成，
--      代价是唯一约束必须携带分区键 created_at（见下方说明）
--   3. 不用外键：跨表删除由应用层与归档任务保证
-- ============================================================

CREATE SCHEMA IF NOT EXISTS agent;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- updated_at 统一由触发器维护（MySQL 的 ON UPDATE CURRENT_TIMESTAMP 在 PG 无对应语法）
CREATE OR REPLACE FUNCTION agent.touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- 一、交互记录（agent.chat_message）—— 排行榜级大表，按月分区
-- ============================================================
-- 分区表约束：主键/唯一索引必须包含分区键 created_at。
--   - id 由 IDENTITY 序列产生，事实上唯一（PK 仅作分区合法性声明）
--   - (session_id, seq) 的严格唯一由写入侧的会话级 advisory lock 保证
--     （SELECT MAX(seq) FOR UPDATE 在 PG 不允许配聚合函数，见 store.py）
CREATE TABLE IF NOT EXISTS agent.chat_message (
  id                BIGINT      GENERATED ALWAYS AS IDENTITY,
  message_uid       CHAR(36)    NOT NULL,
  user_id           VARCHAR(32) NOT NULL,
  session_id        CHAR(36)    NOT NULL,
  app_id            VARCHAR(64) NOT NULL,
  seq               BIGINT      NOT NULL,
  role              VARCHAR(16) NOT NULL,
  content           JSONB       NULL,
  payload_ref       VARCHAR(512) NULL,
  content_bytes     INTEGER     NOT NULL DEFAULT 0,
  content_tokens    INTEGER     NOT NULL DEFAULT 0,
  model             VARCHAR(64) NULL,
  prompt_tokens     INTEGER     NULL,
  completion_tokens INTEGER     NULL,
  finish_reason     VARCHAR(32) NULL,
  created_at        TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at),
  CONSTRAINT uk_message_uid  UNIQUE (message_uid, created_at),
  CONSTRAINT uk_session_seq  UNIQUE (session_id, seq, created_at)
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE agent.chat_message IS '交互记录（按月分区，记忆提炼的数据源头）';
COMMENT ON COLUMN agent.chat_message.content IS '整条消息 JSON（含 tool_calls 等结构字段），jsonb 便于按字段查询与建索引';
COMMENT ON COLUMN agent.chat_message.seq IS '会话内单调递增，记忆提炼位点载体（advisory lock 保证连续）';
COMMENT ON COLUMN agent.chat_message.content_tokens IS '本条内容 token 数，提炼触发与窗口预算用';

-- 分区内索引（分区表上建索引会自动下发到所有子分区，含后续新建分区）
CREATE INDEX IF NOT EXISTS idx_chat_message_extract_scan
  ON agent.chat_message (user_id, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_chat_message_user_created
  ON agent.chat_message (user_id, created_at DESC);

-- 按月建分区（幂等；建表后调用一次，运行时由应用启动钩子补齐未来月份）
CREATE OR REPLACE FUNCTION agent.ensure_chat_message_partitions(months_ahead INT DEFAULT 3)
RETURNS INT AS $$
DECLARE
  base_month DATE := date_trunc('month', now())::date;
  i          INT;
  p_start    DATE;
  p_end      DATE;
  p_name     TEXT;
  created    INT := 0;
BEGIN
  FOR i IN 0..months_ahead LOOP
    p_start := (base_month + make_interval(months => i))::date;
    p_end   := (p_start + INTERVAL '1 month')::date;
    p_name  := 'chat_message_' || to_char(p_start, 'YYYYMM');
    IF NOT EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'agent' AND c.relname = p_name
    ) THEN
      EXECUTE format(
        'CREATE TABLE agent.%I PARTITION OF agent.chat_message FOR VALUES FROM (%L) TO (%L)',
        p_name, p_start, p_end
      );
      created := created + 1;
    END IF;
  END LOOP;
  RETURN created;
END;
$$ LANGUAGE plpgsql;

-- 过期清理：整月 DROP，避免大表 DELETE 产生的长事务与膨胀
CREATE OR REPLACE FUNCTION agent.drop_chat_message_partitions_before(cutoff DATE)
RETURNS INT AS $$
DECLARE
  r      RECORD;
  dropped INT := 0;
BEGIN
  FOR r IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_inherits i ON i.inhrelid = c.oid
    JOIN pg_class p ON p.oid = i.inhparent
    JOIN pg_namespace pn ON pn.oid = p.relnamespace
    WHERE pn.nspname = 'agent' AND p.relname = 'chat_message'
      AND c.relname ~ '^chat_message_[0-9]{6}$'
      AND to_date(right(c.relname, 6) || '01', 'YYYYMMDD') < date_trunc('month', cutoff)::date
  LOOP
    EXECUTE format('DROP TABLE agent.%I', r.relname);
    dropped := dropped + 1;
  END LOOP;
  RETURN dropped;
END;
$$ LANGUAGE plpgsql;

-- 首次建分区（当期 + 未来 24 个月；后续由应用启动钩子按需补齐）
SELECT agent.ensure_chat_message_partitions(24);


-- ============================================================
-- 二、记忆类型字典
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.memory_type_dict (
  app_id       VARCHAR(64)  NOT NULL DEFAULT '',
  type_code    VARCHAR(32)  NOT NULL,
  memory_layer SMALLINT     NOT NULL,
  display_name VARCHAR(64)  NOT NULL,
  weight       NUMERIC(4,3) NOT NULL DEFAULT 0.500,
  half_life_d  SMALLINT     NULL,
  is_builtin   SMALLINT     NOT NULL DEFAULT 0,
  enabled      SMALLINT     NOT NULL DEFAULT 1,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  PRIMARY KEY (app_id, type_code)
);
CREATE INDEX IF NOT EXISTS idx_memory_type_layer ON agent.memory_type_dict (memory_layer, enabled);
COMMENT ON TABLE agent.memory_type_dict IS '记忆类型字典（可扩展 + 权重配置化）';

INSERT INTO agent.memory_type_dict
  (app_id, type_code, memory_layer, display_name, weight, half_life_d, is_builtin) VALUES
  ('', 'hard_constraint',    1, '硬性规范',   1.000, NULL, 1),
  ('', 'system_rule',        1, '系统规则',   1.000, NULL, 1),
  ('', 'user_preference',    1, '用户偏好',   0.900, NULL, 1),
  ('', 'identity_fact',      1, '身份事实',   0.800, NULL, 1),
  ('', 'external_resource',  1, '外部资源',   0.700, NULL, 1),
  ('', 'user_correction',    2, '用户纠正',   1.000,   60, 1),
  ('', 'task_experience',    2, '任务经验',   0.800,   90, 1),
  ('', 'project_background', 2, '项目背景',   0.600,   30, 1),
  ('', 'interaction_habit',  2, '交互习惯',   0.500,   30, 1)
ON CONFLICT (app_id, type_code) DO NOTHING;


-- ============================================================
-- 三、记忆主表（hot / warm 同表分层 + pgvector 向量列）
-- ============================================================
-- 与 MySQL 版本的差异：删除 vector_synced_at —— 向量与行同表同事务写入，
-- 不再需要「行级同步队列」，也不再有 Qdrant 幽灵点/漏点对账。
-- 分层：memory_layer=1 hot  每轮全量注入，embedding 恒 NULL
--       memory_layer=2 warm 情景记忆，embedding 非 NULL，参与语义召回与衰减
-- 去重：hot 靠 uk_slot 确定性 upsert；warm 不判重（append-only）
-- 注意：active_slot 必须由应用层严格维护，status 1→2/3/4 时同语句置 NULL。
CREATE TABLE IF NOT EXISTS agent.agent_memory (
  id             BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  app_id         VARCHAR(64)  NOT NULL DEFAULT '',
  user_id        VARCHAR(64)  NOT NULL,
  memory_id      CHAR(26)     NOT NULL,

  memory_layer   SMALLINT     NOT NULL,
  memory_type    VARCHAR(32)  NOT NULL,
  subject        VARCHAR(64)  NOT NULL DEFAULT 'self',
  slot_key       VARCHAR(64)  NOT NULL,
  active_slot    SMALLINT     NULL,

  summary        VARCHAR(512) NOT NULL,
  content        JSONB        NOT NULL,
  token_cost     SMALLINT     NOT NULL DEFAULT 0,

  source_type    SMALLINT     NOT NULL DEFAULT 1,
  confidence     NUMERIC(3,2) NOT NULL DEFAULT 1.00,

  status         SMALLINT     NOT NULL DEFAULT 1,
  superseded_by  CHAR(26)     NULL,

  session_id     VARCHAR(64)  NOT NULL DEFAULT '',
  source_msg_ids JSONB        NULL,
  task_id        BIGINT       NULL,
  source_agent   VARCHAR(64)  NULL,

  valid_from     TIMESTAMPTZ  NULL,
  valid_to       TIMESTAMPTZ  NULL,

  hit_count      INTEGER      NOT NULL DEFAULT 0,
  last_hit_at    TIMESTAMPTZ  NULL,

  -- 语义检索向量：warm 行写入时同步生成；hot 行恒 NULL（hot 不进向量检索）
  embedding      vector(512)  NULL,

  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

  CONSTRAINT uk_agent_memory_id UNIQUE (memory_id),
  -- active_slot 为 NULL 的行互不冲突（PG 默认 NULLS DISTINCT），保留 hot 历史版本
  CONSTRAINT uk_memory_slot UNIQUE (app_id, user_id, subject, memory_type, slot_key, active_slot)
);
COMMENT ON TABLE agent.agent_memory IS '记忆主表（hot/warm 同表分层，PostgreSQL 为唯一真源）';
COMMENT ON COLUMN agent.agent_memory.content IS '记忆片段原文 {"text","topic"}';
COMMENT ON COLUMN agent.agent_memory.embedding IS 'pgvector 语义向量；hot 行恒 NULL';
COMMENT ON COLUMN agent.agent_memory.active_slot IS 'active 时=1，非 active 时=NULL；配合 uk_memory_slot 保证同槽位仅一条生效';

CREATE INDEX IF NOT EXISTS idx_agent_memory_hot_load
  ON agent.agent_memory (app_id, user_id, memory_layer, status, memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memory_warm_recall
  ON agent.agent_memory (app_id, user_id, memory_layer, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_memory_decay_scan
  ON agent.agent_memory (memory_layer, status, last_hit_at);
CREATE INDEX IF NOT EXISTS idx_agent_memory_session
  ON agent.agent_memory (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_memory_task ON agent.agent_memory (task_id);
-- 关键词兜底通道（标识符/表名/路径等 embedding 弱项）走 pg_trgm
CREATE INDEX IF NOT EXISTS idx_agent_memory_summary_trgm
  ON agent.agent_memory USING gin (summary gin_trgm_ops);
-- 语义召回：只索引「warm + active」行，索引更小、扫描更快
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding_hnsw
  ON agent.agent_memory USING hnsw (embedding vector_cosine_ops)
  WHERE memory_layer = 2 AND status = 1;

DROP TRIGGER IF EXISTS trg_agent_memory_touch ON agent.agent_memory;
CREATE TRIGGER trg_agent_memory_touch
  BEFORE UPDATE ON agent.agent_memory
  FOR EACH ROW EXECUTE FUNCTION agent.touch_updated_at();


-- ============================================================
-- 四、离线任务队列
-- ============================================================
-- 状态机：pending -> running -> done
--   ├-> 失败：retry_count+1，按退避(1m/5m/30m)回 pending；超限 -> dead
--   └-> 卡死回收：running 且 updated_at 超 10 分钟 -> 巡检复位 pending
-- 幂等键：
--   warm_extract      {session_id}:{from_seq}:{to_seq}
--   consolidate       {app_id}:{user_id}:{yyyy-mm-dd}
--   decay_archive     {app_id}:{user_id}:{yyyy-mm-dd}
--   conflict_detect   {app_id}:{user_id}:{yyyy-mm-dd}
--   compliance_delete {app_id}:{user_id}:{request_id}
-- 注：sync_check（双源对账）随 pgvector 迁移整体下线，不再有此任务类型。
CREATE TABLE IF NOT EXISTS agent.memory_task (
  id              BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_type       VARCHAR(32)  NOT NULL,
  idempotency_key VARCHAR(160) NULL,
  app_id          VARCHAR(64)  NOT NULL DEFAULT '',
  user_id         VARCHAR(64)  NOT NULL DEFAULT '',
  session_id      VARCHAR(64)  NOT NULL DEFAULT '',
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending',
  retry_count     INTEGER      NOT NULL DEFAULT 0,
  produced_count  INTEGER      NOT NULL DEFAULT 0,
  next_run_at     BIGINT       NOT NULL DEFAULT 0,
  payload         JSONB        NULL,
  last_error      VARCHAR(2000) NULL,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT uk_memory_task_idempotency UNIQUE (idempotency_key)
);
COMMENT ON TABLE agent.memory_task IS '记忆离线任务队列（含提炼批次幂等与审计）';

CREATE INDEX IF NOT EXISTS idx_memory_task_status_next ON agent.memory_task (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_memory_task_session_type ON agent.memory_task (session_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_memory_task_user_type ON agent.memory_task (app_id, user_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_memory_task_stuck ON agent.memory_task (status, updated_at);

DROP TRIGGER IF EXISTS trg_memory_task_touch ON agent.memory_task;
CREATE TRIGGER trg_memory_task_touch
  BEFORE UPDATE ON agent.memory_task
  FOR EACH ROW EXECUTE FUNCTION agent.touch_updated_at();


-- ============================================================
-- 五、会话消费水位（记忆提炼按会话增量消费 chat_message）
-- ============================================================
-- 双路触发（满足任一即投 warm_extract）：
--   pending_signals >= 5 / pending_tokens >= 3000 /
--   pending_signals >= 1 且距 last_extract_at > 30min / 会话结束事件
CREATE TABLE IF NOT EXISTS agent.memory_watermark (
  session_id      VARCHAR(64) PRIMARY KEY,
  app_id          VARCHAR(64) NOT NULL DEFAULT '',
  user_id         VARCHAR(64) NOT NULL DEFAULT '',
  last_seq        BIGINT      NOT NULL DEFAULT 0,
  pending_signals SMALLINT    NOT NULL DEFAULT 0,
  pending_tokens  INTEGER     NOT NULL DEFAULT 0,
  last_extract_at TIMESTAMPTZ NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE agent.memory_watermark IS '记忆提取会话消费水位（含双路触发计数）';

CREATE INDEX IF NOT EXISTS idx_memory_watermark_user ON agent.memory_watermark (app_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memory_watermark_pending ON agent.memory_watermark (pending_signals, pending_tokens);

DROP TRIGGER IF EXISTS trg_memory_watermark_touch ON agent.memory_watermark;
CREATE TRIGGER trg_memory_watermark_touch
  BEFORE UPDATE ON agent.memory_watermark
  FOR EACH ROW EXECUTE FUNCTION agent.touch_updated_at();

-- 存量升级提示：若已有 MySQL memory_sync_checkpoint 数据，无需迁移——该表随
-- Qdrant 下线一并废弃（同步正确性由单库单表天然保证）。
