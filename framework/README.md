# Loom 通用实现

我把记录、权限、调度和平台接入放在这里。初次使用请从根目录的 [本地示例](../docs/quickstart.md) 开始；下面是部署者和贡献者需要的接口说明。

当前是开发版，离线检查与真实平台验收分开。已有独立 bot 身份、会话并发、主动私聊、图片接续和当前群按需历史；其他模型提供方、私聊统一指挥跨群工作、排队补充合并和新控制台还有待办，见 [当前状态](../docs/status.md)。

`skills/` 是框架方法，`published-skills/` 是发布版本快照。按批准版本加载；目录中有文件不能代替实际加载核验。旧图片兼容测试只使用 `tests/fixtures/legacy_image_v1.py` 的合成记录。

## 记录与部署接口

v1.10三层实现：`originals`把获准取得的文件实际保存在受保护Data中，保留文件名、格式、字节摘要、来源及取得范围；当前单份原件入口上限32MiB，图片预览入口4MiB。超过入口限制必须明确未完整取得，不能把外部链接当作已存原件。保存与解析分开，视频仅首帧、缩略图、OCR等按实际覆盖说明。`materials/material/file`提供登记、原件读取和飞书文件取得；微信及获准网络接口同样保留取得的原字节。

`case_passage`由模型给出事项、经过和真正采用的`{id,revision,quote}`，宿主检查本轮实际读取、当前受众权限、短原文及固定版本，直接把短链接放在该段经过下。`citations`返回各次采用及CL块位置；新一次采用使用新提交号，同次重试沿用原号。CL可引用Data、CL或Context；普通Markdown链接和读过未采用不自动计数。历史版本用`read(id,revision)`，引用登记不补造旧版没有证据的采用。维护入口`loom.references.rebuild(control)`只由受保护动作原件和匹配的CL提交恢复索引，不能依据任意链接猜测。

各层语义仍由模型决定。`case_catalog`提供有权限的事项和Context目录；发信位置与话题资料共同帮助找原事项，换会话不强制新建CL。每阶段凭据将完整已保存答复与实际动作目录接回对应CL，送达结果由宿主追加；Context没有新认识无需重写。检索排序、任意改述和正式团队体验仍需分别验收，不把接口通过算成全量业务完成。

`action_log(recording,after)`按已核验的交付凭据分页读取实际Data动作原件，包括发送结果。须重新核对该任务全部已读资料对当前受众的权限，不能以答复共享权限自动开放更广的操作历史。`citations`支持按来源、版本和原文片段筛选，返回逐次位置和该片段次数；读取旧版未登记资料不凭空增加采用记录。

- `loom/`：文件记录、权限、任务、规则交付、技能版本、平台接收及保存后发送。
- `rules/`、`skills/`：由用户确认基准形成的适用正文与技能。技能发布、部署、实际加载分别留证。
- `tools/`：运行入口和工程工具。发布包只纳入明确列举的运行入口；目标电脑专用诊断和 bootstrap 不打入通用包。
- `tests/`：合成资料测试，不包含真实团队数据和凭据。

团队人员、平台应用/群、批准规则、目录、运行时及账号映射单独配置，位于项目的 `team/`；真实工作原件留目标设备的获准目录。密码、模型认证和应用密钥只存受保护的运行配置，不进入本仓库或通用包。

Windows 部署需要已验证的普通工作账号、其专属工作区、官方 Codex 完整运行包及独立沙箱。官方模型宿主以无本机工具环境运行，通过 HTTP MCP 使用当前授权的记录和命令接口。通用命令由普通账号下的独立 AppContainer 执行；命令工作区根由管理员拥有，容器仅获本任务修改权。新技能目录部署后再次验证实际加载及模型工具正向使用。系统 hook 安装器明确接收 `--python` 路径，不猜测客户盘符。

配置 `model_backends` 可改用共享官方 app-server：`bots` 按机器人设置前台任务容量，`resources` 按独立授权目录设置总容量；同一授权目录只允许一个资源及一个宿主，后台任务同样计入资源总容量。资源配置 `type="codex-app-server"`、`worker_scope`、`max_active`，由 scope 的 `backend` 选择。宿主为每个任务创建独立临时 thread，在 thread/start 和 turn/start 均显式设置 `environments=[]`。模型按已配置名称和推理级别运行，原生文件、命令和 Windows 沙箱配置路径不可用；文件与命令仍通过逐任务授权的 `loom_` 动态工具执行，命令保留现有普通账号 AppContainer 隔离及资源上限。模型目录若使用 Code Mode，工具在官方 JavaScript isolate 内转接到相同任务能力，不获得宿主文件系统或网络。取消只中断对应 thread/turn；宿主故障不重放任务。部署前须运行真实 CLI 加本机假服务的并发、伪造原生调用和隔离测试，不能仅用 mock-model 的工具格式代替实际模型目录。

模型执行采用无本机环境的官方模型宿主、HTTP MCP 和逐命令 AppContainer。部署者仍需验证本任务与子进程写入、越权拒读、ACL 扩权拒绝、超时、取消、输出上限及进程回收；公开版离线检查不代替目标设备的实际隔离验收。

提案由模型提交具体内容；人通过已核验私聊或 @ 对应 Bot 的群消息发送 `/loom-proposal <编号>` 查看，发送 `/loom-vote <编号> <版本> approve` 或 `reject` 明确表决。只有独立的完整命令触发表决，引用中的文字不算；平台发送者、保存的原消息及当前接收范围决定身份与可见性。普通模型没有投票或生效工具。私聊提案不会因其他人有投票权而自动向其披露。提案有效期仅从已确认规则读取，未规定时不擅设到期或默认同意。

权限类提案可包含 `policy_patch`，只支持 `grants`、`decision_rules`、`handoff_rules`；非全员规则还须明确允许对应 `policy_fields`。管理员审阅后运行 `tools/apply_proposal.py --control <受保护管理根> --proposal <编号> --version <版本> --expected-epoch <当前权限版本>`。入口核对完整批准、当前规则、原内容、有效期及受影响配置是否变化，然后在同一事务登记实际生效与新权限版本。不能用它改账号绑定、目录或技术配置。撤销未生效提案与撤销已生效权限分开，后者走现有管理配置入口，不能只撤掉提案记录。

本批新增 `proposal_guards` 表，保留原提案和旧票；没有完整冻结依据的旧提案不自动补成有效批准。旧程序遇到新增表会停止，回退须先检查结构兼容，禁止旧库覆盖。上述平台批准入口仍须在目标设备和真实团队完成对应验收。

微信新采集使用独立 `wechat_captures` 授权目录，已有原件复用授权不会自动变成新采集授权。管理配置必须分别给出账号部署映射，以及获准的 actor、scope、全部 recipients、固定用途、稳定联系人标识、可读历史区间和授权有效期。模型通过 `wechat_capture_targets` 查看可用条目，再以稳定请求编号、明确 UTC 秒区间 `[start,end)` 和原样用途调用 `wechat_capture`。接续使用同一编号；成功返回原 Data，不重复采集；中断状态保留并要求受保护核查，不能假设未采集而自动重跑。

采集器由 `controller.json` 中 `wechat_capture.enabled=true` 显式启用，配置 `deployment_approval`、`python`、`adapter_dir`、`upstream_root` 和 `accounts`（按 account_id 映射 enabled/db_root/account/wxid/可选 alias）。路径与账号均由管理配置提供，模型不能传入。当前现场固定适配器及依赖仍是独立部署前置条件，固定内容由 `loom/wechat_runner.py` 和既有加载器核验；通用包不夹带旧工程、账号缓存或微信资料。新缓存与导出只在管理根下 `WeChatRuntime`，不新增 C 盘缓存。

新进程先加入 Windows Job Object，再获工作参数；读取每批消息前向仍有权限的控制器请求许可，父进程也持续检查取消和成员范围。读取前加时间条件，固定目标表，账户切换、分片异常、部分成功均不作为完整原件发布。进程树在结束、取消或控制器退出时停止，正常返回前核对所有子进程已退出。原件及读数回执经再次核对后登记，新表 `wechat_captures` 仅增加本次采集状态，不导入旧运行库。实际手机历史完整性、跨分片一致快照及附件解码仍不声称已验证。

当前登录账号由新框架读取进程句柄路径核验，要求采集 Python 的 `psutil==7.2.2`；不对无关句柄指向的文件逐个查询属性。账号根必须唯一匹配，进程变化、句柄查询失败或未审查的依赖版本继续停止。受保护 `failure.json` 只记录阶段、异常类型及是否已进入数据库步骤，不记录密钥、原文或异常全文。

失败后的 `reconcile_empty_failure` 仅供管理进程使用，不在 MCP 暴露。先停控制器并核实全部执行已结束、进程树已清理、没有暂存原件/正式原件/部分导出；填入具体核查依据，保留失败尝试后只允许原请求号、原参数的一次重试。发现输出或结束状态不明时应核对既有结果，不能删除账本、另换请求号或以重试掩盖原失败。

启动器支持普通账号的按需计划任务。模型与命令在 Session 0 的实际运行须由部署者核验。受保护的 SYSTEM 辅助程序仅创建并限时持有逐命令窗口站/桌面，不运行模型命令；命令仍在普通账号的 AppContainer 内执行。任务结束核对清理回执并移除计划任务元数据。系统 AppContainer profile 临时使用 C 盘用户目录，工作文件和模型缓存位于批准的数据盘。实际注销后运行、完整中断恢复与正式服务切换尚未验收；不能由 Session 0 检查推断全部无人值守条件通过。

附件工具 `wechat_attachment` 只接收已保存微信原件和消息索引，逐次核对完整受众，不接受任意磁盘路径。控制器的 `wechat_capture.media_converter` 配置须由管理员提供已核验的 `executable` 和 `sha256`，用于 wxgf 图片和视频首帧；解码程序部署在非 C 盘，输出仅在受保护临时目录。它不等于已读音频或整段视频；文件正文仅支持已核对标识的文本、DOCX/XLSX，其他格式明确返回未读。图片缓存不省略当前权限检查。失败诊断另保存源码文件名、行号和函数名，不含密钥、原文或异常全文。

事件只增加经过时，`record` 使用 `append=true`、已读 `base` 和新增文字，沿相同版本冲突、权限、恢复及历史机制提交，避免模型每阶段重写完整档案。完整回复只交 `reply.text`，无需再抄入 `append_data`。

`save_work` 可以合并模型已决定的最多六项记录更新及一条文字或文件交付；中途文字设 `reply.finish=false`。逐项权限、版本、回执不变，部分失败保留已保存项并不发送成功答复。同一会话后续任务会收到最近两轮中当前仍获准读取的记录入口，实际是否相关及如何更正仍由模型判断。

每次模型交付（`reply`、`send_report`、`save_work.reply/report`，含中途说明）必须附 `recording`：正式事件编号 `events`、实际采用且已读取的资料编号 `basis`，以及 `data/event/context` 各自的状态 `updated/no_change/pending`、理由 `reason` 和实际提交编号 `commits`。宿主核对自上次交付检查以来的新增提交、权限和读取版本，保存凭据并在相关事件中链接完整回复，再发送这一份已保存原文；不能仅写 `record_state=saved`。无变化无需重写，语义是否准确仍需实际内容审核。`append_data` 接到已有 Data 事件编号时解析回正式事件，不新开嵌套入口。当前可读的 `context-index-*` 登记导航随规则提供，实际相关性由模型判断。每批动作审计提交后同步保存对应 `Data/actions`，失败阻止继续并保留已提交证据；启动恢复仅按原审计补齐缺件，另记实际补记时间。长任务仍由模型在有意义的阶段形成结果后保存认识再继续，不能以机械动作记录代替。送达不明保持 unknown，不自动重发。

遇到本地网络的 WebSocket TLS 故障，可由管理运行配置 `runtime.model_http_only=true` 选择同一官方登录和模型的 HTTP 传输。`runtime.model_catalog_json` 可指向与已部署 Codex 版本一致、经核验的官方模型目录，避免每轮重复超时刷新；随模型或 Codex 升级同步更新，不改变模型名称、推理级别、权限或采购方式。这些是可选部署配置，不是团队行为要求。

本机维护面板可由 `tools/build-panel.ps1 -Output <绝对exe路径>` 使用现有Windows .NET Framework编译。启动参数为团队独立JSON配置，字段为python、bridge（panel_bridge.py绝对路径）、work_dir、database（现有runtime.sqlite）及tasks（已有计划任务名数组）。面板无网络监听，不向Bot暴露工具，不改权限或模型。状态按打开/手动刷新读取，只启动已配置且Ready的未运行服务；不会重启运行中服务或重发unknown消息。维护命令限本机管理员，60秒/512MiB进程树/64KiB输出，取消或父入口退出清理普通后代；这属于防资源失控，不是对恶意管理员的隔离。运行目录及配置应继承现有受保护控制根ACL，源码/配置/真实资料分别交付。

能力接续由 `controller.json.capabilities` 显式启用：`enabled`、工程批准依据 `approval`、非C盘受保护目录 `projects_root/tools_root`、按已核验成员配置的 `rules`。每条规则包含 `active/approval/scopes/approver/grants`，可选 `expires_at`；首版只接受当前真实成员批准自己的已登记范围，不能由模型指定审批人、变更配置或代替其他成员授权。具体配置、账号映射及批准原件留在团队部署，不放通用包。

`loom-capabilities` 是实际发布和全文加载的方法技能，配合八个 MCP 工具处理查询、申请、项目保存、检查、发布、执行、状态和获准联网请求。沿用官方 Codex 理解和编程；源码快照、相关检查及发布物不可覆盖。已有批准覆盖直接使用；新增范围以已保存的中文消息说明，真实平台身份回复对应消息“同意”后接回原任务，跨任务沿用原确认消息。注册、测试通过或他人自称技术人员都不能代替批准；每次取用重新核对当前身份、受众、规则及账号绑定。

首版自动执行已接通 Python 项目、明确 HTTPS GET/一次 POST 和锁定官方 PyPI 纯 Python wheel；依赖不执行安装脚本，代码仍在任务 AppContainer 内运行。POST 结果不明时不重发。微信本人资料适配只读当前绑定账号的本人字段，调用前后核对活动账号，明确本地资料库可能滞后，不读取群聊。其他宿主接口可以提出范围并保存开发项目，待受保护适配接通后继续，不因为未登记就永久拒绝，也不将“已批准”冒充“已接通”。通用桌面、原生安装器、带凭据接口等未接通部分仍须补具体工程；本版不宣称与桌面端全部工具等同。

检索由模型决定问题含义、对象对应和采用范围。`search` 支持多词同时命中、多种说法任一命中，返回版本及准确原文片段；`read/read_many` 可按字符范围继续读，逐次引用仅接受实际返回范围。`case_catalog/objects/search` 分页需延用 snapshot，当前可读资料变化后重新查。索引为进程内可重建的 SQLite FTS5，48MiB 为规范化文字加词项的输入预算（不是总进程内存上限）；大文件、容量不足或索引故障直接搜索已核验原件，不遗漏、不开额外服务、不修改持久数据库结构。每次仍验证当前获准原件，主要收益是少返回无关全文和减少往返，不声称模型语义或端到端耗时已经验证。

模型可在正式 Context 的 `record.lookup` 中保存有实际已读依据的名称、别名、类型及相关编号。它与正文同版本保存，Obsidian aliases 可直接显示；旧资料不强制改写。`objects` 只返回当前可读的明确登记项，同名多项不自动合并，关联不等于同一。登记项可带可选的 `recheck_after`（YYYY-MM-DD，不可早于当天）；当前版本到期后 `read` 与 `objects` 附 `recheck.due=true` 提示先核来源，只比较日期，不判断内容也不拦截。更改日期等同修改登记信息，须附本任务实际读取的依据；只改正文时保留原日期。业务名称对应不授予平台身份或资料权限；旧版本保留当时内容。正式身份、披露、动作留存和回复前保存沿用原机制。
# Task-bound private contact

The protected `private_contacts` configuration enables `member_find`, `contact_private`, and `contact_status` for specified apps and verified team members. It reuses app/tenant/person-bound p2p chats; otherwise the official message API obtains the p2p route. Reply ingress belongs to the recipient, with only the necessary task context and authorized basis attached. See `templates/private-contact.md`.

The separate `PrivateContacts/state.sqlite` journal retains batches, exclusions, exact recipients and send receipts; it adds nothing to the runtime database schema. Queued messages are rechecked before sending, and uncertain attempts are never replayed. This adds no scan, timer or capacity. Native capability mappings preserve historical pending requests rather than manufacturing their approvals. Model credentials stay in the existing host.


原生图片入口由 `image_generation` 管理配置显式启用。已有 Codex 原生图片产物接入、`generated_images` 查询、`send_image` 交付和 `image_status` 核对；宿主确认当前任务与产物，平台上传后发送图片，状态不明不重放。接续历史原图仍须核 bot、租户、资料范围和原图凭据。本地演示不调用真实生图，真实使用需要对应宿主与平台权限，不能把模型一句“已生成”当作图片已经送达。
群历史按需入口：`group_history` 与实时 `group_all_messages` 分开配置。只有 `group_history.enabled=true`、`approved=true` 且 `apps` 是已批准应用时宿主才接通；平台还需应用身份历史读取基础权限及 `im:message.group_msg` 实际生效。当前真实群任务派生全部身份，不接受模型自选群或用户令牌。默认唤起前1小时，单段24小时、每页50条、本任务累计3页；原件保存到当前群 Data，保护凭据留在控制区 GroupHistory，重复稳定编号不重查。不回填事件、不创建任务、不发送消息或周期扫描。
