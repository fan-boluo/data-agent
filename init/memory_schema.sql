-- ============================================================
-- 记忆系统表
-- 存储栈：MySQL(唯一真源) + Qdrant(agent_memory_warm, 可重建检索层)
-- 约定：id 类字段 VARCHAR(64)；时间 DATETIME(秒级)；
--       字符集 utf8mb4 / utf8mb4_unicode_ci；不使用外键
-- 建表顺序：memory_type_dict -> agent_memory -> memory_task
--           -> memory_watermark -> memory_sync_checkpoint
-- 使用：与 init/schema.sql 同库（codegenx），部署方手动执行。
-- ============================================================

use dataagent;

-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_type_dict (
  app_id       VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '空串=全局内置类型',
  type_code    VARCHAR(32)  NOT NULL COMMENT '类型编码',
  memory_layer TINYINT      NOT NULL COMMENT '1=hot 2=warm',
  display_name VARCHAR(64)  NOT NULL COMMENT '展示名',
  weight       DECIMAL(4,3) NOT NULL DEFAULT 0.500
               COMMENT 'warm=召回类型权重(占0.1那一路)；hot=裁剪优先级',
  half_life_d  SMALLINT UNSIGNED NULL
               COMMENT '衰减半衰期(天)，NULL=不衰减；hot 层一律 NULL',
  is_builtin   TINYINT      NOT NULL DEFAULT 0 COMMENT '1=内置，不可删改',
  enabled      TINYINT      NOT NULL DEFAULT 1 COMMENT '1=启用 0=停用',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (app_id, type_code),
  INDEX idx_layer (memory_layer, enabled)
) COMMENT '记忆类型字典（可扩展 + 权重配置化）' COLLATE = utf8mb4_unicode_ci;

INSERT INTO memory_type_dict
  (app_id, type_code, memory_layer, display_name, weight, half_life_d, is_builtin) VALUES
  ('', 'hard_constraint',    1, '硬性规范',   1.000, NULL, 1),
  ('', 'system_rule',        1, '系统规则',   1.000, NULL, 1),
  ('', 'user_preference',    1, '用户偏好',   0.900, NULL, 1),
  ('', 'identity_fact',      1, '身份事实',   0.800, NULL, 1),
  ('', 'external_resource',  1, '外部资源',   0.700, NULL, 1),
  ('', 'user_correction',    2, '用户纠正',   1.000,   60, 1),
  ('', 'task_experience',    2, '任务经验',   0.800,   90, 1),
  ('', 'project_background', 2, '项目背景',   0.600,   30, 1),
  ('', 'interaction_habit',  2, '交互习惯',   0.500,   30, 1);


-- ------------------------------------------------------------
-- 2. 记忆主表（hot / warm 同表分层）
-- 定位：本表是唯一真源；Qdrant 为可重建的派生索引，Redis 为可丢弃的缓存
-- 分层：memory_layer=1 hot  结构化语义记忆，每轮全量注入，不进向量库，无时间衰减
--       memory_layer=2 warm 情景记忆，append-only，进 Qdrant 语义召回，参与衰减
-- 去重：hot  靠 uk_slot 确定性 upsert，不调 LLM 判重
--       warm 不判重（重复反馈是晋升 hot 的依据），幂等由 memory_task 保证
-- 同步：vector_synced_at IS NULL 即待同步队列，行级状态、天然幂等、崩溃自动续跑
-- 归档：warm 30天未命中 -> status=4；90天 -> 导出 OSS + Qdrant 物理删除
--       hot 永不因时间失效，只由"被推翻"(status=2)或人工撤销(status=3)触发
-- 注意：active_slot 必须由应用层严格维护——status 从 1 改为 2/3/4 时，
--       同一事务内置 active_slot=NULL，否则该槽位永久无法写入新记忆。
--       建议封装为单一 markInactive() 方法，禁止业务代码直接 UPDATE status。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_memory (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY
                   COMMENT '自增主键，同时作为 Qdrant point id（Qdrant 不接受 ULID）',
  app_id           VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '应用id',
  user_id          VARCHAR(64)  NOT NULL COMMENT '用户id',
  memory_id        CHAR(26)     NOT NULL COMMENT '记忆业务id（ULID，单调可比）',

  memory_layer     TINYINT      NOT NULL COMMENT '1=hot 2=warm',
  memory_type      VARCHAR(32)  NOT NULL COMMENT '取值见 memory_type_dict.type_code',
  subject          VARCHAR(64)  NOT NULL DEFAULT 'self' COMMENT '记忆主体：self/项目A/某人',
  slot_key         VARCHAR(64)  NOT NULL COMMENT 'hot=属性名(如 no_emoji)；warm=memory_id',
  active_slot      TINYINT      NULL
                   COMMENT 'active时=1，非active时=NULL；配合uk_slot实现同槽位仅一条生效且保留历史',

  summary          VARCHAR(512) NOT NULL COMMENT '注入 prompt 的一句话摘要',
  content          JSON         NOT NULL COMMENT '记忆片段原文',
  token_cost       SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '注入占用token，写入时算好',

  source_type      TINYINT      NOT NULL DEFAULT 1 COMMENT '1=用户明说 2=模型推断 3=人工录入',
  confidence       DECIMAL(3,2) NOT NULL DEFAULT 1.00
                   COMMENT '置信度；模型推断须<0.70 且不得进 hot 层',

  status           TINYINT      NOT NULL DEFAULT 1
                   COMMENT '1=active 2=superseded 3=revoked 4=archived',
  superseded_by    CHAR(26)     NULL COMMENT '被哪条记忆取代',

  session_id       VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '来源会话id',
  source_msg_ids   JSON         NULL COMMENT '溯源消息id数组，合规级联删除依赖此字段',
  task_id          BIGINT       NULL COMMENT '产生本条记忆的 memory_task.id',
  source_agent     VARCHAR(64)  NULL COMMENT '产生本条记忆的智能体名（P4 偏差①写门控配套溯源）',

  valid_from       DATETIME     NULL COMMENT '生效起始，NULL=立即生效',
  valid_to         DATETIME     NULL COMMENT '生效截止，NULL=长期有效',

  hit_count        INT UNSIGNED NOT NULL DEFAULT 0
                   COMMENT 'hot=注入次数，warm=召回命中次数；语义不同不可混比',
  last_hit_at      DATETIME     NULL COMMENT '最后命中时间，衰减与软删依据（异步批量刷回）',
  vector_synced_at DATETIME     NULL COMMENT 'NULL=待同步Qdrant；非NULL=已同步',

  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

  UNIQUE KEY uk_memory_id (memory_id),
  UNIQUE KEY uk_slot (app_id, user_id, subject, memory_type, slot_key, active_slot),
  INDEX idx_hot_load     (app_id, user_id, memory_layer, status, memory_type),
  INDEX idx_warm_recall  (app_id, user_id, memory_layer, status, created_at),
  INDEX idx_sync_pending (vector_synced_at, status),
  INDEX idx_decay_scan   (memory_layer, status, last_hit_at),
  INDEX idx_session      (session_id, created_at),
  INDEX idx_task         (task_id)
) COMMENT '记忆主表（hot/warm 同表分层，MySQL 为唯一真源）'
  COLLATE = utf8mb4_unicode_ci ROW_FORMAT = DYNAMIC;

-- ------------------------------------------------------------
-- 存量库升级（source_agent 列，P4 偏差①）。新部署直接跑上面的 CREATE 即可；
-- 已建表的库请手动执行：
-- ALTER TABLE agent_memory
--   ADD COLUMN source_agent VARCHAR(64) NULL
--   COMMENT '产生本条记忆的智能体名（P4 偏差①写门控配套溯源）' AFTER task_id;
-- ------------------------------------------------------------


-- ------------------------------------------------------------
-- 3. 离线任务队列
-- 状态机：pending -> running -> done
--   └-> 失败：retry_count+1，按退避(1m/5m/30m)回 pending；超限 -> dead
--   └-> 卡死回收：running 且 updated_at 超过 10 分钟 -> 巡检任务重置为 pending
--                （worker 存活期间应定期心跳 touch 该行）
-- 语义：at-least-once；重复投递由 uk_idempotency 兜底，不会重复执行
-- 幂等键约定：
--   warm_extract      {session_id}:{from_seq}:{to_seq}
--   consolidate       {app_id}:{user_id}:{yyyy-mm-dd}
--   decay_archive     {app_id}:{user_id}:{yyyy-mm-dd}
--   sync_check        {app_id}:{scope}:{yyyy-mm-dd}
--   conflict_detect   {app_id}:{user_id}:{yyyy-mm-dd}
--   compliance_delete {app_id}:{user_id}:{request_id}
--   vector_rebuild    {app_id}:{model_version}
-- ------------------------------------------------------------

drop table if exists memory_task;


CREATE TABLE IF NOT EXISTS memory_task (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  task_type       VARCHAR(32)  NOT NULL
                  COMMENT '任务类型：warm_extract/consolidate/decay_archive/sync_check/conflict_detect/compliance_delete/vector_rebuild',
  idempotency_key VARCHAR(160) NULL
                  COMMENT '幂等键，同键仅允许一条；NULL=不参与幂等（如全局巡检任务）',
  app_id          VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '应用id',
  user_id         VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '用户id（全局任务为空串）',
  session_id      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '会话id（全局任务为空串）',
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending'
                  COMMENT '状态：pending/running/done/dead',
  retry_count     INT          NOT NULL DEFAULT 0 COMMENT '已重试次数',
  produced_count  INT          NOT NULL DEFAULT 0 COMMENT '产出记忆条数，done 时回填',
  next_run_at     BIGINT       NOT NULL DEFAULT 0 COMMENT '最早可领取时间（unix秒），退避重试用',
  payload         JSON         NULL
                  COMMENT '任务参数与执行结果。warm_extract 入参{from_seq,to_seq}；出参{model,prompt_tokens,completion_tokens,cost_ms,memory_ids[]}，批次审计靠此字段承载',
  last_error      VARCHAR(2000) NULL COMMENT '最近一次失败原因',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_idempotency (idempotency_key),
  INDEX idx_status_next  (status, next_run_at),
  INDEX idx_session_type (session_id, task_type, status),
  INDEX idx_user_type    (app_id, user_id, task_type, status),
  INDEX idx_stuck        (status, updated_at)
) COMMENT '记忆离线任务队列（含提炼批次幂等与审计）' COLLATE = utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 4. 会话消息消费水位
-- 记忆提取按会话增量消费 chat_message 表，崩溃后从水位续提
-- 水位 = 已消费到的最大 seq（会话内单调递增，含）
-- 双路触发（满足任一即投 warm_extract 任务）：
--   pending_signals >= 5
--   pending_tokens  >= 3000
--   pending_signals >= 1 AND 距 last_extract_at > 30min
--   会话结束事件
-- 为什么不只按轮次：50 条"嗯"和 3 条长文档按轮次衡量完全失真，
--   轮次不是好的计量单位，必须叠加 token 量
-- idx_pending 用途：事件驱动投任务若因进程崩溃丢失，
--   定时兜底扫描 pending_signals>0 OR pending_tokens>0 的会话补投
-- ------------------------------------------------------------

drop table  if exists memory_watermark;


CREATE TABLE IF NOT EXISTS memory_watermark (
  session_id      VARCHAR(64) PRIMARY KEY COMMENT '会话id',
  app_id          VARCHAR(64) NOT NULL DEFAULT '' COMMENT '应用id',
  user_id         VARCHAR(64) NOT NULL DEFAULT '' COMMENT '用户id',
  last_seq        BIGINT UNSIGNED NOT NULL DEFAULT 0
                  COMMENT '已消费到的最大 seq（chat_message.seq，含）',
  pending_signals SMALLINT UNSIGNED NOT NULL DEFAULT 0
                  COMMENT '轻量信号累计命中轮数，规则/正则判断，不调 LLM',
  pending_tokens  INT UNSIGNED NOT NULL DEFAULT 0
                  COMMENT '累计未提炼的 content_tokens，提炼完成后归零',
  last_extract_at DATETIME NULL COMMENT '上次成功提炼时间，超时兜底触发依据',
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  INDEX idx_user    (app_id, user_id),
  INDEX idx_pending (pending_signals, pending_tokens)
) COMMENT '记忆提取会话消费水位（含双路触发计数）' COLLATE = utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 5. 双数据源对账进度
-- 【语义修正】旧设计 last_entry_id = "已同步到向量库的最大记忆id"，
--   存在高水位漏洞：异步同步会乱序完成，id=100 失败重试中而 101~200 已成功，
--   水位推到 200 后，100 被永久跳过；且 per-user 粒度会让一条失败卡住整个用户。
-- 新语义：last_entry_id = 上次对账扫描到的位置（长任务断点续跑用）。
--   同步正确性完全由 agent_memory.vector_synced_at IS NULL 保证，与本表无关。
-- 对账双向：
--   正向 missing —— MySQL 有(warm,active) / Qdrant 无 -> 补写
--   反向 ghost   —— Qdrant 有 / MySQL 无或 status!=1 -> 清理
--     （缺反向对账会让已撤销记忆变成幽灵数据，白占 top-k 名额）
-- ------------------------------------------------------------

drop table  if exists memory_sync_checkpoint;

CREATE TABLE IF NOT EXISTS memory_sync_checkpoint (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  app_id            VARCHAR(64) NOT NULL COMMENT '应用id',
  user_id           VARCHAR(64) NOT NULL DEFAULT '' COMMENT '用户id（全局对账为空串）',
  scope             VARCHAR(32) NOT NULL COMMENT '同步范围：warm',
  last_entry_id     VARCHAR(64) NOT NULL DEFAULT ''
                    COMMENT '上次对账扫描到的记忆id（ULID，单调可比），仅作断点续跑，不代表同步水位',
  last_reconcile_at DATETIME    NULL COMMENT '上次对账完成时间',
  scanned_rows      BIGINT      NOT NULL DEFAULT 0 COMMENT '上次对账扫描行数',
  missing_found     INT         NOT NULL DEFAULT 0 COMMENT 'MySQL有/Qdrant无，已补写条数',
  ghost_found       INT         NOT NULL DEFAULT 0 COMMENT 'Qdrant有/MySQL无或已失效，已清理条数',
  updated_at        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_user_app_scope (user_id, app_id, scope)
) COMMENT '记忆双数据源对账进度（不承担同步正确性保证）' COLLATE = utf8mb4_unicode_ci;


