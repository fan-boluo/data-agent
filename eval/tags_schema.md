# 评测标签体系（固化版）

> 所有评测题目（dvdrental / tpcds_sf1 / 后续新增）的标签必须从下方枚举中取值，不得自定义。
> 判定标准不统一的地方，以「执行语义」而非「SQL 语法形态」为准。

---

## 1. difficulty

直接沿用，不用固化。

| 值 | 含义 | 判定标准 |
|----|------|---------|
| easy | 单表基础查询 | 无 JOIN，单粒度过滤/聚合/排序 |
| medium | 多表或单表中等复杂度 | 多表 JOIN（≤2）、GROUP BY+HAVING、TOP-N、日期区间 |
| hard | 复杂多表 / 高级语法 | 多表 JOIN（≥3）、CTE+子查询、窗口函数、相关子查询、GROUPING SETS |

---

## 2. ops（操作标签）

**按执行语义分类，每条题目标 ops 时可多选。

### 2.1 表结构操作

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `single_table` | 单表操作 | SQL 只引用 1 张表 |
| `multi_join` | 多表 JOIN | SQL 引用 ≥2 张表（不管用 JOIN 还是逗号隐式连接）。|
| `union` | 集合操作 | UNION / UNION ALL / INTERSECT / EXCEPT |

### 2.2 查询结构操作

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `cte` | 公共表表达式 | WITH ... AS (SELECT ...) |
| `subquery` | 非相关子查询 | WHERE col IN (SELECT...) / 标量子查询 / FROM 子查询，**子查询结果不依赖外层** |
| `correlated_subquery` | 相关子查询 | EXISTS / WHERE col > (SELECT ... WHERE outer.col = inner.col)，**子查询引用外层变量** |
| `window_function` | 窗口函数 | ROW_NUMBER / RANK / SUM(...) OVER(...) / PERCENTILE_CONT OVER() 等。 |
| `case_when` | 条件表达式 | CASE WHEN ... THEN ... ELSE ... END |
| `grouping_set` | 分组扩展 | GROUPING SETS / CUBE / ROLLUP |

### 2.3 过滤操作

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `date_filter` | 日期过滤 | WHERE date_col BETWEEN ... / date_col >= '...' AND date_col < '...' / EXTRACT(YEAR FROM date_col) = ... |
| `filter_null` | NULL 值处理 | WHERE col IS NULL / IS NOT NULL |

### 2.4 聚合结构

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `group_by` | 分组聚合 | GROUP BY 子句（和 aggregate 函数搭配使用） |
| `having` | 聚合结果过滤 | HAVING 子句（对 GROUP BY 后的聚合结果再过滤） |
| `order_by` | 排序 | ORDER BY 子句 |
| `limit` | Top-N 截断 | LIMIT / FETCH FIRST / TOP（只要有截断语义就打，不限定是排序后的 TOP-N） |

### 2.5 聚合函数（执行语义，有就打）

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `aggregate` | 存在聚合函数 | SELECT 列里出现任一聚合函数（SUM / COUNT / AVG / MAX / MIN），**这是一个"存在性"标记** |
| `sum` | 求和函数 | SUM(...) |
| `count` | 计数函数 | COUNT(*) / COUNT(col) / COUNT(DISTINCT col) |
| `avg` | 平均函数 | AVG(...) |
| `distinct` | 去重语义 | DISTINCT 关键字（SELECT DISTINCT / COUNT DISTINCT） |
| `ratio_calc` | 比率/比例计算 | a / b 形式的除法（比如 amount / avg(amount)、revenue / sum(revenue) over...） |

---

## 3. grain（结果粒度）

| 值 | 含义 | 判定标准 |
|----|------|---------|
| `detail` | 明细行输出 | 输出结果每行是原始表级别的数据（无 GROUP BY） |
| `aggregate` | 聚合行输出 | 输出结果每行是 GROUP BY 分组后的汇总。**原 dvdrental 的 `aggregate` / tpcds 的 `agg` 统一为此值** |
| `single_value` | 单值输出 | 输出只有一行一列（无 GROUP BY 的聚合，如 SELECT COUNT(*)） |
| `top_n` | 排名截断 | ORDER BY + LIMIT 取前 N 行（聚合后按指标排序的前 N） |
| `distinct_count` | 去重计数 | 只有 COUNT(DISTINCT col) 语义的输出 |

**判定优先级**：`single_value` > `top_n` > `distinct_count` > `aggregate` > `detail`
- 无 GROUP BY 且只有单值 → `single_value`
- 有 LIMIT + ORDER BY + 聚合 → `top_n`
- COUNT DISTINCT 无 GROUP BY → `distinct_count`
- 有 GROUP BY → `aggregate`
- 其他 → `detail`

---

## 4. multi_hop（跨表跳数）

**直接是数字，保留。** 指 SQL 中出现的 JOIN 链条长度（最大的表路径深度）。

| 值 | 含义 |
|----|------|
| 0 | 单表查询 |
| 1 | 1 层 JOIN（A JOIN B） |
| 2 | 2 层 JOIN（A JOIN B JOIN C） |
| 3+ | 3 层或以上 |

---

## 5. domain（业务域）

**自由填：题目的核心业务域（数据模型层面的"哪个业务场景"）。** 可选值建议：
- dvdrental：actor / film / customer / rental / payment / staff / category / city / country / address / inventory / store 等
- tpcds_sf1：customer / store / warehouse / item / promotion / reason / web_site / call_center / date / store_sales / store_returns / catalog_sales / catalog_returns / web_sales / web_returns / inventory 等
