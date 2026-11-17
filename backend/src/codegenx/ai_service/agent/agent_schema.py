from enum import Enum
from typing import Any
from pydantic import BaseModel

class AgentState(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class AgentEventType(str, Enum):
    ON_TURN_START = "OnTurnStart"
    LLM_THINKING_START = "LLM_Thinking_Start"
    LLM_RESPONSE_CHUNK = "LLM_Response_Chunk"
    TOOL_EXECUTION_START = "ToolExecutionStart"
    TOOL_EXECUTION_END = "ToolExecutionEnd"
    COMPACT_EVENT = "CompactEvent"
    REQUEST_COMPLETED = "RequestCompleted"
    REQUEST_STOPPED = "RequestStopped"
    ERROR = "Error"
    PLAN_STEP_CHANGED = "PlanStepChanged"
    # HITL 闸门：高风险工具调用挂起，等待人工审批（前端弹窗消费）
    APPROVAL_REQUIRED = "ApprovalRequired"

class MessageType(str, Enum):
    TEXT = "TEXT"
    # CHART：ECharts option（JSON）驱动前端交互式渲染
    # IMAGE：普通图片（PNG/SVG 等）文件渲染
    CHART = "CHART"
    IMAGE = "IMAGE"
    REPORT = "REPORT"


class AgentEvent(BaseModel):
    session_id:str
    request_id:str
    round_counter:int
    event_type: AgentEventType
    data: Any = None
    state: AgentState
    message_type : MessageType = MessageType.TEXT
