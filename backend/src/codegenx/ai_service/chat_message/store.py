"""chat_message 表 DAO（asyncpg 原生，raw SQL）。

职责：聊天消息的追加 / 水位增量读 / 会话最近消息查询 / 保留期清理。

存储约定：
- content 列 = 整条消息 dict 的 JSON 序列化（jsonb，含 tool_calls / tool_call_id 等
  结构字段），读侧 json.loads 还原，保证与原 jsonl 文件时代完全同构；
- 超长消息外置（P1-13）：content 超过 _BLOB_THRESHOLD_BYTES 时，原文写本地
  blob 文件（.data/{user}/{app}/session/{session}/message_blobs/{seq}.json），行内
  content 只存 {"payload_ref": <路径>} 桩；读侧 _parse_content 透明还原，
  blob 文件丢失时返回占位 dict（记忆提取/历史展示不中断）；
- role 列冗余存储（user/assistant/tool），供 SQL 过滤与统计；
- seq 会话内单调递增：PG 不允许 SELECT MAX(seq) FOR UPDATE（FOR UPDATE 不能配
  聚合函数），改用会话级 advisory 事务锁 pg_advisory_xact_lock 串行化同会话并发
  写入，锁随事务结束自动释放；uk_session_seq 唯一索引作为兜底，也是记忆提取水位；
- assistant 消息的 tool_calls.arguments 存储时简化为 {"参数名": null}
  （与原 jsonl 行为一致：上下文里保留完整参数，落库只留结构）；
- content_tokens 为 len//4 估算（本服务无本地 tokenizer）。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db.postgres.postgres_client import (
    memory_execute,
    memory_fetch,
    memory_fetchval,
    memory_transaction,
)
from codegenx.ai_service.component import BaseComponent, ComponentType
from shared import log

# 超长消息外置阈值（字节）：普通消息直存，超限写 blob 防 PG 行膨胀
_BLOB_THRESHOLD_BYTES = 32 * 1024

# 分区补齐幂等标记：首次写入前补齐一次未来月份分区（进程级）
_partitions_ensured = False


async def ensure_partitions(months_ahead: int = 3) -> int:
    """补齐 agent.chat_message 未来月份分区，返回本次新建的分区数。

    供应用启动钩子调用；内部实现为幂等的 PL/pgSQL 函数。
    """
    created = await memory_fetchval(
        "SELECT agent.ensure_chat_message_partitions($1)", int(months_ahead)
    )
    return int(created or 0)


async def _ensure_partitions_once() -> None:
    """首次写入前补齐一次分区；失败只告警，不阻断写入。"""
    global _partitions_ensured
    if _partitions_ensured:
        return
    try:
        await ensure_partitions()
        _partitions_ensured = True
    except Exception as exc:  # 建分区失败不应影响正常落库
        log.warning("chat_message 分区补齐失败（不阻断写入）: {}", exc)


def _blob_path(user_id, app_id, session_id: str, seq: int) -> Path:
    # blob 归属会话目录，随会话过期整体删除
    from shared.constants import get_session_dir
    return (
        get_session_dir(user_id, app_id) / str(session_id)
        / "message_blobs" / f"{seq}.json"
    )


class ChatMessageStore(BaseComponent):
    """chat_message 表 DAO（进程级组件，见 get_chat_message_store）。"""

    name = ComponentType.CHAT_MESSAGE_STORE

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    # === 写入 ===

    async def append_message(
        self, user_id: str, app_id: str, session_id: str, message: dict,
        meta: dict | None = None,
    ) -> int:
        """追加一条聊天消息，返回分配的会话内 seq。不改动传入的 message 对象。

        meta：assistant 行的用量元数据（BUG-5 接线）——model/finish_reason/
        prompt_tokens/completion_tokens，落 chat_message 四个独立用量列；
        user/tool 行不传自然为 NULL。
        """
        stored = _storage_view(message)
        content_json = json.dumps(stored, ensure_ascii=False, default=str)
        content_bytes = len(content_json.encode("utf-8"))

        await _ensure_partitions_once()  # 首次写入前补齐未来月份分区

        role = str(message.get("role") or "user")
        chat_meta = meta if meta else {}

        # P1-13 超长外置：先算 seq 再写 blob（路径含 seq）；写失败回退直存。
        # 加锁 + 取 MAX(seq) + INSERT 必须在同一连接同一事务内完成：
        # FOR UPDATE 不能配聚合函数，用会话级 advisory 事务锁串行化同会话追加。
        async with memory_transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", session_id)
            max_seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) FROM agent.chat_message WHERE session_id = $1",
                session_id,
            )
            seq = int(max_seq or 0) + 1
            if content_bytes > _BLOB_THRESHOLD_BYTES:
                ref_path = _blob_path(user_id, app_id, session_id, seq)
                try:
                    ref_path.parent.mkdir(parents=True, exist_ok=True)
                    ref_path.write_text(content_json, encoding="utf-8")
                    # content_bytes/tokens 记原文体量（水位统计与漏斗口径一致）
                    content_json = json.dumps({"payload_ref": str(ref_path)}, ensure_ascii=False)
                except OSError as exc:
                    log.error("超长消息 blob 写盘失败，回退直存: {}", exc)

            await conn.execute(
                "INSERT INTO agent.chat_message "
                "(message_uid, user_id, session_id, app_id, seq, role, "
                "content, content_bytes, content_tokens, "
                "model, prompt_tokens, completion_tokens, finish_reason) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13)",
                str(uuid.uuid4()),
                str(user_id),
                session_id,
                str(app_id),
                seq,
                role,
                content_json,
                content_bytes,
                content_bytes // 4,  # 估算 token（无本地 tokenizer）
                str(chat_meta.get("model") or "") or None,
                int(chat_meta.get("prompt_tokens") or 0) or None,
                int(chat_meta.get("completion_tokens") or 0) or None,
                str(chat_meta.get("finish_reason") or "") or None,
            )
        return seq

    # === 读取 ===

    async def read_since(
        self, session_id: str, after_seq: int = 0, limit: int | None = None
    ) -> list[tuple[int, dict]]:
        """按 seq 升序读取水位之后的消息，返回 [(seq, message_dict), ...]。

        供记忆提取增量消费；解析失败的行跳过（坏行不阻断水位推进）。
        """
        sql = (
            "SELECT seq, message_uid, content FROM agent.chat_message "
            "WHERE session_id = $1 AND seq > $2 ORDER BY seq ASC"
        )
        args: list = [session_id, int(after_seq)]
        if limit is not None:
            sql += " LIMIT $3"
            args.append(int(limit))
        rows = await memory_fetch(sql, *args)
        result: list[tuple[int, dict]] = []
        for seq, message_uid, content in rows:
            message = _parse_content(content)
            if message is not None:
                if message_uid:  # P2-7：溯源字段（写入 agent_memory.source_msg_ids）
                    message["message_uid"] = str(message_uid)
                result.append((int(seq), message))
        return result

    async def get_by_uids(self, message_uids: list[str], limit: int = 200) -> list[dict]:
        """按 message_uid 批量取原始消息（P2-7 ② 记忆溯源反查；uk_message_uid 点查）。"""
        if not message_uids:
            return []
        rows = await memory_fetch(
            "SELECT message_uid, session_id, seq, role, content, created_at "
            "FROM agent.chat_message WHERE message_uid = ANY($1::text[]) "
            "ORDER BY session_id, seq ASC LIMIT $2",
            list(message_uids[:500]), int(limit),
        )
        result: list[dict] = []
        for uid, sid, seq, role, content, created_at in rows:
            message = _parse_content(content) or {}
            result.append({
                "message_uid": str(uid),
                "session_id": str(sid or ""),
                "seq": int(seq or 0),
                "role": str(role or message.get("role") or ""),
                "content": message.get("content"),
                "create_time": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
            })
        return result

    async def get_recent(
        self, user_id: str, session_id: str, limit: int = 50
    ) -> list[dict]:
        """读取会话最近 N 条消息（按 seq 升序返回），供前端会话历史展示。

        每条消息补 create_time / seq / user_id（与原 jsonl 时代响应字段对齐）。
        """
        rows = await memory_fetch(
            "SELECT seq, user_id, content, created_at FROM agent.chat_message "
            "WHERE session_id = $1 AND user_id = $2 "
            "ORDER BY seq DESC LIMIT $3",
            session_id, str(user_id), int(limit),
        )
        result: list[dict] = []
        for seq, row_user_id, content, created_at in reversed(rows):
            message = _parse_content(content)
            if message is None:
                continue
            message["seq"] = int(seq)
            message["user_id"] = str(row_user_id)
            message["create_time"] = created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or "")
            result.append(message)
        return result

    async def list_sessions(
        self, user_id: str, app_id: str, limit: int = 5, days: int | None = None,
    ) -> list[dict]:
        """列出「用户 + app」下最近的会话（直接聚合 chat_message 表，无独立索引）。

        每会话取首条 user 消息正文前 50 字符作 first_message（与原
        session_index.json 口径一致）；按会话最后一条 user 消息时间倒序，
        create_time 为会话首条消息时间。
        days：只返回"最后活跃时间在 N 天内"的会话（None/<=0 = 不限），用于历史列表展示窗口。
        """
        # 窗口函数：rn=1 取每会话首条 user 消息，last_time 用于"最近活跃"排序；
        # user 消息总是先于本轮其余消息落库，故 last user 消息时间即最后一轮起点。
        # content 为 jsonb，->>'content' 直接取正文文本。
        args: list = [str(user_id), str(app_id), int(limit)]
        where_time = ""
        if days and days > 0:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=int(days))
            args.append(cutoff)
            where_time = f" AND last_time >= ${len(args)}"
        sql = (
            "SELECT session_id, first_message, create_time FROM ("
            " SELECT session_id,"
            " COALESCE(SUBSTRING(content->>'content', 1, 50), '') AS first_message,"
            " created_at AS create_time,"
            " ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq ASC) AS rn,"
            " MAX(created_at) OVER (PARTITION BY session_id) AS last_time"
            " FROM agent.chat_message"
            " WHERE user_id = $1 AND app_id = $2 AND role = 'user'"
            ") t WHERE rn = 1"
            f"{where_time}"
            " ORDER BY last_time DESC LIMIT $3"
        )
        rows = await memory_fetch(sql, *args)
        return [
            {
                "session_id": str(sid or ""),
                "first_message": str(first_message or ""),
                "create_time": (
                    created_at.isoformat()
                    if isinstance(created_at, datetime)
                    else str(created_at or "")
                ),
            }
            for sid, first_message, created_at in rows
        ]

    # === 清理 ===

    async def delete_extracted_before(self, cutoff: datetime) -> int:
        """物理删除 cutoff 之前**且已被记忆提炼覆盖**的消息，返回删除行数。

        记忆保护：agent.memory_watermark.last_seq 是会话内单调递增的提炼消费位点，
        只有 seq <= last_seq 的行才允许删除；无水位记录（未启用记忆）的会话按纯时间删除。
        """
        result = await memory_execute(
            "DELETE FROM agent.chat_message m"
            " WHERE m.created_at < $1"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM agent.memory_watermark w"
            "     WHERE w.session_id = m.session_id AND m.seq > w.last_seq"
            "   )",
            cutoff,
        )
        # asyncpg execute 返回命令标签（如 'DELETE 5'），取末段为行数
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0


# === 内部 ===

def _storage_view(message: dict) -> dict:
    """存储视图：assistant 消息的 tool_calls.arguments 简化为 {"参数名": null}。

    与原 SessionManager.append_chat_history_message 行为一致 —— 上下文中的
    完整参数不落库；返回浅拷贝，不改动调用方的 message 对象。
    """
    tool_calls = message.get("tool_calls")
    if message.get("role") != "assistant" or not isinstance(tool_calls, list):
        return message
    stored = dict(message)
    simplified_calls = []
    for call in tool_calls:
        if isinstance(call, dict) and call.get("type") == "function" and isinstance(call.get("function"), dict):
            func = call["function"]
            arguments_str = func.get("arguments", "")
            if arguments_str:
                try:
                    args_dict = json.loads(arguments_str)
                    simplified_args = {k: None for k in args_dict}
                    func_view = {**func, "arguments": json.dumps(simplified_args, ensure_ascii=False)}
                except json.JSONDecodeError:
                    func_view = func
                simplified_calls.append({**call, "function": func_view})
            else:
                simplified_calls.append(call)
        else:
            simplified_calls.append(call)
    stored["tool_calls"] = simplified_calls
    return stored


def _parse_content(raw) -> dict | None:
    """content 列 JSON → message dict；坏行返回 None 并记日志。

    asyncpg 默认把 jsonb 解码为 str，此处按原行为 json.loads 还原。
    P1-13：payload_ref 桩在此透明还原为原文；blob 文件丢失返回占位
    dict（提取/展示拿到的是空正文而非异常）。
    """
    if isinstance(raw, dict):  # 防御：驱动直接返回反序列化结果
        data = raw
    elif not raw:
        return None
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("chat_message.content 解析失败，跳过该行")
            return None
    if not isinstance(data, dict):
        return None
    ref = data.get("payload_ref")
    if not ref:
        return data
    try:
        return json.loads(Path(str(ref)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("超长消息 blob 读取失败（返回占位）: {} | {}", ref, exc)
        return {"payload_ref": str(ref), "content_missing": True, "role": "user", "content": ""}


# === 组件访问 ===

def get_chat_message_store() -> ChatMessageStore:
    """取聊天消息存储组件（经容器查表；须在 initialize_components 注册之后）。"""
    from codegenx.ai_service.system_app import get_app

    return ChatMessageStore.get_instance(get_app())


def initialize_chat_messages(system_app) -> ChatMessageStore:
    """注册聊天消息存储组件（system_app.initialize_components 调用）。"""
    return system_app.register(ChatMessageStore)
