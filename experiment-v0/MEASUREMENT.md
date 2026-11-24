# 数据与指标

## 文件在哪里

每个批次写入 `runs/<时间戳>-<smoke、baseline 或 hard>-<随机后缀>/`。

| 文件 | 用途 |
|---|---|
| `manifest.json` | 模型路由、DSH 版本、题目、重复次数、运行顺序、超时、固定计价和实验输入哈希 |
| `summary.json` | 每组运行数、通过数、分类通过数、已核对档位数、中位耗时、用量与费用 |
| `<题号>-<档位>-<重复编号>/result.json` | 独立评分、结束状态、开始和结束时间、耗时、会话 ID、原始日志路径、档位核对、工具调用数与用量 |
| `<题号>-<档位>-<重复编号>/events.jsonl` | headless 事件，含工具轨迹、结束事件、每个步骤的可用用量 |
| `<题号>-<档位>-<重复编号>/stderr.log` | 启动、提供方和运行诊断 |
| `<题号>-<档位>-<重复编号>/workspace/` | 保留的任务工作目录，可对照 `tasks.py` 中的原始 fixture |
| `dsh-home/sessions/` | 持久会话事件，含真实请求配置和重试尝试 |

## 读数据的顺序

1. **核对身份和档位。** 运行器要求 `request/header` 中的 `provider`、`model` 和 `reasoningEffort` 与实验组一致。检查 `route_verified` 是否等于 `runs`。headless 的开始与结束事件用来检查步骤是否对齐，持久会话用来核对请求头。 [DSH 请求头](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/docs/subsystems/llm-streaming.md)
2. **判分。** 以独立验证的 `passed` 为准。启动错误、超时与会话内错误分别保留在结果里。任务完成后才执行验收。
3. **计量。** `inputTokens` 是未缓存输入，`cacheReadTokens` 是命中输入，`outputTokens` 是总输出，`reasoningTokens` 在提供方有报时单列。输出已经包含推理 token。固定版 headless 的 `step_end.usage` 会合并该步骤已报告的重试用量；若某次尝试缺样本，会省略完整用量。脚本把不完整指标写成 `null`，组汇总保留缺失数。 [固定版事件与用量实现](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/packages/bundle/headless/src/json-stream.ts) · [DSH token 计量](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/packages/llm/token-meter/README.md)
4. **计价。** 将已核对的各组按同一套高峰美元费率估算，避免比较结果被时段价格变化影响。费率快照为每百万未缓存输入 $0.30、命中输入 $0.006、输出 $1.20。每成功任务费用是该组所有运行的费用之和除以通过数；有缺失用量时不输出这个指标。实际账单以账户记录为准。实验关闭了网页工具、模型标题、子代理和自动会话压缩，避免这些额外调用进入比较。 [官方计价](https://api-docs.deepseek.com/quick_start/pricing/)
5. **解释时间。** `elapsed_s` 从启动 DSH 到结束，含模型、工具和进程开销，不含独立评分。首 token 延迟需要流式边界埋点。headless 在步骤提交时才输出文本和推理事件，因此本版只报端到端耗时。 [固定版 headless 事件实现](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/packages/bundle/headless/src/json-stream.ts)

## 分析记录模板

| 指标 | 固定 high | 固定 low | 自动升档（后续） |
|---|---|---|---|
| 已核对的运行数 / 总数 | | | |
| 局部修复通过 / 总数 | | | |
| 跨文件通过 / 总数 | | | |
| 长日志通过 / 总数 | | | |
| 未缓存输入 / 缓存输入 / 总输出 | | | |
| 用量缺失次数 | | | |
| 每成功任务估算费用 | | | |
| 单任务耗时中位数 | | | |
| 一档成功、另一档稳定失败的题号 | | | |

每个值得复查的失败，记下题号、目录、失败阶段、当时能观察到的证据、是否需要升档。同一批内，先看同题配对，再看全组平均；不要把重复运行当作独立新题。

## 排查

- `check` 报版本旧时，按 README 安装项目版。实验通过固定路径找二进制，不依赖终端里默认的 `dsh`。
- `stderr.log` 显示认证或余额错误时，先解决账号问题，再用 `smoke`。新版 headless 在这个实验中使用 API Key，平时 GUI 的登录凭据不会被读入。
- `route_ok=false` 时，看 `request_configs` 和 `stderr.log`。空请求头通常表示请求没有走到持久化阶段。读取 `manifest.json` 核对配置版本。
- 用量为 `null` 时，看是否每个 `step_start` 都有匹配的 `step_end`，以及结束步骤是否有 `usage`。保留原始事件；未知用量不能按 0 计。
- API 返回限流或网络不稳定时，先查看单次日志并交错两组的时段。只有需要重跑时才开新批次。
