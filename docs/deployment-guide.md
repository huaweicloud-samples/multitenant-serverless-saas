# 部署指南

本文给出从零到可验证部署的完整命令，每一步都附预期输出。
命令默认在本仓库根目录执行，需要进入子目录时会显式写出 `cd`。

架构与设计取舍见 [architecture.md](architecture.md)。

## 0. 约定与占位符

| 占位符 | 含义 | 取值方式 |
|-|-|-|
| `<HW_ACCESS_KEY>` | Access Key ID | 控制台「我的凭证」页面的访问密钥区域 |
| `<HW_SECRET_KEY>` | Secret Access Key | 同上；两者只填入当前 shell，不要写入代码或提交到仓库 |
| `<APIG 专享实例 ID>` | 已就绪的网关实例 | 网关控制台的实例列表 |
| `<数据面地址>` | 文档数据服务的数据面地址 | 由实例详情页获取 |

凭据在本实践中有两处用途，彼此独立：

- 部署：由执行 `terraform` 的进程读取 `HW_ACCESS_KEY`、`HW_SECRET_KEY`。
- 运行期：控制面函数以委托身份调用网关管理面；数据面凭据由部署参数下发。
  审计函数由事件网格以独立委托异步投递。

## 1. 自检本地工具

```bash
python --version
terraform version
```

预期输出（版本不低于下表要求即可）：

```text
Python 3.11.9
Terraform v1.9.8
```

要求：Terraform CLI `>= 1.3`，huaweicloud Provider `1.96.1`，Python 3.9 及以上。

## 2. 构建函数部署包

```bash
python src/scripts/build.py
```

预期输出（三个包，摘要随源码变化）：

```text
{
  "tenant_admin": {"bytes": 7329, "file": "src/build/tenant_admin.zip", "sha256": "e5d2c5cb…"},
  "catalog_api": {...},
  "events_audit": {...}
}
```

构建说明：

- 三个包都以固定时间戳与固定成员顺序产出，同一份源码重复构建摘要一致。
- ZIP 根目录直接放处理文件（`tenant_admin.py` 等），入口为 `<文件名>.handler`。
- 打包前会编译校验源码，语法错误在构建阶段即暴露。

## 3. 本地单元测试

```bash
cd src
python -m pytest tests -q
cd ..
```

预期输出：

```text
3 passed in 0.03s
```

单元测试覆盖应用凭据解析、租户状态判定与目录查询规则，不依赖云端资源。

## 4. 静态校验

```bash
cd infra
terraform init
terraform validate
```

预期输出：

```text
Success! The configuration is valid, but there were some
validation warnings as shown above.
```

未使用官方插件镜像时，警告内容是本地开发覆盖的提示，属预期输出。

## 5. 配置访问凭据

```bash
export HW_ACCESS_KEY=<HW_ACCESS_KEY>
export HW_SECRET_KEY=<HW_SECRET_KEY>
```

## 6. 提供部署参数

必填项三个：`apig_instance_id`、`gemini_db_endpoint`、`gemini_db_access_key`、`gemini_db_secret_key`。

```bash
export TF_VAR_apig_instance_id="<APIG 专享实例 ID>"
export TF_VAR_gemini_db_endpoint="<数据面地址>"
export TF_VAR_gemini_db_access_key="<数据面访问密钥 ID>"
export TF_VAR_gemini_db_secret_key="<数据面访问密钥 Secret>"
```

### 6.1 主要参数

| 变量 | 类型 | 默认值 | 必填 | 说明 |
|-|-|-|-|-|
| `region` | string | `cn-north-4` | 否 | 部署区域 |
| `app_name` | string | `mtsaas` | 否 | 资源名称前缀；字母开头，长度 4~31 |
| `namespace` | string | `mtsaas` | 否 | 表命名空间；物理表名为 `<namespace>_tenants`、`<namespace>_products` |
| `apig_instance_id` | string | 无 | 是 | 网关专享实例 ID |
| `apig_env_name` | string | `prod` | 否 | 发布环境名（仅自建环境时生效） |
| `shared_apig_env_id` | string | 空 | 否 | 已有共享环境 ID；非空时不创建环境 |
| `shared_network_id` | string | 空 | 否 | 函数挂接的网络 ID；与 `shared_subnet_id` 成对 |
| `shared_subnet_id` | string | 空 | 否 | 函数挂接的子网 ID；与 `shared_network_id` 成对 |
| `basic_quota_limit` | number | `50` | 否 | 基础档每日调用配额（正整数） |
| `standard_quota_limit` | number | `100` | 否 | 标准档每日调用配额（正整数） |
| `gemini_db_endpoint` | string | 无 | 是 | 数据面地址 |
| `gemini_db_port` | number | `8000` | 否 | 数据面端口 |
| `gemini_db_protocol` | string | `http` | 否 | `http` 或 `https` |
| `gemini_db_region` | string | `cn-north-4` | 否 | 签名区域 |
| `gemini_db_access_key` | string | 无 | 是 | 数据面访问密钥 ID（敏感） |
| `gemini_db_secret_key` | string | 无 | 是 | 数据面访问密钥 Secret（敏感） |

### 6.2 几处容易填错的位置

- 网关应用与配额名称不允许连字符，实现里已统一下划线，`app_name` 仍须字母开头。
- 共享网络与子网必须成对提供，否则 `check` 块在计划阶段直接报错。
- 档位配额必须是正整数；达到上限后网关直接返回 429，函数不会执行。
- 数据面凭据随部署包进入函数运行期配置，状态文件需按密文管理。

## 7. 初始化数据表

两张表需在首次部署后创建一次（脚本幂等，重复执行无副作用）：

```bash
python src/scripts/init_tables.py
```

预期输出：

```text
[INFO] table <namespace>_tenants ready
[INFO] table <namespace>_products ready
```

## 8. 创建资源

```bash
cd infra
terraform plan -out=mtsaas.tfplan
terraform apply mtsaas.tfplan
```

`terraform plan` 预期会创建以下资源：

```text
Plan: 27 to add, 0 to change, 0 to destroy.
```

构成：1 个日志组与 3 条日志流、3 个委托、1 个自定义策略、3 个函数、
1 个事件通道与 1 个事件源与 1 条订阅、1 个分组与 1 个环境（共享环境时为 0）、
5 条 API、5 条发布记录、1 个管理端应用凭据、1 个 appcode、1 条应用授权、
2 档配额与 1 条配额绑定。

`terraform apply` 完成后预期输出：

```text
Apply complete! Resources: 27 added, 0 changed, 0 destroyed.
```

## 9. 读取部署输出

```bash
terraform output
```

预期列出以下输出名：

```text
admin_appcode        api_base_url         api_stage
basic_quota_id       basic_quota_limit    catalog_api_function
catalog_api_log_stream_id                 catalog_paths
control_plane_paths  eg_channel_id        eg_source_name
events_audit_function                     events_audit_log_stream_id
log_group_id         project_id           region
standard_quota_id    tenant_admin_function
tenant_admin_log_stream_id
```

常用取值（管理端 appcode 为敏感值，需要显式取值）：

```bash
terraform output api_base_url
terraform output api_stage
terraform output -raw admin_appcode
```

## 10. 验证部署

```bash
python src/scripts/verify_e2e.py
```

脚本以只读探针校验五项：控制面可读、控制面缺凭据被拒、目录缺凭据被拒、
管理端凭据访问目录被拒、缺失租户返回 404。脚本不会创建租户或凭据，探针结束后共享实例上无残留。

预期输出结尾：

```text
[PASS] cloud-probe verification completed
[PASS] result: .../verify-result.json
```

## 11. 手工验证示例

先取基地址、环境名与管理端 appcode：

```bash
export BASE="$(terraform output -raw api_base_url)"
export STAGE="$(terraform output -raw api_stage)"
export APPCODE="$(terraform output -raw admin_appcode)"
```

注册一个租户：

```bash
curl -X POST "$BASE/tenants" -H "X-Stage: $STAGE" -H "X-Apig-AppCode: $APPCODE" \
  -H "Content-Type: application/json" \
  -d '{"name":"acme","tier":"basic"}'
```

预期返回 201 与租户视图，其中状态为 `Active`，并带回该租户的 appcode
（该 appcode 用于调用目录接口）。

```json
{"tenantId":"<租户标识>","name":"acme","tier":"basic","status":"Active","appcode":"<租户 appcode>"}
```

列出与查询租户：

```bash
curl "$BASE/tenants?status=Active" -H "X-Stage: $STAGE" -H "X-Apig-AppCode: $APPCODE"
curl "$BASE/tenants/<租户标识>" -H "X-Stage: $STAGE" -H "X-Apig-AppCode: $APPCODE"
```

用租户凭据读写商品目录：

```bash
export TENANT_CODE="<租户 appcode>"

curl -X POST "$BASE/products" -H "X-Stage: $STAGE" -H "X-Apig-AppCode: $TENANT_CODE" \
  -H "Content-Type: application/json" \
  -d '{"sku":"SKU-1","name":"示例商品","price":99}'

curl "$BASE/products" -H "X-Stage: $STAGE" -H "X-Apig-AppCode: $TENANT_CODE"
```

用一个租户的凭据访问另一个租户的商品会返回 404——目录按 appcode 反查到的租户做键前缀隔离。

### 11.1 验证配额生效

把 `basic_quota_limit` 调到很小的值（例如 2）后重新应用，再用基础档租户连续调用目录接口。
达到上限后网关直接返回 429：

```text
< HTTP/1.1 429 Too Many Requests
```

配额为按天窗口，恢复需等下一个窗口或重新调整配额值。

### 11.2 验证事件投递

租户注册成功后会发布一条生命周期事件。查询审计函数日志即可确认：

```bash
terraform output events_audit_log_stream_id
```

在云日志服务控制台按该日志流 ID 检索，预期看到事件标识、类型、来源与完整内容。
首次投递可能因授权传播而延迟，重试或等待数分钟。

**部署完成后请按本节步骤在真实环境自行验证，确认功能可用后方可视为交付完成。**

## 12. 更新函数代码

```bash
python src/scripts/build.py
cd infra
terraform apply
```

部署包内容变化会触发对应函数更新。预期输出包含被更新函数的替换或就地更新。

## 13. 释放资源

```bash
cd infra
terraform destroy
```

预期输出：

```text
Destroy complete! Resources: 27 destroyed.
```

销毁会删除函数、分组/环境/API/发布、应用凭据与 appcode、两档配额、事件通道与订阅、
日志组与日志流、三个委托与自定义策略。
数据表与网关专享实例属共享资源，不在本目录的清理范围；需要清空时执行
`python src/scripts/init_tables.py --drop`（仅作用于本命名空间的表）。

## 14. 故障排查

| 现象 | 定位方向 |
|-|-|
| 函数报「找不到入口」 | ZIP 根目录必须有 `tenant_admin.py` 等处理文件，入口为 `<文件名>.handler` |
| 调用返回 401 | 是否缺少 `X-Apig-AppCode`；凭据是否已授权到目标环境 |
| 调用返回 404 且路径正确 | 是否遗漏 `X-Stage` 请求头 |
| 租户注册返回 403 | 控制面委托是否具备网关管理面权限；角色需按项目级挂载 |
| 租户注册返回 500 | 查看控制面函数日志中的失败步骤；入驻流程会在失败时写回 Failed 状态 |
| 目录请求返回 403 | 租户状态非 Active，或 appcode 未绑定配额 |
| 事件未投递 | 事件 `source` 必须与注册事件源同名；订阅创建后授权传播需时间 |
| 达到配额后持续 429 | 配额为按天窗口，需等下一窗口或调整配额值 |
