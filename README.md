# Better DSH

Better DSH 是面向 DeepSeek Harness（DSH）的自动收尾插件。它在明确限定的 Git 文件范围内运行定向检查、回归测试和边界检查。检查通过且补丁保持稳定时，可以在下一次模型调用前结束任务。

## 首次试用

首版目标环境为 **macOS、Node.js 24.19.0、DSH 0.2.2-rc.1 的 headless 模式**，模型使用 DeepSeek 官方 `deepseek-flash`。插件需显式启用。第一次试用使用 `mode: observe`，可在日志中查看适用范围与候选收尾点。

在 Better DSH 仓库根目录执行：

```sh
npm ci --prefix experiment-v0/.runtime --no-audit --no-fund
node --test plugin-v0/checkpoint.test.mjs
```

随后在自己的 Git 仓库里配置文件范围、测试命令和 DeepSeek 凭据。 [配置、运行与续跑](plugin-v0/README.md)

## 验证结果

固定 `low` 的一批 6 题配对对照中，插件开关两组均交付 12/12，开启后输出 tokens 减少 32.4%，估算费用减少 32.1%。历史上也观测到通过配置检查却未通过独立验收的补丁。试用适合目标明确、有可靠回归检查的代码任务。 [实验结果与适用范围](analysis/README.md)

## 仓库结构

| 目录 | 内容 |
|---|---|
| `plugin-v0/` | 插件源码、DSH 配置和测试 |
| `experiment-v0/` | 固定版 DSH 的锁文件、配置和基线实验 |
| `analysis/` | 实验结果和计量口径 |

项目原创代码与文档使用 [MIT License](LICENSE)。项目内的第三方材料沿用各自的许可。 [第三方许可说明](THIRD_PARTY_NOTICES.md)
