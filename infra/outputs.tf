output "region" {
  description = "部署区域"
  value       = var.region
}

output "project_id" {
  description = "当前区域项目 ID（验证脚本调用 LTS 管理接口时使用）"
  value       = data.huaweicloud_account.current.current_project_id
}

output "api_base_url" {
  description = "API 调用根地址（调用需带 X-Stage 请求头指向发布环境）"
  value       = "https://${try(data.huaweicloud_apig_groups.this.groups[0].sl_domain, "")}"
}

output "api_stage" {
  description = "调用时需携带的 X-Stage 请求头取值（共享环境时为反查到的环境名）"
  value       = local.apig_env_name
}

output "control_plane_paths" {
  description = "控制面 API 路径（管理端凭据调用）"
  value = [
    "POST /tenants",
    "GET /tenants",
    "GET /tenants/{tenant_id}",
    "PUT /tenants/{tenant_id}/status",
  ]
}

output "catalog_paths" {
  description = "租户应用面（商品目录）API 路径（租户凭据调用）"
  value = [
    "POST /products",
    "GET /products",
    "GET /products/{product_id}",
    "PUT /products/{product_id}",
    "DELETE /products/{product_id}",
  ]
}

output "admin_appcode" {
  description = "管理端凭据 appcode（请求头 X-Apig-AppCode）"
  value       = huaweicloud_apig_appcode.admin.value
  sensitive   = true
}

output "tenant_admin_function" {
  description = "控制面函数名"
  value       = huaweicloud_fgs_function.tenant_admin.name
}

output "catalog_api_function" {
  description = "商品目录函数名"
  value       = huaweicloud_fgs_function.catalog_api.name
}

output "events_audit_function" {
  description = "生命周期事件审计函数名"
  value       = huaweicloud_fgs_function.events_audit.name
}

output "log_group_id" {
  description = "LTS 日志组 ID"
  value       = huaweicloud_lts_group.this.id
}

output "tenant_admin_log_stream_id" {
  description = "控制面函数 LTS 日志流 ID"
  value       = huaweicloud_lts_stream.tenant_admin.id
}

output "catalog_api_log_stream_id" {
  description = "目录函数 LTS 日志流 ID"
  value       = huaweicloud_lts_stream.catalog_api.id
}

output "events_audit_log_stream_id" {
  description = "审计函数 LTS 日志流 ID（验证脚本按 ID 直查审计日志）"
  value       = huaweicloud_lts_stream.events_audit.id
}

output "eg_channel_id" {
  description = "租户生命周期事件通道 ID"
  value       = huaweicloud_eg_custom_event_channel.tenant_events.id
}

output "eg_source_name" {
  description = "租户生命周期事件源名（发布事件时 source 字段取值）"
  value       = local.eg_source_name
}

output "basic_quota_id" {
  description = "基础档每日用量配额 ID"
  value       = huaweicloud_apig_application_quota.basic.id
}

output "basic_quota_limit" {
  description = "基础档每日调用次数；云端验证据此触发 429"
  value       = var.basic_quota_limit
}

output "standard_quota_id" {
  description = "标准档每日用量配额 ID"
  value       = huaweicloud_apig_application_quota.standard.id
}
