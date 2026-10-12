# 出厂包安装与升级

出厂包只含代码、规则、技能、运行入口、测试和配置骨架；**不含任何数据、凭据、真实账号、真实 id、测试残留**。`tools/factory.py verify` 在出库前逐项检查；安装前先对包再校验一次（FACTORY.json 逐文件 SHA256）。

## 安装前提（缺任何一项，不得认定新装可用）

模板与 `provision.py` 只负责代码、空库、策略、技能和目录；下列项目必须在现场另行准备并逐项核对，缺一项就不是可用的新装：

1. **凭据文件**：`controller.json` 的 `runtime.credentials` 指向一个**文件**（`launch-job.ps1` 的 `CredentialFile`），内含各工作账号的登录凭据；由运维本人在受保护目录创建，仅管理员可读，不进包、不进仓库、不回显。
2. **传输密钥**：随机生成、至少 32 字符的文件，控制器与全部网关共用同一份；`transport_secret_file` 指向它，权限仅管理员。
3. **每个工作账号**：独立 Windows 账号；授予"作为批处理作业登录"（`SeBatchLogonRight`，只授该账号，不给管理员）；其工作目录与 scope 记录目录的 ACL 只允许对应主体；**该成员本人用官方方式登录自己的模型账号**，在其个人 `CodexHome` 下生成 `auth.json`（程序不读、不代办、不复制）。未登录前，该 scope 不能回应，属预期。
4. **飞书应用**：应用凭据文件（含 `app_id`、`app_secret`）、事件订阅为长连接，网关配置中的 `allowed_chats` 写 `[企业 tenant_key, 会话 chat_id]` 成对；`controller_ingress` 路径固定为 `/ingress`，端口为控制器本机端口。
5. **运行包**：官方 Codex、Python、SDK 依赖目录（核验后放入，不得是链接）、模型目录文件。可用包内工具取得并核验固定版本：`python tools/provision_codex.py --root <安装根> [--proxy <http 代理>]`（下载官方 Windows 包并核对 SHA256）、`python tools/provision_feishu_sdk.py --root <安装根> [--proxy …]`（按 `sdk-download-manifest.json` 与 `requirements-feishu-win-py312.lock` 离线安装已锁定依赖，要求框架已放在 `<安装根>\Framework`）。两者都必须明示 `--root`，不再带任何现场默认路径。
6. **启动命令**（路径含空格必须加引号；`-LogDirectory` 须**先创建**，脚本检查它已存在；技能部署位置是各 scope 的 `<worker_home>\Work\.agents\skills`，工作模型从这里读取）：控制器 `tools/run-controller.ps1 -Python <python> -Config <controller.json> -LogDirectory <日志目录>`，每个网关 `tools/run-gateway.ps1 -Python <python> -Config <gateway.json> -LogDirectory <该网关目录>`；各以计划任务登记，先控制器后网关。
7. **可选的 scope 参数**：`scopes.<名>` 可加 `model`、`reasoning`（例如指定该成员的模型与推理强度），不加则用默认。

## 全新安装

1. **准备**：目标电脑上已有独立 Windows 工作账号、官方 Codex 完整运行包、SDK 依赖目录（不得为链接）、飞书应用及其凭据文件。凭据只由本人放入受保护目录，不进包、不进仓库。
2. **校验并展开**：`python tools/factory.py verify --dir <包目录>`，通过后复制到框架目录。
3. **写策略**：以 `deploy/policy.minimal.json.template` 为骨架，填成已核验的成员、应用、scope（须有真实核验依据，不能凭昵称）。保存为 policy 文件。
4. **创建运行库并发布技能**：`python tools/provision.py --control <新控制目录> --policy <policy 文件> --approval "<批准依据>" --skills <包内 skills> --version <出厂版本> --workspace <scope>=<该 scope 的 worker_home>\Work ... --personnel-template <包内 rules/personnel.template.md>`。它拒绝覆盖已有运行库，建立空库、写入策略、发布并部署全部技能、创建 scope 根与人员文件草稿。
5. **写配置**：以 `controller.json.template`、`gateway.json.template` 填写（先完成上面的安装前提）（可选能力从 `controller.optional-sections.json.template` 取整段，每段须有独立批准）。
6. **人员文件**：把 scope 的人员与权限文件填成"已核验身份 + 行为规则"，不要写入测试或采集得来的事实；团队成员的认识由引导流程在真实使用中建立。
7. **启动**：先控制器，后网关。第一条真实消息触发引导；不要用测试消息。

**状态说明**：本包的工具已在隔离目录和 Windows 上演练（构建与校验、provision、复位），但**还没有在一台新机器上完整新装过**；第一次真实新装之前，不得把本文写成"新机验收已完成"。

## 升级（只换代码，不带数据进出）

替换框架目录中的 `loom/`、`rules/`、`tools/`。已部署技能换版本用 `tools/skill_upgrade.py`：按 `--scopes` 逐个范围切换，可先试一个范围再扩大，切回旧版同样用它；新增技能用 `tools/skill_add.py`（版本发布不可变，旧版保留）。升级前备份；升级不改数据库、scope 记录和配置。数据库结构变化须有显式兼容迁移，旧程序遇到新结构会停止。`deploy_update.py` 升级失败时会自动备份并降级数据库；升级成功后又要退回旧代码时，先停服务、备份 `runtime.sqlite`，用**新代码**执行 `python -c "import sys; sys.path.insert(0, '<framework>'); from loom.database import downgrade; downgrade(r'<control>/runtime.sqlite', <旧版本号>)"`，再换回旧代码。例如从 6 退到 5 只删除 `member_preferences`（期间保存的个人偏好随之丢失），其他记录不变。

## 复位

只用于重置到空库：`tools/wipe_rebuild.py`（计划、备份、清空重建、整体恢复）。它保留 policy、技能与配置，清空业务数据，先备份可整体恢复。乙现场不提供"一键恢复出厂"；需要时由本包重新安装。

## 出厂验收

见 `FACTORY-ACCEPTANCE.md`。
