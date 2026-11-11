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
    # 分析链路事件（前端"分析链路"Tab 消费；命名与前端 eventType 对齐）
    SQL_START = "SQL_Start"
    SQL_END = "SQL_End"


class AgentEvent(BaseModel):
    session_id:str
    request_id:str
    round_counter:int
    event_type: AgentEventType
    data: Any = None
    state: AgentState