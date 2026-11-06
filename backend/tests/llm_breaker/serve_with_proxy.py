"""带故障注入的服务启动器（熔断/压测专用，不改 config.json）。

只负责一件事：起后端服务前，把 dashscope 的 apiBase 指向本地故障代理。
代理由独立脚本 llm_fault_proxy.py 负责，这里不再内嵌启动。

    终端1：backend\\\\.venv\\\\Scripts\\\\python.exe backend\\\\tests\\\\llm_fault_proxy.py
    终端2：backend\\\\.venv\\\\Scripts\\\\python.exe backend\\\\tests\\\\serve_with_proxy.py
    终端3：backend\\\\.venv\\\\Scripts\\\\python.exe backend\\\\tests\\\\test_circuit_breaker.py

故障注入：
    curl -X POST http://127.0.0.1:8899/__ctl/break   # 上游变 503
    curl -X POST http://127.0.0.1:8899/__ctl/heal    # 恢复正常

原理：
    代理是 HTTP 明文接收 → HTTPS 转发到 dashscope；apiBase 必须保留
    /compatible-mode/v1 前缀，否则 SDK 拼出的 /chat/completions 会被
    转发到上游根路径而 404。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND / "src"))

import uvicorn


def _main() -> None:
    parser = argparse.ArgumentParser(description="带故障注入的服务启动器（需先起 llm_fault_proxy.py）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8457)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8899/compatible-mode/v1",
                        help="故障代理地址，必须包含 /compatible-mode/v1 路径前缀")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    from codegenx.ai_service.llm.client_registry import _clients
    from codegenx.ai_service.utils.config import config as agent_config
    from codegenx.main import create_app
    from shared.config.config import get_settings

    settings = get_settings()
    print(f"[ENV] qdrant_url={settings.qdrant_url} port={settings.qdrant_port}")

    # 预校验 Qdrant 连通性（启动期 vector_store 立即用到，Qdrant 未就绪会持续报错）
    try:
        import socket as _sk
        s = _sk.socket(); s.settimeout(2)
        s.connect((settings.qdrant_url, int(settings.qdrant_port)))
        s.close()
        print(f"[OK] Qdrant {settings.qdrant_url}:{settings.qdrant_port} 连通")
    except Exception as exc:
        print(f"[WARN] Qdrant 未就绪：{exc}，先等 3s 再试一次...")
        import time as _t
        _t.sleep(3)
        try:
            s = _sk.socket(); s.settimeout(2)
            s.connect((settings.qdrant_url, int(settings.qdrant_port)))
            s.close()
            print(f"[OK] Qdrant 就绪")
        except Exception as exc2:
            print(f"[ERROR] Qdrant 仍不可用：{exc2}，服务可能持续报 warm 检索失败")

    # 在任何 LLM client 创建前改写 apiBase，确保首个请求就走代理；不改 config.json
    agent_config.providers.dashscope.api_base = args.proxy_url
    _clients.clear()

    # 在 create_app()（触发 SystemApp init）之前预热 Qdrant，打印实际 host/port
    from db.qdrant.client import get_qdrant_client_manager
    mgr = get_qdrant_client_manager()
    try:
        info = getattr(mgr.client, "_client", None) or getattr(mgr.client, "_url", None)
        print(f"[QDRANT] manager → {info}")
    except Exception as exc:
        print(f"[QDRANT] manager → （探测失败 {exc}）")

    print(f"[OK] 服务监听 {args.host}:{args.port}")
    print(f"[OK] dashscope.api_base → {args.proxy_url}")
    print(f"[OK] 故障注入：POST http://127.0.0.1:8899/__ctl/break")

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    _main()
