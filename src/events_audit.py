# -*- coding: utf-8 -*-
"""多租户 SaaS 参考实现：租户生命周期事件审计函数。

事件网格自定义通道订阅的目标函数：接收租户入驻 / 状态变更等
CloudEvents 生命周期事件（ORIGINAL 转换，函数输入即完整事件信封），
将审计行写入函数绑定的 LTS 日志流，供运维检索与合规审计。

事件网格异步投递时事件 ID 即函数 RequestID，可用同一 ID 关联
事件网格投递轨迹与函数日志。
"""

import json


def handler(event, context):
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except ValueError:
            event = {"raw": event}
    request_id = context.getRequestID() if context is not None else "-"
    event_id = event.get("id", "-")
    event_type = event.get("type", "-")
    source = event.get("source", "-")
    data = event.get("data") or {}
    tenant_id = data.get("tenant_id", "-") if isinstance(data, dict) else "-"

    # 每次 print 是一条独立 LTS 记录：审计行带全部可检索字段。
    print("[TENANT-EVENT] requestId=%s event_id=%s type=%s source=%s tenant_id=%s"
          % (request_id, event_id, event_type, source, tenant_id))
    print("[TENANT-EVENT] payload=%s" % json.dumps(event, ensure_ascii=False, sort_keys=True))
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps({"audited": True, "event_id": event_id}),
    }
