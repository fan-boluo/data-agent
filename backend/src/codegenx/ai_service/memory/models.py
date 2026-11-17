"""
记忆条目数据模型 —— PostgreSQL agent_memory 真源（设计方案 v2.1）的行结构与常量。

一条记忆的落点（v2.1 起真源为 PostgreSQL，json 文件降级为只读迁移源/导出视图）：
  - agent.agent_memory（唯一真源，hot/warm 同表分层，memory_layer 区分）
  - 向量即同表 embedding 列（pgvector，HNSW 余弦索引，仅 warm 行非空）

ULID（memory_id 业务主键）：48bit 毫秒时间戳 + 80bit 随机，字典序即时间序，
仅用于排序/断点比较；行定位用自增 id。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field

# ── 分层（agent_memory.memory_layer）──────────────────────────────────────────
LAYER_HOT = 1   # 结构化语义记忆：每轮全量注入，embedding 恒 NULL，无时间衰减
LAYER_WARM = 2  # 情景记忆：append-only，走 pgvector 语义召回，参与时间衰减

# ── 状态（agent_memory.status）────────────────────────────────────────────────
STATUS_ACTIVE = 1      # 正常生效
STATUS_SUPERSEDED = 2  # 被 uk_slot 同槽位新记录取代（hot 专属语义）
STATUS_REVOKED = 3     # 撤销（提炼出错/幻觉/用户要求/合规删除-软删）
STATUS_ARCHIVED = 4    # 衰减软删（30 天未命中）；归档（90 天）后置 valid_to，不再参与召回

# ── 来源（agent_memory.source_type）──────────────────────────────────────────
SOURCE_USER = 1      # 用户明说（可进 hot 层）
SOURCE_INFERRED = 2  # 模型推断：confidence 必须 <0.70，且不得进 hot 层（设计 §4.3）
SOURCE_MANUAL = 3    # 人工录入/系统迁移


# ── 内置记忆类型 ────────────────────────────────────────────────────────────────
# 与 init/memory_schema.sql 的 memory_type_dict 种子数据一致；运行时调参以字典表为
# 准（P2 接入在线读取），此处常量供校验/打分零成本使用。
# weight：warm=召回类型权重（占 0.1 那一路）；hot=裁剪优先级。
# half_life_d：衰减半衰期（天），None=不衰减（hot 层一律不衰减）。
BUILTIN_MEMORY_TYPES: dict[str, dict] = {
    # hot 层（确定性全量生效，不进向量库）
    "hard_constraint":    {"layer": LAYER_HOT,  "weight": 1.0, "half_life_d": None},
    "system_rule":        {"layer": LAYER_HOT,  "weight": 1.0, "half_life_d": None},
    "user_preference":    {"layer": LAYER_HOT,  "weight": 0.9, "half_life_d": None},
    "identity_fact":      {"layer": LAYER_HOT,  "weight": 0.8, "half_life_d": None},
    "external_resource":  {"layer": LAYER_HOT,  "weight": 0.7, "half_life_d": None},
    # warm 层（情景记忆，append-only）
    "user_correction":    {"layer": LAYER_WARM, "weight": 1.0, "half_life_d": 60},
    "task_experience":    {"layer": LAYER_WARM, "weight": 0.8, "half_life_d": 90},
    "project_background": {"layer": LAYER_WARM, "weight": 0.6, "half_life_d": 30},
    "interaction_habit":  {"layer": LAYER_WARM, "weight": 0.5, "half_life_d": 30},
}

# hot 层裁剪优先级（溢出兜底排序用，设计 §5.4）
HOT_TYPE_PRIORITY: dict[str, float] = {
    t: m["weight"] for t, m in BUILTIN_MEMORY_TYPES.items() if m["layer"] == LAYER_HOT
}

DEFAULT_TYPE_WEIGHT = 0.3      # 字典外类型的兜底权重
DEFAULT_HALF_LIFE_DAYS = 30


def layer_of(memory_type: str) -> int | None:
    """类型 → 分层；未知类型返回 None（由调用方决定拒绝或兜底）。"""
    meta = BUILTIN_MEMORY_TYPES.get(memory_type)
    return meta["layer"] if meta else None


def type_weight(memory_type: str) -> float:
    return BUILTIN_MEMORY_TYPES.get(memory_type, {}).get("weight", DEFAULT_TYPE_WEIGHT)


def half_life_of(memory_type: str) -> int:
    """召回衰减半衰期（天）；未配置/None 一律 30。"""
    v = BUILTIN_MEMORY_TYPES.get(memory_type, {}).get("half_life_d")
    return int(v) if v else DEFAULT_HALF_LIFE_DAYS


def _now_iso() -> str:
    """UTC ISO-8601 带时区，秒级精度（展示与生命周期判断用）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def estimate_text_tokens(text: str) -> int:
    """粗略 token 估算（字符数÷4），与 utils.context_utils.rough_tokens 同口径。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ── ULID ─────────────────────────────────────────────────────────────────────
_B32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32

# 同毫秒单调递增：保证「生成顺序 = ULID 字典序」，对账断点比较的前提
_ulid_lock = threading.Lock()
_ulid_last_value = 0


def new_ulid(ts_ms: int | None = None) -> str:
    """生成 26 位 ULID（同毫秒内单调递增，多线程安全）。"""
    global _ulid_last_value
    with _ulid_lock:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        ts_part = ts_ms & ((1 << 48) - 1)
        value = (ts_part << 80) | secrets.randbits(80)
        if value <= _ulid_last_value and (value >> 80) <= (_ulid_last_value >> 80):
            value = _ulid_last_value + 1
        _ulid_last_value = value
    chars = []
    for _ in range(26):
        chars.append(_B32_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


# ── 行结构 ────────────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    """agent_memory 一行（v2.1）。写入侧构造后交 memory_store 落库；
    读取侧由 DB 行经 from_row 还原。"""
    id: int = 0                            # agent_memory.id（自增主键）
    memory_id: str = field(default_factory=new_ulid)
    app_id: str = ""
    user_id: str = ""

    memory_layer: int = LAYER_WARM
    memory_type: str = "project_background"
    subject: str = "self"
    slot_key: str = ""                     # hot=属性名（预定义/规整化）；warm=memory_id
    active_slot: int | None = 1            # active 时=1，失效置 NULL（uk_slot 依赖）

    summary: str = ""                      # 注入 prompt 的一句话摘要
    content: str = ""                      # 记忆原文（content 列存 {"text","topic"} JSON）
    topic: str = ""                        # 话题标签（存 content JSON 内，展示用）
    token_cost: int = 0                    # 注入占用 token，写入时算好

    source_type: int = SOURCE_USER
    confidence: float = 1.00               # 模型推断必须 <0.70 且不进 hot
    status: int = STATUS_ACTIVE
    superseded_by: str = ""                # 被哪条 memory_id 取代

    session_id: str = ""                   # 来源会话
    source_msg_ids: list[str] = field(default_factory=list)  # 溯源（合规级联删除依赖）
    task_id: int | None = None             # 产生本条的 memory_task.id
    source_agent: str = ""                 # 产生本条的智能体名（偏差①写门控配套溯源）

    hit_count: int = 0
    last_hit_at: str = ""                  # ISO；空=未命中过（衰减用 COALESCE 回退 created_at）
    vector_synced: bool = False            # 兼容字段：warm 行向量与行同事务写入，恒 True

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # ── 派生 ──────────────────────────────────────────────────────────────────

    @property
    def type_weight(self) -> float:
        return type_weight(self.memory_type)

    def content_json(self) -> str:
        """content 列的存储格式（JSON 类型，存对象而非裸字符串）。"""
        return json.dumps({"text": self.content, "topic": self.topic}, ensure_ascii=False)

    def to_row_dict(self) -> dict:
        """全字段字典（归档导出/审计用，字段均 JSON 可序列化）。"""
        return asdict(self)

    def inject_text(self) -> str:
        """注入 prompt 的正文：优先 summary，缺省回退原文。"""
        return self.summary or self.content

    # ── DB 行还原 ─────────────────────────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict) -> "MemoryEntry":
        """DB 行 → MemoryEntry。content/source_msg_ids 兼容 jsonb 两种返回形态：
        asyncpg 默认返回 JSON 文本（str），驱动注册 codec 时返回 dict/list。"""
        content_raw = row.get("content")
        text, topic = "", ""
        if isinstance(content_raw, dict):
            text = str(content_raw.get("text", "") or "")
            topic = str(content_raw.get("topic", "") or "")
        elif isinstance(content_raw, (str, bytes)):
            try:
                parsed = json.loads(content_raw)
                if isinstance(parsed, dict):
                    text = str(parsed.get("text", "") or "")
                    topic = str(parsed.get("topic", "") or "")
                else:
                    text = str(parsed or "")
            except (json.JSONDecodeError, TypeError):
                text = str(content_raw)
        msgs = row.get("source_msg_ids")
        if isinstance(msgs, list):
            pass
        elif isinstance(msgs, (str, bytes)):
            try:
                loaded = json.loads(msgs)
                msgs = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, TypeError):
                msgs = []
        # 向量与行同表同事务写入（pgvector），warm 行天然已同步
        synced_layer = int(row.get("memory_layer") or 0)
        return cls(
            id=int(row.get("id") or 0),
            memory_id=str(row.get("memory_id") or ""),
            app_id=str(row.get("app_id") or ""),
            user_id=str(row.get("user_id") or ""),
            memory_layer=int(row.get("memory_layer") or LAYER_WARM),
            memory_type=str(row.get("memory_type") or ""),
            subject=str(row.get("subject") or "self"),
            slot_key=str(row.get("slot_key") or ""),
            summary=str(row.get("summary") or ""),
            content=text,
            topic=topic,
            token_cost=int(row.get("token_cost") or 0),
            source_type=int(row.get("source_type") or SOURCE_USER),
            confidence=float(row.get("confidence") or 1.0),
            status=int(row.get("status") or STATUS_ACTIVE),
            superseded_by=str(row.get("superseded_by") or ""),
            session_id=str(row.get("session_id") or ""),
            source_msg_ids=[str(m) for m in (msgs or [])],
            task_id=row.get("task_id"),
            source_agent=str(row.get("source_agent") or ""),
            hit_count=int(row.get("hit_count") or 0),
            last_hit_at=_dt_iso(row.get("last_hit_at")),
            vector_synced=synced_layer == 2,
            created_at=_dt_iso(row.get("created_at")) or _now_iso(),
            updated_at=_dt_iso(row.get("updated_at")) or _now_iso(),
        )


def _dt_iso(value) -> str:
    """DB datetime → ISO 字符串；NULL → 空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except AttributeError:
        return str(value)
