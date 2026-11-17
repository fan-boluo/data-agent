"""统一绘图工具（P0-4 重构）：基础绘图能力收口为 plot_chart，图表标准化产出。

设计：
- 图表规划/排版/结论由报告子 Agent（reporter）负责，本工具只做"数据 → 标准图"的
  确定性转换（LLM 不写绘图代码），保证出图风格统一；
- **双输出**：必出 ECharts option（JSON，前端交互式渲染，message_type=CHART）；
  save_image=true 时才落 PNG（report 内嵌引用/下载用，message_type=IMAGE）；
- 过期图片清理统一由 `schedule/retention.py` 负责（被报告引用的永久保留）。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log

# 支持的图表类型
SUPPORTED_CHART_TYPES = ("bar", "line", "pie", "scatter")
# 单图最多系列数（pie 只取第一组）
MAX_SERIES = 4


def build_chart_option(chart_type: str, title: str, categories: list[str],
                       series: list[dict], x_label: str = "", y_label: str = "") -> dict:
    """把绘图参数转成 ECharts option（前端直接 setOption 渲染，无需落盘）。

    参数已由 execute 校验（chart_type 合法、values 与 categories 等长）。
    """
    names = [str(s.get("name") or f"系列{i + 1}") for i, s in enumerate(series)]
    if chart_type == "pie":
        # 饼图只取第一组系列（多系列无意义）
        s0 = series[0]
        return {
            "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "item"},
            "legend": {"bottom": 0},
            "series": [{
                "name": names[0], "type": "pie", "radius": ["35%", "65%"],
                "avoidLabelOverlap": True,
                "label": {"show": True, "formatter": "{b}: {d}%"},
                "data": [{"name": str(c), "value": s0["values"][i]}
                         for i, c in enumerate(categories)],
            }],
        }

    # bar / line / scatter：类目轴 + 多系列
    echarts_type = {"bar": "bar", "line": "line", "scatter": "scatter"}[chart_type]
    data_series = []
    for i, s in enumerate(series):
        values = s["values"]
        item: dict[str, Any] = {"name": names[i], "type": echarts_type}
        if chart_type == "scatter":
            # 散点：类目序号作 x，数值作 y
            item["data"] = [[j, v] for j, v in enumerate(values)]
        else:
            item["data"] = values
        if chart_type == "line":
            item["smooth"] = False
            item["symbolSize"] = 6
        if chart_type == "bar" and len(series) > 1:
            item["barGap"] = "10%"
        data_series.append(item)
    return {
        "title": {"text": title, "left": "center"},
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "data": names},
        "grid": {"left": "3%", "right": "4%", "bottom": "12%", "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": categories,
            **({"name": x_label} if x_label else {}),
        },
        "yAxis": {"type": "value", **({"name": y_label} if y_label else {})},
        "series": data_series,
    }


class PlotChartTool(BaseTool):
    """标准图表工具：输出 ECharts option（必选）+ PNG（可选），report 引用 PNG。"""

    @property
    def name(self) -> str:
        return "plot_chart"

    @property
    def label(self) -> str:
        return "chart"

    @property
    def description(self) -> str:
        return (
            "生成一张标准图表。默认输出 ECharts 配置（前端交互式渲染，无需写绘图代码）。"
            f"支持类型：{'/'.join(SUPPORTED_CHART_TYPES)}；bar/line/scatter 可多系列，pie 只取第一组。"
            "若图表要写进 Markdown 报告，传 save_image=true 额外保存 PNG，"
            "并在报告中用 ![标题](charts/文件名.png) 引用。"
            "plot_chart 表达不了的复杂图（组合图/桑基/热力/自定义标注）才改用 run_python。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": list(SUPPORTED_CHART_TYPES),
                    "description": "图表类型：bar 柱状图 / line 折线图 / pie 饼图 / scatter 散点图",
                },
                "title": {"type": "string", "description": "图表标题（同时作为文件名主体）"},
                "categories": {
                    "type": "array", "items": {"type": "string"},
                    "description": "横轴类目（如月份、品类）；scatter 时为 x 值",
                },
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "系列名（图例）"},
                            "values": {"type": "array", "items": {"type": "number"},
                                       "description": "与 categories 等长的数值序列"},
                        },
                        "required": ["name", "values"],
                    },
                    "description": "数据系列（1-4 组）；pie 只用第一组",
                },
                "x_label": {"type": "string", "description": "横轴标签（可选）"},
                "y_label": {"type": "string", "description": "纵轴标签（可选）"},
                "save_image": {
                    "type": "boolean",
                    "description": "是否额外保存 PNG（写报告时置 true；仅对话展示保持默认 false 即可）",
                },
                "filename": {"type": "string", "description": "PNG 文件名（可选，默认按标题生成）"},
            },
            "required": ["chart_type", "title", "categories", "series"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        chart_type = str(params.get("chart_type") or "").strip().lower()
        title = str(params.get("title") or "").strip()
        categories = [str(c) for c in (params.get("categories") or [])]
        series = list(params.get("series") or [])
        if chart_type not in SUPPORTED_CHART_TYPES:
            return ToolResult(success=False,
                              message=f"chart_type 仅支持 {'/'.join(SUPPORTED_CHART_TYPES)}，收到: {chart_type}")
        if not title:
            return ToolResult(success=False, message="title 为空，请提供图表标题")
        if not categories:
            return ToolResult(success=False, message="categories 为空，请提供横轴类目")
        if not series:
            return ToolResult(success=False, message="series 为空，请提供至少一组数据")
        series = series[:MAX_SERIES]
        for s in series:
            values = s.get("values") or []
            if len(values) != len(categories):
                return ToolResult(
                    success=False,
                    message=f"系列「{s.get('name')}」的 values 长度（{len(values)}）"
                            f"必须与 categories 数量（{len(categories)}）一致",
                )
            if not all(isinstance(v, (int, float)) for v in values):
                return ToolResult(success=False,
                                  message=f"系列「{s.get('name')}」的 values 必须是数字")
        if chart_type == "pie" and len(series) > 1:
            log.warning("plot_chart pie 只取第一组系列（共 {} 组）", len(series))
            series = series[:1]

        # ── 主产物：ECharts option（前端交互式渲染） ──
        option = build_chart_option(
            chart_type, title, categories, series,
            str(params.get("x_label") or ""), str(params.get("y_label") or ""),
        )

        # ── 可选产物：PNG（报告内嵌/下载；仅此路径才引入 matplotlib） ──
        images: list[str] = []
        save_image = bool(params.get("save_image"))
        if save_image:
            image_result = await self._save_png(params, chart_type, title, categories, series)
            if not image_result.success:
                return image_result  # 明确要求 PNG 却失败：如实报错，不静默降级
            images = image_result.extras["images"]

        data = f"图表已生成：「{title}」（{chart_type}，{len(series)} 组系列，{len(categories)} 个类目）。"
        if images:
            data += f" 图片文件：{images[0]}，报告中用 ![{title}]({images[0]}) 引用。"
        else:
            data += " 已在对话中交互式展示；若要写进报告，请传 save_image=true 重新生成。"
        return ToolResult(
            success=True,
            data=data,
            render=f"生成图表「{title}」（{chart_type}）",
            extras={"chart_option": option, "images": images},
        )

    async def _save_png(self, params: dict, chart_type: str, title: str,
                        categories: list[str], series: list[dict]) -> ToolResult:
        """渲染 PNG 到系统产物目录 charts/（Agg 后端 + 中文字体，固定尺寸）。"""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc:
            return ToolResult(success=False, message=f"保存图片需要 matplotlib，当前环境不可用: {exc}")

        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=(8, 5))
        try:
            x = range(len(categories))
            if chart_type == "bar":
                width = 0.8 / len(series)
                for i, s in enumerate(series):
                    ax.bar([xi + i * width for xi in x], s["values"], width,
                           label=str(s.get("name") or f"系列{i + 1}"))
                ax.set_xticks([xi + 0.4 - width / 2 for xi in x])
                ax.set_xticklabels(categories, fontsize=9)
            elif chart_type == "line":
                for s in series:
                    ax.plot(list(x), s["values"], marker="o", label=str(s.get("name") or "系列"))
                ax.set_xticks(list(x))
                ax.set_xticklabels(categories, fontsize=9)
            elif chart_type == "pie":
                ax.pie(series[0]["values"], labels=categories, autopct="%1.1f%%", startangle=90)
                ax.set_aspect("equal")
            else:  # scatter
                for s in series:
                    ax.scatter(list(x), s["values"], label=str(s.get("name") or "系列"))
                ax.set_xticks(list(x))
                ax.set_xticklabels(categories, fontsize=9)
            if chart_type != "pie":
                if params.get("x_label"):
                    ax.set_xlabel(str(params["x_label"]))
                if params.get("y_label"):
                    ax.set_ylabel(str(params["y_label"]))
                if len(series) > 1:
                    ax.legend(fontsize=9)
            ax.set_title(title)
            fig.tight_layout()

            # 落盘：系统产物目录 agent_output/charts/，相对路径供报告与前端共用
            sess = await ToolSession.load(params)
            chart_dir = (sess.output_dir() / "charts").resolve()
            chart_dir.mkdir(parents=True, exist_ok=True)
            safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:50] or "chart"
            filename = str(params.get("filename") or "").strip() or \
                f"{safe_title}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            if not filename.lower().endswith(".png"):
                filename += ".png"
            file_path = chart_dir / filename
            fig.savefig(file_path)
            log.info("PlotChartTool PNG 已落盘 | path={} | type={} | series={}",
                     file_path, chart_type, len(series))
            return ToolResult(success=True, extras={"images": [f"charts/{filename}"]})
        except OSError as exc:
            return ToolResult(success=False, message=f"图表保存失败: {exc}")
        finally:
            plt.close(fig)
