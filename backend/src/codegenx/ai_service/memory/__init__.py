"""
codegenx.ai_service.memory — 记忆系统（跨会话持久记忆，v2）。

与上下文工程的边界：
  - 会话内摘要（压缩边界）已移交 compact/session_summary.py，属于上下文工程；
  - 本包只负责跨会话的持久记忆：hot（核心约束，每轮全量注入）+ warm（历史经验，按需召回）。

数据架构（详见 docs/Agent记忆系统设计方案v2.md）：
  PostgreSQL agent.agent_memory 为唯一真源（hot/warm 同表分层 memory_layer=1/2），
  向量即同表 embedding 列（pgvector HNSW，warm 行非空），json 文件仅为迁移源；
  单库单表消除了「行与向量分叉」这一整类故障，不再有同步队列与双源对账。
  离线任务统一走 memory_task 表队列（at-least-once）：
    warm_extract / consolidate / decay_archive / conflict_detect /
    compliance_delete（P1：卡死回收 + 心跳 + 幂等键）。

模块地图：
  models.py        MemoryEntry + 类型字典/权重/半衰期 + ULID
  memory_store.py  agent_memory DAO（hot upsert / warm append / pgvector 召回 / 生命周期）
  store_init.py    启动自检：扩展与 schema 存在性 + chat_message 月分区补齐
  hot_store.py     hot 层注入（token 预算 + 类型优先级裁剪；P1 起经 Redis 缓存读）
  hot_cache.py     P1 Redis hot 缓存（写时 DEL 失效，TTL 24h，读侧租户复核）
  hit_buffer.py    P1 last_hit_at 异步冲刷（Redis Hash 聚合 5 分钟落库，含直写兜底）
  locks.py         P1 会话级 Redis 锁（SETNX EX 300 + Lua 释放；Redis 挂时降级放行）
  writer.py        在线/离线写入统一入口（准入校验 + 敏感信息过滤 + 向量同事务落库）
  retriever.py     混合召回（pgvector 语义 + pg_trgm 关键词）+ 三因子重排 + token 窗口
                   + debug_recall 模拟检索（P2-7 ④，不登记命中）
  trigger.py       P1 二级漏斗：每轮信号检测 → watermark 计数 → 阈值触发提炼
  hot_compact.py   P2-2 hot 层压缩：consolidate 每日同类 LLM 合并（超阈值才触发，
                   hard_constraint 默认豁免，写时 DEL 由 DAO 内置）
  sync.py          已删除（pgvector 单表后无需双源对账）
  memory_manager.py 门面：SessionContext 每轮调用组装注入（含 hot>warm 冲突规则）
  lifecycle.py     每日整理（warm 合并去重）+ 衰减归档（hot 豁免）
  compliance.py    合规删除级联（PostgreSQL 单库，scope=all/memory_id/subject/source_msg_id）
  metrics.py       P1 指标安全门面（prometheus codegenx_memory_* + 结构化日志，不含正文）
"""

# 包加载即装配本包 hook 监听器（trigger.py 内 @on 注册 turn/session 信号，
# 随包被应用 import 链加载自动生效，无需手工清单）
from codegenx.ai_service.memory import trigger  # noqa: F401,E402
