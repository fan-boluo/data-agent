"""
Python 代码执行工具

- run_python: 执行 pandas 分析代码（csv_explore 智能体核心工具）

环境分支策略与原 CsvAction 保持一致：
- dev    → 本地 subprocess 调用 PYTHON_ENV 执行
- 非 dev → docker exec 在 sql_exec_docker_container 容器内执行（宿主机 .data/ 挂载到 docker_data_root/）

代码中用 __CSV_DIR__ 占位符表示 csv 文件所在目录，框架执行时替换为真实路径，
路径解析由服务端完成，LLM 不接触宿主机真实路径。
"""

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log
from shared.constants import DATA_ROOT_DIR, PYTHON_ENV

# 代码执行超时（秒）
EXEC_TIMEOUT_SECONDS = 60
# 返回给 LLM 的 stdout 最大字符数
MAX_STDOUT_CHARS = 4000
# 代码中表示 csv 目录的占位符
CSV_DIR_PLACEHOLDER = "__CSV_DIR__"
# 代码中表示系统产物目录的占位符（P0-4：run_python 沙箱内出图落盘 agent_output/）
OUTPUT_DIR_PLACEHOLDER = "__OUTPUT_DIR__"

# matplotlib 绘图前置样板（检测到绘图代码时自动注入）：Agg 无显示后端 +
# 中文字体 + 固定 DPI，LLM 代码无需重复设置
_PLOT_BOOTSTRAP = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']\n"
    "plt.rcParams['axes.unicode_minus'] = False\n"
    "plt.rcParams['figure.dpi'] = 100\n"
)
# 识别绘图代码的特征
_PLOT_HINTS = ("matplotlib", "plt.", "pyplot", "savefig", "seaborn", "sns.")


class RunPythonTool(BaseTool):
    """pandas 分析代码执行工具。

    归属由 session_id 解析，用于定位当前用户项目 code_dir；
    占位符按环境替换（dev=宿主机路径，docker=容器内路径）后执行。
    """

    @property
    def name(self) -> str:
        return "run_python"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "执行一段 pandas/numpy 分析代码，返回 stdout 输出。"
            f"代码中用 {CSV_DIR_PLACEHOLDER} 占位符表示 csv 文件所在目录，文件名自行拼接，"
            f"例如：pd.read_csv({CSV_DIR_PLACEHOLDER} + '/order_2023.csv')。"
            "结果必须通过 print() 输出；仅可用 pandas/numpy/matplotlib/标准库；"
            f"需要出图时用 matplotlib 并把图片保存到 {OUTPUT_DIR_PLACEHOLDER} 占位目录"
            f"（例如 plt.savefig({OUTPUT_DIR_PLACEHOLDER} + '/chart.png')），禁止 plt.show()、弹窗与写其他文件。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": f"完整可运行的 Python 代码，读取 csv 路径用 {CSV_DIR_PLACEHOLDER} 占位符，"
                                   f"保存图表用 {OUTPUT_DIR_PLACEHOLDER} 占位符",
                },
            },
            "required": ["code"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return ToolResult(success=False, message=f"代码执行超时（>{EXEC_TIMEOUT_SECONDS}s）")
        except Exception as exc:
            log.error("run_python 执行异常: {}", exc)
            return ToolResult(success=False, message=f"代码执行失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        code = str(params.get("code") or "").strip()
        if not code:
            return ToolResult(success=False, message="code 为空，无法执行")

        # ── 解析项目目录并替换占位符（会话归属由 session_id 解析） ──
        sess = await ToolSession.load(params)
        code_dir = sess.workspace().resolve()
        if not code_dir.exists():
            return ToolResult(success=False, message=f"项目目录不存在: {code_dir}")

        # 输出目录（图表等系统产物统一落隐藏目录 agent_output/，不与用户文件混放）
        out_dir = sess.output_dir().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # 绘图检测：命中特征时注入 Agg 后端 + 中文字体样板（P0-4 沙箱内出图）
        final_code = code
        if any(hint in code for hint in _PLOT_HINTS):
            final_code = _PLOT_BOOTSTRAP + final_code
        # 执行起点时间戳：之后落盘的图片才认定为本次产出
        t0 = time.time()

        from shared import settings
        if settings.env == "dev":
            # 本地：占位符替换为宿主机绝对路径（windows 含反斜杠，用 raw 字符串）
            final_code = final_code.replace(CSV_DIR_PLACEHOLDER, f"r'{code_dir}'")
            final_code = final_code.replace(OUTPUT_DIR_PLACEHOLDER, f"r'{out_dir}'")
            stdout, stderr, rc = await self._run_local(final_code, cwd=code_dir)
        else:
            # docker：占位符替换为容器内路径（宿主机 .data/ → docker_data_root/，统一正斜杠）
            container_dir = self._to_container_path(code_dir)
            container_out_dir = self._to_container_path(out_dir)
            final_code = final_code.replace(CSV_DIR_PLACEHOLDER, f"r'{container_dir}'")
            final_code = final_code.replace(OUTPUT_DIR_PLACEHOLDER, f"r'{container_out_dir}'")
            stdout, stderr, rc = await self._run_docker(final_code)

        if rc != 0:
            # 非零退出：把 stderr 回传给 LLM 修正代码
            return ToolResult(
                success=False,
                message=f"代码非零退出 rc={rc}\nstderr: {stderr[:1000]}",
            )

        # ── stdout 截断后返回 ──
        truncated = len(stdout) > MAX_STDOUT_CHARS
        out = stdout[:MAX_STDOUT_CHARS]
        if not out.strip():
            data = "（代码执行成功但无 stdout 输出，请确认已用 print() 输出结果）"
        else:
            data = out + (f"\n...(输出已截断，原长 {len(stdout)} 字符)" if truncated else "")

        # ── 收集本次新生成的图表（render/extras 承载展示元数据，不进 LLM 上下文） ──
        images = self._collect_new_images(out_dir, t0)
        log.info("RunPythonTool 执行成功 | env={} | stdout_len={} | images={}",
                 settings.env, len(stdout), len(images))
        return ToolResult(
            success=True,
            data=data,
            render=f"执行成功，生成图表 {len(images)} 张（已落盘系统产物目录）" if images else "Python 代码执行成功",
            extras={"images": images} if images else {},
        )

    @staticmethod
    def _collect_new_images(out_dir: Path, since_ts: float) -> list[str]:
        """收集 since_ts 之后新落盘的图片文件（png/jpg/svg）。

        返回相对系统产物目录的 posix 路径（LLM 引用与前端 /ai/output-file 拼参共用）。
        """
        if not out_dir.exists():
            return []
        images: list[str] = []
        for p in sorted(out_dir.rglob("*")):
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
                try:
                    if p.stat().st_mtime >= since_ts - 1:
                        images.append(p.relative_to(out_dir).as_posix())
                except (OSError, ValueError):
                    continue
        return images

    @staticmethod
    def _to_container_path(host_dir: Path) -> str:
        """把宿主机 .data/ 下的绝对路径映射为容器内路径（统一正斜杠）。

        约定：宿主机 DATA_ROOT_DIR 整个挂载到容器的 settings.docker_data_root。
        """
        from shared import settings
        rel = host_dir.relative_to(DATA_ROOT_DIR.resolve())
        return f"{settings.docker_data_root.rstrip('/')}/{rel.as_posix()}"

    @staticmethod
    async def _run_local(code: str, cwd: Path) -> tuple[str, str, int]:
        """dev 环境：本地 subprocess 调用 PYTHON_ENV 执行，返回 (stdout, stderr, returncode)。

        用临时文件而不是 -c 传代码，避免命令行长度限制与引号转义问题。
        """
        python_exe = PYTHON_ENV.get("model") or sys.executable

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as fp:
                fp.write(code)
                tmp_path = fp.name

            proc = await asyncio.create_subprocess_exec(
                python_exe, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=EXEC_TIMEOUT_SECONDS
            )
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            return stdout, stderr, proc.returncode or 0
        finally:
            # 不管成功失败都清理临时文件
            if tmp_path:
                with contextlib.suppress(Exception):
                    os.unlink(tmp_path)

    @staticmethod
    async def _run_docker(code: str) -> tuple[str, str, int]:
        """非 dev 环境：docker exec 在容器内执行 python，返回 (stdout, stderr, returncode)。"""
        from codegenx.ai_service.agent.db.docker_exec import execute_python_in_docker
        from shared import settings
        result = await execute_python_in_docker(
            container=settings.sql_exec_docker_container,
            code=code,
            python_bin=settings.docker_python_bin,
            timeout=EXEC_TIMEOUT_SECONDS,
        )
        return result.stdout, result.stderr, result.exit_code
