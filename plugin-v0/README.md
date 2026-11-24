# Better DSH 使用指南

首版验证环境为 macOS、Node.js 24.19.0 和 DSH `0.2.2-rc.1` 的 headless 模式，模型使用 DeepSeek 官方 `deepseek-flash`。准备好 DeepSeek API 凭据，并按仓库首页的命令安装固定版 DSH。

## 配置检查

选择一个干净的 Git 工作区，列出任务允许修改的已有文件。把下面的启用配置保存到工作区外，替换绝对路径、文件清单和命令。

```yaml
- id: better-dsh-checkpoint
  config:
    enabled: true
    mode: observe
    effort: high
    workspace: /absolute/path/to/your/repo
    paths:
      - src/example.py
    checks:
      - name: 目标行为
        command: python3 -B -m pytest -q tests/test_example.py
      - name: 仓库回归
        command: python3 -B -m pytest -q
      - name: 边界检查
        command: python3 -B /absolute/path/to/verify_boundary.py
    timeoutMs: 10000
```

检查命令应能在同一补丁上独立复跑，并保持工作区内容不变。验收条款分开覆盖，包括异常路径、默认参数和公开导出等。支持两项或三项不同的检查命令。

## 观察和自动收尾

从待修复仓库的 Git 根目录运行。把两个绝对路径替换成你的实际位置。

```sh
BETTER_DSH_ROOT='/absolute/path/to/better-dsh'
BETTER_DSH_CONFIG='/absolute/path/to/enable-checkpoint.yml'
"$BETTER_DSH_ROOT/experiment-v0/.runtime/node_modules/.bin/dsh" \
  --profile headless \
  --patch "$BETTER_DSH_ROOT/experiment-v0/base.patch.yml" \
  --patch "$BETTER_DSH_ROOT/experiment-v0/high.patch.yml" \
  --patch "$BETTER_DSH_ROOT/plugin-v0/checkpoint.patch.yml" \
  --patch "$BETTER_DSH_CONFIG" \
  --json --permission-mode workspace-write --approval-policy never \
  "描述本次修复，并说明需要通过的检查"
```

`mode: observe` 会执行检查并保留原有模型调用。候选收尾写入 stderr 中以 `better-dsh: ` 开头的 `would-stop` 事件。检查结果符合预期后，改成 `mode: stop` 启用自动收尾。使用 `low` 时，同时换成 `low.patch.yml` 并设置 `effort: low`。

收尾要求 Git 根目录、当前目录和沙箱的工作区根目录一致、修改位于配置范围、所有检查通过、检查前后补丁一致，且没有待处理输入或会话后台任务。脏启动、新建或删除文件、符号链接、超出范围的修改和检查失败会继续走常规 DSH 流程。命令失败、超时和跳过原因可在 stderr 查到。

设置 `mode: stop` 时可再加 `failureFeedback: true`。遇到稳定补丁上的检查退出错误，插件每轮最多发送一条带宿主来源的通知。可为检查配置最多 240 字符的 `feedbackHint`，该提示会随通知发送给模型服务。测试进程的原始输出留在本地。反馈默认关闭。

## 关闭插件后续跑

从 JSON 最后一条 `final` 事件中取 `sessionId`。使用同一工作区、DSH home、基础和档位配置，省略两项插件补丁。

```sh
"$BETTER_DSH_ROOT/experiment-v0/.runtime/node_modules/.bin/dsh" \
  --profile headless \
  --patch "$BETTER_DSH_ROOT/experiment-v0/base.patch.yml" \
  --patch "$BETTER_DSH_ROOT/experiment-v0/high.patch.yml" \
  --session-id '<上一次 final 的 sessionId>' \
  --json --permission-mode workspace-write --approval-policy never \
  "继续验证刚才的改动，检查边界场景并报告结果"
```

自动收尾的 `stopped` 事件会记录 `source: "host"`、会话和补丁指纹。可用它与收尾摘要对应。自动摘要只确认配置的检查结果，试用时用独立验收决定是否交付。检查命令可以通过改动后的代码执行项目逻辑，配置时应选可信的仓库和命令。

## 本地验证

从 Better DSH 仓库根目录运行以下命令。冒烟使用临时 Git 工作区和本机模拟端点。

```sh
node --test plugin-v0/checkpoint.test.mjs
python3 -B plugin-v0/smoke.py
```

更多测量条件与结果见 [实验结果](../analysis/README.md)。
