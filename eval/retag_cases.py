"""重打 eval 题目的 ops/grain/domain/multi_hop 标签。

根据 gold_sql 的实际语法结构推断，比人工打标更准确。
固化标签体系见 eval/tags_schema.md。

用法：
    python eval/retag_cases.py --dataset dvdrental
    python eval/retag_cases.py --dataset tpcds_sf1 --input eval/tpcds_sf1/tpcds_eval_sf1_final.json
    python eval/retag_cases.py --dataset all

输出：原地更新原文件（备份后缀 .bak）。
"""
import argparse, json, os, re, sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def classify_ops(sql: str) -> list[str]:
    """根据 gold_sql 推断 ops 标签（固化枚举，可多选）。"""
    ops = set()
    sql_lower = sql.lower()

    # ── 表结构 ──
    # 数 FROM + JOIN 里的表（排除 CTE 别名）
    # 先去掉 CTE 定义部分
    main_sql = re.sub(r'with\s+[\w_]+\s+as\s*\([^)]*\)\s*', '', sql_lower, flags=re.IGNORECASE | re.DOTALL)
    # 简单统计表引用（FROM/JOIN 后的表名）
    tables_in_sql = re.findall(
        r'(?:from|join)\s+(?:[a-zA-Z_][\w\.]*\s+(?:as\s+)?[a-zA-Z_][\w]*\s*(?:,|\(|join|where|group|order|limit|having|union|intersect|except|on|using|$))?',
        main_sql, flags=re.IGNORECASE
    )
    # 更准确的数法：FROM/JOIN 后跟的第一个标识符
    table_refs = re.findall(r'(?:from|join)\s+([a-zA-Z_][\w\.]*)', main_sql, flags=re.IGNORECASE)
    # 过滤 CTE 别名（CTE 名一般是短词且后面有 AS）
    cte_names = re.findall(r'with\s+([\w_]+)\s+as\s*\(', sql_lower, flags=re.IGNORECASE)
    real_tables = [t for t in table_refs if t not in cte_names and not t.startswith('(')]

    if len(real_tables) <= 1 and 'union' not in sql_lower and not re.search(r'from\s*\(', main_sql):
        ops.add('single_table')
    if len(real_tables) >= 2 or re.search(r'join\s', sql_lower, re.IGNORECASE) or \
       re.search(r'from\s+\w+\s*,\s*\w+', sql_lower) or \
       re.search(r'from\s*\(\s*select', main_sql, re.IGNORECASE):
        ops.add('multi_join')
    if re.search(r'\bunion\b', sql_lower):
        ops.add('union')

    # ── 查询结构 ──
    if re.search(r'\bwith\b.*\bas\s*\(', sql_lower, re.IGNORECASE | re.DOTALL):
        ops.add('cte')
    # 子查询：FROM/JOIN/WHERE 里嵌套 SELECT
    # 相关子查询：子查询里有外层表引用（简单启发：EXISTS 或子查询的 ON/WHERE 里出现子查询外面的表别名）
    if re.search(r'from\s*\(\s*select', sql_lower, re.IGNORECASE) or \
       re.search(r'join\s*\(\s*select', sql_lower, re.IGNORECASE) or \
       re.search(r'in\s*\(\s*select', sql_lower, re.IGNORECASE) or \
       re.search(r'exists\s*\(\s*select', sql_lower, re.IGNORECASE) or \
       re.search(r'[<>=!]+\s*\(\s*select', sql_lower, re.IGNORECASE):
        ops.add('subquery')
    if re.search(r'exists\s*\(\s*select', sql_lower, re.IGNORECASE):
        ops.add('correlated_subquery')
    # 窗口函数
    if re.search(r'\b(?:row_number|rank|dense_rank|percent_rank|cume_dist|ntile|lag|lead|first_value|last_value|nth_value)\s*\(', sql_lower, re.IGNORECASE) or \
       re.search(r'\b(?:sum|avg|count|min|max)\s*\([^)]*\)\s*over\s*\(', sql_lower, re.IGNORECASE) or \
       re.search(r'over\s*\(\s*(?:partition|order)', sql_lower, re.IGNORECASE):
        ops.add('window_function')
    if re.search(r'\bcase\b.*\bwhen\b', sql_lower, re.IGNORECASE | re.DOTALL):
        ops.add('case_when')
    if re.search(r'\bgrouping\s+sets\b|\bcube\s*\(|\brollup\s*\(', sql_lower, re.IGNORECASE):
        ops.add('grouping_set')

    # ── 过滤 ──
    if re.search(
        r'(?:where|having).*?(?:between\s+[\'"\d]|>\s*[\'"\d]{4}|[<>=]+\s*\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})',
        sql_lower, re.IGNORECASE | re.DOTALL
    ) or re.search(r'extract\s*\(\s*(?:year|month|day|quarter)', sql_lower, re.IGNORECASE):
        ops.add('date_filter')
    if re.search(r'is\s+(?:not\s+)?null', sql_lower, re.IGNORECASE):
        ops.add('filter_null')

    # ── 聚合结构 ──
    if re.search(r'\bgroup\s+by\b', sql_lower, re.IGNORECASE):
        ops.add('group_by')
    if re.search(r'\bhaving\b', sql_lower, re.IGNORECASE):
        ops.add('having')
    if re.search(r'\border\s+by\b', sql_lower, re.IGNORECASE):
        ops.add('order_by')
    if re.search(r'\blimit\b|\bfetch\s+first|\btop\b', sql_lower, re.IGNORECASE):
        ops.add('limit')

    # ── 聚合函数 ──
    has_sum = bool(re.search(r'\bsum\s*\(', sql_lower, re.IGNORECASE))
    has_count = bool(re.search(r'\bcount\s*\(', sql_lower, re.IGNORECASE))
    has_avg = bool(re.search(r'\bavg\s*\(', sql_lower, re.IGNORECASE))
    has_max_min = bool(re.search(r'\b(?:max|min)\s*\(', sql_lower, re.IGNORECASE))
    if has_sum or has_count or has_avg or has_max_min:
        ops.add('aggregate')
    if has_sum:
        ops.add('sum')
    if has_count:
        ops.add('count')
    if has_avg:
        ops.add('avg')
    if re.search(r'count\s*\(\s*distinct\b|\bselect\s+distinct\b', sql_lower, re.IGNORECASE):
        ops.add('distinct')
    # 比率计算：除法 a / b
    # 启发式：sum(x) / sum(y), a.amt / b.avg, ... over() / ... over() 等
    if re.search(r'\bsum\s*\([^)]+\)\s*/\s*\(?\s*(?:sum|avg|count|max|min)\s*\(', sql_lower, re.IGNORECASE) or \
       re.search(r'/\s*avg\s*\([^)]+\)\s*\)', sql_lower, re.IGNORECASE) or \
       re.search(r'amount\s*/\s*avg\b|revenue\s*/\s*sum\b|total\s*/\s*avg\b', sql_lower, re.IGNORECASE):
        ops.add('ratio_calc')

    return sorted(ops)


def classify_grain(sql: str, ops: list[str]) -> str:
    """根据 sql 和 ops 推断 grain（固化枚举）。"""
    sql_lower = sql.lower()
    ops_set = set(ops)

    has_group = 'group_by' in ops_set
    has_limit = 'limit' in ops_set
    has_order = 'order_by' in ops_set
    has_count_distinct = bool(re.search(r'count\s*\(\s*distinct', sql_lower, re.IGNORECASE))
    has_distinct_select = bool(re.search(r'\bselect\s+distinct\b', sql_lower, re.IGNORECASE))
    is_single_agg = not has_group and bool(
        re.search(r'\b(count|sum|avg|max|min)\s*\(', sql_lower, re.IGNORECASE)
    )

    # 优先级
    if has_count_distinct and not has_group:
        return 'distinct_count'
    if is_single_agg:
        return 'single_value'
    if has_limit and has_order and has_group:
        return 'top_n'
    if has_group:
        return 'aggregate'
    if has_limit and has_order:
        return 'top_n'
    return 'detail'


def count_multi_hop(sql: str) -> int:
    """推断 multi_hop：最大 JOIN 链条深度（简单启发）。"""
    sql_lower = sql.lower()
    # 统计 JOIN 关键字数量（隐式逗号 JOIN 也算）
    explicit_join = len(re.findall(r'\bjoin\b', sql_lower, re.IGNORECASE))
    # 隐式 JOIN：FROM a, b, c → 逗号分隔的表数 - 1
    from_match = re.search(r'\bfrom\b([^;]*?)(?:\bwhere\b|\bgroup\b|\border\b|\blimit\b|\bhaving\b|\bunion\b|$)',
                           sql_lower, re.IGNORECASE | re.DOTALL)
    implicit_join = 0
    if from_match:
        from_clause = from_match.group(1)
        # 简单数逗号分隔的表名（排除嵌套子查询的括号内）
        # 去掉括号内容
        flat = re.sub(r'\([^()]*\)', '', from_clause)
        # 数顶层逗号
        top_level_commas = len(re.findall(r',', flat))
        implicit_join = top_level_commas

    # 包含子查询的情况：子查询里也可能有 JOIN
    # 简单取显式+隐式
    return explicit_join + implicit_join


def infer_domain(sql: str, existing_domain: str = '') -> str:
    """推断 domain：取 gold_sql 里 FROM/JOIN 出现的第一张业务表名（简化处理）。"""
    if existing_domain and existing_domain != '?':
        return existing_domain  # 保留已有正确值

    table_refs = re.findall(r'(?:from|join)\s+([a-zA-Z_][\w]*)', sql, re.IGNORECASE)
    cte_names = set(re.findall(r'with\s+([\w_]+)\s+as\s*\(', sql, re.IGNORECASE))
    for t in table_refs:
        t_lower = t.lower()
        if t_lower not in cte_names and t_lower not in (
            'select', 'on', 'using', 'as', 'where', 'group', 'order', 'limit', 'having', 'union'
        ):
            return t_lower
    return existing_domain or '?'


def retag_dataset(filepath: str, name: str):
    """重打一个数据集的全部标签。"""
    print(f"\n{'='*60}")
    print(f"处理数据集: {name} ({filepath})")

    with open(filepath, encoding='utf-8') as f:
        data = json.load(f)

    ops_counter = Counter()
    grain_counter = Counter()
    changed = 0

    for q in data:
        sql = q.get('sql', q.get('gold_sql', ''))
        if not sql:
            continue

        new_ops = classify_ops(sql)
        new_grain = classify_grain(sql, new_ops)
        new_mh = count_multi_hop(sql)
        new_domain = infer_domain(sql, q.get('domain', '?'))

        old_ops = sorted(q.get('ops', []))
        old_grain = q.get('grain', '')
        old_mh = q.get('multi_hop', '')
        old_domain = q.get('domain', '?')

        if old_ops != new_ops or old_grain != new_grain or old_mh != new_mh or old_domain != new_domain:
            changed += 1
            q['ops'] = new_ops
            q['grain'] = new_grain
            q['multi_hop'] = new_mh
            q['domain'] = new_domain

        for op in new_ops:
            ops_counter[op] += 1
        grain_counter[new_grain] += 1

    print(f"共 {len(data)} 题，标签变化 {changed} 题")
    print(f"\nops 分布:")
    for op, cnt in ops_counter.most_common():
        print(f"  {op:30s} ×{cnt}")
    print(f"\ngrain 分布: {dict(grain_counter)}")

    # 写回（备份原文件）
    bak = filepath + '.bak'
    if not os.path.exists(bak):
        import shutil
        shutil.copy2(filepath, bak)
        print(f"\n原文件已备份到: {bak}")

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已写回: {filepath}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', required=True, choices=['dvdrental', 'tpcds_sf1', 'all'])
    parser.add_argument('--input', help='tpcds 指定输入文件（不指定用 final.json）')
    args = parser.parse_args()

    if args.dataset in ('dvdrental', 'all'):
        retag_dataset(os.path.join(ROOT, 'eval', 'dvdrental', 'question.json'), 'dvdrental')

    if args.dataset in ('tpcds_sf1', 'all'):
        tpcds_file = args.input or os.path.join(ROOT, 'eval', 'tpcds_sf1', 'tpcds_eval_sf1_final.json')
        retag_dataset(tpcds_file, 'tpcds_sf1')


if __name__ == '__main__':
    main()
