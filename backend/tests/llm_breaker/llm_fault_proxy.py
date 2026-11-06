"""LLM 故障注入代理：把 provider 的 apiBase 指到本代理，用 HTTP 开关模拟上游 503。

为什么是 HTTP 反代而不是裸 TCP 转发：
    代理需要以「明文 HTTP 接收 → HTTPS 访问真实上游」的方式工作；裸 TCP 透传时
    客户端校验的证书 CN 是上游域名、连接的却是 127.0.0.1，TLS 校验必然失败。

为什么故障返回 503：
    backend/src/codegenx/ai_service/llm/errors.py 把 5xx 归为 RETRYABLE，
    只有 RETRYABLE 才会 record_failure 累计到熔断器；401/403/404 属 FATAL 不计数。

单独运行：
    python -m tests.llm_fault_proxy --port 8899
    curl -X POST http://127.0.0.1:8899/__ctl/break                     # 全部模型故障
    curl -X POST "http://127.0.0.1:8899/__ctl/break?model=glm-5.3"      # 只让指定模型故障
    curl -X POST http://127.0.0.1:8899/__ctl/heal                      # 全部恢复（清空按模型故障）
    curl -X POST "http://127.0.0.1:8899/__ctl/heal?model=glm-5.3"       # 只恢复指定模型
    curl http://127.0.0.1:8899/__ctl/state

按模型注入故障的用途：
    fallback 链每次调用都从链首开始，链首恢复后后续模型收不到流量、永远停在 OPEN。
    要让第 2/3/4 个模型也经历 HALF_OPEN → CLOSED，必须把当前链首打回故障，把流量
    逼到下一个模型（见 README「全链恢复」）。
"""
from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM_DEFAULT = "https://dashscope.aliyuncs.com"

# 逐跳头不能透传，交给 httpx 重新计算
_HOP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


class FaultProxy:
    """可开关的上游故障代理：全量故障或按模型名故障时返回 503。"""

    def __init__(self, upstream: str = UPSTREAM_DEFAULT):
        self.upstream = upstream.rstrip("/")
        self.fault_all = False          # 全部请求故障
        self.fault_models: set[str] = set()   # 按请求体 model 字段定点故障
        self.ok = 0        # 正常转发次数
        self.failed = 0    # 注入故障次数
        self.app = self._build()

    def should_fail(self, model: str | None) -> bool:
        """是否对本次请求注入故障（全量开关优先，其次看模型名）。"""
        if self.fault_all:
            return True
        return bool(model and model in self.fault_models)

    def _state(self) -> dict:
        return {
            "fault_all": self.fault_all,
            "fault_models": sorted(self.fault_models),
            "ok": self.ok,
            "failed": self.failed,
        }

    def _build(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            client = httpx.AsyncClient(
                base_url=self.upstream,
                timeout=httpx.Timeout(600.0, read=300.0, write=30.0, connect=20.0),
                trust_env=False,
            )
            _app.state.client = client
            try:
                yield
            finally:
                await client.aclose()

        app = FastAPI(title="llm-fault-proxy", lifespan=lifespan)

        # ── 控制面 ──
        @app.post("/__ctl/break")
        async def _break(request: Request):
            model = request.query_params.get("model")
            if model:
                self.fault_models.add(model)
            else:
                self.fault_all = True
            return self._state()

        @app.post("/__ctl/heal")
        async def _heal(request: Request):
            model = request.query_params.get("model")
            if model:
                self.fault_models.discard(model)
            else:
                self.fault_all = False
                self.fault_models.clear()
            return self._state()

        @app.get("/__ctl/state")
        async def _state_route():
            return self._state()

        # ── 数据面：所有其它路径原样转发 ──
        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def _forward(path: str, request: Request):
            body = await request.body()
            model = _request_model(body)
            if self.should_fail(model):
                self.failed += 1
                print(f"[FAULT] 503 model={model or '-'} path=/{path}", flush=True)
                return JSONResponse(
                    status_code=503,
                    content={"error": {"message": "injected upstream outage", "type": "server_error"}},
                )
            self.ok += 1
            headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
            req = request.app.state.client.build_request(
                request.method,
                "/" + path,
                content=body,
                headers=headers,
                params=request.query_params,
            )
            resp = await request.app.state.client.send(req, stream=True)

            async def _stream():
                try:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                finally:
                    await resp.aclose()

            out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS}
            return StreamingResponse(_stream(), status_code=resp.status_code, headers=out_headers)

        return app


def _request_model(body: bytes) -> str | None:
    """从 OpenAI 兼容请求体取 model 字段（取不到按“未指定模型”处理）。"""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) else None


def make_server(port: int = 8899, upstream: str = UPSTREAM_DEFAULT) -> tuple[FaultProxy, uvicorn.Server]:
    """构造代理与其 uvicorn server（供测试脚本同进程 await server.serve()）。"""
    proxy = FaultProxy(upstream)
    server = uvicorn.Server(
        uvicorn.Config(proxy.app, host="127.0.0.1", port=port, log_level="warning")
    )
    return proxy, server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM 故障注入代理")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--upstream", default=UPSTREAM_DEFAULT)
    args = parser.parse_args()
    uvicorn.run(FaultProxy(args.upstream).app, host="127.0.0.1", port=args.port, log_level="info")
