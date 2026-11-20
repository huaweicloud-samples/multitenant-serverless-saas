# -*- coding: utf-8 -*-
"""多租户 SaaS 参考实现：共享（Pooled）商品目录微服务（租户应用面）。

单个函数按方法路由实现商品 CRUD：
  - POST   /products                  创建商品
  - GET    /products                  列出本租户全部商品
  - GET    /products/{product_id}     查询单个商品
  - PUT    /products/{product_id}     更新商品
  - DELETE /products/{product_id}     删除商品

租户身份解析：请求头 X-Apig-AppCode（APIG 简单认证凭据）-> 扫描租户表
解析出 tenant_id；租户停用（status != Active）时拒绝访问。
数据隔离：商品表分区键固定为 TENANT#<tenant_id>，所有读写都限定在
解析出的租户键空间内，请求体中无法伪造租户身份。

依赖：仅 Python 标准库。数据面为 GeminiDB DynamoDB 兼容接口。
函数以确定性 ZIP 部署，数据面客户端在此自包含。
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

PRODUCT_TABLE_DEF = {
    "key_schema": [
        {"AttributeName": "tenant_key", "KeyType": "HASH"},
        {"AttributeName": "product_id", "KeyType": "RANGE"},
    ],
    "attribute_definitions": [
        {"AttributeName": "tenant_key", "AttributeType": "S"},
        {"AttributeName": "product_id", "AttributeType": "S"},
    ],
}

PRODUCT_FIELDS = ("sku", "name", "category")


def _tenant_key(tenant_id):
    return "TENANT#%s" % tenant_id


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
    部署环境使用；此时直连端点可为空。
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

    def query_items(self, table, tenant_key):
        payload = {
            "TableName": table,
            "KeyConditionExpression": "tenant_key = :tenant_key",
            "ExpressionAttributeValues": {":tenant_key": self._av(tenant_key)},
        }
        items = []
        while True:
            result = self._call("Query", payload)
            items.extend(result.get("Items", []))
            last_key = result.get("LastEvaluatedKey")
            if not last_key:
                break
            payload["ExclusiveStartKey"] = last_key
        return [{k: self._plain(v) for k, v in item.items()} for item in items]


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
        body = base64.b64decode(body).decode("utf-8")
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _header(event, name):
    headers = event.get("headers") or {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(name.lower(), "")


def _resolve_tenant(db, appcode):
    """按 appcode 解析租户：返回 (租户记录, 错误响应)。"""
    if not appcode:
        return None, _resp(401, {"error": "missing credential appcode header"})
    for item in db.scan_items(os.environ["TENANT_TABLE"]):
        if item.get("appcode") == appcode:
            if item.get("status") != "Active":
                return None, _resp(403, {"error": "tenant is not active", "status": item.get("status")})
            return item, None
    return None, _resp(403, {"error": "unknown credential"})


def _normalize_price(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("price must be a number")


def handler(event, context):
    if isinstance(event, str):
        event = json.loads(event)
    method = (event.get("httpMethod") or "").upper()
    path = event.get("path") or ""
    params = event.get("pathParameters") or {}
    request_id = context.getRequestID() if context is not None else "-"

    try:
        db = GeminiClient.from_env()
        tenant, error = _resolve_tenant(db, _header(event, "X-Apig-AppCode"))
        if error:
            print("[CATALOG] requestId=%s %s %s rejected" % (request_id, method, path))
            return error
        tenant_id = tenant["tenant_id"]
        table = os.environ["PRODUCT_TABLE"]
        payload = _body_payload(event)
        print("[CATALOG] requestId=%s tenant_id=%s %s %s" % (request_id, tenant_id, method, path))

        if path == "/products" and method == "POST":
            missing = [field for field in PRODUCT_FIELDS if not str(payload.get(field) or "").strip()]
            try:
                price = _normalize_price(payload.get("price"))
            except ValueError as exc:
                return _resp(400, {"error": str(exc)})
            if missing or price is None:
                return _resp(400, {"error": "sku, name, category and numeric price are required"})
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            product = {
                "tenant_id": tenant_id,
                "tenant_key": _tenant_key(tenant_id),
                "product_id": uuid.uuid4().hex,
                "sku": str(payload["sku"]).strip(),
                "name": str(payload["name"]).strip(),
                "category": str(payload["category"]).strip(),
                "price": price,
                "created_at": now,
                "updated_at": now,
            }
            db.put_item(table, product)
            return _resp(201, product)

        if path == "/products" and method == "GET":
            items = db.query_items(table, _tenant_key(tenant_id))
            items.sort(key=lambda x: str(x.get("created_at", "")))
            return _resp(200, items)

        product_id = params.get("product_id", "")
        key = {"tenant_key": _tenant_key(tenant_id), "product_id": product_id}

        if path == "/products/%s" % product_id and method == "GET":
            product = db.get_item(table, key)
            if not product:
                return _resp(404, {"error": "product not found"})
            return _resp(200, product)

        if path == "/products/%s" % product_id and method == "PUT":
            product = db.get_item(table, key)
            if not product:
                return _resp(404, {"error": "product not found"})
            for field in PRODUCT_FIELDS:
                value = payload.get(field)
                if value is not None and str(value).strip():
                    product[field] = str(value).strip()
            if payload.get("price") is not None:
                try:
                    price = _normalize_price(payload.get("price"))
                except ValueError as exc:
                    return _resp(400, {"error": str(exc)})
                if price is None:
                    return _resp(400, {"error": "price must be a number"})
                product["price"] = price
            product["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.put_item(table, product)
            return _resp(200, product)

        if path == "/products/%s" % product_id and method == "DELETE":
            product = db.get_item(table, key)
            if not product:
                return _resp(404, {"error": "product not found"})
            db.delete_item(table, key)
            return _resp(200, {"deleted": True, "product_id": product_id})

        return _resp(404, {"error": "no route for %s %s" % (method, path)})
    except Exception as exc:  # noqa: BLE001
        print("[CATALOG-ERROR] requestId=%s error=%s" % (request_id, exc))
        return _resp(500, {"error": str(exc)})
