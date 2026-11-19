provider "huaweicloud" {
  region = var.region
  # 认证使用环境变量：HW_ACCESS_KEY / HW_SECRET_KEY
}

data "huaweicloud_account" "current" {}

locals {
  tenant_table  = "${var.namespace}_tenants"
  product_table = "${var.namespace}_products"
  # 事件网格自定义事件源名仅小写字母/数字/下划线/连字符/点，账户级全局唯一
  eg_source_name = "${var.app_name}-tenant-lifecycle"
  # 凭据 / 配额名不允许连字符
  name_underscore = replace(var.app_name, "-", "_")

  # APIG 环境：共享独享实例上同名环境只能存在一个。提供共享环境 ID 时直接
  # 复用，否则本目录自建；引用环境的资源统一使用 local.apig_env_id。
  use_shared_apig_env = var.shared_apig_env_id != ""
  apig_env_id         = local.use_shared_apig_env ? var.shared_apig_env_id : huaweicloud_apig_environment.this[0].id

  # 共享私网接入：成对提供时把需要访问共享 GeminiDB 私网数据面的函数挂到
  # 共享网络/子网；成对接线或都不接。
  shared_network_configured = var.shared_network_id != "" && var.shared_subnet_id != ""

  gemini_env = merge({
    GEMINI_ENDPOINT   = var.gemini_db_endpoint
    GEMINI_PORT       = tostring(var.gemini_db_port)
    GEMINI_PROTOCOL   = var.gemini_db_protocol
    GEMINI_REGION     = var.gemini_db_region
    GEMINI_ACCESS_KEY = var.gemini_db_access_key
    GEMINI_SECRET_KEY = var.gemini_db_secret_key
  }, local.gemini_proxy_env)

  # 无 VPC 连通性的部署机：提供代理入口后，函数数据面改走公网代理；
  # 留空时保持直连签名路径，user_data 不注入空值（平台拒绝空环境变量）。
  gemini_proxy_env = var.gemini_proxy_url != "" ? {
    GEMINI_PROXY_URL   = var.gemini_proxy_url
    GEMINI_PROXY_TOKEN = var.gemini_proxy_token
  } : {}

  # 控制面与租户面路由分开声明，避免“控制面 API 引用控制面函数、
  # 控制面函数又引用目录 API ID”的循环依赖
  control_routes = {
    control_tenants = {
      method = "ANY"
      path   = "/tenants"
    }
    control_tenant = {
      method = "ANY"
      path   = "/tenants/{tenant_id}"
    }
    control_status = {
      method = "ANY"
      path   = "/tenants/{tenant_id}/status"
    }
  }
  catalog_routes = {
    catalog_products = {
      method = "ANY"
      path   = "/products"
    }
    catalog_product = {
      method = "ANY"
      path   = "/products/{product_id}"
    }
  }
}

# ---------------------------------------------------------------------------
# LTS 日志：一个日志组 + 每个函数一条日志流（函数侧必须绑定 id+name 四参数）
# ---------------------------------------------------------------------------
resource "huaweicloud_lts_group" "this" {
  group_name  = "${var.app_name}-logs"
  ttl_in_days = 7
}

resource "huaweicloud_lts_stream" "tenant_admin" {
  group_id    = huaweicloud_lts_group.this.id
  stream_name = "${var.app_name}-tenant-admin"
}

resource "huaweicloud_lts_stream" "catalog_api" {
  group_id    = huaweicloud_lts_group.this.id
  stream_name = "${var.app_name}-catalog-api"
}

resource "huaweicloud_lts_stream" "events_audit" {
  group_id    = huaweicloud_lts_group.this.id
  stream_name = "${var.app_name}-events-audit"
}

# ---------------------------------------------------------------------------
# IAM：控制面函数委托（op_svc_cff）
#   - APIG FullAccess（系统策略）：注册流程内创建凭据 / appcode / 授权 / 绑定配额
#   - 自定义最小策略：仅允许向事件网格自定义通道发布事件
# 委托名账户级唯一，带本示例专属前缀
# ---------------------------------------------------------------------------
resource "huaweicloud_identity_role" "eg_put_events" {
  name        = "${var.app_name}-eg-put-events"
  description = "Allow the control-plane function to publish tenant lifecycle events to EventGrid"
  type        = "XA"

  policy = jsonencode({
    Version = "1.1"
    Statement = [{
      Effect   = "Allow"
      Action   = ["eg:channels:putEvents"]
      Resource = ["*"]
    }]
  })
}

resource "huaweicloud_identity_agency" "tenant_admin" {
  name                  = "${var.app_name}-admin-func"
  description           = "Control-plane function agency: APIG app management + EventGrid publish"
  delegated_domain_name = "op_svc_cff"
  duration              = "FOREVER"

  project_role {
    project = var.region
    roles = concat(
      [
        "APIG FullAccess",
        huaweicloud_identity_role.eg_put_events.name,
      ],
      # 函数挂接共享私网时需要 VPC 权限
      local.shared_network_configured ? ["VPC FullAccess"] : [],
    )
  }

  lifecycle {
    ignore_changes = [delegated_service_name]
  }
}

# IAM：目录函数挂接共享私网所需委托（仅在成对提供网络变量时创建）
resource "huaweicloud_identity_agency" "catalog_api_vpc" {
  count = local.shared_network_configured ? 1 : 0

  name                  = "${var.app_name}-catalog-vpc"
  description           = "Agency for the catalog function to attach to the shared VPC"
  delegated_domain_name = "op_svc_cff"
  duration              = "FOREVER"

  project_role {
    project = var.region
    roles   = ["VPC FullAccess"]
  }

  lifecycle {
    ignore_changes = [delegated_service_name]
  }
}

# IAM：事件网格投递函数目标所需委托（op_svc_eg + FunctionGraph FullAccess）
resource "huaweicloud_identity_agency" "eg_target" {
  name                  = "${var.app_name}-eg-target"
  description           = "Allow EventGrid to invoke the tenant lifecycle audit function"
  delegated_domain_name = "op_svc_eg"
  duration              = "FOREVER"

  project_role {
    project = var.region
    roles   = ["FunctionGraph FullAccess"]
  }

  lifecycle {
    ignore_changes = [delegated_service_name]
  }
}

# ---------------------------------------------------------------------------
# 事件网格：自定义通道 + 应用类事件源 + 订阅（目标为审计函数）
# 控制面函数发布事件时 CloudEvents source 必须与注册事件源同名，否则不投递
# ---------------------------------------------------------------------------
resource "huaweicloud_eg_custom_event_channel" "tenant_events" {
  name        = "${var.app_name}-tenant-events"
  description = "Tenant lifecycle event channel"
}

resource "huaweicloud_eg_custom_event_source" "tenant_lifecycle" {
  channel_id  = huaweicloud_eg_custom_event_channel.tenant_events.id
  name        = local.eg_source_name
  type        = "APPLICATION"
  description = "Tenant lifecycle publisher (control-plane function)"
}

resource "random_uuid" "sub_source" {}
resource "random_uuid" "sub_target" {}

resource "huaweicloud_eg_event_subscription" "audit" {
  depends_on = [
    huaweicloud_eg_custom_event_source.tenant_lifecycle,
    huaweicloud_identity_agency.eg_target,
  ]

  channel_id  = huaweicloud_eg_custom_event_channel.tenant_events.id
  name        = "${var.app_name}-audit"
  description = "Audit all tenant lifecycle events into LTS via FunctionGraph"

  sources {
    id            = random_uuid.sub_source.result
    provider_type = "CUSTOM"
    name          = local.eg_source_name
    filter_rule = jsonencode({
      "source" : [{
        "op" : "StringIn",
        "values" : [local.eg_source_name]
      }]
    })
  }

  targets {
    id            = random_uuid.sub_target.result
    provider_type = "OFFICIAL"
    name          = "HC.FunctionGraph"
    detail_name   = "detail"
    detail = jsonencode({
      "urn" : trimsuffix(huaweicloud_fgs_function.events_audit.urn, ":latest"),
      "invoke_type" : "ASYNC",
      "agency_name" : huaweicloud_identity_agency.eg_target.name
    })
    transform = jsonencode({
      "type" : "ORIGINAL",
      "value" : ""
    })
  }
}

# ---------------------------------------------------------------------------
# FunctionGraph 函数（均为最低规格 128MB / 15s，Python3.10，确定性 ZIP）
# ---------------------------------------------------------------------------

# 控制面：租户注册 / 列表 / 查询 / 停用 + 函数内顺序编排入驻流程
# 成对提供共享网络变量时挂接共享私网，使函数可达共享 GeminiDB 私网数据面
resource "huaweicloud_fgs_function" "tenant_admin" {
  name           = "${var.app_name}-tenant-admin"
  app            = "default"
  handler        = "tenant_admin.handler"
  runtime        = "Python3.10"
  memory_size    = 128
  timeout        = 15
  code_type      = "zip"
  code_filename  = "tenant_admin.zip"
  func_code      = filebase64("${path.module}/../src/build/tenant_admin.zip")
  agency         = huaweicloud_identity_agency.tenant_admin.name
  enable_lts_log = true
  vpc_id         = local.shared_network_configured ? var.shared_network_id : null
  network_id     = local.shared_network_configured ? var.shared_subnet_id : null

  log_group_id    = huaweicloud_lts_group.this.id
  log_group_name  = huaweicloud_lts_group.this.group_name
  log_stream_id   = huaweicloud_lts_stream.tenant_admin.id
  log_stream_name = huaweicloud_lts_stream.tenant_admin.stream_name

  user_data = jsonencode(merge(local.gemini_env, {
    REGION              = var.region
    PROJECT_ID          = data.huaweicloud_account.current.current_project_id
    APP_NAME_UNDERSCORE = local.name_underscore
    APIG_INSTANCE_ID    = var.apig_instance_id
    APIG_ENV_ID         = local.apig_env_id
    CATALOG_API_IDS = jsonencode([
      huaweicloud_apig_api.catalog["catalog_products"].id,
      huaweicloud_apig_api.catalog["catalog_product"].id,
    ])
    BASIC_QUOTA_ID    = huaweicloud_apig_application_quota.basic.id
    STANDARD_QUOTA_ID = huaweicloud_apig_application_quota.standard.id
    EG_CHANNEL_ID     = huaweicloud_eg_custom_event_channel.tenant_events.id
    EG_SOURCE_NAME    = local.eg_source_name
    TENANT_TABLE      = local.tenant_table
    PRODUCT_TABLE     = local.product_table
  }))

  depends_on = [huaweicloud_identity_agency.tenant_admin]
}

# 租户应用面：共享商品目录微服务（appcode 解析租户，键前缀隔离数据）
# 成对提供共享网络变量时挂接共享私网（委托随之创建，使函数可达私网数据面）
resource "huaweicloud_fgs_function" "catalog_api" {
  name           = "${var.app_name}-catalog-api"
  app            = "default"
  handler        = "catalog_api.handler"
  runtime        = "Python3.10"
  memory_size    = 128
  timeout        = 15
  code_type      = "zip"
  code_filename  = "catalog_api.zip"
  func_code      = filebase64("${path.module}/../src/build/catalog_api.zip")
  agency         = local.shared_network_configured ? huaweicloud_identity_agency.catalog_api_vpc[0].name : null
  enable_lts_log = true
  vpc_id         = local.shared_network_configured ? var.shared_network_id : null
  network_id     = local.shared_network_configured ? var.shared_subnet_id : null

  log_group_id    = huaweicloud_lts_group.this.id
  log_group_name  = huaweicloud_lts_group.this.group_name
  log_stream_id   = huaweicloud_lts_stream.catalog_api.id
  log_stream_name = huaweicloud_lts_stream.catalog_api.stream_name

  user_data = jsonencode(merge(local.gemini_env, {
    TENANT_TABLE  = local.tenant_table
    PRODUCT_TABLE = local.product_table
  }))
}

# 生命周期事件审计：事件网格订阅目标，审计行写入 LTS
resource "huaweicloud_fgs_function" "events_audit" {
  name           = "${var.app_name}-events-audit"
  app            = "default"
  handler        = "events_audit.handler"
  runtime        = "Python3.10"
  memory_size    = 128
  timeout        = 15
  code_type      = "zip"
  code_filename  = "events_audit.zip"
  func_code      = filebase64("${path.module}/../src/build/events_audit.zip")
  enable_lts_log = true

  log_group_id    = huaweicloud_lts_group.this.id
  log_group_name  = huaweicloud_lts_group.this.group_name
  log_stream_id   = huaweicloud_lts_stream.events_audit.id
  log_stream_name = huaweicloud_lts_stream.events_audit.stream_name
}

# ---------------------------------------------------------------------------
# APIG：分组 + 环境 + API + 发布
# 所有 API 均为 APP 认证 + appcode 简单认证（请求头 X-Apig-AppCode）
# ---------------------------------------------------------------------------
resource "huaweicloud_apig_group" "this" {
  instance_id           = var.apig_instance_id
  name                  = "${var.app_name}-group"
  description           = "Multi-tenant SaaS reference: control-plane and tenant catalog APIs"
  domain_access_enabled = true
}

resource "huaweicloud_apig_environment" "this" {
  count = local.use_shared_apig_env ? 0 : 1

  instance_id = var.apig_instance_id
  name        = var.apig_env_name
  description = "Validation environment for the multi-tenant SaaS reference"
}

# 使用共享环境时按 ID 反查环境名（X-Stage 取值）；ID 不存在时 plan 阶段即报错
data "huaweicloud_apig_environments" "this" {
  count       = local.use_shared_apig_env ? 1 : 0
  instance_id = var.apig_instance_id
}

locals {
  shared_apig_env_matches = [
    for env in flatten(data.huaweicloud_apig_environments.this[*].environments) :
    env if env.id == var.shared_apig_env_id
  ]
  apig_env_name = local.use_shared_apig_env ? local.shared_apig_env_matches[0].name : huaweicloud_apig_environment.this[0].name
}

resource "huaweicloud_apig_api" "control" {
  for_each = local.control_routes

  instance_id             = var.apig_instance_id
  group_id                = huaweicloud_apig_group.this.id
  type                    = "Public"
  name                    = "${var.app_name}-${each.key}"
  request_protocol        = "HTTPS"
  request_method          = each.value.method
  request_path            = each.value.path
  security_authentication = "APP"
  simple_authentication   = true
  matching                = "Exact"

  func_graph {
    # urn 刷新后可能带回 :latest 后缀，剥离后以 version 字段为准
    function_urn     = trimsuffix(huaweicloud_fgs_function.tenant_admin.urn, ":latest")
    version          = "latest"
    network_type     = "V1"
    request_protocol = "HTTPS"
    timeout          = 15000
    invocation_type  = "sync"
  }
}

resource "huaweicloud_apig_api" "catalog" {
  for_each = local.catalog_routes

  instance_id             = var.apig_instance_id
  group_id                = huaweicloud_apig_group.this.id
  type                    = "Public"
  name                    = "${var.app_name}-${each.key}"
  request_protocol        = "HTTPS"
  request_method          = each.value.method
  request_path            = each.value.path
  security_authentication = "APP"
  simple_authentication   = true
  matching                = "Exact"

  func_graph {
    function_urn     = trimsuffix(huaweicloud_fgs_function.catalog_api.urn, ":latest")
    version          = "latest"
    network_type     = "V1"
    request_protocol = "HTTPS"
    timeout          = 15000
    invocation_type  = "sync"
  }
}

resource "huaweicloud_apig_api_publishment" "control" {
  for_each = local.control_routes

  instance_id = var.apig_instance_id
  env_id      = local.apig_env_id
  api_id      = huaweicloud_apig_api.control[each.key].id
  description = "Published by ${var.app_name}"
}

resource "huaweicloud_apig_api_publishment" "catalog" {
  for_each = local.catalog_routes

  instance_id = var.apig_instance_id
  env_id      = local.apig_env_id
  api_id      = huaweicloud_apig_api.catalog[each.key].id
  description = "Published by ${var.app_name}"
}

# ---------------------------------------------------------------------------
# 管理端凭据（控制面调用身份）+ appcode + 控制面 API 授权
# ---------------------------------------------------------------------------
resource "huaweicloud_apig_application" "admin" {
  instance_id = var.apig_instance_id
  # 凭据（应用）名称不允许连字符
  name        = "${local.name_underscore}_admin_app"
  description = "Admin credential for the control-plane APIs"
}

resource "random_id" "admin_appcode" {
  byte_length = 32
}

resource "huaweicloud_apig_appcode" "admin" {
  instance_id    = var.apig_instance_id
  application_id = huaweicloud_apig_application.admin.id
  value          = random_id.admin_appcode.hex
}

resource "huaweicloud_apig_application_authorization" "admin" {
  instance_id    = var.apig_instance_id
  application_id = huaweicloud_apig_application.admin.id
  env_id         = local.apig_env_id
  api_ids = [
    huaweicloud_apig_api.control["control_tenants"].id,
    huaweicloud_apig_api.control["control_tenant"].id,
    huaweicloud_apig_api.control["control_status"].id,
  ]

  # 授权要求 API 已发布到目标环境
  depends_on = [
    huaweicloud_apig_api_publishment.control,
  ]
}

# ---------------------------------------------------------------------------
# 每租户档位用量配额（每日窗口）；租户注册时由控制面函数把新凭据绑定到对应配额
# ---------------------------------------------------------------------------
resource "huaweicloud_apig_application_quota" "basic" {
  instance_id = var.apig_instance_id
  # 配额名称不允许连字符
  name          = "${local.name_underscore}_basic_daily"
  description   = "Daily call quota for basic-tier tenant credentials"
  call_limits   = var.basic_quota_limit
  time_interval = 1
  time_unit     = "DAY"
}

resource "huaweicloud_apig_application_quota" "standard" {
  instance_id   = var.apig_instance_id
  name          = "${local.name_underscore}_standard_daily"
  description   = "Daily call quota for standard-tier tenant credentials"
  call_limits   = var.standard_quota_limit
  time_interval = 1
  time_unit     = "DAY"
}

# 管理凭据使用标准档配额；租户注册时，控制面函数再把新建应用绑定到对应档位配额。
resource "huaweicloud_apig_application_quota_associate" "admin" {
  instance_id = var.apig_instance_id
  quota_id    = huaweicloud_apig_application_quota.standard.id

  applications {
    id = huaweicloud_apig_application.admin.id
  }
}

# ---------------------------------------------------------------------------
# 分组调试子域名（不依赖发布资源，避免 sl_domain 延迟解析）
# ---------------------------------------------------------------------------
data "huaweicloud_apig_groups" "this" {
  instance_id = var.apig_instance_id
  name        = huaweicloud_apig_group.this.name
}

check "shared_network_pair" {
  assert {
    condition     = (var.shared_network_id == "") == (var.shared_subnet_id == "")
    error_message = "shared_network_id 与 shared_subnet_id 必须同时为空或同时填写。"
  }
}

check "gemini_proxy_pair" {
  assert {
    condition     = var.gemini_proxy_url != "" || var.gemini_proxy_token == ""
    error_message = "提供了 gemini_proxy_token 时必须同时提供 gemini_proxy_url。"
  }
}
