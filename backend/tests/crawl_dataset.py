"""数据集语义层采集脚本：把 PG 库 catalog 落到 semantic schema。

用法示例：
  # 采集单个数据集（datasource 名默认等于库名）
  python tests/crawl_dataset.py --db tpcds_sf1

  # 指定数据源名与说明
  python tests/crawl_dataset.py --db business --description "DVD 租赁示例库"

  # 采集 shared.constants.PUBLIC_DATASETS 中配置的全部公共数据集
  python tests/crawl_dataset.py --all

  # 跳过向量化（仅采集结构化元数据）
  python tests/crawl_dataset.py --db tpcds_sf1 --no-embed
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codegenx.ai_service.semantic.catalog.pg_crawler import crawl
from shared.config.config import get_settings
from shared.constants import PUBLIC_DATASETS


def _dsn(database: str) -> str:
    """目标库 DSN：凭据统一取 .env 的 POSTGRES_*，仅库名可变。"""
    s = get_settings()
    return (
        f"postgresql://{quote_plus(s.postgres_user)}:{quote_plus(s.postgres_password)}"
        f"@{s.postgres_host}:{s.postgres_port}/{database}"
    )


async def _crawl_one(database: str, name: str, description: str, domain: str, embed: bool) -> None:
    print(f"\n>>> 采集 {database}（datasource={name}）...")
    summary = await crawl(name, _dsn(database), description=description, domain=domain, embed=embed)
    print("采集摘要:", summary)


async def _run(args: argparse.Namespace) -> int:
    if args.all:
        # PUBLIC_DATASETS 的 value 是 datasource.schema，datasource 部分即 PG 库名
        for item in PUBLIC_DATASETS:
            db = item["value"].split(".", 1)[0]
            await _crawl_one(db, db, item["label"], args.domain, not args.no_embed)
        return 0

    if not args.db:
        print("缺少 --db 或 --all", file=sys.stderr)
        return 2

    await _crawl_one(args.db, args.name or args.db, args.description, args.domain, not args.no_embed)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="采集 PG 数据集 catalog 到 semantic schema")
    parser.add_argument("--db", help="目标 PG 库名")
    parser.add_argument("--name", help="数据源名（语义层 meta_datasource.name，默认等于 --db）")
    parser.add_argument("--description", default="", help="数据源说明")
    parser.add_argument("--domain", default="", help="主题域（如 销售/会员）")
    parser.add_argument("--no-embed", action="store_true", help="跳过表卡向量化")
    parser.add_argument("--all", action="store_true", help="采集 PUBLIC_DATASETS 中全部公共数据集")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
