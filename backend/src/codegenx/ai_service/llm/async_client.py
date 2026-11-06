import asyncio
import contextlib
import json
from functools import lru_cache

from openai.types.chat import ChatCompletionMessage

from shared import log
from typing import AsyncGenerator, Dict, Any, List, Optional
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.llm.client_registry import get_openai_client


def _safe_build_tool_calls(tool_calls_buffer: dict) -> list[dict[str, Any]]:
    """安全构建 tool_calls 列表，每个 tool_call 的 arguments 做容错 JSON 解析。"""
    tool_calls_list = []
    for _, v in sorted(tool_calls_buffer.items()):
        args = {}
        raw_args = str(v.get("arguments", "") or "").strip()
        if raw_args:
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning(
                    "Failed to parse tool call arguments for {}, using raw string: {}",
                    v.get("name", "unknown"), exc,
                )
                args = {"_raw_arguments": raw_args}
        tool_calls_list.append({
            "id": v["id"],
            "name": v["name"],
            "arguments": args,
        })
    return tool_calls_list


async def _stream_chunk_generator(agen, timeout_seconds: float):
    """Stream-level timeout via queue.

    A background task pulls from the upstream (httpx) async generator and pushes
    chunks into a queue.  The foreground reads from the queue with
    ``asyncio.wait_for`` — so the wait_for timeout only cancels ``queue.get()``
    (which is safe to cancel), never an httpx coroutine.
    """
    from asyncio.queues import Queue

    queue: Queue = Queue()
    pump_done = False

    async def _pump():
        nonlocal pump_done
        try:
            async for item in agen:
                await queue.put(item)
        finally:
            pump_done = True
            await queue.put(None)  # sentinel

    pump_task = asyncio.ensure_future(_pump())

    try:
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
            if item is None:
                # Pump finished cleanly, check for exception
                if pump_task.done() and pump_task.exception():
                    raise pump_task.exception()
                return
            yield item
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if not pump_task.done():
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
        raise
    except Exception:
        if not pump_task.done():
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
        raise

class AsyncLLMClient:
    """Async LLM Client wrapping OpenAI's Async interface."""
    def __init__(self, model_name: Optional[str] = None):
        # 默认选用第一个
        self.model_name = model_name or config.get_default_model()
        provider_config = config.get_provider_by_model_name(self.model_name)
        provider_name = config.get_provider_name_by_model_name(self.model_name)
        self.api_key = provider_config.api_key
        self.model_base_url = provider_config.api_base

        # P0-1 修复：复用 provider 级共享客户端（连接池复用），不再每次实例化新建 httpx 连接池
        self.client = get_openai_client(provider_name, provider_config)
        log.debug(f"Init AsyncLLMClient with model={self.model_name}")

    async def invoke(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> tuple[dict[Any, Any], Any]:
        """非流式调用（纯传输）：返回 (message_dict, usage)。

        usage 为 provider 用量对象或 None；token 计数不在客户端采集——
        韧性层（resilient_invoke）是全部 LLM 调用的统一出口，在那里统一喂入。
        """
        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            completion = await self.client.chat.completions.create(**kwargs)
            usage = getattr(completion, "usage", None)
            if not completion.choices:
                # 空 choices：与"模型返回空串"无法区分会掩盖内容过滤/上游异常，至少留痕
                log.warning("[llm] model={} 返回空 choices", self.model_name)
                return {}, usage

            message = completion.choices[0].message
            return message.to_dict() or {}, usage
        except Exception as e:
            log.error(f"LLM Invoke Error: {e}")
            raise e

    async def invoke_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        timeout: Optional[float] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Yields parsed chunks of execution:
        - {"type": "content", "data": "chunk_string"}
        - {"type": "tool_calls", "data": [{"id": "...", "name": "...", "arguments": "{...}"}]}

        Args:
            timeout: 整个流式调用的超时秒数。超时后抛出 asyncio.TimeoutError。
        """
        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            # 流式必须显式要求末尾 chunk 回传 usage，否则 provider 返回 None，
            # 导致 llm span 的 token 恒为 0（成本/延迟统计失去数据源）
            "stream_options": {"include_usage": True},
        }
        if tools:
            # Drop empty tools list or None to avoid OpenAI validation errors
            kwargs["tools"] = tools

        try:
            stream = await self.client.chat.completions.create(**kwargs)

            tool_calls_buffer = {}
            finish_reason = None
            captured_usage: Any = None  # P1：机会性采集 usage（部分 provider 在末尾 chunk 携带）

            def _response_info() -> Dict[str, Any]:
                # BUG-5 接线：response_info 扩为 finish_reason + 真实 usage + 实际服务模型，
                # 供调用方落 chat_message 用量四列（model/prompt_tokens/completion_tokens/finish_reason）
                usage = None
                if captured_usage is not None:
                    usage = {
                        "prompt_tokens": int(getattr(captured_usage, "prompt_tokens", 0) or 0),
                        "completion_tokens": int(getattr(captured_usage, "completion_tokens", 0) or 0),
                    }
                return {"finish_reason": finish_reason, "usage": usage, "model": self.model_name}

            try:
                async def _consume_stream():
                    nonlocal finish_reason, captured_usage
                    async for chunk in stream:
                        if getattr(chunk, "usage", None) is not None:
                            captured_usage = chunk.usage
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        delta = choice.delta
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason

                        if delta.content:
                            yield {"type": "content", "data": delta.content}

                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                idx = tc.index
                                if idx not in tool_calls_buffer:
                                    tool_calls_buffer[idx] = {
                                        "id": tc.id or "",
                                        "name": tc.function.name if tc.function else "",
                                        "arguments": tc.function.arguments if tc.function and tc.function.arguments else ""
                                    }
                                else:
                                    if tc.function and tc.function.arguments:
                                        tool_calls_buffer[idx]["arguments"] += tc.function.arguments

                chunk_source = _consume_stream()
                if timeout is not None:
                    chunk_source = _stream_chunk_generator(chunk_source, timeout)

                async for chunk_item in chunk_source:
                    yield chunk_item

            except (asyncio.TimeoutError, asyncio.CancelledError):
                # 流中断时仍然返回已累积的 tool_calls 和 finish_reason
                log.warning("LLM stream interrupted (timeout/cancel), returning partial result")
                if tool_calls_buffer:
                    tool_calls_list = _safe_build_tool_calls(tool_calls_buffer)
                    if tool_calls_list:
                        yield {"type": "tool_calls", "data": tool_calls_list}
                if finish_reason:
                    yield {"type": "response_info", "data": _response_info()}
                raise

            # 流正常结束：yield 累积 tool_calls 与 response_info（usage 由韧性层采集）
            if tool_calls_buffer:
                tool_calls_list = _safe_build_tool_calls(tool_calls_buffer)
                if tool_calls_list:
                    yield {"type": "tool_calls", "data": tool_calls_list}

            yield {"type": "response_info", "data": _response_info()}

        except Exception as e:
            log.error(f"LLM Stream Error: {e}")
            raise e


@lru_cache(maxsize=8)
def get_llm(model_name: Optional[str] = None) -> AsyncLLMClient:
    """进程级 AsyncLLMClient 复用入口：相同 model_name 返回同一实例。

    AsyncLLMClient 本身无状态（模型名 + 共享底层客户端），可安全并发使用；
    各调用方应通过本入口获取，禁止再随手 `AsyncLLMClient()` 即用即弃。
    """
    log.debug(f"get_llm model_name: {model_name}")
    return AsyncLLMClient(model_name)