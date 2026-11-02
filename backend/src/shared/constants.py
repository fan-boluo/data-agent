from pathlib import Path

# ----------------------页面展示------------------------------
DEFAULT_PAGE_NUM = 1
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


# Safety settings TODO 前端添加
MAX_PROMPT_LENGTH = 10000
MAX_CODE_LENGTH = 50000

# ---------------------------------------------------------
# 用户数据位置，日志位置
# ---------------------------------------------------------
# 日志
# 获得当前项目的绝对路径（src/shared/constants.py → 上跳 3 级为仓库根）
ROOT_DIR = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT_DIR / "logs"  # 存放项目日志目录的绝对路径
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"  # 存储日志的文件

"""
用户数据按 用户/项目 两级隔离存储（同一项目的成员各自只能看到自己的文件）：
-- frontend
-- backend
-- .data
  {userId}
    {appId}
      -- code    源代码
      -- memory
      -- context
      -- session
"""

DATA_ROOT_DIR = Path(__file__).resolve().parents[3] / ".data"

def get_runtime_app_dir(user_id: str | int, app_id: str | int) -> Path:
    # 用户/项目 两级目录：.data/{userId}/{appId}
    return DATA_ROOT_DIR / str(user_id) / str(app_id)

def get_code_dir(user_id: str | int, app_id: str | int) -> Path:
    return get_runtime_app_dir(user_id, app_id) / "code"


def get_context_dir(user_id: str | int, app_id: str | int) -> Path:
    return get_runtime_app_dir(user_id, app_id) / "context"


def get_memory_dir(user_id: str | int, app_id: str | int) -> Path:
    return get_runtime_app_dir(user_id, app_id) / "memory"


def get_session_dir(user_id: str | int, app_id: str | int) -> Path:
    return get_runtime_app_dir(user_id, app_id) / "session"

def get_current_session_dir(user_id: str | int, app_id: str | int, session_id: str) -> Path:
    return get_session_dir(user_id, app_id) / session_id

# ------------------------监控---------------------------
# 是否开启
TELEMETRY_OPEN = False

# ----------------------python虚拟环境--------------------
PYTHON_ENV = {
    "model":r"D:\CondaEnvs\model\python.exe"
}

# ----------------------数据源与库绑定--------------------
# 所有用户项目共用同一个 PG 数据库，每个项目一个 schema
ANALYSIS_DB = "analysis"
# 公共数据集（所有用户可见），agent 仅基于这些公共库做问答
# value 格式 datasource.schema，datasource 名即 PG database 名，需与语义层 meta_datasource.name 一致
PUBLIC_DATASETS = [
    {"value": "business.dvdrental", "label": "DVD 租赁库（dvdrental）"},
    {"value": "tpcds_sf1.public", "label": "TPC-DS SF1 数仓"},
]
# 权限白名单（后续权限管控时按用户圈定）
PUBLIC_SCHEMAS = [d["value"] for d in PUBLIC_DATASETS]
