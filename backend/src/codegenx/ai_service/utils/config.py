import json
from pathlib import Path
from typing import ClassVar, List
from functools import lru_cache
from pydantic import AliasChoices, ConfigDict, Field, BaseModel
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config.json"  # ai_service/utils/config.py → 上跳 4 级为 backend/


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys.
    可包含除了configmodel里面明确的其它字段
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AgentMemoryPolicy(Base):
    """智能体记忆读写策略（P4 §10.5，hot/warm 仍按 app+user 全局共享，起步不分片）。"""
    read_types: List[str] | None = None   # 注入时允许的记忆类型过滤；None=全部
    write_enabled: bool = True            # 是否参与记忆提炼（纯执行类智能体可关写，防污染）
    write_types: List[str] | None = None  # 允许提炼成的记忆类型；None=不限


def split_model_chain(model_name: str | None, default: str = "") -> list[str]:
    """逗号分隔模型链 → 模型列表（剔除空项；空配置回落 default）。

    组件（agent / compact / summary / memory）统一用本函数解析 model_name，
    避免「整串逗号名被当成单个模型名」导致预热与调用全部落空。
    """
    raw = str(model_name or "").strip()
    if not raw:
        return [default] if default else []
    chain = [m.strip() for m in raw.split(",") if m.strip()]
    return chain or ([default] if default else [])


class AgentConfig(Base):
    id: str = ""
    role:str = ""
    name: str = ""
    defaults: bool = False
    model_name: str = "qwen3.7-plus"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_max_tokens: int = Field(
        default=8192,
        validation_alias=AliasChoices("contextMaxTokens"),
    )
    context_max_history_turns: int = Field(
        default=5,
        validation_alias=AliasChoices("contextMaxHistoryTurns", "context_max_history_turns"),
    )
    context_summary_max_length: int = Field(
        default=150,
        validation_alias=AliasChoices("contextSummaryMaxLength", "context_summary_max_length"),
    )
    temperature: float = 0.1
    max_tool_iterations: int = 20
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    max_same_tool_calls: ClassVar[int] = 3
    max_continuation_attempts: ClassVar[int] = 3
    max_compact_attempts: ClassVar[int] = 2
    # 传输层重试参数已上移至 llm.*（LLMConfig，P1 韧性层），此处不再保留
    llm_stream_timeout_seconds: ClassVar[float] = 300.0  # 单次 LLM 流式调用总超时（秒）
    max_steps: int = Field(
        default=50,
        validation_alias=AliasChoices("maxSteps", "max_steps"),
    )  # 一次turn的最大llm推理步数

    # ── P4 多智能体（docs/SystemApp架构设计.md §10）：AgentConfig 扩展为 AgentSpec 超集，
    description: str = ""                  # 给规划智能体的派活描述（subagent 工具 description 来源）
    tools: List[str] | None = None         # 工具 allowlist；None=全部
    skills: List[str] | None = None        # skill allowlist；None=全部
    # 智能体模型即上方 model 字段（无场景维度覆盖；多智能体 = agents[] 每项各自配 model）
    memory: AgentMemoryPolicy = Field(default_factory=AgentMemoryPolicy)

    @property
    def context_window_tokens(self) -> int:
        return self.context_max_tokens

    @property
    def resolved_model_name(self) -> str:
        chain = self.resolved_model_name_chain
        return chain[0] if chain else ""

    @property
    def resolved_model_name_chain(self) -> list[str]:
        return split_model_chain(self.model_name)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    # model_name: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""
    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)


class EmbeddingConfig(Base):
    model_name: str = "text-embedding-v4"
    model_path: str = Field(
        default="",
        validation_alias=AliasChoices("modelPath", "model_path"),
    )
    max_text_length: int = Field(
        default=8000,
        validation_alias=AliasChoices("maxTextLength", "max_text_length"),
    )
    api_timeout_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("apiTimeoutSeconds", "api_timeout_seconds", "apiTimeout"),
    )
    dimensions: int = 512
    # 熔断：连续失败 N 批打开，冷却期后半开探测（与 LLM/工具级同构）
    breaker_failure_threshold: int = Field(
        default=3,
        validation_alias=AliasChoices("breakerFailureThreshold", "breaker_failure_threshold"),
    )
    breaker_recovery_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("breakerRecoveryTimeoutSeconds", "breaker_recovery_timeout_seconds"),
    )

    @property
    def embedding_path(self) -> str:
        return self.model_path or self.model_name


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


#  tool -------------------------
class ToolsMysqlConfig(Base):
    """数据分析 MySQL 只读连接（F-4：原先硬编码 readonly/空密码，凭据无处配置）。

    对应数据源账号需在 MySQL 侧预先建好，例如：
      CREATE USER 'readonly'@'localhost' IDENTIFIED BY '***';
      GRANT SELECT ON prj_ec.* TO 'readonly'@'localhost';
    """
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "readonly"
    password: str = ""
    databases: List[str] | None = None  # 白名单，None = 不限制
    pool_size: int = Field(
        default=5,
        validation_alias=AliasChoices("poolSize", "pool_size"),
    )
    connect_timeout: int = Field(
        default=10,
        validation_alias=AliasChoices("connectTimeout", "connect_timeout"),
    )


class ToolCircuitConfig(Base):
    """工具/数据源级熔断（P0-2）。

    只统计基础设施类失败（异常/超时/连接与资源错误）；SQL 语法、参数错误等
    业务失败仅上报指标与日志，不计入熔断，避免用户连续写错 SQL 触发工具不可用。
    """
    enabled: bool = True
    failure_threshold: int = 5              # 连续基础设施失败次数阈值
    recovery_timeout_seconds: float = 30.0  # OPEN 冷却期
    half_open_max_calls: int = 2            # 半开探测名额
    half_open_success_rate: float = 0.5     # 探测成功率达标即闭合
    timeout_seconds: float = 0.0            # 单次工具执行超时；0=不启用（沿用工具内部超时）


class ToolsConfig(Base):
    """Tools configuration."""
    excluded:list = Field(default_factory=list)
    persist_threshold: int = Field(default=30000)  # 持久化阈值
    preview_chars: int = Field(default=2000) # 展示长度
    mysql: ToolsMysqlConfig = Field(default_factory=ToolsMysqlConfig)  # 数据分析工具的只读连接
    circuit: ToolCircuitConfig = Field(default_factory=ToolCircuitConfig)  # 工具级熔断
    # exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    # restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    # mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class SemanticConfig(Base):
    """语义层检索参数（元数据卡片召回 + JOIN 路径 + 指标）。"""
    enabled: bool = True
    recall_top_k: int = 8          # 表卡召回条数（注入 prompt 的上限）
    metric_top_k: int = 5          # 指标卡召回条数
    verified_query_top_k: int = 3  # few-shot 示例条数
    hybrid_vector_weight: float = 0.7  # 混合检索向量权重，其余给关键词
    min_score: float = 0.25        # 召回分数下限（低于则丢弃）
    max_join_hops: int = 3         # JOIN 路径最大跳数
    context_max_chars: int = 6000  # 语义上下文注入 prompt 的字符预算
    embed_timeout_seconds: float = 30.0  # 查询向量化超时，超时降级为关键词召回


# memory ----------------------------------
class MemorySearchConfig(Base):
    """记忆检索配置：混合召回（向量+关键词）+ 三因子重排（语义/时间/类型）"""
    enabled: bool = Field(default=True)
    top_k: int = Field(
        default=10,
        validation_alias=AliasChoices("topK", "top_k", "searchTopK"),
    )  # 兼容旧字段；向量召回条数以 recall_top_n 为准
    recall_top_n: int = Field(
        default=24,
        validation_alias=AliasChoices("recallTopN", "recall_top_n"),
    )  # 向量通道召回条数 N（§5.2：N 明显大于最终注入条数，重排才有意义）
    score_threshold: float = Field(
        default=0.45,
        validation_alias=AliasChoices("scoreThreshold", "score_threshold", "searchScoreThreshold"),
    )  # 向量原始相似度下限
    keyword_top_k: int = Field(
        default=10,
        validation_alias=AliasChoices("keywordTopK", "keyword_top_k"),
    )  # 关键词通道召回条数
    # 三因子重排权重（语义 / 时间衰减 / 类型），和为 1
    rerank_vector_weight: float = Field(default=0.7, validation_alias=AliasChoices("rerankVectorWeight", "rerank_vector_weight"))
    rerank_time_weight: float = Field(default=0.2, validation_alias=AliasChoices("rerankTimeWeight", "rerank_time_weight"))
    rerank_type_weight: float = Field(default=0.1, validation_alias=AliasChoices("rerankTypeWeight", "rerank_type_weight"))
    time_decay_half_life_days: int = Field(
        default=30,
        validation_alias=AliasChoices("timeDecayHalfLifeDays", "time_decay_half_life_days"),
    )  # 时间衰减半衰期：0.5^(age_days/half_life)
    hot_token_budget: int = Field(
        default=2500,
        validation_alias=AliasChoices("hotTokenBudget", "hot_token_budget"),
    )  # hot 层注入 token 预算（§5.4 B1 建议值，需按上下文压测调整）
    warm_token_budget: int = Field(
        default=8192,
        validation_alias=AliasChoices("warmTokenBudget", "warm_token_budget"),
    )  # warm 层注入 token 预算
    hot_load_timeout_ms: int = Field(
        default=200,
        validation_alias=AliasChoices("hotLoadTimeoutMs", "hot_load_timeout_ms"),
    )  # hot 加载超时（§9 建议值），超时降级为无 hot 注入
    warm_load_timeout_ms: int = Field(
        default=1500,
        validation_alias=AliasChoices("warmLoadTimeoutMs", "warm_load_timeout_ms"),
    )  # warm 召回超时，超时降级为无 warm 注入（F-1：500ms 对 embedding 网络往返过紧，实测零注入）


class MemoryTriggerConfig(Base):
    """提炼触发两级漏斗（P1-2，§4.1）：轻量信号判断 → 双路阈值触发 LLM 提炼"""
    enabled: bool = Field(default=True)
    signal_threshold: int = Field(
        default=5,
        validation_alias=AliasChoices("signalThreshold", "signal_threshold"),
    )  # pending_signals 达到即触发
    token_threshold: int = Field(
        default=3000,
        validation_alias=AliasChoices("tokenThreshold", "token_threshold"),
    )  # pending_tokens 达到即触发（长文档场景不被轮次掩盖）
    idle_trigger_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices("idleTriggerMinutes", "idle_trigger_minutes"),
    )  # 有信号且距上次提炼超过该分钟数即触发
    stale_scan_minutes: int = Field(
        default=15,
        validation_alias=AliasChoices("staleScanMinutes", "stale_scan_minutes"),
    )  # 兜底扫描：有积压信号且超过该分钟数未处理则补投


class MemoryFlushConfig(Base):
    """记忆写入/生命周期配置（条件触发离线提取 + 衰减归档）"""
    enabled: bool = Field(default=True)
    model_name: str | None = Field(default=None)  # 离线提取小模型；空则用默认模型
    extract_max_concurrency: int = Field(default=2)  # 离线 LLM 并发上限（信号量隔离，避免挤占在线对话资源）

    consolidate_hour: int = Field(default=3)  # 每日整理/衰减/对账任务触发小时（24h 制）
    decay_days: int = Field(
        default=30,
        validation_alias=AliasChoices("decayDays", "decay_days"),
    )  # 未访问软删除天数
    archive_days: int = Field(
        default=90,
        validation_alias=AliasChoices("archiveDays", "archive_days"),
    )  # 归档天数（导出 + Qdrant 物理删除；软删后再满 archive_days-decay_days 天）
    hot_max_entries: int = Field(
        default=80,
        validation_alias=AliasChoices("hotMaxEntries", "hot_max_entries"),
    )  # hot 层写入期条数硬上限（§5.4：达限拒绝写入转投 consolidate）
    hot_compress_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("hotCompressEnabled", "hot_compress_enabled"),
    )  # P2-2：consolidate 时对超阈值 hot 层做 LLM 同类压缩
    hot_compress_threshold_ratio: float = Field(
        default=0.8,
        validation_alias=AliasChoices("hotCompressThresholdRatio", "hot_compress_threshold_ratio"),
    )  # active 条数达 hot_max_entries 的该比例才触发压缩（压到阈值下即停，不追极简）
    hot_compress_exclude_types: str = Field(
        default="hard_constraint",
        validation_alias=AliasChoices("hotCompressExcludeTypes", "hot_compress_exclude_types"),
    )  # 逗号分隔：不参与压缩的类型（硬约束语义丢失风险大，默认豁免）
    # v1 写入判重阈值：v2 已移除写入时 LLM 判重，字段暂留避免旧配置文件报错
    shortDuplicatedScoreThreshold: float = Field(default=0.90)
    longMatchesScoreThreshold: float = Field(default=0.7)
    longMatchesTopK: int = Field(default=3)


class MemoryConfig(Base):
    search: MemorySearchConfig = Field(default_factory=MemorySearchConfig)
    store: MemoryFlushConfig = Field(default_factory=MemoryFlushConfig)
    trigger: MemoryTriggerConfig = Field(default_factory=MemoryTriggerConfig)


class MonitorStorageConfig(Base):
    enabled: bool = Field(default=True)
    persistSpans: bool = Field(default=True)
    persistSessionMetrics: bool = Field(default=True)
    persistTurnMetrics: bool = Field(default=True)
    useRedisWindows: bool = Field(default=True)


class MonitorAlertLevel(str):
    WARN = "WARN"
    ERROR = "ERROR"


class MonitorLatencyWindowRuleConfig(Base):
    enabled: bool = Field(default=True)
    level: str = Field(default=MonitorAlertLevel.WARN)
    windowSize: int = Field(default=5)
    thresholdSeconds: int = Field(default=10)


class MonitorLatencyThresholdRuleConfig(Base):
    enabled: bool = Field(default=True)
    level: str = Field(default=MonitorAlertLevel.ERROR)
    thresholdSeconds: int = Field(default=60)


class MonitorRatioRuleConfig(Base):
    enabled: bool = Field(default=True)
    level: str = Field(default=MonitorAlertLevel.WARN)
    thresholdRatio: float = Field(default=0.90)


class MonitorConsecutiveFailureRuleConfig(Base):
    enabled: bool = Field(default=True)
    level: str = Field(default=MonitorAlertLevel.ERROR)
    thresholdCount: int = Field(default=3)


class MonitorTurnThresholdRuleConfig(Base):
    enabled: bool = Field(default=True)
    level: str = Field(default=MonitorAlertLevel.WARN)
    thresholdTurns: int = Field(default=50)


class MonitorAlertRulesConfig(Base):
    llmAvgLatencyLast5: MonitorLatencyWindowRuleConfig = Field(
        default_factory=MonitorLatencyWindowRuleConfig,
        validation_alias=AliasChoices("llmAvgLatencyLast5", "llmLast5AvgLatency"),
    )
    llmSingleTimeout: MonitorLatencyThresholdRuleConfig = Field(
        default_factory=MonitorLatencyThresholdRuleConfig,
        validation_alias=AliasChoices("llmSingleTimeout", "llmInvokeLatency"),
    )
    tokenQuotaUsage: MonitorRatioRuleConfig = Field(
        default_factory=MonitorRatioRuleConfig,
        validation_alias=AliasChoices("tokenQuotaUsage", "TokenUsagePercent"),
    )
    toolConsecutiveFailures: MonitorConsecutiveFailureRuleConfig = Field(
        default_factory=MonitorConsecutiveFailureRuleConfig,
        validation_alias=AliasChoices("toolConsecutiveFailures", "ToolContinueFailCount"),
    )
    sessionMaxTurns: MonitorTurnThresholdRuleConfig = Field(
        default_factory=MonitorTurnThresholdRuleConfig,
        validation_alias=AliasChoices("sessionMaxTurns", "TurnMax"),
    )


class MonitorConfig(Base):
    enabled: bool = Field(default=True)
    storage: MonitorStorageConfig = Field(default_factory=MonitorStorageConfig)
    alerts: MonitorAlertRulesConfig = Field(default_factory=MonitorAlertRulesConfig)


class TraceConfig(Base):
    """全链路追踪配置（telemetry schema，docs/链路追踪设计.md §9）。"""
    enabled: bool = True                 # 总开关（关闭则监听器全部空转）
    payload_max_chars: int = Field(
        default=32768,
        validation_alias=AliasChoices("payloadMaxChars", "payload_max_chars"),
    )  # 单字段字符上限（messages/sql/result 等），超长截断并标 truncated
    rows_preview_limit: int = Field(
        default=20,
        validation_alias=AliasChoices("rowsPreviewLimit", "rows_preview_limit"),
    )  # execute_postgresql rows_preview 最大行数
    flush_batch_size: int = Field(
        default=200,
        validation_alias=AliasChoices("flushBatchSize", "flush_batch_size"),
    )  # 落库攒批大小
    retention_days: int = Field(
        default=90,
        validation_alias=AliasChoices("retentionDays", "retention_days"),
    )  # 数据保留天数（T2 清理任务消费）
    capture_content: bool = Field(
        default=True,
        validation_alias=AliasChoices("captureContent", "capture_content"),
    )  # 是否记录输入输出内容（false 只留元数据）


class CompactConfig(Base):
    max_tool_result_tokens: int = Field(
        default=3000,
        validation_alias=AliasChoices("maxToolResultTokens", "max_tool_result_tokens"),
    )
    model_name: str | None = Field(default=None)

    protect_last_n_results: int = Field(default=5)
    # Leader 会话历史注入 token 预算：超出从最旧截断，更早轮次由会话摘要兜底承载
    leader_history_budget_tokens: int = Field(
        default=2500,
        validation_alias=AliasChoices("leaderHistoryBudgetTokens", "leader_history_budget_tokens"),
    )
    # 单次摘要提取最多喂入的消息条数（水位增量超限时取最近 N 条，水位只推进到实际喂入范围）
    summary_extract_max_messages: int = Field(
        default=200,
        validation_alias=AliasChoices("summaryExtractMaxMessages", "summary_extract_max_messages"),
    )
    # 统一预算分配器总预算比例：总预算 = context_max_tokens × 该比例（预留输出 max_tokens 与安全余量）
    context_budget_ratio: float = Field(
        default=0.85,
        validation_alias=AliasChoices("contextBudgetRatio", "context_budget_ratio"),
    )
    # tools schema 占总预算比例（函数调用契约，超限只截描述不删工具）
    tool_budget_ratio: float = Field(
        default=0.15,
        validation_alias=AliasChoices("toolBudgetRatio", "tool_budget_ratio"),
    )
    # 单个工具描述的最大字符数（第一级瘦身）
    tool_description_max_chars: int = Field(
        default=240,
        validation_alias=AliasChoices("toolDescriptionMaxChars", "tool_description_max_chars"),
    )

class ModelsConfig(Base):
    name: str = Field(default="qwen3.8-flash")
    provider: str = Field(default="dashscope")

class LLMConfig(Base):
    """LLM 调用韧性层参数（P1：重试/熔断/降级，见 docs/LLM调用设计方案.md §5-§6）"""
    max_attempts: int = Field(
        default=2,
        validation_alias=AliasChoices("maxAttempts", "max_attempts"),
    )  # 单模型瞬态错误重试上限（不含首次）；重试耗尽后切 fallback 模型
    backoff_base_seconds: float = Field(
        default=0.5,
        validation_alias=AliasChoices("backoffBaseSeconds", "backoff_base_seconds"),
    )  # 指数退避基数
    backoff_max_seconds: float = Field(
        default=8.0,
        validation_alias=AliasChoices("backoffMaxSeconds", "backoff_max_seconds"),
    )  # 退避上限（Retry-After 超过该值时按该值封顶）
    breaker_failure_threshold: int = Field(
        default=5,
        validation_alias=AliasChoices("breakerFailureThreshold", "breaker_failure_threshold"),
    )  # 连续失败阈值（CLOSED→OPEN）
    breaker_recovery_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("breakerRecoveryTimeoutSeconds", "breaker_recovery_timeout_seconds"),
    )  # OPEN 冷却期，到期转 HALF_OPEN 放行探测
    breaker_half_open_max_calls: int = Field(
        default=2,
        validation_alias=AliasChoices("breakerHalfOpenMaxCalls", "breaker_half_open_max_calls"),
    )  # 半开态并发探测数上限
    breaker_half_open_success_rate: float = Field(
        default=0.5,
        validation_alias=AliasChoices("breakerHalfOpenSuccessRate", "breaker_half_open_success_rate"),
    )  # 探测成功率达标则闭合

class SessionConfig(Base):
    """SessionPool 组件配置（会话池参数由组件自读 config.session，与智能体配置解耦）。"""
    max_sessions: int = Field(default=100)  # session pool 的 session 最多个数
    idle_timeout_seconds: int = Field(default=1800)  # session 空闲 30 分钟即清除
    cleanup_interval_seconds: int = Field(default=300)  # 每隔 5 分钟进行一次闲置 session 清理
    swap_idle_seconds: int = Field(default=300)  # P3：闲置超过该秒数且无在途任务的会话卸载 chat_messages（须远小于 idle_timeout_seconds）
    grace_stop_seconds: int = Field(default=2.0)  # session中任务优雅停止时间

class MultiAgentsConfig(Base):
    """多Agent参数。"""
    max_parallel: int = Field(default=3)  # 批量派发并行执行上限（同批子任务最大并发数）
    enabled: bool = Field(default=True)  # 是否启用多agent自动模式，默认是，单agent模式后续再定
    replan_max: int = Field(default=2)  # Critic 驳回后局部重规划上限（第二期启用）
    plan_confirm: bool = Field(default=False)  # 计划 human-in-the-loop 确认（第二期 interrupt）
    checkpointer_enabled: bool = Field(default=True)  # MySQL checkpointer（复杂任务中断恢复）
    artifact_ttl_hours: int = Field(default=24)  # 中间产物（结果集 DataFrame 等）Redis 缓存 TTL
    df_spill_threshold_mb: int = Field(default=50)  # 产物序列化超过该阈值落盘 parquet，否则进 Redis


class Config(BaseSettings):
    agents: List[AgentConfig] = Field(default_factory=list)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    models:List[ModelsConfig] = Field(default_factory=lambda: [ModelsConfig()])
    # 模型选择由各使用模型的组件从自身配置解析（agents[].model /
    # compact.model_name / memory.store.model_name），不再有全局场景路由
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    semantic: SemanticConfig = Field(default_factory=SemanticConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    monitor:MonitorConfig = Field(default_factory=MonitorConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    multi_agents: MultiAgentsConfig = Field(default_factory=MultiAgentsConfig)

    def get_default_agent(self) -> AgentConfig:
        if not self.agents:
            return AgentConfig()
        for agent in self.agents:
            if agent.defaults:
                return agent
        return self.agents[0]

    def get_agent(self, identifier: str | None = None) -> AgentConfig:
        if not self.agents:
            return AgentConfig()
        if identifier:
            normalized = str(identifier).strip().lower()
            for agent in self.agents:
                if normalized in {str(agent.id).strip().lower(), str(agent.token).strip().lower(), str(agent.name).strip().lower()}:
                    return agent
        return self.get_default_agent()

    def get_provider(self, provider_name: str | None = None) -> ProviderConfig:
        normalized = str(provider_name or "").strip().lower()
        if normalized and hasattr(self.providers, normalized):
            return getattr(self.providers, normalized)
        return self.providers.custom

    def get_provider_by_model_name(self, model_name: str | None = None) -> ProviderConfig:
        normalized = str(model_name or "").strip().lower()
        if  normalized:
            for model in self.models:
                if normalized == str(model.name or "").strip().lower():
                    # get_provider 内部对无效 provider 名回落 custom，避免配置手误直接 AttributeError
                    return self.get_provider(model.provider)
        return self.providers.custom

    def get_provider_name_by_model_name(self, model_name: str | None = None) -> str:
        """模型名 → provider 名（client_registry 用作共享客户端的键）；未命中回落 custom。"""
        normalized = str(model_name or "").strip().lower()
        if normalized:
            for model in self.models:
                if normalized == str(model.name or "").strip().lower():
                    provider = str(model.provider or "").strip().lower()
                    if provider and hasattr(self.providers, provider):
                        return provider
        return "custom"

    def get_default_model(self) -> str:
        if not self.models:
            return  ModelsConfig().name
        return self.models[0].name  # 默认选第一个

@lru_cache(maxsize=1)
def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.
    Args:
        config_path: Optional path to config file. Uses default if not provided.
    Returns:
        Loaded configuration object.
    """
    path = config_path or _CONFIG_PATH

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


config = load_config()

if __name__ == '__main__':
    config = load_config()
    print(config)