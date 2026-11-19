#!/usr/bin/env python3
"""创建 / 校验 GeminiDB（DynamoDB 兼容接口）两张表：{namespace}_tenants、{namespace}_products。

用法（Git Bash）：
  export GEMINI_ENDPOINT="<GeminiDB 兼容接口地址>"
  export GEMINI_ACCESS_KEY="<实例级 access key>"
  export GEMINI_SECRET_KEY="<实例级 secret key>"
  export GEMINI_PORT="8000"            # 可选
  export GEMINI_PROTOCOL="http"        # 可选，http/https
  export GEMINI_REGION="cn-north-4"    # 可选
  python scripts/init_tables.py [--namespace mtsaas]

无 VPC 连通性的部署机可改走共享代理：设置 GEMINI_PROXY_URL + GEMINI_PROXY_TOKEN
后所有请求经公网 APIG 入口转发，GEMINI_ENDPOINT 可省略。
幂等：表已存在时跳过创建并等待 ACTIVE。
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tenant_admin  # noqa: E402
import catalog_api  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create GeminiDB tables for the multi-tenant sample")
    parser.add_argument("--namespace", default=os.environ.get("TABLE_NAMESPACE", "mtsaas"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,30}", args.namespace):
        parser.error("namespace must start with a letter and contain 3-31 letters, digits, or underscores")

    client = tenant_admin.GeminiClient.from_env()
    tenants = "%s_tenants" % args.namespace
    products = "%s_products" % args.namespace

    client.ensure_table(tenants, tenant_admin.TENANT_TABLE_DEF["key_schema"], tenant_admin.TENANT_TABLE_DEF["attribute_definitions"])
    print("[INIT] table ready: %s" % tenants)
    client.ensure_table(products, catalog_api.PRODUCT_TABLE_DEF["key_schema"], catalog_api.PRODUCT_TABLE_DEF["attribute_definitions"])
    print("[INIT] table ready: %s" % products)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
