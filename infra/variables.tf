variable "region" {
  description = "华为云区域"
  type        = string
  default     = "cn-north-4"
}

variable "app_name" {
  description = "应用前缀；所有资源名称均由该前缀派生"
  type        = string
  default     = "mtsaas"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{3,30}$", var.app_name))
    error_message = "app_name 必须以字母开头，仅含字母、数字、连字符，长度为 4~31。"
  }
}

variable "namespace" {
  description = "GeminiDB 表命名空间（物理表名为 {namespace}_tenants / {namespace}_products）"
  type        = string
  default     = "mtsaas"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{2,30}$", var.namespace))
    error_message = "namespace 必须以字母开头，仅含字母、数字、下划线，长度为 3~31。"
  }
}

variable "apig_instance_id" {
  description = "APIG 专享实例 ID（由主环境集中创建后经此变量传入，本模块不创建实例）"
  type        = string

  validation {
    condition     = length(var.apig_instance_id) > 0
    error_message = "apig_instance_id 不能为空。"
  }
}

variable "apig_env_name" {
  description = "本模块在实例上创建的发布环境名（仅自建环境时生效）"
  type        = string
  default     = "prod"
}

variable "shared_apig_env_id" {
  description = "已有共享 APIG 环境 ID；非空时不创建环境，直接使用该环境（独享实例上同名环境只能存在一个）"
  type        = string
  default     = ""
}

variable "shared_network_id" {
  description = "函数挂接的共享虚拟私有云（VPC）ID，用于访问共享 GeminiDB 私网数据面；必须与 shared_subnet_id 同时填写或同时留空"
  type        = string
  default     = ""
}

variable "shared_subnet_id" {
  description = "函数挂接的共享子网 ID（FGS network_id）；必须与 shared_network_id 同时填写或同时留空"
  type        = string
  default     = ""
}

variable "basic_quota_limit" {
  description = "基础档租户每日调用配额"
  type        = number
  default     = 50

  validation {
    condition     = var.basic_quota_limit >= 1 && floor(var.basic_quota_limit) == var.basic_quota_limit
    error_message = "basic_quota_limit 必须是正整数。"
  }
}

variable "standard_quota_limit" {
  description = "标准档租户每日调用配额"
  type        = number
  default     = 100

  validation {
    condition     = var.standard_quota_limit >= 1 && floor(var.standard_quota_limit) == var.standard_quota_limit
    error_message = "standard_quota_limit 必须是正整数。"
  }
}

variable "gemini_db_endpoint" {
  description = "GeminiDB DynamoDB 兼容接口数据面主机或 URL（不含凭据）"
  type        = string

  validation {
    condition     = length(var.gemini_db_endpoint) > 0
    error_message = "gemini_db_endpoint 不能为空。"
  }
}

variable "gemini_db_port" {
  description = "GeminiDB 兼容接口端口"
  type        = number
  default     = 8000

  validation {
    condition     = var.gemini_db_port > 0 && var.gemini_db_port < 65536
    error_message = "gemini_db_port 必须是 1~65535 的端口。"
  }
}

variable "gemini_db_protocol" {
  description = "GeminiDB 兼容接口协议，仅支持 http 或 https"
  type        = string
  default     = "http"

  validation {
    condition     = contains(["http", "https"], lower(var.gemini_db_protocol))
    error_message = "gemini_db_protocol 必须是 http 或 https。"
  }
}

variable "gemini_db_region" {
  description = "GeminiDB 兼容接口 SigV4 凭据范围使用的区域"
  type        = string
  default     = "cn-north-4"
}

variable "gemini_db_access_key" {
  description = "GeminiDB 兼容接口 access key；建议经 TF_VAR_gemini_db_access_key 注入"
  type        = string
  sensitive   = true
}

variable "gemini_db_secret_key" {
  description = "GeminiDB 兼容接口 secret key；建议经 TF_VAR_gemini_db_secret_key 注入"
  type        = string
  sensitive   = true
}

variable "gemini_proxy_url" {
  description = "共享数据面代理函数的公网 APIG 入口 URL；非空时函数与建表脚本改走公网代理"
  type        = string
  default     = ""
}

variable "gemini_proxy_token" {
  description = "代理入口校验令牌（X-Proxy-Token）；与 gemini_proxy_url 同时提供"
  type        = string
  sensitive   = true
  default     = ""
}
