# 本地合成演示

这是一个不联网的合成演示，用来看三层记录、跨任务读取、共享开关，以及发送状态不明时拒绝重发。需要 Python 3.12 或更新版本；最小演示只用标准库，不安装飞书 SDK，也不用模型账号。下面是标准 Python 命令；Windows 也可以把 `python` 换成 `py -3`。离线演示、核心检查和完整测试已在 Windows 与 Linux 验证，结果见 [测试说明](testing.md)。

## 1. 拿到代码

```sh
git clone https://github.com/suanrongbaicai/loom-team-ai.git
cd loom-team-ai
python --version
```

也可以下载仓库 ZIP。不要把自己原来的数据库、配置或登录目录复制进来。

## 2. 跑一遍记录和交付

```sh
python examples/local_demo.py
```

演示用固定 `MockModel` 提供合成文字，用真实 `loom.control.Control` 保存原件、CL 和 Context，生成并领取 outbox，再由 `MockTransport` 返回标注为 mock 的本地回执。它不理解任意自然语言，不代表真实模型完成了推理，也没有把消息发给任何人。

你会看到：

1. 成员 A 的合成介绍进入 Data。
2. CL 保存这件事的经过，Context 保存当前职责。
3. 回复先保存，之后显示 `sent（本地 mock）`。
4. 新任务读到已有档案。
5. 成员 B 在共享关闭时被拒绝；模拟明确批准后可以读到；关闭后再次拒绝。
6. 发送状态不明的另一条回复保留 `unknown`，重复发送被拒绝。

默认临时目录在运行结束时清理。想查看实际文件和数据库结构，用一个**新的空目录**：

```sh
python examples/local_demo.py --output .demo-work/first-run
```

`demo-result.json` 是演示摘要；实际原件和档案在成员目录，SQLite 在 `control/`。这些文件已被 Git 忽略，不属于公开源码。再次运行换新目录；脚本拒绝覆盖已有文件。

真实平台的共享前提、限制和空模板见 [共享策略](sharing.md)、[当前限制](status.md) 和 `framework/factory/`。配置身份、范围与凭据之后仍须独立验收。
