#!/usr/bin/env python3
"""验证两租户注册、隔离 CRUD、停用、配额 429 和生命周期审计。

默认运行纯本地模式，不访问云端：
    python scripts/verify_e2e.py

部署后的云端验证默认只跑安全探针（只读 / 健康检查类断言：
读端点、鉴权拒绝、404 探测），不创建任何租户或 appcode，
结束时不在共享实例上遗留任何东西：
    python scripts/verify_e2e.py --cloud

完整租户开通流程（约消耗 55 次函数调用配额，会创建 2 个租户
与对应 appcode 并保留在共享资源上）仅在显式 --full 时执行：
    export HW_ACCESS_KEY=...
    export HW_SECRET_KEY=...
    python scripts/verify_e2e.py --cloud --full

无论成功或失败，结果都写入项目根目录 verify-result.json。
"""

import argparse
import base64
import copy
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
RESULT_FILE = ROOT / "verify-result.json"
sys.path.insert(0, str(SRC))

import catalog_api  # noqa: E402
import events_audit  # noqa: E402
import tenant_admin  # noqa: E402


class VerificationError(RuntimeError):
    pass


class FakeContext:
    def __init__(self):
        self.counter = 0

    def getToken(self):
        return "local-token"

    def getRequestID(self):
        self.counter += 1
        return "local-request-%04d" % self.counter


class MemoryDb:
    def __init__(self):
        self.tables = {}

    @staticmethod
    def _key(item):
        if "product_id" in item:
            return item["tenant_key"], item["product_id"]
        return (item["tenant_key"],)

    def put_item(self, table, item):
        self.tables.setdefault(table, {})[self._key(item)] = copy.deepcopy(item)
        return {}

    def get_item(self, table, key):
        item = self.tables.get(table, {}).get(self._key(key))
        return copy.deepcopy(item) if item else None

    def delete_item(self, table, key):
        self.tables.get(table, {}).pop(self._key(key), None)
        return {}

    def scan_items(self, table):
        return [copy.deepcopy(item) for item in self.tables.get(table, {}).values()]

    def query_items(self, table, tenant_key):
        return [
            copy.deepcopy(item)
            for item in self.tables.get(table, {}).values()
            if item.get("tenant_key") == tenant_key
        ]


class LocalGateway:
    def __init__(self, context, quota_limits):
        self.context = context
        self.quota_limits = quota_limits
        self.usage = {}

    @staticmethod
    def event(method, path, payload=None, appcode=None, path_parameters=None, query=None):
        body = ""
        encoded = False
        if payload is not None:
            body = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
            encoded = True
        headers = {"X-Apig-AppCode": appcode} if appcode else {}
        return {
            "httpMethod": method,
            "path": path,
            "pathParameters": path_parameters or {},
            "queryStringParameters": query,
            "headers": headers,
            "isBase64Encoded": encoded,
            "body": body,
        }

    def control(self, method, path, payload=None, path_parameters=None, query=None):
        return parse_function_response(
            tenant_admin.handler(self.event(method, path, payload, path_parameters=path_parameters, query=query), self.context)
        )

    def catalog(self, method, path, appcode, payload=None, path_parameters=None):
        limit = self.quota_limits[appcode]
        used = self.usage.get(appcode, 0)
        if used >= limit:
            return 429, {"error": "APIG application quota exceeded"}
        self.usage[appcode] = used + 1
        return parse_function_response(
            catalog_api.handler(self.event(method, path, payload, appcode, path_parameters), self.context)
        )


def parse_function_response(response):
    if not isinstance(response, dict) or "statusCode" not in response:
        raise VerificationError("函数未返回标准响应结构")
    required = {"statusCode", "headers", "isBase64Encoded", "body"}
    if not required.issubset(response):
        raise VerificationError("函数响应缺少字段: %s" % sorted(required - set(response)))
    body = response.get("body") or "{}"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = body
    return int(response["statusCode"]), payload


def expect(condition, message):
    if not condition:
        raise VerificationError(message)


def local_verify():
    os.environ.update(
        {
            "TENANT_TABLE": "local_tenants",
            "PRODUCT_TABLE": "local_products",
            "REGION": "cn-north-4",
            "PROJECT_ID": "local-project",
            "APIG_INSTANCE_ID": "local-instance",
            "APIG_ENV_ID": "local-env",
            "CATALOG_API_IDS": json.dumps(["products", "product"]),
            "BASIC_QUOTA_ID": "local-basic",
            "STANDARD_QUOTA_ID": "local-standard",
            "EG_CHANNEL_ID": "local-channel",
            "EG_SOURCE_NAME": "mtsaas-tenant-lifecycle",
        }
    )
    db = MemoryDb()
    context = FakeContext()
    lifecycle_events = []
    app_counter = {"value": 0}

    def fake_api_request(method, path, payload=None, token=""):
        del payload, token
        if method == "POST" and path == "/apps":
            app_counter["value"] += 1
            return {"id": "local-app-%d" % app_counter["value"]}
        return {}

    def fake_eg_publish(_context, event_type, subject, data):
        event_id = uuid.uuid4().hex
        lifecycle_events.append(
            {
                "id": event_id,
                "source": os.environ["EG_SOURCE_NAME"],
                "specversion": "1.0",
                "type": event_type,
                "subject": subject,
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "datacontenttype": "application/json",
                "data": copy.deepcopy(data),
            }
        )
        return event_id

    patches = (
        mock.patch.object(tenant_admin.GeminiClient, "from_env", return_value=db),
        mock.patch.object(catalog_api.GeminiClient, "from_env", return_value=db),
        mock.patch.object(tenant_admin, "_api_request", side_effect=fake_api_request),
        mock.patch.object(tenant_admin, "_eg_publish", side_effect=fake_eg_publish),
    )
    for patch in patches:
        patch.start()
    try:
        gateway = LocalGateway(context, {})
        status, tenant_one = gateway.control(
            "POST", "/tenants", {"tenant_name": "Tenant One", "email": "one@example.test", "tier": "basic"}
        )
        expect(status == 201, "租户一注册失败")
        status, tenant_two = gateway.control(
            "POST", "/tenants", {"tenant_name": "Tenant Two", "email": "two@example.test", "tier": "standard"}
        )
        expect(status == 201, "租户二注册失败")
        expect(tenant_one["tenant_id"] != tenant_two["tenant_id"], "租户 ID 不唯一")
        gateway.quota_limits = {tenant_one["appcode"]: 9, tenant_two["appcode"]: 20}

        status, active_basic = gateway.control("GET", "/tenants", query={"status": "Active", "tier": "basic"})
        expect(status == 200 and [item["tenant_id"] for item in active_basic] == [tenant_one["tenant_id"]], "租户查询过滤失败")

        status, product_one = gateway.catalog(
            "POST", "/products", tenant_one["appcode"], {"sku": "ONE-1", "name": "One", "category": "demo", "price": 10}
        )
        expect(status == 201 and product_one["tenant_key"].startswith("TENANT#"), "租户一商品创建失败或键无前缀")
        status, product_two = gateway.catalog(
            "POST", "/products", tenant_two["appcode"], {"sku": "TWO-1", "name": "Two", "category": "demo", "price": 20}
        )
        expect(status == 201, "租户二商品创建失败")

        status, list_one = gateway.catalog("GET", "/products", tenant_one["appcode"])
        expect(status == 200 and [item["product_id"] for item in list_one] == [product_one["product_id"]], "租户一列表发生串租")
        status, list_two = gateway.catalog("GET", "/products", tenant_two["appcode"])
        expect(status == 200 and [item["product_id"] for item in list_two] == [product_two["product_id"]], "租户二列表发生串租")

        cross_path = "/products/%s" % product_one["product_id"]
        status, _ = gateway.catalog("GET", cross_path, tenant_two["appcode"], path_parameters={"product_id": product_one["product_id"]})
        expect(status == 404, "跨租户读取未被隔离")

        status, updated = gateway.catalog(
            "PUT",
            cross_path,
            tenant_one["appcode"],
            {"name": "One Updated", "price": 11, "tenant_id": tenant_two["tenant_id"]},
            {"product_id": product_one["product_id"]},
        )
        expect(status == 200 and updated["tenant_id"] == tenant_one["tenant_id"] and updated["name"] == "One Updated", "商品更新或身份防伪失败")

        product_two_path = "/products/%s" % product_two["product_id"]
        status, deleted = gateway.catalog(
            "DELETE", product_two_path, tenant_two["appcode"], path_parameters={"product_id": product_two["product_id"]}
        )
        expect(status == 200 and deleted.get("deleted") is True, "商品删除失败")

        status, stopped = gateway.control(
            "PUT",
            "/tenants/%s/status" % tenant_two["tenant_id"],
            {"status": "Inactive"},
            {"tenant_id": tenant_two["tenant_id"]},
        )
        expect(status == 200 and stopped["status"] == "Inactive", "租户停用失败")
        status, _ = gateway.catalog("GET", "/products", tenant_two["appcode"])
        expect(status == 403, "停用租户仍可访问目录")

        quota_status = None
        for _ in range(20):
            quota_status, _ = gateway.catalog("GET", "/products", tenant_one["appcode"])
            if quota_status == 429:
                break
        expect(quota_status == 429, "未观察到配额 HTTP 429")

        audit_output = io.StringIO()
        with redirect_stdout(audit_output):
            for event in lifecycle_events:
                audit_response = events_audit.handler(event, context)
                audit_status, audit_payload = parse_function_response(audit_response)
                expect(audit_status == 200 and audit_payload.get("audited") is True, "审计函数响应失败")
        audit_text = audit_output.getvalue()
        expect(tenant_one["tenant_id"] in audit_text and tenant_two["tenant_id"] in audit_text, "审计日志未覆盖两个租户")
        expect(any(event["type"] == "tenant.status_changed" for event in lifecycle_events), "缺少租户状态变更事件")

        return {
            "mode": "local",
            "passed": True,
            "checks": {
                "tenant_registration": True,
                "tenant_query": True,
                "isolated_crud": True,
                "inactive_denied": True,
                "quota_429": True,
                "lts_audit_contract": True,
            },
            "evidence": {
                "tenants": 2,
                "lifecycle_events": len(lifecycle_events),
                "tenant_key_prefix": "TENANT#",
                "quota_status": quota_status,
            },
        }
    finally:
        for patch in reversed(patches):
            patch.stop()


def terraform_outputs():
    candidates = [
        shutil.which("terraform"),
        shutil.which("terraform.exe"),
        "C:/Users/Administrator/AppData/Local/Temp/terraform.exe",
    ]
    terraform = next((item for item in candidates if item and Path(item).is_file()), None)
    if not terraform:
        raise VerificationError("未找到 terraform 可执行文件")
    result = subprocess.run([terraform, "output", "-json"], cwd=ROOT / "infra", capture_output=True, check=False)
    if result.returncode:
        raise VerificationError("terraform output 失败: %s" % result.stderr.decode("utf-8", "replace")[:300])
    raw = json.loads(result.stdout.decode("utf-8", "replace"))
    return {name: item["value"] for name, item in raw.items()}


def cloud_api_call(base_url, stage, appcode, method, path, payload=None):
    headers = {"X-Stage": stage, "Content-Type": "application/json"}
    if appcode:
        headers["X-Apig-AppCode"] = appcode
    response = requests.request(method, base_url.rstrip("/") + path, headers=headers, json=payload, timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


def canonical_uri(path):
    segments = [urllib.parse.quote(urllib.parse.unquote(part), safe="-_.~") for part in path.split("/")]
    value = "/".join(segments) or "/"
    if not value.startswith("/"):
        value = "/" + value
    if not value.endswith("/"):
        value += "/"
    return value


def signed_headers(method, url, body, access_key, secret_key):
    parsed = urllib.parse.urlsplit(url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "content-type": "application/json",
        "host": parsed.netloc,
        "x-sdk-date": timestamp,
    }
    canonical_headers = "".join("%s:%s\n" % (name, headers[name].strip()) for name in sorted(headers))
    names = ";".join(sorted(headers))
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join([method, canonical_uri(parsed.path), query, canonical_headers, names, payload_hash])
    string_to_sign = "SDK-HMAC-SHA256\n%s\n%s" % (
        timestamp,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    )
    signature = hmac.new(secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "Host": parsed.netloc,
        "X-Sdk-Date": timestamp,
        "Authorization": "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s" % (access_key, names, signature),
    }


def query_audit_logs(outputs, access_key, secret_key, tenant_ids, timeout=300):
    region = outputs["region"]
    project_id = outputs["project_id"]
    group_id = outputs["log_group_id"]
    stream_id = outputs["events_audit_log_stream_id"]
    url = "https://lts.%s.myhuaweicloud.com/v2/%s/groups/%s/streams/%s/content/query" % (
        region,
        project_id,
        group_id,
        stream_id,
    )
    start_ms = int(time.time() * 1000) - 15 * 60 * 1000
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = {
            "start_time": str(start_ms),
            "end_time": str(int(time.time() * 1000)),
            "limit": 500,
            "is_desc": True,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = requests.post(url, headers=signed_headers("POST", url, raw, access_key, secret_key), data=raw, timeout=30)
        if response.status_code < 300:
            result = response.json()
            records = result.get("logs") or []
            contents = "\n".join(str(record.get("content", "")) for record in records if isinstance(record, dict))
            if all(tenant_id in contents for tenant_id in tenant_ids) and "tenant.status_changed" in contents:
                return len(records)
        time.sleep(10)
    raise VerificationError("LTS 审计日志在超时前未覆盖两个租户和状态变更事件")


def cloud_verify():
    access_key = os.environ.get("HW_ACCESS_KEY", "")
    secret_key = os.environ.get("HW_SECRET_KEY", "")
    if not access_key or not secret_key:
        raise VerificationError("云端模式需要 HW_ACCESS_KEY/HW_SECRET_KEY")
    outputs = terraform_outputs()
    required = {
        "api_base_url",
        "api_stage",
        "admin_appcode",
        "basic_quota_limit",
        "region",
        "project_id",
        "log_group_id",
        "events_audit_log_stream_id",
    }
    missing = required - set(outputs)
    if missing:
        raise VerificationError("Terraform output 缺少: %s" % sorted(missing))
    base_url = outputs["api_base_url"]
    stage = outputs["api_stage"]
    admin_code = outputs["admin_appcode"]

    status, tenant_one = cloud_api_call(
        base_url, stage, admin_code, "POST", "/tenants", {"tenant_name": "Verify Tenant One", "email": "verify-one@example.test", "tier": "basic"}
    )
    expect(status == 201, "云端租户一注册失败: HTTP %s" % status)
    status, tenant_two = cloud_api_call(
        base_url, stage, admin_code, "POST", "/tenants", {"tenant_name": "Verify Tenant Two", "email": "verify-two@example.test", "tier": "standard"}
    )
    expect(status == 201, "云端租户二注册失败: HTTP %s" % status)

    status, product_one = cloud_api_call(
        base_url, stage, tenant_one["appcode"], "POST", "/products", {"sku": "VERIFY-ONE", "name": "One", "category": "verify", "price": 10}
    )
    expect(status == 201, "云端租户一商品创建失败")
    status, product_two = cloud_api_call(
        base_url, stage, tenant_two["appcode"], "POST", "/products", {"sku": "VERIFY-TWO", "name": "Two", "category": "verify", "price": 20}
    )
    expect(status == 201, "云端租户二商品创建失败")

    status, list_one = cloud_api_call(base_url, stage, tenant_one["appcode"], "GET", "/products")
    expect(status == 200 and {item["product_id"] for item in list_one} == {product_one["product_id"]}, "云端租户一列表串租")
    status, list_two = cloud_api_call(base_url, stage, tenant_two["appcode"], "GET", "/products")
    expect(status == 200 and {item["product_id"] for item in list_two} == {product_two["product_id"]}, "云端租户二列表串租")

    path_one = "/products/%s" % product_one["product_id"]
    status, _ = cloud_api_call(base_url, stage, tenant_two["appcode"], "GET", path_one)
    expect(status == 404, "云端跨租户读取未被隔离")
    status, updated = cloud_api_call(base_url, stage, tenant_one["appcode"], "PUT", path_one, {"name": "One Updated", "price": 11})
    expect(status == 200 and updated.get("name") == "One Updated", "云端商品更新失败")
    path_two = "/products/%s" % product_two["product_id"]
    status, deleted = cloud_api_call(base_url, stage, tenant_two["appcode"], "DELETE", path_two)
    expect(status == 200 and deleted.get("deleted") is True, "云端商品删除失败")

    status, stopped = cloud_api_call(
        base_url,
        stage,
        admin_code,
        "PUT",
        "/tenants/%s/status" % tenant_two["tenant_id"],
        {"status": "Inactive"},
    )
    expect(status == 200 and stopped.get("status") == "Inactive", "云端租户停用失败")
    status, _ = cloud_api_call(base_url, stage, tenant_two["appcode"], "GET", "/products")
    expect(status == 403, "云端停用租户仍可访问")

    quota_status = None
    for _ in range(int(outputs["basic_quota_limit"]) + 5):
        quota_status, _ = cloud_api_call(base_url, stage, tenant_one["appcode"], "GET", "/products")
        if quota_status == 429:
            break
    expect(quota_status == 429, "云端未观察到配额 HTTP 429")

    log_records = query_audit_logs(outputs, access_key, secret_key, [tenant_one["tenant_id"], tenant_two["tenant_id"]])
    return {
        "mode": "cloud-full",
        "passed": True,
        "checks": {
            "tenant_registration": True,
            "isolated_crud": True,
            "inactive_denied": True,
            "quota_429": True,
            "lts_audit": True,
        },
        "evidence": {"tenants": 2, "quota_status": quota_status, "audit_records_scanned": log_records},
    }


def cloud_probe():
    """云端安全探针：只读 / 健康检查类断言，不创建租户或 appcode。"""
    outputs = terraform_outputs()
    required = {"api_base_url", "api_stage", "admin_appcode"}
    missing = required - set(outputs)
    if missing:
        raise VerificationError("Terraform output 缺少: %s" % sorted(missing))
    base_url = outputs["api_base_url"]
    stage = outputs["api_stage"]
    admin_code = outputs["admin_appcode"]

    checks = {}

    # 1. 控制面读端点（管理端凭据）：200 + JSON 数组
    status, body = cloud_api_call(base_url, stage, admin_code, "GET", "/tenants")
    checks["control_plane_read"] = status == 200 and isinstance(body, list)

    # 2. 控制面无凭据：APP 认证拒绝
    status, _ = cloud_api_call(base_url, stage, "", "GET", "/tenants")
    checks["control_requires_appcode"] = status in (401, 403)

    # 3. 不存在的租户详情：404（只读探测）
    status, _ = cloud_api_call(base_url, stage, admin_code, "GET", "/tenants/%s" % uuid.uuid4().hex)
    checks["missing_tenant_404"] = status == 404

    # 4. 目录面无凭据：拒绝
    status, _ = cloud_api_call(base_url, stage, "", "GET", "/products")
    checks["catalog_requires_appcode"] = status in (401, 403)

    # 5. 目录面持管理端凭据：网关未授权 / 函数层非租户凭据，均须拒绝
    status, _ = cloud_api_call(base_url, stage, admin_code, "GET", "/products")
    checks["catalog_denied_for_admin"] = status in (401, 403)

    return {
        "mode": "cloud-probe",
        "passed": all(checks.values()),
        "checks": checks,
        "evidence": {
            "created_tenants": 0,
            "created_appcodes": 0,
            "note": "read-only probes; nothing left on the shared instance",
        },
    }


def write_result(result, path):
    result["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Verify multi-tenant control and catalog behavior")
    parser.add_argument("--cloud", action="store_true",
                        help="云端验证：默认只跑只读安全探针，不创建任何租户或 appcode")
    parser.add_argument("--full", action="store_true",
                        help="与 --cloud 联用：完整租户开通流程（消耗函数配额并创建租户与 appcode，默认不跑）")
    parser.add_argument("--output", default=str(RESULT_FILE), help="验证结果 JSON 路径")
    args = parser.parse_args()
    if args.full and not args.cloud:
        parser.error("--full 需要与 --cloud 一起使用")
    if args.full:
        mode = "cloud-full"
    elif args.cloud:
        mode = "cloud-probe"
    else:
        mode = "local"
    output = Path(args.output).resolve()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if mode == "cloud-full":
            result = cloud_verify()
        elif mode == "cloud-probe":
            result = cloud_probe()
        else:
            result = local_verify()
        result["started_at"] = started
        write_result(result, output)
        print("[PASS] %s verification completed" % result["mode"])
        print("[PASS] result: %s" % output)
        return 0
    except Exception as exc:  # noqa: BLE001 - 必须落失败 JSON
        result = {
            "mode": mode,
            "passed": False,
            "started_at": started,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
        write_result(result, output)
        print("[FAIL] %s" % result["error"], file=sys.stderr)
        print("[FAIL] result: %s" % output, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
