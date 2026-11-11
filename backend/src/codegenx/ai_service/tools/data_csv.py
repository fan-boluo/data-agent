"""
CSV 数据分析工具

- describe_csv: CSV 文件基本信息
- sample_csv_rows: CSV 采样数据
- describe_csv_stats: CSV 详细统计
"""

import asyncio
from pathlib import Path
from typing import Any

import numpy as np

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from codegenx.ai_service.data_analysis.csv_manager import CsvManager, CsvConfig
from codegenx.ai_service.data_analysis.stats_engine import (
    compute_numeric_stats, compute_categorical_stats,
    compute_correlation, infer_column_types,
    format_numeric_stats, format_categorical_stats, format_correlation,
    _resolve_sample_pct,
    _HAS_PANDAS,
)
from shared import log

# CSV 宽表上限（比 MySQL 宽松）
MAX_COLS_CSV_WIDE = 30
# 列统计超时
CSV_STATS_TIMEOUT = 30

# ── 路径解析 ──────────────────────────────────────────────

async def _resolve_csv_context(params: dict, file_path: str) -> tuple[CsvManager, str]:
    """解析 LLM 传入的 file_path 并返回守卫目录正确的 CsvManager。

    LLM 只见文件名（真实路径不可见）：传文件名/相对路径时按用户项目 code_dir 解析；
    绝对路径且存在则直接使用（是否越界由 _check_safety 守卫）。
    守卫目录必须设为 code_dir：默认 /data/csv_files 在宿主机不存在，
    任何路径都会被误拒（app.log:149「文件不在允许的目录中」根因）。
    """
    sess = await ToolSession.load(params)
    root = sess.workspace().resolve()

    p = Path(file_path)
    if not (p.is_absolute() and p.exists()):
        candidate = (root / file_path).resolve()
        try:
            # 越界路径（../ 等）不解析，保持原样交给 _check_safety 报错
            candidate.relative_to(root)
        except ValueError:
            candidate = None
        if candidate is not None:
            # 目录内但文件不存在也用解析路径：守卫报「文件不存在」而非误导性的「不在允许目录」
            p = candidate

    return CsvManager(CsvConfig(data_dir=str(root))), str(p)


# ── 格式化辅助 ────────────────────────────────────────────

def _format_csv_rows(columns: list[str], rows: list[list], max_cell_len: int = 200) -> str:
    """将列名 + 行数据格式化为对齐表格。"""
    if not rows:
        return "(无数据)"
    n_cols = len(columns)
    widths = [len(c) for c in columns]
    for row in rows:
        for i in range(min(len(row), n_cols)):
            val = str(row[i]) if row[i] is not None else "NULL"
            widths[i] = max(widths[i], min(len(val), max_cell_len))
    header = " | ".join(c.ljust(w) for c, w in zip(columns, widths))
    sep = "-+-".join("-" * w for w in widths)
    lines = [header, sep]
    for row in rows[:50]:
        cells = []
        for i in range(n_cols):
            val = str(row[i]) if i < len(row) and row[i] is not None else "NULL"
            if len(val) > max_cell_len:
                val = val[:max_cell_len] + "..."
            cells.append(val.ljust(widths[i]))
        lines.append(" | ".join(cells))
    if len(rows) > 50:
        lines.append(f"... 还有 {len(rows) - 50} 行")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 5. describe_csv
# ═══════════════════════════════════════════════════════════

class DescribeCsvTool(BaseTool):

    @property
    def name(self) -> str:
        return "describe_csv"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回 CSV 文件的基本信息：文件大小、估算行数列数、编码、分隔符、"
            "列名、前 5 行样本。用于了解数据概况后再决定分析方向。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "CSV 文件路径",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_csv 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取 CSV 信息失败: {exc}")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        file_path = params["file_path"]
        mgr, file_path = await _resolve_csv_context(params, file_path)

        info = mgr.read_info(file_path)
        sample = mgr.read_sample(file_path, limit=5)


        lines = [
            f"文件: {info['file_name']}",
            f"路径: {info['file_path']}",
            f"大小: {info['file_size_mb']} MB | 编码: {info['encoding']} | 分隔符: '{info['separator']}'",
            f"估算 {info['estimated_rows']} 行 × {info['n_cols']} 列",
            "",
            f"── 列名 ({info['n_cols']}) ──",
            ", ".join(info["columns"]),
            "",
            f"── 前 {sample['n_rows_sampled']} 行采样 ──",
            _format_csv_rows(sample["columns"], sample["rows"]),
        ]
        return ToolResult(success=True, data="\n".join(lines), render=f"CSV 概况: {info['file_name']} ({info['n_cols']} 列)")


# ═══════════════════════════════════════════════════════════
# 6. sample_csv_rows
# ═══════════════════════════════════════════════════════════

class SampleCsvRowsTool(BaseTool):

    @property
    def name(self) -> str:
        return "sample_csv_rows"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回 CSV 文件的采样数据（默认 10 行），用于理解字段的实际内容和格式。"
            "长文本会被截断。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "CSV 文件路径",
                },
                "limit": {
                    "type": "integer",
                    "description": "采样行数，默认 10",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sample_csv_rows 执行异常: {}", exc)
            return ToolResult(success=False, message=f"CSV 采样失败: {exc}")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        file_path = params["file_path"]
        limit = int(params.get("limit", 10))
        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100

        mgr, file_path = await _resolve_csv_context(params, file_path)
        sample = mgr.read_sample(file_path, limit=limit)

        output = f"CSV 采样 ({sample['n_rows_sampled']} 行):\n"
        output += _format_csv_rows(sample["columns"], sample["rows"])
        return ToolResult(success=True, data=output, render=f"CSV 采样: {Path(file_path).name} ({sample['n_rows_sampled']} 行)")


# ═══════════════════════════════════════════════════════════
# 7. describe_csv_stats
# ═══════════════════════════════════════════════════════════

class DescribeCsvStatsTool(BaseTool):

    @property
    def name(self) -> str:
        return "describe_csv_stats"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回 CSV 文件的详细统计信息：数值字段的均值/方差/分位数，"
            "类别字段的频率分布，缺失值，列间相关性。"
            "大文件自动降级采样。通过 columns 参数指定要统计的列。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "CSV 文件路径",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要统计的列名列表，不指定则自动选择（最多 30 列）",
                },
                "sample_pct": {
                    "type": "integer",
                    "description": "采样百分比 1-100，默认 100（全量）。大文件自动降级",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "统计超时秒数，默认 30",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_csv_stats 执行异常: {}", exc)
            return ToolResult(success=False, message=f"CSV 统计失败: {exc}")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        if not _HAS_PANDAS:
            return ToolResult(
                success=False,
                message="pandas 未安装，describe_csv_stats 不可用",
            )

        file_path = params["file_path"]
        requested_columns = params.get("columns")
        requested_sample_pct = int(params.get("sample_pct", 100))
        timeout = int(params.get("timeout_seconds", CSV_STATS_TIMEOUT))

        mgr, file_path = await _resolve_csv_context(params, file_path)
        info = mgr.read_info(file_path)
        est_rows = info["estimated_rows"]
        all_col_names = info["columns"]
        n_cols = info["n_cols"]

        # 采样率决策
        from codegenx.ai_service.data_analysis.csv_manager import LARGE_CSV_ROWS, VERY_LARGE_CSV_ROWS
        if est_rows > VERY_LARGE_CSV_ROWS:
            auto_pct = 10
        elif est_rows > LARGE_CSV_ROWS:
            auto_pct = 30
        else:
            auto_pct = 100
        sample_pct = min(auto_pct, requested_sample_pct)
        sample_pct = max(sample_pct, 1)

        # 选列
        if requested_columns:
            target_cols = [c for c in requested_columns if c in all_col_names]
        else:
            target_cols = all_col_names[:MAX_COLS_CSV_WIDE]
            skipped = all_col_names[MAX_COLS_CSV_WIDE:]

        if not target_cols:
            return ToolResult(success=False, message="未找到有效列")

        # 读数据
        import pandas as pd
        try:
            df = mgr.read_dataframe(file_path, sample_pct=sample_pct)
        except Exception as exc:
            return ToolResult(success=False, message=f"读取 CSV 失败: {exc}")

        # 只保留目标列
        df = df[[c for c in target_cols if c in df.columns]]
        if df.empty:
            return ToolResult(success=False, message="未读取到有效数据")

        # 推断列类型
        col_types = infer_column_types(
            df.columns.tolist(),
            [df[c].values for c in df.columns],
        )

        # 统计
        notes = []
        if sample_pct < 100:
            notes.append(f"采样率 {sample_pct}%，全量约 {est_rows} 行")
        if not requested_columns and n_cols > MAX_COLS_CSV_WIDE:
            notes.append(f"已跳过 {len(skipped)} 列: {', '.join(skipped[:10])}...")

        numeric_results = {}
        categorical_results = {}
        incomplete = []

        for col in df.columns:
            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

            dtype = col_types.get(col, "unknown")
            try:
                if dtype in ("numeric", "integer"):
                    vals = pd.to_numeric(df[col], errors="coerce").values
                    numeric_results[col] = compute_numeric_stats(vals, sample_pct)
                else:
                    vals = df[col].astype(str).values
                    categorical_results[col] = compute_categorical_stats(vals)
            except Exception as exc:
                incomplete.append(col)
                log.warning("列 {} 统计异常: {}", col, exc)

        # 相关性
        corr_section = ""
        if sample_pct >= 100 and len(numeric_results) >= 2 and len(numeric_results) <= 15:
            try:
                num_cols = list(numeric_results.keys())
                matrix = df[num_cols].apply(pd.to_numeric, errors="coerce").dropna().values.T
                corr = compute_correlation(matrix, num_cols)
                corr_section = "\n\n── 相关性 ──\n" + format_correlation(corr)
            except Exception:
                corr_section = "\n\n[相关性计算失败]"
        elif len(numeric_results) >= 2:
            corr_section = "\n\n[相关性需全量数据，使用 sample_pct=100 可开启]"

        # 组装输出
        lines = [
            f"文件统计: {info['file_name']} ({est_rows} 行 × {n_cols} 列)",
        ]
        if notes:
            lines.append("[" + "] [".join(notes) + "]")
        if incomplete:
            lines.append(f"[未完成 {len(incomplete)} 列: {', '.join(incomplete[:10])}]")

        if numeric_results:
            lines.append("")
            lines.append("── 数值字段 ──")
            for col_name, stats in numeric_results.items():
                lines.append(format_numeric_stats(col_name, stats))

        if categorical_results:
            lines.append("")
            lines.append("── 类别字段 ──")
            for col_name, stats in categorical_results.items():
                lines.append(format_categorical_stats(col_name, stats))

        lines.append(corr_section)
        lines.append("")
        lines.append(f"共统计 {len(numeric_results) + len(categorical_results)} 列")
        return ToolResult(success=True, data="\n".join(lines), render=f"CSV 统计: {info['file_name']} ({len(numeric_results) + len(categorical_results)} 列)")


# ═══════════════════════════════════════════════════════════
# 8. list_csv_files
# ═══════════════════════════════════════════════════════════

class ListCsvFilesTool(BaseTool):
    """列出当前用户项目下的 csv 文件（归属由 session_id 解析）。

    返回的完整路径可后续作为 describe_csv 的 file_path 参数使用。
    """

    @property
    def name(self) -> str:
        return "list_csv_files"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "列出当前项目下所有 csv 文件，包括文件名、大小与完整路径。"
            "返回的路径可作为 describe_csv 的 file_path 参数。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("list_csv_files 执行异常: {}", exc)
            return ToolResult(success=False, message=f"列出 csv 文件失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        sess = await ToolSession.load(params)
        code_dir = sess.workspace()
        if not code_dir.exists():
            return ToolResult(success=True, data=f"（项目目录不存在: {code_dir}）")

        csv_files = sorted(code_dir.glob("*.csv"))
        if not csv_files:
            return ToolResult(success=True, data=f"（{code_dir} 下暂无 csv 文件）")

        lines = [f"共找到 {len(csv_files)} 个 csv 文件:"]
        for fp in csv_files:
            size_mb = fp.stat().st_size / 1024 / 1024
            lines.append(f"- {fp.name} ({size_mb:.2f}MB) 路径: {fp}")
        return ToolResult(success=True, data="\n".join(lines), render=f"csv 文件列表（{len(csv_files)} 个）")
