-- 平台资源 / 项目授权表（PostgreSQL）
CREATE SCHEMA IF NOT EXISTS agent;
SET search_path TO agent, public;

CREATE TABLE agent.tool (
    id BIGSERIAL PRIMARY KEY,
    tool_code VARCHAR(64) NOT NULL UNIQUE, -- 唯一标识，程序代码里用，优先不用tool_name，防止改名炸逻辑，例：code_exec / plot
    tool_name VARCHAR(128) NOT NULL,       -- 展示名称：代码执行沙箱、图表绘图
    description TEXT,                      -- 用户可见描述
    default_config JSONB NOT NULL DEFAULT '{}'::jsonb, -- 默认参数：现在还没给每个工具单独配参数
    is_platform_enabled BOOLEAN NOT NULL DEFAULT true, -- 平台全局总开关：false=全平台所有app都不能用这个工具（紧急熔断用！）
    remark TEXT,                           -- 管理员内部备注、风险说明、维护信息
    operator_id VARCHAR(32),               -- 最后操作人user id（关联mysql user表：user_xxxx）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN tool.tool_code IS '程序内部唯一编码，硬编码依赖这个，不要用名称';
COMMENT ON COLUMN tool.tool_name IS '后台展示名称，可修改';
COMMENT ON COLUMN tool.is_platform_enabled IS '平台全局总开关，紧急风控一键下线';
COMMENT ON COLUMN tool.description IS '面向使用者的功能介绍，前端可展示';
COMMENT ON COLUMN tool.remark IS '管理员内部备注，风险、维护记录，普通用户不可见';


CREATE TABLE agent.app_tool (
    id BIGSERIAL PRIMARY KEY,
    app_id VARCHAR(32) NOT NULL, -- 关联mysql app表主键（app_xxxx）
    tool_code VARCHAR(64) NOT NULL REFERENCES tool(tool_code),
    is_enabled BOOLEAN NOT NULL DEFAULT false, -- 该app下是否启用这个工具（前提是tool.is_platform_enabled=true才生效）
    override_config JSONB NOT NULL DEFAULT '{}'::jsonb, -- 本app单独覆盖默认配置，空则继承tool.default_config
    remark TEXT,
    operator_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(app_id, tool_code)
);
COMMENT ON CONSTRAINT app_tool_app_id_tool_code_key ON app_tool IS '一个app同一个工具只能一条授权记录';

CREATE TABLE agent.skill (
    id BIGSERIAL PRIMARY KEY,
    skill_code VARCHAR(64) NOT NULL UNIQUE,
    skill_name VARCHAR(128) NOT NULL,
    description TEXT,
    is_platform_enabled BOOLEAN NOT NULL DEFAULT true, -- 技能全局总开关
    remark TEXT,
    operator_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 技能 ↔ 工具多对多关联,现在好像没啥用，skill还没仔细看，下线工具的时候应该是要校验skill有没有在用的
CREATE TABLE agent.skill_tool_rel (
    id BIGSERIAL PRIMARY KEY,
    skill_code VARCHAR(64) NOT NULL REFERENCES skill(skill_code),
    tool_code VARCHAR(64) NOT NULL REFERENCES tool(tool_code),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(skill_code, tool_code)
);

CREATE TABLE agent.app_skill (
    id BIGSERIAL PRIMARY KEY,
    app_id VARCHAR(32) NOT NULL,
    skill_code VARCHAR(64) NOT NULL REFERENCES skill(skill_code),
    is_enabled BOOLEAN NOT NULL DEFAULT false,
    remark TEXT,
    operator_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(app_id, skill_code)
);

-- 平台智能体（全局层，无 app 级授权）
-- agent_code 对应 AgentSpec.name / config.json agents[].name（leader/planner/data_explore/...）
-- 注意：LLM 可用性（模型链熔断器状态）由后端实时计算，不入库；这里只持久化手动总开关
CREATE TABLE agent.agent (
    id BIGSERIAL PRIMARY KEY,
    agent_code VARCHAR(64) NOT NULL UNIQUE,    -- 程序内部唯一编码，硬编码依赖这个
    agent_name VARCHAR(128) NOT NULL,          -- 后台展示名称，可修改
    description TEXT,
    is_enabled BOOLEAN NOT NULL DEFAULT true,  -- 管理员手动总开关：false=全局下线
    remark TEXT,                               -- 启停原因、维护说明
    operator_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE agent.agent IS '平台智能体管理：手动启停 + 备注；LLM 可用性实时按模型链熔断器计算';
COMMENT ON COLUMN agent.agent.is_enabled IS '手动总开关；实际可用 = is_enabled ∧ 模型链存在未熔断模型';
