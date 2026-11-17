from __future__ import annotations

import asyncio
import json
from math import ceil
import os
from pathlib import Path
import shutil
import tempfile
from typing import AsyncGenerator

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared import log
from shared.constants import PYTHON_ENV, get_code_dir
from shared.exceptions.error_code import ErrorCode
from shared.exceptions.throw_utils import ThrowUtils
from codegenx.app_service.orm.app import App
from codegenx.app_service.orm.app_member import AppMember
from codegenx.app_service.schema.app import (
    AppAddRequest,
    AppAdminUpdateRequest,
    AppMemberAddRequest,
    AppMemberRemoveRequest,
    AppMemberVO,
    AppQueryRequest,
    AppUpdateRequest,
    AppVO,
)
from shared.schema.common import PageData
from shared.utils.id_utils import generate_app_id
from codegenx.app_service.services.access import (
    get_active_member,
    require_manager,
    require_participant,
)
from codegenx.gateway.middleware.jwt_auth import JWTUser
from codegenx.user_service.models.user import User
from codegenx.app_service.services.db_manager import create_project_database, list_database_tables
from codegenx.resource_service.store import init_app_resources


class AppService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_app(self, app_add_request: AppAddRequest, login_user: JWTUser, trace_id: str | None = None) -> int:
        app_name = app_add_request.app_name.strip()
        ThrowUtils.throw_if(not app_name, ErrorCode.PARAMS_ERROR, "项目名称不能为空")
        ThrowUtils.throw_if(len(app_name) > 20, ErrorCode.PARAMS_ERROR, "项目名称不能超过20字")
        db_name = app_add_request.db_name.strip() if app_add_request.db_name else None
        log.info(
            "app-service create app start traceId={} userId={} appName={} dbName={}",
            trace_id,
            login_user.user_id,
            app_name,
            db_name,
        )
        app = App(
            id=await self._generate_unique_app_id(),
            owner=login_user.user_id,
            app_name=app_name,
            db_name=db_name,
        )
        self.db.add(app)
        await self.db.commit()
        await self.db.refresh(app)

        # 能力授权初始化：按平台已开放资源复制一份本项目的默认授权（失败不阻塞建项目）
        try:
            await init_app_resources(str(app.id))
        except Exception as exc:
            log.warning(
                "app-service init app resources failed traceId={} appId={} error={}",
                trace_id, app.id, exc,
            )

        # 每个用户的项目目录相互隔离：.data/{userId}/{appId}/code
        code_dir = get_code_dir(login_user.user_id, app.id)
        code_dir.mkdir(parents=True, exist_ok=True)
        log.info("app-service create app code dir traceId={} appId={} codeDir={}", trace_id, app.id, code_dir)

        if db_name:
            try:
                await create_project_database(db_name)
                log.info("app-service create app db created traceId={} appId={} dbName={}", trace_id, app.id, db_name)
            except Exception as exc:
                log.warning("app-service create app db failed traceId={} appId={} dbName={} error={}", trace_id, app.id, db_name, exc)

        log.info("app-service create app completed traceId={} userId={} appId={}", trace_id, login_user.user_id, app.id)
        return app.id

    async def _generate_unique_app_id(self) -> str:
        """生成 app_xxxx 前缀 ID，撞主键时重试"""
        for _ in range(5):
            new_id = generate_app_id()
            if not await self.db.scalar(select(App.id).where(App.id == new_id)):
                return new_id
        ThrowUtils.throw_if(True, ErrorCode.SYSTEM_ERROR, "应用ID生成失败，请重试")
        raise AssertionError("unreachable")

    async def delete_app(self, app_id: str, login_user: JWTUser) -> bool:
        app = await self._get_existing_app(app_id)
        await require_manager(self.db, app, login_user)
        await self._delete_app_by_id(app_id)
        return True

    async def get_app_by_id(self, app_id: str) -> App | None:
        return await self.db.get(App, app_id)

    async def get_app_vo_by_id(self, app_id: str, login_user: JWTUser) -> AppVO:
        app = await self._get_viewable_app(app_id, login_user)
        owner_names = await self._get_owner_names([app.owner])
        return self._to_app_vo(app, owner_names.get(app.owner))

    async def update_app(self, request: AppUpdateRequest, login_user: JWTUser) -> bool:
        app = await self._get_existing_app(request.id)
        await require_manager(self.db, app, login_user)
        if request.app_name is not None:
            app.app_name = request.app_name
        await self.db.commit()
        return True

    async def admin_update_app(self, request: AppAdminUpdateRequest, login_user: JWTUser) -> bool:
        self._require_admin(login_user)
        app = await self._get_existing_app(request.id)
        if request.app_name is not None:
            app.app_name = request.app_name
        await self.db.commit()
        return True

    async def delete_app_by_admin(self, app_id: str, login_user: JWTUser) -> bool:
        self._require_admin(login_user)
        await self._delete_app_by_id(app_id)
        return True

    async def list_my_app_vo_by_page(self, query_request: AppQueryRequest, login_user: JWTUser) -> PageData[AppVO]:
        # 我参与的项目：属主或成员
        return await self._list_app_vo_by_page(query_request, login_user=login_user)

    async def list_all_app_vo_by_page(self, query_request: AppQueryRequest, login_user: JWTUser) -> PageData[AppVO]:
        self._require_admin(login_user)
        return await self._list_app_vo_by_page(query_request)

    # ---------------------- 项目成员管理 ----------------------

    async def list_members(self, app_id: str, login_user: JWTUser) -> list[AppMemberVO]:
        app = await self._get_existing_app(app_id)
        await require_participant(self.db, app, login_user)
        result = await self.db.execute(
            select(AppMember, User)
            .join(User, User.id == AppMember.user_id)
            .where(AppMember.app_id == app_id, AppMember.is_delete == 0)
            .order_by(AppMember.create_time.asc())
        )
        return [
            AppMemberVO(
                appId=member.app_id,
                userId=member.user_id,
                userName=user.user_name if user else None,
                userAccount=user.user_account if user else None,
                createTime=member.create_time,
            )
            for member, user in result.all()
        ]

    async def add_member(self, request: AppMemberAddRequest, login_user: JWTUser) -> bool:
        app = await self._get_existing_app(request.app_id)
        await require_manager(self.db, app, login_user)
        # 邀请对象：优先按用户账号解析，否则按 userId
        if request.user_account and str(request.user_account).strip():
            result = await self.db.execute(
                select(User).where(User.user_account == str(request.user_account).strip())
            )
            target_user = result.scalars().first()
            ThrowUtils.throw_if(target_user is None, ErrorCode.NOT_FOUND_ERROR, "用户不存在")
        else:
            ThrowUtils.throw_if(not request.user_id, ErrorCode.PARAMS_ERROR, "请指定要邀请的用户")
            target_user = await self.db.get(User, request.user_id)
            ThrowUtils.throw_if(target_user is None, ErrorCode.NOT_FOUND_ERROR, "用户不存在")
        member_user_id = target_user.id  # user_xxxx 前缀字符串
        ThrowUtils.throw_if(member_user_id == app.owner, ErrorCode.PARAMS_ERROR, "该用户已是项目属主")
        ThrowUtils.throw_if(
            await get_active_member(self.db, request.app_id, member_user_id) is not None,
            ErrorCode.PARAMS_ERROR,
            "该用户已是项目成员",
        )
        # 曾被移除的成员恢复原记录（唯一键 uk_app_user），否则新增
        result = await self.db.execute(
            select(AppMember).where(
                AppMember.app_id == request.app_id,
                AppMember.user_id == member_user_id,
            )
        )
        existing = result.scalars().first()
        if existing is not None:
            existing.is_delete = 0
        else:
            self.db.add(AppMember(app_id=request.app_id, user_id=member_user_id))
        await self.db.commit()
        log.info(
            "app-service add member appId={} userId={} operator={}",
            request.app_id, member_user_id, login_user.user_id,
        )
        return True

    async def remove_member(self, request: AppMemberRemoveRequest, login_user: JWTUser) -> bool:
        app = await self._get_existing_app(request.app_id)
        await require_manager(self.db, app, login_user)
        member = await get_active_member(self.db, request.app_id, request.user_id)
        ThrowUtils.throw_if(member is None, ErrorCode.NOT_FOUND_ERROR, "该用户不是项目成员")
        # 逻辑删除：重新加入时恢复原记录
        member.is_delete = 1
        await self.db.commit()
        log.info(
            "app-service remove member appId={} userId={} operator={}",
            request.app_id, request.user_id, login_user.user_id,
        )
        return True

    # ---------------------- 文件与运行（成员各自目录隔离） ----------------------

    async def download_app_code(self, app_id: str, login_user: JWTUser) -> Path:
        app = await self._get_participant_app(app_id, login_user)
        source_dir = get_code_dir(login_user.user_id, app.id)
        ThrowUtils.throw_if(not source_dir.exists(), ErrorCode.NOT_FOUND_ERROR, "应用代码不存在，请先生成代码")
        temp_dir = Path(tempfile.mkdtemp(prefix=f"app-download-{app.id}-"))
        archive_base = temp_dir / str(app.id)
        archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=source_dir)
        return Path(archive_path)

    async def get_code_tree(self, app_id: str, login_user: JWTUser) -> list:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id)
        if not code_dir.exists():
            return []
        return AppService._build_file_tree(code_dir, code_dir)

    async def get_code_file(self, app_id: str, file_path: str, login_user: JWTUser) -> str:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / file_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)

        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        ThrowUtils.throw_if(not target.exists() or not target.is_file(), ErrorCode.NOT_FOUND_ERROR, "文件不存在")

        max_size = 500 * 1024
        max_lines = 50
        file_size = target.stat().st_size

        if file_size > max_size:
            content = []
            with target.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if idx >= max_lines:
                        break
                    content.append(line)
            return "".join(content) + "[文件超过500KB，仅展示前50行]"
        return target.read_text(encoding="utf-8", errors="replace")

    async def save_code_file(self, app_id: str, file_path: str, content: str, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / file_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    async def create_file(self, app_id: str, file_path: str, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / file_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        ThrowUtils.throw_if(not file_path.strip(), ErrorCode.PARAMS_ERROR, "文件名不能为空")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        return True

    async def create_folder(self, app_id: str, dir_path: str, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / dir_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        ThrowUtils.throw_if(not dir_path.strip(), ErrorCode.PARAMS_ERROR, "文件夹名不能为空")
        target.mkdir(parents=True, exist_ok=False)
        return True

    async def upload_file(self, app_id: str, file_path: str, content: bytes, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / file_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return True

    async def delete_node(self, app_id: str, node_path: str, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / node_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        if not target.exists():
            ThrowUtils.throw_if(True, ErrorCode.NOT_FOUND_ERROR, "文件/文件夹不存在")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True

    async def rename_node(self, app_id: str, old_path: str, new_path: str, login_user: JWTUser) -> bool:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        old_target = (code_dir / old_path).resolve()
        new_target = (code_dir / new_path).resolve()
        code_dir_str = str(code_dir)
        if not (str(old_target) == code_dir_str or str(old_target).startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        if not (str(new_target) == code_dir_str or str(new_target).startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "新路径无效")
        ThrowUtils.throw_if(not old_target.exists(), ErrorCode.NOT_FOUND_ERROR, "源文件/文件夹不存在")
        ThrowUtils.throw_if(new_target.exists(), ErrorCode.PARAMS_ERROR, "目标已存在")
        new_target.parent.mkdir(parents=True, exist_ok=True)
        old_target.rename(new_target)
        return True

    async def run_script(self, app_id: str, file_path: str, env: str, login_user: JWTUser) -> AsyncGenerator[str, None]:
        app = await self._get_participant_app(app_id, login_user)
        code_dir = get_code_dir(login_user.user_id, app.id).resolve()
        target = (code_dir / file_path).resolve()
        code_dir_str = str(code_dir)
        target_str = str(target)
        if not (target_str == code_dir_str or target_str.startswith(code_dir_str + os.sep)):
            ThrowUtils.throw_if(True, ErrorCode.PARAMS_ERROR, "路径无效")
        ThrowUtils.throw_if(not target.exists() or not target.is_file(), ErrorCode.NOT_FOUND_ERROR, "文件不存在")

        python_exe = PYTHON_ENV.get(env)
        if not python_exe:
            yield f"data: {json.dumps({'event_type': 'error', 'data': {'message': f'未知运行环境: {env}'}})}\n\n"
            return

        process = await asyncio.create_subprocess_exec(
            python_exe, str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(code_dir),
        )
        stdout_pipe = process.stdout
        stderr_pipe = process.stderr

        async def read_stream(pipe, tag):
            while True:
                line = await pipe.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                yield f"data: {json.dumps({'event_type': 'output', 'data': {'stream': tag, 'text': text}}, ensure_ascii=False)}\n\n"

        async def drain():
            async for chunk in read_stream(stdout_pipe, "stdout"):
                yield chunk
            async for chunk in read_stream(stderr_pipe, "stderr"):
                yield chunk
            await process.wait()
            yield f"data: {json.dumps({'event_type': 'done', 'data': {'code': process.returncode}}, ensure_ascii=False)}\n\n"

        async for chunk in drain():
            yield chunk

    async def get_db_tables(self, app_id: str, login_user: JWTUser) -> list:
        app = await self._get_participant_app(app_id, login_user)
        if not app.db_name:
            return []
        try:
            return await list_database_tables(app.db_name)
        except Exception as exc:
            log.warning("get_db_tables failed appId={} dbName={} error={}", app_id, app.db_name, exc)
            return []

    @staticmethod
    def _build_file_tree(base_dir: Path, current_dir: Path) -> list:
        result = []
        try:
            items = sorted(current_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for item in items:
                rel = str(item.relative_to(base_dir)).replace("\\", "/")
                if item.is_dir():
                    result.append({
                        "name": item.name,
                        "path": rel,
                        "type": "dir",
                        "children": AppService._build_file_tree(base_dir, item),
                    })
                else:
                    result.append({
                        "name": item.name,
                        "path": rel,
                        "type": "file",
                    })
        except PermissionError:
            pass
        return result

    async def _get_participant_app(self, app_id: str, login_user: JWTUser) -> App:
        """参与类接口的取应用入口：owner/member/admin 均放行。"""
        ThrowUtils.throw_if(not app_id, ErrorCode.PARAMS_ERROR, "应用 ID 错误")
        app = await self.db.get(App, app_id)
        ThrowUtils.throw_if(app is None, ErrorCode.NOT_FOUND_ERROR, "应用不存在")
        await require_participant(self.db, app, login_user)
        return app

    async def _get_viewable_app(self, app_id: str, login_user: JWTUser) -> App:
        """查看详情：成员即可（admin 天然放行）。"""
        ThrowUtils.throw_if(not app_id, ErrorCode.PARAMS_ERROR, "应用 ID 错误")
        app = await self.db.get(App, app_id)
        ThrowUtils.throw_if(app is None, ErrorCode.NOT_FOUND_ERROR, "应用不存在")
        await require_participant(self.db, app, login_user)
        return app

    async def _get_existing_app(self, app_id: str) -> App:
        ThrowUtils.throw_if(not app_id, ErrorCode.PARAMS_ERROR, "应用 ID 错误")
        app = await self.db.get(App, app_id)
        ThrowUtils.throw_if(app is None, ErrorCode.NOT_FOUND_ERROR, "应用不存在")
        return app

    async def _delete_app_by_id(self, app_id: str) -> None:
        """删除项目：物理删 app 记录；成员记录逻辑删除。"""
        await self._get_existing_app(app_id)
        await self.db.execute(delete(App).where(App.id == app_id))
        await self.db.execute(
            update(AppMember).where(AppMember.app_id == app_id).values(is_delete=1)
        )
        await self.db.commit()

    async def _list_app_vo_by_page(self, query_request: AppQueryRequest, login_user: JWTUser | None = None) -> PageData[AppVO]:
        page_num = query_request.page_num if query_request.page_num > 0 else 1
        page_size = query_request.page_size if 0 < query_request.page_size <= 20 else 10
        stmt = self._build_query(query_request, login_user)
        total_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total_row = int((await self.db.scalar(total_stmt)) or 0)
        page_stmt = stmt.offset((page_num - 1) * page_size).limit(page_size)
        result = await self.db.execute(page_stmt)
        rows = result.scalars().all()
        # 批量查属主用户名，一次 IN 查询避免逐条 N+1
        owner_names = await self._get_owner_names({row.owner for row in rows})
        records = [self._to_app_vo(item, owner_names.get(item.owner)) for item in rows]
        return PageData[AppVO](
            records=records,
            pageNumber=page_num,
            pageSize=page_size,
            totalPage=ceil(total_row / page_size) if total_row else 0,
            totalRow=total_row,
            optimizeCountQuery=True,
        )

    def _build_query(self, query_request: AppQueryRequest, login_user: JWTUser | None = None):
        stmt = select(App)
        if query_request.id is not None:
            stmt = stmt.where(App.id == query_request.id)
        if query_request.app_name:
            stmt = stmt.where(App.app_name.like(f"%{query_request.app_name}%"))
        if login_user is not None:
            # 属主 或 未删除的成员 记录（"我参与的项目"）
            member_app_ids = select(AppMember.app_id).where(
                AppMember.user_id == login_user.user_id,
                AppMember.is_delete == 0,
            )
            stmt = stmt.where(or_(App.owner == login_user.user_id, App.id.in_(member_app_ids)))
        order_field = getattr(App, query_request.sort_field, None) if query_request.sort_field else App.create_time
        if order_field is None:
            order_field = App.create_time
        stmt = stmt.order_by(order_field if query_request.sort_order == "ascend" else desc(order_field))
        return stmt

    async def _get_owner_names(self, owner_ids) -> dict[int, str | None]:
        """按属主 ID 批量查用户名，返回 {userId: userName} 映射"""
        ids = [i for i in set(owner_ids) if i is not None]
        if not ids:
            return {}
        rows = await self.db.execute(select(User.id, User.user_name).where(User.id.in_(ids)))
        return {uid: name for uid, name in rows.all()}

    def _to_app_vo(self, app: App, owner_name: str | None = None) -> AppVO:
        return AppVO.model_validate(
            {
                "id": app.id,
                "appName": app.app_name,
                "owner": app.owner,
                "ownerName": owner_name,
                "dbName": app.db_name,
                "createTime": app.create_time,
                "updateTime": app.update_time,
            }
        )

    def _require_admin(self, login_user: JWTUser) -> None:
        ThrowUtils.throw_if(login_user.user_role != "admin", ErrorCode.NO_AUTH_ERROR, "仅管理员可执行该操作")
