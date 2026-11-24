# Better DSH 初版实验计划

在本机运行 DSH、任务和评分器，通过 DeepSeek 官方 API 使用 `deepseek-flash`。实验比较固定 `high` 与固定 `low`，记录成功率、用量和延迟。实验包包含 9 道基线题和 4 道进阶题、独立判分、固定版本配置和日志汇总。

## 1. 进入目录并离线检查

```zsh
cd experiment-v0
node --version
python3 -B bench.py self-check
```

从 Better DSH 仓库根目录进入实验目录。需要 Node.js 24.9 或更新版本，以及 Python 3.10 或更新版本。离线检查逐个验证初始样本判失败、参考结果判通过。可以在没有 API Key 时完成。

## 2. 安装独立的实验版 DSH

```zsh
npm ci --prefix .runtime --no-audit --no-fund
python3 -B bench.py check
```

安装需要访问 npm。命令把 DSH 安装在当前项目的 `.runtime/`。`check` 使用临时 DSH home，验证版本、headless 参数和两档配置合成。每个实验批次也有独立的 DSH home。全局 `dsh` 和日常配置继续用于原来的工作。 [固定版发布说明](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1) · [固定版 headless 源码](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/packages/bundle/headless/src/startup.ts)

## 3. 先跑 4 次冒烟实验

```zsh
python3 -B bench.py smoke
```

使用 DeepSeek API Key。脚本优先使用当前终端的 `DEEPSEEK_API_KEY`，否则在交互终端提示隐藏输入。Key 由子进程环境传给 DSH。冒烟和正式基线会产生 API 用量。默认每次 DSH 最多运行 180 秒；可以用 Ctrl-C 结束批次并保留已写入的结果。 [模型路由与价格](https://api-docs.deepseek.com/quick_start/pricing/)

冒烟顺序从 **X01 配置优先级**开始，再跑 **C01 构建日志**，每题各跑一次 high 和 low。脚本每次打印通过、档位核对、费用估算，并输出批次目录。继续跑基线之前，检查 `summary.json` 中两组各有 2 个结果，且 `route_verified` 和 `runs_with_known_cost` 都为 2。再打开一次 `result.json`，确认独立判分给出了合理结果。失败样本也是数据，先区分模型答错、DSH 启动失败和用量缺失。

## 4. 跑 36 次基线

```zsh
python3 -B bench.py baseline
```

同一批次 9 题 × 2 档 × 2 次，共 36 次。脚本在每题内交错 high/low 顺序，从同一目录路径下的干净 fixture 开新会话，完成后归档工作目录。两组固定模型、权限、工具与超时。模型可选择先搜索再读日志，因此长日志样本评测的是 DSH 的定位任务，实际模型上下文长度由轨迹决定。工具输出与账单口径详见 [数据与指标](MEASUREMENT.md)。

如果一批在启动或档位核对阶段停止，保留失败记录。修正环境后重新启动一个批次，汇总时以完整批次为单位。若正常接入但个别运行超时，保留超时和相应缺失用量；需要调大超时时，给两组统一使用新参数并重跑该批。

| ID | 任务 | 独立判分 |
|---|---|---|
| L01 | 修复闰年计算 | 普通年、百年和四百年边界 |
| L02 | 修复分页多一项 | 首页、末页、空页 |
| L03 | 保留显式重试次数 0 | 0、正数、缺省 |
| X01 | 修复 CLI、环境变量和文件配置优先级 | 五组优先级和缺省路径 |
| X02 | 保留 JSON 中的显式 null | 省略、null、空字符串和普通值 |
| X03 | 兼容历史 SKU 格式 | 大小写、空白、零库存和未知值 |
| C01–C03 | 在 1,800 行构建日志中定位根因，证据位置分别在前、中、后 | 组件路径、错误码和精确行号 |

任务源码和参考结果保存在 `tasks.py`。运行器只把该题的初始文件写入模型工作目录。验收器另外运行，使用与模型分开的进程。

## 5. 跑 16 次进阶实验

```zsh
python3 -B bench.py hard
```

4 道新题各跑两次 high 和两次 low，第二次反转同题的档位顺序。题目包括乱序事件与删除版本、嵌套配置与覆盖顺序、稳定游标分页、订单原子库存预留。测试分别核对多文件实现、边界行为和输入是否被修改。进阶批次与基线单独汇总。

### 验收触发升档的留出题

```zsh
python3 -B adaptive_bench.py --suite holdout --check
python3 -B adaptive_bench.py --suite holdout
```

3 道新题各跑两次固定 high 和固定 low。升档策略复用每对的 low 首次结果，仅在事先定义的独立检查失败时，另开 high 会话修补已有代码。最终评分使用不同的案例。每对保存 `*-pair-*.json`，门控详情保存在对应 low 运行的 `gate.json`，补救运行保存在 `*-repair-*/`。自适应策略的费用包含首次 low 和实际触发的 high。

## 6. 用结果决定下一阶段

1. 先看同题是否有 `high` 成功而 `low` 失败的案例，再读失败前的工具轨迹。只有能在失败之前看到的信号，才有资格做调档条件。
2. 对照 `summary.json` 的分类通过数、总输出、未缓存输入、缓存输入、估算费用和中位耗时。把错误档位和缺失用量先排查清楚。优先保留能解释变化的原始运行。
3. 若 `low` 在局部题稳定、跨文件题容易在验证阶段卡住，设计一个记录版插件，记录“最近一次测试结论、连续重复操作、是否获得新证据、建议档位”。对照 `request/header` 确认记录与实际调用一致。
4. 冻结一条最小规则后接入 `agent/request`。默认 low；在**修改后重复出现同一失败，且这轮没有新证据**，或**同一搜索重复且结果没有变化**时，从下一次请求升到 high，之后保持 high。第一版只允许升一次。先在离线轨迹上检查触发是否发生在预期步骤，再通过真实新运行验证效果。 [DSH 请求钩子](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md)
5. 用新题对比固定 high、固定 low、自动升档三组。9 道题用于调试和发现问题；正式验证建议从至少 30 道未参与规则设计的题开始，每题每组重复 3 次，按同题成对汇总。可以先把“每个成功任务费用至少降 10%，总通过率下降不超过 5 个百分点，且关键跨文件场景没有稳定退化”作为验收目标。阈值是项目建议，开始留出集前定好。保留同题差异和失败样本，样本不足时把结果标成初步。

如果固定 low 已经能解决绝大多数题并最省钱，先考虑给 DSH 提供一个简单的省 token 配置。如果换档后的未缓存输入抵消输出收益，下一步先测更少换档或固定档位的策略。如果本轮只出现启动或记录问题，先修采集，再做模型比较。

## 7. 什么时候接自部署模型

当目标转为推理引擎吞吐时，固定 1017 权重和量化、任务、提示词与推理档位，在同一个引擎对比 DSpark 关闭、固定验证、动态验证。先测单会话，再测并发，并记录首 token 延迟、生成速度、接受的草稿长度和完成耗时。CSA2 或 SWA 回放另开实验，每组只改变一个开关。当前套件的 API 结果适合作为任务级参考，不能直接与另一套硬件或量化的速度归因。 [DeepInfer](https://github.com/deepseek-ai/DeepInfer) · [V4.1 技术报告](https://arxiv.org/html/2609.19969v1)
