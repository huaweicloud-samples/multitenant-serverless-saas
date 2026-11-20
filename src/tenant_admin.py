# -*- coding: utf-8 -*-
"""多租户 SaaS 参考实现：控制面（租户注册/列表/查询/停用）。

职责：
1. 租户生命周期 API（经 APIG 专享实例，APP 认证 + appcode 简单认证）：
   - POST   /tenants                 注册租户（tier：basic / standard）
   - GET    /tenants                 租户列表
   - GET    /tenants/{tenant_id}     租户详情（含凭据 appcode）
   - PUT    /tenants/{tenant_id}/status  停用 / 启用（body：{"status":"Inactive"|"Active"}）
2. 注册时在函数内顺序编排租户入驻：
   写租户表 -> 创建 APIG 应用 -> 创建 appcode -> 绑定档位用量配额 ->
   授权目录 API -> 置为 Active -> 发布生命周期事件。
   任一步失败则把租户置为 Failed 并返回失败步骤。
3. 生命周期事件写入事件网格自定义通道（租户入驻 / 状态变更），由审计函数落日志。

依赖：仅 Python 标准库 + requests（运行时内置）。
数据面：GeminiDB DynamoDB 兼容接口，请求以四段式派生凭据签名，实例级凭据由运行期配置提供。
云面：以函数委托注入的令牌（context.getToken）调用 APIG / 事件网格管理接口。
"""

# Capacity mode used when a table is created: on demand, no provisioned throughput.
CAPACITY_MODE = "PAY_PER_REQUEST"

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import requests

TIER_QUOTA_ENV = {"basic": "BASIC_QUOTA_ID", "standard": "STANDARD_QUOTA_ID"}


# ---------------------------------------------------------------------------
# GeminiDB（DynamoDB 兼容接口）客户端：SigV4 签名，仅标准库
# ---------------------------------------------------------------------------
class GeminiError(RuntimeError):
    def __init__(self, operation, status, code, message=""):
        self.operation = operation
        self.status = status
        self.code = code
        self.message = message
        detail = " (%s)" % message if message else ""
        super().__init__("GeminiDB %s failed with HTTP %s: %s%s" % (operation, status, code, detail))


class GeminiClient:
    """DynamoDB JSON API 适配器（实例级 access/secret key，SigV4 签名）。

    环境变量 GEMINI_PROXY_URL 非空时，所有请求改为经共享数据面代理函数的
    公网 APIG 入口转发（body {"target","payload"}），供无 VPC 连通性的
    部署机建表使用；此时直连端点可为空。
    """

    def __init__(self, endpoint, access_key, secret_key, port=8000, protocol="http", region="cn-north-4"):
        if not endpoint and not os.environ.get("GEMINI_PROXY_URL", ""):
            raise ValueError("GeminiDB endpoint is required")
        self.endpoint = ""
        self.host = ""
        if endpoint:
            raw = endpoint if "://" in endpoint else "%s://%s" % (protocol, endpoint)
            parsed = urllib.parse.urlsplit(raw)
            scheme = (parsed.scheme or protocol).lower()
            if scheme not in {"http", "https"}:
                raise ValueError("GeminiDB protocol must be http or https")
            hostname = parsed.hostname or ""
            if not hostname:
                raise ValueError("GeminiDB endpoint must contain a host")
            selected_port = parsed.port or int(port)
            host = "[%s]" % hostname if ":" in hostname and not hostname.startswith("[") else hostname
            self.endpoint = "%s://%s:%s%s" % (scheme, host, selected_port, parsed.path.rstrip("/"))
            self.host = "%s:%s" % (host, selected_port)
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("GEMINI_ENDPOINT", ""),
            os.environ.get("GEMINI_ACCESS_KEY", ""),
            os.environ.get("GEMINI_SECRET_KEY", ""),
            int(os.environ.get("GEMINI_PORT", "8000")),
            os.environ.get("GEMINI_PROTOCOL", "http"),
            os.environ.get("GEMINI_REGION", "cn-north-4"),
        )

    @staticmethod
    def _av(value):
        if value is None:
            return {"NULL": True}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, (int, float)):
            return {"N": str(value)}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, list):
            return {"L": [GeminiClient._av(v) for v in value]}
        if isinstance(value, dict):
            return {"M": {str(k): GeminiClient._av(v) for k, v in value.items()}}
        raise TypeError("unsupported value type: %r" % type(value))

    @classmethod
    def _plain(cls, value):
        if isinstance(value, dict):
            if "S" in value:
                return value["S"]
            if "N" in value:
                text = str(value["N"])
                return int(text) if text.lstrip("-").isdigit() else float(text)
            if "BOOL" in value:
                return bool(value["BOOL"])
            if "NULL" in value:
                return None
            if "L" in value:
                return [cls._plain(v) for v in value["L"]]
            if "M" in value:
                return {k: cls._plain(v) for k, v in value["M"].items()}
        return value

    @staticmethod
    def _signing_key(secret_key, date, region):
        def sign(key, value):
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        key = sign(("AWS4" + secret_key).encode("utf-8"), date)
        key = sign(key, region)
        key = sign(key, "dynamodb")
        return sign(key, "aws4_request")

    def _signed_request(self, operation, body):
        if not self.access_key or not self.secret_key:
            raise RuntimeError("GeminiDB access key and secret key are required")
        now = time.gmtime()
        date = time.strftime("%Y%m%d", now)
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
        payload_hash = hashlib.sha256(body).hexdigest()
        path = urllib.parse.urlsplit(self.endpoint).path or "/"
        canonical_uri = urllib.parse.quote(path, safe="/-_.~")
        if not canonical_uri.startswith("/"):
            canonical_uri = "/" + canonical_uri
        headers = {
            "content-type": "application/x-amz-json-1.0",
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "x-amz-target": "DynamoDB_20120810.%s" % operation,
        }
        canonical_headers = "".join("%s:%s\n" % (k, " ".join(str(headers[k]).strip().split())) for k in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join(["POST", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
        scope = "%s/%s/dynamodb/aws4_request" % (date, self.region)
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
        )
        signature = hmac.new(
            self._signing_key(self.secret_key, date, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        request_headers = {
            "Content-Type": headers["content-type"],
            "Host": headers["host"],
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
            "X-Amz-Target": headers["x-amz-target"],
            "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (self.access_key, scope, signed_headers, signature),
        }
        return urllib.request.Request(self.endpoint, data=body, headers=request_headers, method="POST")

    @staticmethod
    def _proxy_request(proxy_url, token, operation, body):
        """经共享数据面代理函数（APIG 公网入口）转发一次 DynamoDB 操作。"""
        headers = {"Content-Type": "application/json", "X-Stage": "prod"}
        if token:
            headers["X-Proxy-Token"] = token
        return urllib.request.Request(
            proxy_url,
            data=json.dumps({"target": operation, "payload": json.loads(body.decode("utf-8"))},
                            separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _call(self, operation, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        proxy_url = os.environ.get("GEMINI_PROXY_URL", "")
        if proxy_url:
            request = self._proxy_request(proxy_url, os.environ.get("GEMINI_PROXY_TOKEN", ""), operation, body)
        else:
            request = self._signed_request(operation, body)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                envelope = json.loads(raw) if raw else {}
                if not isinstance(envelope, dict):
                    raise RuntimeError("GeminiDB returned a non-object response")
        except urllib.error.HTTPError as exc:
            raw = exc.read(1024).decode("utf-8", "replace")
            code, message = "HTTPError", ""
            try:
                error = json.loads(raw)
                code = str(error.get("__type") or error.get("code") or code).split("#")[-1]
                message = str(error.get("message") or "")
            except (TypeError, ValueError):
                pass
            raise GeminiError(operation, exc.code, code, message) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("GeminiDB %s network failure: %s" % (operation, exc.reason)) from exc
        if proxy_url:
            status = int(envelope.get("status", 0))
            body_text = str(envelope.get("body", ""))
            if status != 200:
                code, message = "ProxyError", ""
                try:
                    error = json.loads(body_text)
                    code = str(error.get("__type") or error.get("code") or code).split("#")[-1]
                    message = str(error.get("message") or "")
                except (TypeError, ValueError):
                    message = body_text[:512]
                raise GeminiError(operation, status, code, message)
            result = json.loads(body_text) if body_text else {}
            if not isinstance(result, dict):
                raise RuntimeError("GeminiDB returned a non-object response")
            return result
        return envelope

    def create_table(self, name, key_schema, attribute_definitions):
        return self._call(
            "CreateTable",
            {
                "TableName": name,
                "KeySchema": key_schema,
                "AttributeDefinitions": attribute_definitions,
                "BillingMode": CAPACITY_MODE,
            },
        )

    def describe_table(self, name):
        return self._call("DescribeTable", {"TableName": name}).get("Table", {})

    def ensure_table(self, name, key_schema, attribute_definitions, timeout=180.0, poll_interval=2.0):
        try:
            self.create_table(name, key_schema, attribute_definitions)
        except GeminiError as exc:
            if exc.code not in {"ResourceInUseException", "TableAlreadyExistsException"}:
                raise
        deadline = time.monotonic() + timeout
        while True:
            try:
                description = self.describe_table(name)
            except GeminiError as exc:
                if exc.code != "ResourceNotFoundException" or time.monotonic() >= deadline:
                    raise
                time.sleep(poll_interval)
                continue
            status = str(description.get("TableStatus", ""))
            if status == "ACTIVE":
                return
            if status in {"DELETING", "ARCHIVING", "ARCHIVED"}:
                raise RuntimeError("GeminiDB table %s is not usable: %s" % (name, status))
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for GeminiDB table %s" % name)
            time.sleep(poll_interval)

    def put_item(self, table, item):
        return self._call("PutItem", {"TableName": table, "Item": {k: self._av(v) for k, v in item.items()}})

    def get_item(self, table, key):
        result = self._call("GetItem", {"TableName": table, "Key": {k: self._av(v) for k, v in key.items()}})
        raw = result.get("Item")
        return {k: self._plain(v) for k, v in raw.items()} if raw else None

    def delete_item(self, table, key):
        return self._call("DeleteItem", {"TableName": table, "Key": {k: self._av(v) for k, v in key.items()}})

    def scan_items(self, table):
        payload = {"TableName": table}
        items = []
        while True:
            result = self._call("Scan", payload)
            items.extend(result.get("Items", []))
            last_key = result.get("LastEvaluatedKey")
            if not last_key:
                break
            payload["ExclusiveStartKey"] = last_key
        return [{k: self._plain(v) for k, v in item.items()} for item in items]


TENANT_TABLE_DEF = {
    "key_schema": [{"AttributeName": "tenant_key", "KeyType": "HASH"}],
    "attribute_definitions": [{"AttributeName": "tenant_key", "AttributeType": "S"}],
}


def _tenant_key(tenant_id):
    return "TENANT#%s" % tenant_id


# ---------------------------------------------------------------------------
# 云管理面调用：函数委托令牌 + X-Auth-Token（同区域）
# ---------------------------------------------------------------------------
def _token(context):
    value = context.getToken()
    if not value:
        raise RuntimeError("未获取到委托令牌，请检查函数委托（agency）配置")
    return value


def _api_base():
    return "https://apig.%s.myhuaweicloud.com/v2/%s/apigw/instances/%s" % (
        os.environ["REGION"],
        os.environ["PROJECT_ID"],
        os.environ["APIG_INSTANCE_ID"],
    )


def _api_request(method, path, payload=None, token=""):
    url = _api_base() + path
    headers = {"Content-Type": "application/json", "X-Auth-Token": token}
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else None
    resp = requests.request(method, url, headers=headers, data=body, timeout=20)
    if resp.status_code >= 300:
        raise RuntimeError("APIG %s %s -> HTTP %s: %s" % (method, path, resp.status_code, resp.text[:400]))
    try:
        return resp.json()
    except ValueError:
        return {}


def _eg_publish(context, event_type, subject, data):
    """向自定义通道发布一条 CloudEvents 生命周期事件（source 必须与注册事件源同名）。"""
    event = {
        "id": uuid.uuid4().hex,
        "source": os.environ["EG_SOURCE_NAME"],
        "specversion": "1.0",
        "type": event_type,
        "subject": subject,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datacontenttype": "application/json",
        "data": data,
    }
    url = "https://eg.%s.myhuaweicloud.com/v1/%s/channels/%s/events" % (
        os.environ["REGION"],
        os.environ["PROJECT_ID"],
        os.environ["EG_CHANNEL_ID"],
    )
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json", "X-Auth-Token": _token(context)},
        data=json.dumps({"events": [event]}, separators=(",", ":")),
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError("EG put_events -> HTTP %s: %s" % (resp.status_code, resp.text[:400]))
    return event["id"]


# ---------------------------------------------------------------------------
# 租户入驻：函数内顺序编排（各步骤串行执行，失败即停并标记 Failed）
# ---------------------------------------------------------------------------
def _onboard_tenant(context, db, tenant):
    token = _token(context)
    app_prefix = os.environ.get("APP_NAME_UNDERSCORE", "mtsaas")
    app_name = "%s_tenant_%s" % (app_prefix, tenant["tenant_id"][:12])
    appcode = secrets.token_hex(32)  # 64 位十六进制（凭据编码要求 64~128 位）
    steps_done = []
    try:
        db.put_item(os.environ["TENANT_TABLE"], dict(tenant, status="Provisioning"))
        steps_done.append("write_tenant")

        app = _api_request("POST", "/apps", {"name": app_name, "remark": "Tenant credential: %s" % tenant["tenant_name"]}, token)
        application_id = str(app.get("id", ""))
        if not application_id:
            raise RuntimeError("APIG app response has no id: %s" % json.dumps(app)[:200])
        steps_done.append("create_application")

        _api_request("POST", "/apps/%s/app-codes" % application_id, {"app_code": appcode}, token)
        steps_done.append("create_appcode")

        quota_id = os.environ[TIER_QUOTA_ENV[tenant["tier"]]]
        _api_request("POST", "/app-quotas/%s/binding-apps" % quota_id, {"app_ids": [application_id]}, token)
        steps_done.append("bind_quota")

        api_ids = json.loads(os.environ["CATALOG_API_IDS"])
        _api_request(
            "POST",
            "/app-auths",
            {"app_ids": [application_id], "env_id": os.environ["APIG_ENV_ID"], "api_ids": api_ids},
            token,
        )
        steps_done.append("authorize_catalog_api")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = dict(
            tenant,
            status="Active",
            application_id=application_id,
            application_name=app_name,
            appcode=appcode,
            updated_at=now,
        )
        db.put_item(os.environ["TENANT_TABLE"], record)
        steps_done.append("activate_tenant")

        event_id = _eg_publish(
            context,
            "tenant.onboarded",
            "tenant/%s" % tenant["tenant_id"],
            {
                "tenant_id": tenant["tenant_id"],
                "tenant_name": tenant["tenant_name"],
                "tier": tenant["tier"],
                "application_id": application_id,
                "steps": steps_done,
            },
        )
        print("[ONBOARD] tenant_id=%s tier=%s application_id=%s event_id=%s steps=%s"
              % (tenant["tenant_id"], tenant["tier"], application_id, event_id, ",".join(steps_done)))
        return record, None
    except Exception as exc:  # noqa: BLE001 - 编排失败需标记并上报
        failed_step = steps_done[-1] if steps_done else "prepare"
        print("[ONBOARD-FAILED] tenant_id=%s after_step=%s error=%s" % (tenant["tenant_id"], failed_step, exc))
        try:
            db.put_item(
                os.environ["TENANT_TABLE"],
                dict(tenant, status="Failed", updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
        except Exception:  # noqa: BLE001 - 标记失败不覆盖原始错误
            pass
        return None, {"step": failed_step, "error": str(exc)}


def _register(context, db, payload):
    tenant_name = str(payload.get("tenant_name") or "").strip()
    email = str(payload.get("email") or "").strip()
    tier = str(payload.get("tier") or "").strip().lower()
    if not tenant_name or not email:
        return _resp(400, {"error": "tenant_name and email are required"})
    if tier not in TIER_QUOTA_ENV:
        return _resp(400, {"error": "tier must be one of: basic, standard"})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tenant_id = uuid.uuid4().hex
    tenant = {
        "tenant_id": tenant_id,
        "tenant_key": _tenant_key(tenant_id),
        "tenant_name": tenant_name,
        "email": email,
        "tier": tier,
        "created_at": now,
        "updated_at": now,
    }
    record, failure = _onboard_tenant(context, db, tenant)
    if failure:
        return _resp(500, {"error": "onboarding failed", **failure, "tenant_id": tenant["tenant_id"]})
    return _resp(201, record)


def _set_status(context, db, tenant_id, payload):
    status = str(payload.get("status") or "").strip()
    if status not in {"Active", "Inactive"}:
        return _resp(400, {"error": "status must be Active or Inactive"})
    record = db.get_item(os.environ["TENANT_TABLE"], {"tenant_key": _tenant_key(tenant_id)})
    if not record:
        return _resp(404, {"error": "tenant not found"})
    record["status"] = status
    record["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.put_item(os.environ["TENANT_TABLE"], record)
    event_id = _eg_publish(
        context,
        "tenant.status_changed",
        "tenant/%s" % tenant_id,
        {"tenant_id": tenant_id, "tenant_name": record.get("tenant_name", ""), "status": status},
    )
    print("[STATUS] tenant_id=%s status=%s event_id=%s" % (tenant_id, status, event_id))
    return _resp(200, record)


# ---------------------------------------------------------------------------
# APIG 事件处理
# ---------------------------------------------------------------------------
def _resp(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _body_payload(event):
    body = event.get("body")
    if not body:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def handler(event, context):
    if isinstance(event, str):
        event = json.loads(event)
    method = (event.get("httpMethod") or "").upper()
    path = event.get("path") or ""
    params = event.get("pathParameters") or {}
    request_id = context.getRequestID() if context is not None else "-"
    print("[CONTROL] requestId=%s %s %s" % (request_id, method, path))

    try:
        db = GeminiClient.from_env()
        tenant_table = os.environ["TENANT_TABLE"]
        payload = _body_payload(event)

        if path == "/tenants" and method == "POST":
            return _register(context, db, payload)
        if path == "/tenants" and method == "GET":
            items = db.scan_items(tenant_table)
            query = event.get("queryStringParameters") or {}
            status_filter = str(query.get("status") or "").strip()
            tier_filter = str(query.get("tier") or "").strip().lower()
            if status_filter:
                items = [item for item in items if item.get("status") == status_filter]
            if tier_filter:
                items = [item for item in items if item.get("tier") == tier_filter]
            summary = [
                {
                    "tenant_id": item.get("tenant_id"),
                    "tenant_name": item.get("tenant_name"),
                    "email": item.get("email"),
                    "tier": item.get("tier"),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                }
                for item in sorted(items, key=lambda x: str(x.get("created_at", "")))
            ]
            return _resp(200, summary)

        tenant_id = params.get("tenant_id", "")
        if path == "/tenants/%s/status" % tenant_id and method == "PUT":
            return _set_status(context, db, tenant_id, payload)
        if path == "/tenants/%s" % tenant_id and method == "GET":
            record = db.get_item(tenant_table, {"tenant_key": _tenant_key(tenant_id)})
            if not record:
                return _resp(404, {"error": "tenant not found"})
            return _resp(200, record)

        return _resp(404, {"error": "no route for %s %s" % (method, path)})
    except Exception as exc:  # noqa: BLE001
        print("[CONTROL-ERROR] requestId=%s error=%s" % (request_id, exc))
        return _resp(500, {"error": str(exc)})

