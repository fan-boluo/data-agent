

-- =====================================================================
-- 以下为 PostgreSQL（agent schema，表结构见 init/params_schema.sql）初始化数据
-- 该节为 PG 专用语法，需单独提取后用 psql 执行：
--   psql -U postgres -h 127.0.0.1 -p 5433 -d postgres -f init/params_schema.sql
--   再将本节内容另存或重定向给 psql 执行（MySQL 客户端不支持本节语法）
-- =====================================================================

-- 平台工具全集（tool_code 与代码内 BaseTool.name 保持一致，新增工具需同步补录）
INSERT INTO agent.tool (tool_code, tool_name, description, is_platform_enabled, remark) VALUES
('code_check',             '代码语法检查',     '静态检查 Python 代码语法是否正确（仅 AST 解析，不执行代码）', true, NULL),
('run_python',             'Python 代码执行',  '在沙箱中执行 Python 代码并返回运行输出', true, NULL),
('describe_csv',           'CSV 概况',         '读取 CSV 文件大小、行列数、编码、列名与前 5 行样本', true, NULL),
('sample_csv_rows',        'CSV 采样',         '返回 CSV 文件的采样数据，用于理解字段实际内容和格式', true, NULL),
('describe_csv_stats',     'CSV 统计',         '返回 CSV 数值字段统计、类别频率分布、缺失值与相关性', true, NULL),
('list_csv_files',         'CSV 文件列表',     '列出当前项目下所有 CSV 文件及完整路径', true, NULL),
('execute_postgresql',     'SQL 查询执行',     '在公共数据仓库执行只读 SQL 查询，返回结果表格', true, NULL),
('list_tables',            '数据表列表',       '列出当前数据集 schema 下的表名、注释与行数估算', true, NULL),
('describe_table',         '表结构说明',       '返回指定表的列、类型、索引、注释与行数估算', true, NULL),
('sample_rows',            '数据表采样',       '返回数据表采样数据，用于理解字段实际值', true, NULL),
('describe_table_stats',   '数据表统计',       '返回数据表数值字段统计、类别分布与缺失情况', true, NULL),
('guess_analysis_task',    '分析方向推荐',     '根据表/CSV 结构推断 3-5 条可能的数据分析方向', true, NULL),
('dispatch_agent',         '子智能体派发',     '把子任务派发给指定智能体执行并回收结果', true, NULL),
('read_file',              '读取文件',         '读取工作区内文件内容', true, NULL),
('write_file',             '写入文件',         '写入文件内容（不存在则创建，存在则覆盖）', true, NULL),
('edit_file',              '编辑文件',         '对文件执行精确字符串替换', true, NULL),
('list_directory',         '目录浏览',         '列出目录下的文件与子目录', true, NULL),
('delete_file',            '删除文件',         '删除工作区内的文件或空目录', true, NULL),
('find',                   '文件查找',         '按名称模式查找工作区文件', true, NULL),
('grep',                   '内容检索',         '按正则检索工作区文件内容', true, NULL),
('plot_chart',             '图表绘制',         '参数化绘制柱状/折线/饼图/散点图，输出 ECharts 配置与可选图片', true, NULL),
('generate_report',        '报告生成',         '将分析结论落盘为 Markdown 报告', true, NULL),
('search_tables',          '语义表检索',       '在语义层按自然语言检索候选表卡', true, NULL),
('describe_table_card',    '表卡片详情',       '读取语义层单表卡片的列、同义词与敏感字段', true, NULL),
('find_join_path',         '关联路径查询',     '查询两张表之间已登记的 JOIN 路径与置信度', true, NULL),
('search_metrics',         '指标检索',         '检索业务指标卡的口径、表达式与可下钻维度', true, NULL),
('find_verified_queries',  '已验证查询召回',   '召回与当前问题相似的历史已验证问答与 SQL', true, NULL),
('load_skill',             '技能加载',         '按名称加载 skill 全文到当前上下文', true, NULL),
('task_create',            '任务创建',         '在持久化任务看板创建任务并声明依赖', true, NULL),
('task_update',            '任务更新',         '更新任务状态、执行人或描述', true, NULL),
('task_get',               '任务查询',         '按 ID 获取单条任务记录', true, NULL),
('task_list',              '任务列表',         '列出当前请求看板上的全部任务', true, NULL)
ON CONFLICT (tool_code) DO NOTHING;

-- 平台技能全集（skill_code 与 skill/SKILL.md 的 name 保持一致）
INSERT INTO agent.skill (skill_code, skill_name, description, is_platform_enabled, remark) VALUES
('sql-analysis',         'SQL 分析',         '生成数据分析 SQL 语句，适用于 PostgreSQL 数据仓库查询、表关联与聚合统计', true, NULL),
('python-analysis',      'Python 数据分析',  '生成 pandas/numpy 数据分析代码，支持加载、清洗、聚合与统计检验', true, NULL),
('data-cleaning',        '数据清洗',         '数据清洗流程及方法', true, NULL),
('feature-engineering',  '特征工程',         '生成机器学习前的特征工程代码（缺失值、编码、标准化、特征构造）', true, NULL),
('modeling',             '机器学习建模',     '生成分类/回归/聚类/时序预测的建模、训练、评估与调优代码', true, NULL),
('plot',                 '数据可视化',       '生成数据可视化代码，支持柱状图、折线图、散点图、热力图等', true, NULL),
('report',               '分析报告生成',     '数据分析报告（Markdown）的编写要求与结构', true, NULL)
ON CONFLICT (skill_code) DO NOTHING;

-- skill ↔ tool 关联：由 skill 定义决定，初始化保持为空
-- INSERT INTO agent.skill_tool_rel (skill_code, tool_code) VALUES ...

-- 存量项目默认授权：平台已开放的工具/技能全部给这 5 个项目开一份授权（幂等）
--INSERT INTO agent.app_tool (app_id, tool_code, is_enabled)
--SELECT a.app_id, t.tool_code, true
--FROM (VALUES ('app_0001'), ('app_3hn0'), ('app_9tez'), ('app_fv8x'), ('app_okj3')) AS a(app_id)
--CROSS JOIN agent.tool t
--WHERE t.is_platform_enabled = true
--ON CONFLICT (app_id, tool_code) DO NOTHING;
--
--INSERT INTO agent.app_skill (app_id, skill_code, is_enabled)
--SELECT a.app_id, s.skill_code, true
--FROM (VALUES ('app_0001'), ('app_3hn0'), ('app_9tez'), ('app_fv8x'), ('app_okj3')) AS a(app_id)
--CROSS JOIN agent.skill s
--WHERE s.is_platform_enabled = true
--ON CONFLICT (app_id, skill_code) DO NOTHING;

-- 平台智能体（agent_code 与 config.json agents[].name / AgentSpec.name 对应）
INSERT INTO agent.agent (agent_code, agent_name, description) VALUES
('leader', 'Lead 编排智能体', 'lead agent：直接执行任务，编排派发子智能体，复杂任务先规划'),
('planner', '规划智能体', '复杂任务制定计划，需要多步执行的任务编排'),
('data_explore', '数据探索智能体', '根据数据库资源进行 SQL 数据分析'),
('csv_explore', 'CSV 分析智能体', '读取当前项目下的 csv 表格文件，完成数据分析'),
('reporter', '报告智能体', '规划图表并调用 plot_chart 逐张出图，生成数据分析报告')
ON CONFLICT (agent_code) DO NOTHING;
