"""
向量化服务 —— memory 系统专用的 embedding 客户端。

  - 走 DashScope 兼容模式（OpenAI SDK 的 /embeddings 接口）
  - 模型与维度以 backend/config.json 的 embedding 配置为唯一来源
    （维度即 Qdrant 建库维度，避免双配置漂移）
  - 批量分片（BATCH_SIZE=10，DashScope 单次上限内）+ 截断 + 重试
  - 熔断（与 LLM/工具级同构的三态状态机）：服务硬宕时快速失败，
    避免每批都付出全额重试超时；调用方已有降级路径（关键词召回/留待补写）
"""
from __future__ import annotations

import asyncio
import time
from functools import lru_cache

import httpx
from openai import AsyncOpenAI

from shared import log
from codegenx.ai_service.llm.resilience import CircuitBreaker
from codegenx.ai_service.trace.metrics import classify_embedding_error, feed_embedding_call
from codegenx.ai_service.utils.config import config

BATCH_SIZE = 10
RETRY_TIMES = 2


class EmbeddingClient:
    """embedding 客户端（进程内单例，见 get_embedding_client）。"""

    def __init__(self) -> None:
        emb = config.embedding
        self.model_name = emb.model_name
        self.dimensions = emb.dimensions
        self.max_text_length = emb.max_text_length

        provider = config.get_provider_by_model_name(self.model_name)
        api_base = (provider.api_base or "").strip() or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        api_key = (provider.api_key or "").strip()
        if not api_key:
            # 兼容只配了全局 AI_* 环境变量的部署
            from shared.config.config import get_settings
            api_key = get_settings().ai_api_key
            api_base = api_base if provider.api_base else get_settings().ai_base_url

        # 显式禁用代理（避免继承 Windows 系统代理导致 Connection error）
        http_client = httpx.AsyncClient(
            proxy=None, trust_env=False,
            timeout=httpx.Timeout(float(emb.api_timeout_seconds), connect=15.0),
        )
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base.rstrip("/"),
            http_client=http_client,
        )
        # embedding 级熔断器：key=provider:model，指标前缀 embedding_*
        self._breaker = CircuitBreaker(
            f"{config.get_provider_name_by_model_name(self.model_name)}:{self.model_name}",
            failure_threshold=emb.breaker_failure_threshold,
            recovery_timeout=emb.breaker_recovery_timeout_seconds,
            half_open_max_calls=2,
            half_open_success_rate=0.5,
            metric_prefix="embedding",
            metric_label="circuit",
            log_tag="embedding_breaker",
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：熔断准入 → 分片 + 截断 + 重试；单条失败抛出（由调用方决定降级）。"""
        if not await self._breaker.acquire():
            # 熔断打开：快速失败，不再烧重试超时（调用方降级路径现成）
            raise RuntimeError(f"embedding 熔断打开: {self._breaker.key}")
        texts = [self._truncate(t) for t in texts]
        results: list[list[float]] = []
        try:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start:start + BATCH_SIZE]
                results.extend(await self._embed_batch_with_retry(batch))
        except asyncio.CancelledError:
            await self._breaker.release()  # 取消：归还半开探测名额
            raise
        except Exception:
            await self._breaker.record_failure()
            raise
        await self._breaker.record_success()
        return results

    async def embed_query(self, text: str) -> list[float]:
        """单条向量化（检索侧）。"""
        vectors = await self.embed_texts([text])
        return vectors[0]

    # ── 内部 ───────────────────────────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) > self.max_text_length:
            return text[: self.max_text_length]
        return text

    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        last_exc: Exception | None = None
        started = time.monotonic()
        for attempt in range(RETRY_TIMES + 1):
            try:
                resp = await self.client.embeddings.create(
                    model=self.model_name,
                    input=batch,
                    dimensions=self.dimensions,
                )
                # 成功收口：批大小 + 往返延迟（含重试耗时）
                feed_embedding_call(
                    model=self.model_name,
                    batch_size=len(batch),
                    latency_s=time.monotonic() - started,
                )
                return [item.embedding for item in resp.data]
            except Exception as exc:  # noqa: BLE001 — 重试需捕获全部传输/限流错误
                last_exc = exc
                log.warning("embedding 批次失败（第{}次）:{}", attempt + 1, exc)
                if attempt < RETRY_TIMES:
                    await asyncio.sleep(1.5 * (attempt + 1))
        # 重试耗尽按失败收口，error_class 取最后一次真实异常
        feed_embedding_call(
            model=self.model_name,
            batch_size=len(batch),
            latency_s=time.monotonic() - started,
            error_class=classify_embedding_error(last_exc or RuntimeError("unknown")),
        )
        raise RuntimeError(f"embedding 调用重试耗尽: {last_exc}") from last_exc


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient()
