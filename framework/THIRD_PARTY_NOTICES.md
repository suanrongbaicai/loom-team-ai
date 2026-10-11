# 来源与适配

本框架的 AGENTS、loom-work-records 和人员权限模板从用户指定的执行样式 v1.9 提取。该设计借鉴 HA7CH 的规范和资料组织方法，保留下列许可。loom-onboard 为依据用户基准和本任务要求编写的自然接引技能，未安装上游固定访谈模板。

HA7CH/ai-native-company 固定提交：90e4fb24ec326f5f279dead70633fc4b9e3514b1。

飞书传输采用官方 [larksuite/oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python) 的 PyPI lark-oapi 1.7.3（MIT）。目标 Python 3.12 / Windows x64 所需依赖及下载物 SHA-256 固定在 requirements-feishu-win-py312.lock，原始 wheel 自带许可复制在 licenses/feishu-sdk。安装时保留各包 dist-info；框架包不含凭据、客户资料或旧传输二进制。

## 随包许可索引

自有内容的 Apache 2.0 不替代下列第三方条款；原许可证和版权声明随相应材料保留。

| 材料 | 版本 / 用途 | 许可与原文 |
|---|---|---|
| Apache ECharts | 6.1.0，报表图表 | Apache-2.0；[LICENSE](templates/reports/vendor/echarts-LICENSE)、[NOTICE](templates/reports/vendor/echarts-NOTICE) |
| AntV G2 | 5.4.8，交互图表 | MIT；[运行库许可](templates/reports/vendor/antv-g2-LICENSE) |
| AntV 技能材料 | G2 / GPT-Vis 的方法与发布快照 | MIT；[G2 技能许可](skills/antv-g2-chart/LICENSE)、[GPT-Vis 技能许可](skills/antv-gpt-vis/LICENSE)；`published-skills/` 中对应版本也保留许可 |
| Tabler | 1.6.0，报表样式与布局 | MIT；[LICENSE](templates/reports/vendor/tabler-LICENSE) |
| Frappe Gantt | 1.2.2，排期组件 | MIT；[LICENSE](templates/reports/vendor/frappe-gantt-LICENSE) |
| certifi | 2026.7.22，飞书 SDK 依赖锁中的 CA 证书包 | MPL-2.0；[随包许可](licenses/feishu-sdk/certifi-2026.7.22/LICENSE)，不属于 MIT 依赖 |

报表组件的固定来源和完整性记录见 [vendor/sources.json](templates/reports/vendor/sources.json)，用途见 [报表来源](templates/reports/NOTICE.md)。飞书 SDK 的版本和 wheel 摘要见 [依赖锁](requirements-feishu-win-py312.lock)，各包原许可保留在 `licenses/feishu-sdk/`；依赖实现和安装凭据没有打进这个源码快照。

## HA7CH MIT License

Copyright (c) 2026 HA7CH

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## 本项目格式器与外部前置依赖

`loom/xlsx_mobile.py` 复用本项目既有的两列手机表格格式器，并固定 ZIP 时间，使相同保存版本可复现。当前授权、原话定位、报告版本、保存和发送检查由框架负责。

微信采集接口使用另行部署的固定加载器与账号/来源适配器，调用 `wechatauto-replica 1.2.2.5` 导出函数。本仓库没有复制或分发该实现、数据库、安装目录或账号缓存。适配器摘要见 `loom/wechat_runner.py`，登录账号核验使用 `psutil 7.2.2` 原生句柄元数据，不复制其实现。部署者须独立检查依赖许可并保留安装包的许可和 dist-info。

图片转换可配置为独立部署的 `imageio-ffmpeg 0.6.0` 官方 Windows x64 wheel 中的 FFmpeg 7.1 essentials；发行包 SHA-256 为 `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a`。来源：<https://pypi.org/project/imageio-ffmpeg/0.6.0/>。本框架不包含或分发该可执行程序，部署时保留它自身的许可。

Codex 模型目录应与已安装版本一致。已有部署接口使用过 Codex 0.154.0 的官方模型元数据，来源：<https://github.com/openai/codex/tree/rust-v0.154.0/codex-rs/models-manager>。模型元数据和账号认证不纳入本仓库。
