# Loom

[![License: Apache 2.0](docs/images/license-apache-2.0.svg)](LICENSE)

给正在把 AI 放进真实团队的人参考的工作框架。它来自一套已经在用的工程，公开前做了脱敏。这里没有一键部署。想用的人拉取后按自己的团队改；我会继续维护这个仓库。

隔一天、换个会话，之前说过什么、改过什么、为什么这样做，都应该能找回来。回复不能只靠模型说完成了，还要有保存记录和实际回执。

## 记录怎么放

三层是三个抽屉，管存成什么样。知识性质是贴在内容上的标签，管这是哪种知识。两者不互相替代。

| 层 | 保存什么 |
|---|---|
| Data | 原始消息、实际动作和结果 |
| CL | 一件事的经过，以及每次真正采用的资料 |
| Context | 有来源的当前认识 |

| 性质 | 放在哪 |
|---|---|
| 做法 | 技能，有版本；对话里教的先当候选，批准后才对大家生效 |
| 定论 | Context；私聊里形成的，本人确认后可以只把结论共享到群 |
| 个人偏好 | 只在回应本人时使用 |
| 易变事实 | 留在原处，登记复核日期，到期再查 |
| 进行中 | 留在事情经过里，不写进 Context |

模型判断意思。程序核对身份、权限、版本和实际动作。检查通过，不等于理解一定正确。

## 一条消息怎么走

![Loom 架构](docs/images/architecture.svg)

## 视频

- [面向团队](docs/videos/loom-for-teams.mp4)：Loom 想解决的问题，以及它怎么帮团队把事情接着做下去。
- [面向技术人员](docs/videos/loom-technical-breakdown.mp4)：三层记录、技能放在哪，以及一条消息从进来到发出。

视频里的人名和数据都是合成的。

## 怎么用

主要拿来参考，不是安装包。席位数量、接入的平台、谁来批准，都来自原来的那套团队。换一个团队要自己改，没有单独的简易部署步骤。

适合把仓库交给 AI，让它对照 `theory/` 和 `framework/` 按你的团队改。真实身份、聊天、配置和运行数据不在这个仓库里。

| 目录 | 内容 |
|---|---|
| `theory/` | 为什么这样设计 |
| `framework/` | 当前这套实现 |
| `docs/` | 状态、共享前提、限制 |

自有内容采用 [Apache 2.0](LICENSE)。第三方材料保留各自条款，见 [NOTICE](NOTICE)。

## English

Loom is a reference framework for putting AI to work inside a real team. The public repository is a desensitized production codebase, not an installable product. There is no one-click deploy. Pull it and adapt the seats, platform, and approval rules to your own team. The repository is actively maintained.

Records sit in three layers: Data for sources and actions, CL for case history and citations, and Context for current understanding that has a source. Methods live in versioned skills. The host checks identity, permission, version, and delivery. The model decides what the content means.
