"""CodeGenX 单体应用入口

启动：uv run python -m codegenx（或控制台命令 codegenx），默认 0.0.0.0:8456，基础路径 /api。
"""

from __future__ import annotations

import traceback
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


from codegenx.ai_service.api.memory_router import memory_admin_router
from codegenx.ai_service.api.eval_router import eval_router
from codegenx.app_service.router import router as app_router
from codegenx.gateway.api.blacklist import router as blacklist_router
from codegenx.gateway.api.chat import router as chat_router
from codegenx.gateway.api.health import router as health_router
from codegenx.gateway.middleware.ip_blacklist import IpBlacklistMiddleware
from codegenx.user_service.router import router as user_router
from shared.config.config import get_settings
from shared import log
from shared.exceptions.business_exception import BusinessException
from shared.exceptions.error_code import ErrorCode
from shared.utils.result_utils import error

settings = get_settings()


def _format_exception(exc: Exception) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class TraceIdMiddleware(BaseHTTPMiddleware):
    # 链路追踪中间件，为每个请求生成唯一的traceId
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        request.state.trace_id = request_id
        request.state.login_user_id = None
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动 SystemApp 全局容器（hook 冻结/基础设施/注册表/runtime/后台任务，docs/SystemApp架构设计.md §3.3）
    from codegenx.ai_service.system_app import  init_app, start_app, stop_app
    # 两段式启动：init_app 注册组件+广播同步钩子；start_app 在事件循环内完成异步启动
    app = init_app()
    await start_app(app)
    log.info("codegenx monolith startup completed")
    try:
        yield
    finally:
        # 关闭顺序由 SystemApp stop_app 统一保证：组件逆序关闭后，再统一释放基础设施（redis/mysql/postgres）
        await stop_app()
        log.info("codegenx monolith shutdown completed")


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    # fastapi的中间件是倒序执行的，先添加的后执行，这个黑名单拦截是最后执行的
    app.add_middleware(IpBlacklistMiddleware)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(BusinessException)
    async def business_exception_handler(request: Request, exc: BusinessException):
        if exc.code == ErrorCode.NOT_LOGIN_ERROR.get_code():
            log.info(
                "BusinessException path={} userId={} code={} message={} traceId={}",
                request.url.path,
                getattr(request.state, "login_user_id", None),
                exc.code,
                exc.message,
                getattr(request.state, "trace_id", None),
            )
        else:
            log.error(
                "BusinessException path={} userId={} code={} message={} traceId={}\n{}",
                request.url.path,
                getattr(request.state, "login_user_id", None),
                exc.code,
                exc.message,
                getattr(request.state, "trace_id", None),
                _format_exception(exc),
            )
        return JSONResponse(
            status_code=200,
            content=error(exc.code, exc.message).model_dump(by_alias=True),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        log.error(
            "ValidationException path={} userId={} code={} message={} traceId={} details={}",
            request.url.path,
            getattr(request.state, "login_user_id", None),
            ErrorCode.PARAMS_ERROR,
            "请求参数错误",
            getattr(request.state, "trace_id", None),
            exc.errors(),
        )
        return JSONResponse(
            status_code=200,
            content=error(ErrorCode.PARAMS_ERROR.get_code(), "请求参数错误").model_dump(by_alias=True),
        )

    @app.exception_handler(Exception)
    async def runtime_exception_handler(request: Request, exc: Exception):
        log.error(
            "RuntimeException path={} userId={} code={} message={} traceId={}\n{}",
            request.url.path,
            getattr(request.state, "login_user_id", None),
            ErrorCode.SYSTEM_ERROR,
            str(exc),
            getattr(request.state, "trace_id", None),
            _format_exception(exc),
        )
        return JSONResponse(
            status_code=200,
            content=error(ErrorCode.SYSTEM_ERROR.get_code(), "系统错误").model_dump(by_alias=True),
        )

    from codegenx.ai_service.api.router import (
        chat_router as ai_chat_router,
        metrics_router,
        monitor_router,
    )
    app.include_router(health_router, prefix=settings.app_base_path)
    app.include_router(user_router, prefix=settings.app_base_path)
    app.include_router(app_router, prefix=settings.app_base_path)
    app.include_router(ai_chat_router, prefix=settings.app_base_path)
    app.include_router(chat_router, prefix=settings.app_base_path)
    app.include_router(blacklist_router, prefix=settings.app_base_path)
    app.include_router(monitor_router, prefix=settings.app_base_path)
    app.include_router(memory_admin_router, prefix=settings.app_base_path)
    app.include_router(eval_router, prefix=settings.app_base_path)
    # Prometheus 指标无 /api 前缀
    app.include_router(metrics_router)

    return app


app = create_app()


def run() -> None:
    uvicorn.run(app, host=settings.app_host, port=int(settings.app_port))
