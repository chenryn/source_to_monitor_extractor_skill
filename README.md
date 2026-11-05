# source_to_monitor_extractor

> 从软件项目的**源码**中逆向提取可观测信号，自动生成**监控方案**。

**English:** A skill that statically analyzes source code to extract the three observability planes — **log instrumentation, metrics, and distributed traces** — and reverse-engineers a monitoring & alerting plan grounded in source evidence. Every conclusion is traceable back to a file and a line.

---

## 为什么需要它

监控方案最常见的错误来源，是「只看一个平面就下结论」。

一个真实教训：某在线服务第一版分析只扫了日志，得出「缺少请求级延迟监控，建议新增埋点」——这是错的。延迟分位、排队时间、资源水位全都在**指标**里，通过暴露端点提供给生产监控，日志只是补充。

生产环境的信号分工与业务领域无关：

| 想回答的问题 | 该看哪个平面 | 原因 |
|---|---|---|
| 服务活着吗、启动成功了吗、崩了吗 | **日志**（事件类） | 一次性事件，指标聚合看不出来 |
| 错误率、延迟分位、资源水位、趋势 | **指标** | 可聚合、可算分位、可做同比环比 |
| 这一次处理慢在哪一段 | **链路** | 只有 span（或等价的分段记录）能给出分段耗时 |
| 为什么失败、失败原因是什么 | **日志**（上下文类） | 指标只有计数，没有原因 |

本 Skill 的核心思路：**确定性脚本提取结构化信号 → LLM 做语义分类与规则生成 → 人工校准阈值**。它描述的是一套**方法**，不绑定任何具体项目、语言或业务领域。

---

## 核心能力

- **项目画像（Step 0）**：先判断运行形态（在线服务 / 批处理 / 库与 SDK / CLI / 基础设施组件）、语言构成、可观测技术栈、暴露端点与默认开关，再决定怎么跑。不可跳过。
- **三平面信号提取（Step 1）**：日志埋点、指标定义/写入点/暴露点、链路 span 三类信号，能用脚本的语言优先跑脚本。
- **语义分类（Step 2）**：基于脚本输出的上下文证据字段（循环内 / 异常分支 / 单次打印 / 条件开关 / 断言 / 占位符风格）打标签，不凭感觉。
- **信号仲裁（Step 3）**：每个监控需求落到哪个平面、源码里是否已有信号、是真缺还是没提取到——下「缺少 X 监控」结论前必须三平面都查过。
- **分层告警（Step 4）**：指标为主、日志事件为辅、链路可选，阈值一律标注「待校准」。
- **结构化方案文档（Step 5）**：9 章固定格式交付物。
- **版本增量 diff（Step 6）**：对比新旧版本的三平面信号变化，避免僵尸告警规则。

---

## 工作原理

```
源码目录
   │
   ▼
[Step 0] 项目画像        ──→ 运行形态 / 语言 / 技术栈 / 暴露方式 / 默认开关 / 脚本能力
   │
   ▼
[Step 1] 三套提取脚本     ──→ loggers.json + metrics.json + traces.json（不支持的语言走降级路径）
   │
   ▼
[Step 2] 语义分类        ──→ 打标签（日志按语义、指标按类型与 RED/USE、span 按阶段）
   │
   ▼
[Step 3] 信号仲裁        ──→ 每个监控需求 → 落到哪个平面 → 有无现成信号
   │
   ▼
[Step 4] 分层告警        ──→ 指标告警(主) + 日志事件告警(辅) + 链路告警(可选)
   │
   ▼
[Step 5] 输出方案文档    ──→ 9 章节结构化交付物
   │
   ▼
[Step 6] 增量更新        ──→ diff_signals.py 对比新旧版本
```

完整的约束、Prompt 模板、陷阱清单见 [`SKILL.md`](./SKILL.md)。

---

## 目录结构

```
source_to_monitor_extractor/
├── SKILL.md                      # Skill 主文档（方法论 + 工作流 + 14 条强约束）
├── scripts/                      # 纯标准库，无第三方依赖
│   ├── extract_loggers.py        # 日志埋点提取（Python AST + C++/Rust 正则 + Java/Go/JS 后端）
│   ├── extract_metrics.py        # 指标定义/写入点/暴露点提取（Python AST + Java/Go/JS 后端）
│   ├── extract_traces.py         # 链路 span 提取（Python AST + Java/Go/JS 后端）
│   ├── lang_extract.py           # Java/Go/JS 通用正则后端：掩码全文 + 括号平衡参数解析
│   ├── signal_patterns.json      # Java/Go/JS 三平面规则表（新增 SDK 写法从这里扩展）
│   ├── diff_signals.py           # 三类信号通用增量对比
│   ├── diff_loggers.py           # 兼容入口，已转发到 diff_signals.py
│   └── logger_patterns.json      # C++/Rust 日志库正则模式（新增语言从这里扩展）
├── references/                   # 子文档，SKILL.md 在各步骤引用
│   ├── semantic_taxonomy.md      # 日志 / 指标 / span 分类体系
│   ├── signal_arbitration.md     # 信号仲裁流程 + 三平面检查清单
│   ├── alert_rules.md            # 分层告警模板、等级定义、通用基线
│   └── output_format.md          # 输出文档 9 章格式规范
└── examples/                     # 九个真实项目的完整输出示例（运行形态各异）
```

---

## 快速开始

### 作为 WorkBuddy Skill 安装

将本仓库克隆到你的 WorkBuddy 用户级或项目级 skills 目录：

```bash
# 用户级（跨项目可用）
git clone git@github.com:chenryn/source_to_monitor_extractor_skill.git \
  ~/.workbuddy-ai/skills/source_to_monitor_extractor

# 或项目级（团队共享）
git clone git@github.com:chenryn/source_to_monitor_extractor_skill.git \
  <你的项目>/.workbuddy-ai/skills/source_to_monitor_extractor
```

克隆后在对话中描述需求即可触发，例如：「分析 `<源码路径>` 的可观测体系，给我一份监控方案」。

### 直接运行提取脚本

脚本全部基于 Python 标准库，无需安装任何第三方包：

```bash
cd scripts/

# 1. 日志埋点（自动识别 .py / C/C++ / .rs / .java .kt / .go / .js 系；单语言项目可加 --lang）
python extract_loggers.py <源码根目录> --output loggers_extracted.json

# 2. 指标定义、写入点、暴露点
python extract_metrics.py <源码根目录> --output metrics_extracted.json

# 3. 链路 span
python extract_traces.py <源码根目录> --output traces_extracted.json

# 6. 版本增量对比（三个平面都要，输出到 *_v2.json 后逐对 diff）
python diff_signals.py loggers_v1.json loggers_v2.json --output diff_logs.json
```

通用参数：`--exclude GLOB`（可重复）、`--include-all`（不排除常见非生产目录）、`--no-summary`；提取日志另有 `--lang auto|python|cpp|rust|java|go|js`、`--include-print`；提取指标/链路也有 `--lang auto|python|java|go|js`。

提取后，按 [`SKILL.md`](./SKILL.md) 的 Step 2~5 做语义分类、信号仲裁、分层告警与文档输出；Java/Go/JS 的写法规则集中在 `scripts/signal_patterns.json`，遇到新 SDK 写法优先往那里加规则再重跑。

---

## 脚本能力矩阵

内置脚本只覆盖一部分语言与框架，**先按矩阵判断，再决定跑脚本还是走降级路径**：

| 语言 | 日志提取 | 指标提取 | 链路提取 |
|---|---|---|---|
| Python | AST + 正则（logging / loguru / structlog / `*_once`） | AST（prometheus_client / OTel meter / 常量拼接 / 枚举 / f-string） | AST（OTel span / 装饰器 / NVTX / 自研 DSL / 阶段注册表） |
| C / C++ | 正则（glog / spdlog / 带前缀宏 / `CHECK_*` 断言） | 未内置 | 仅能识别 profiler range（NVTX / `record_function`） |
| Rust | 正则（`tracing::` / `log::` / `panic!` / `assert!`） | 未内置 | 未内置 |
| Java / Kotlin | 正则（slf4j / log4j2 / JUL / `assert`） | 正则（Micrometer / Prometheus Java client / OTel / 写入点溯源） | 正则（`spanBuilder` / `@WithSpan` / `setAttribute`） |
| Go | 正则（slog / zap / logrus / zerolog / stdlib / `panic`） | 正则（client_golang Opts / promauto / OTel / 写入点溯源） | 正则（`tracer.Start` / `SetAttributes` / `AddEvent`） |
| JS / TS | 正则（pino / winston / bunyan / Nest / `console.*` 默认排除） | 正则（prom-client / OTel / 写入点溯源） | 正则（`start(Active)Span` / `setAttributes` / `addEvent`） |
| C# / 其他 | 未内置 | 未内置 | 未内置 |

遇到未覆盖的语言、框架或写法，走降级路径（详见 SKILL.md §0.5）：列出调用点的文件+行号+名称人工汇总成同构 JSON → 优先往 `signal_patterns.json` / `logger_patterns.json` / 构造器集合里加规则再重跑 → 实在无法扩展时明确标注「人工提取，覆盖率未知」。**脚本输出为 0 不等于项目没有该平面。**

---

## 输出示例

`examples/` 下的九份方案是九个真实项目上的完整输出示例，覆盖四种运行形态，用来展示交付格式和静态分析的典型陷阱。**它们不代表本 Skill 只适用于这几类项目；其中的指标名、阈值、开关名称、目录结构一律不可跨项目复用。**

| 样例 | 运行形态 | 它验证的通用教训 |
|---|---|---|
| [`examples/vllm-monitoring-plan.md`](./examples/vllm-monitoring-plan.md) | 在线推理服务 | 三平面互补；延迟分位属于指标而非日志 |
| [`examples/sglang-monitoring-plan.md`](./examples/sglang-monitoring-plan.md) | 在线推理服务 | 多语言仓库必须逐语言扫；可观测能力可能默认全关 |
| [`examples/tensorrt-llm-monitoring-plan.md`](./examples/tensorrt-llm-monitoring-plan.md) | 在线推理服务 | 端点协议歧义；多层默认开关；链路可能是指标的投影 |
| [`examples/prometheus-monitoring-plan.md`](./examples/prometheus-monitoring-plan.md) | 基础设施组件 | 抓取时合成的样本不是注册指标；断言失败即进程消失 |
| [`examples/dubbo-monitoring-plan.md`](./examples/dubbo-monitoring-plan.md) | 库 / RPC 框架 | sparse checkout 必须写明扫描范围；指标名走枚举时需人工降级 |
| [`examples/fastify-monitoring-plan.md`](./examples/fastify-monitoring-plan.md) | 库 / Web 框架 | 库类方案重点在「暴露什么信号给宿主」 |
| [`examples/nest-monitoring-plan.md`](./examples/nest-monitoring-plan.md) | 库 / 框架（monorepo） | monorepo 必须先圈定生产代码根目录 |
| [`examples/opentelemetry-demo-monitoring-plan.md`](./examples/opentelemetry-demo-monitoring-plan.md) | 多语言微服务集群 | 13 语言逐语言降级提取的完整样例 |
| [`examples/train-ticket-monitoring-plan.md`](./examples/train-ticket-monitoring-plan.md) | 微服务集群（指标/链路贫乏） | 三平面空白时以日志事件为主并写明取舍 |

---

## 强约束与注意事项

为防止幻觉，SKILL.md 规定了 14 条强约束，摘要如下：

- **三平面必查**：任何「缺少 X 监控」的结论必须同时给出日志、指标、链路三平面的检查结果。
- **结论必须溯源到源码**：每条告警、每个指标、每个关键字都标注文件 + 行号。
- **阈值都是候选**：标注「待 SLO / 压测 / 线上数据校准」，源码里的 `timeout=5s` 是业务配置不是告警阈值。
- **区分「源码里没有」和「没提取到」**：自定义 DSL、动态命名、封装函数、未覆盖语言都会造成遗漏；拿不准就标「需人工确认」。
- **断言不是日志**：`CHECK_*` / `assert!` / `panic!` 失败即中止，未必以日志落盘，不能做成日志检索关键字。
- **暴露端点不可假设**：路径、协议、格式、端口、开关都要给出「注册代码 + 配置默认值」两处证据。
- **默认开关单独列一章**：能力默认关闭时，方案第一优先级是「怎么打开并验证数据流」，不是「加什么埋点」。
- **名称从源码到暴露会变形**：SDK 会补后缀、改分隔符、注入前缀（如计数器补 `_total`）；面板/告警统一用**抓取名**。

更多陷阱（符号化名称、注册表优先于调用点、分段耗时未必在子 span 里、指标名非标准构造等）见 SKILL.md §1「静态提取的常见陷阱」与「典型翻车案例」。

---

## 扩展与贡献

- 新增 Java/Go/JS 的 SDK 写法：往 [`scripts/signal_patterns.json`](./scripts/signal_patterns.json) 添加规则后重跑。
- 新增 C++/Rust 的日志库模式：往 [`scripts/logger_patterns.json`](./scripts/logger_patterns.json) 添加模式后重跑。
- 新增 Python 侧写法：往 [`scripts/extract_metrics.py`](./scripts/extract_metrics.py) / [`extract_traces.py`](./scripts/extract_traces.py) 的构造器集合里添加。
- 任何扩展后**必须抽查验证**，确认提取结果自洽。

---

## License

本项目以 MIT License 发布（详见 LICENSE 文件）。
