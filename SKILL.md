---
name: source_to_monitor_extractor
description: 从软件项目源码中提取「日志埋点 + 指标 + 链路 span」三类可观测信号，反向生成监控方案——项目画像、语义分类、关键字库、指标清单、信号仲裁、分层告警规则、埋点缺口与版本迭代增量 diff。适用于在线服务、批处理/定时任务、库与 SDK、CLI 工具、基础设施组件等各类形态，语言或框架不被内置脚本覆盖时提供人工提取的降级路径。当用户需要：(1) 分析某项目源码的可观测体系，(2) 从代码推导监控告警规则，(3) 对比新旧版本的信号变化，(4) 为已有服务补充可观测性方案，(5) 评估第三方组件的被监控能力时，使用此 Skill。
---

# 源码 → 监控方案（三平面）

从源码逆向生成监控方案。核心思路：**确定性脚本提取结构化信号 → LLM 做语义分类与规则生成 → 人工校准阈值**。

本 Skill 描述的是**方法**，不绑定任何具体项目、语言或业务领域。文中的探测关键词、词表、案例都只是起点或样本；每个项目都必须先做 Step 0 画像，再决定用什么工具、关注什么信号、下什么结论。

## 为什么必须扫三个平面，而不是只扫日志

**只看一个平面会得出错误结论。** 一次真实教训：某在线服务第一版分析只扫了 logger，结论是「缺少请求级延迟监控，建议新增埋点」——这是错的。延迟分位、排队时间、资源水位全都在指标里，通过暴露端点提供给生产监控，日志只是补充。

生产环境的信号分工与领域无关：

| 想回答的问题 | 该看哪个平面 | 为什么 |
|---|---|---|
| 服务活着吗、启动成功了吗、崩了吗 | **日志**（事件类） | 一次性事件，指标聚合看不出来 |
| 错误率、延迟分位、资源水位、趋势 | **指标** | 可聚合、可算分位、可做同比环比 |
| 这一次处理慢在哪一段 | **链路** | 只有 span（或等价的分段记录）能给出分段耗时 |
| 为什么失败、失败原因是什么 | **日志**（上下文类） | 指标只有计数，没有原因 |

**硬规则：在写下任何「缺少 X 监控」的结论之前，必须确认三个平面都查过了。** 详见 `references/signal_arbitration.md`。

三条公理，贯穿全流程：

1. **先查已有什么，再谈缺什么**；「该平面没有」≠「没有监控」。
2. **结论必须来自源码证据**，不能来自领域经验或想当然的指标名。
3. **静态分析看不到运行时**：默认值、开关、采样率、注册条件必须单独查证。

## 工作流总览

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

---

## Step 0：项目画像（决定后面怎么跑，不可跳过）

在跑脚本之前产出一张**项目画像表**（写进方案第 1 章）。它决定：关注哪些监控需求、跑哪些脚本、哪些结论根本不适用。

### 0.1 运行形态 —— 最容易忽略的隐含假设

三平面方法通用，但「该监控什么」高度依赖形态。不要给所有项目套同一套 RED/USE 需求清单。

| 运行形态 | 核心问题 | 主用平面 | 典型信号 |
|---|---|---|---|
| 在线服务 / API | 可用性、错误率、延迟分位、饱和度 | 指标 + 日志事件 | 请求计数、耗时分布、就绪日志、健康检查 |
| 批处理 / 定时任务 / 数据管道 | 作业是否跑完、耗时、重试、数据滞后与完整性 | 日志事件 + 指标 | 作业起止、成功/失败、处理条数、checkpoint、lag |
| 库 / SDK / 客户端 | 被集成方如何观测、错误如何暴露 | 日志 + API 语义 | 公开错误类型、回调/hook、日志门面、可注入的 meter |
| CLI / 一次性工具 | 退出是否成功、失败归因 | 日志 + 退出码 | stderr、错误码、运行摘要 |
| 基础设施组件（DB/MQ/代理/存储） | 容量、饱和度、连接与副本状态 | 指标（USE） | 队列深度、连接池、磁盘/内存、副本延迟 |

**库与 SDK 尤其要单独判断**：它们通常不该自带告警，方案重点应是「暴露什么信号给宿主」以及「如何验证宿主能采集到」，而不是凭空设计一套 P0 告警。

### 0.2 语言构成与扫描范围

```bash
# 语言分布（POSIX 命令；按需增删扩展名）
find <src> -type f \( -name '*.py' -o -name '*.go' -o -name '*.java' -o -name '*.kt' \
  -o -name '*.ts' -o -name '*.js' -o -name '*.rs' -o -name '*.cc' -o -name '*.cpp' \
  -o -name '*.cxx' -o -name '*.c' -o -name '*.h' -o -name '*.hpp' -o -name '*.cu' \
  -o -name '*.cs' -o -name '*.rb' -o -name '*.php' \) \
  | sed 's/.*\.//' | sort | uniq -c | sort -rn
```

同时确认目录语义：脚本默认排除 `tests/ docs/ examples/ benchmarks/ build/ ...`，因为「这些不是生产路径」这个假设对多数项目成立，但**并非总是**（有些项目把生产代码放在 `examples/`、把核心实现放在名字带 test 的目录里）。发现异常分布时用 `--exclude` / `--include-all` 调整，并在方案里说明。

画像表里要记：**语言清单、各语言占比、生产代码根目录、被排除的目录及理由**。多语言仓库必须逐语言确认，只扫主语言得出的「某层没有埋点」基本都是错的。统计到的扩展名还要与 0.5 的能力矩阵对照：脚本目前认 `.py` / `.cc .cpp .cxx .h .hpp` / `.rs` / `.java .kt` / `.go` / `.js .jsx .mjs .cjs .ts .tsx`，其余后缀即使统计到了也不会被扫，需按降级路径处理。

### 0.3 可观测技术栈

用你手边的全文检索工具（`rg` / IDE 检索 / Grep 工具均可）找下列线索，关键词只是起点，命中后要回到源码确认：

| 要确认的事 | 检索起点 | 需要记下的结论 |
|---|---|---|
| 日志门面 | `logging` `logger` `slog` `tracing::` `log4j` `slf4j` `zap` `logrus` `winston` `pino` | 库名、封装函数、是否有 `*_once` / 结构化字段 |
| 指标 SDK | `Counter(` `Histogram(` `meter.` `prometheus` `opentelemetry` `micrometer` `statsd` `client_golang` `jmx` | 构造器形态、命名空间前缀、指标名是否动态构造 |
| 链路 SDK | `start_as_current_span` `start_span` `#[instrument]` `info_span!` `Tracer` `trace_span` | 是否用标准 SDK、有无自研 DSL / 阶段注册表 |
| 暴露方式 | `metrics` `exporter` `pushgateway` `otlp` `route` `handler` `endpoint` `generate_latest` | 路径、协议（文本/JSON/OTLP/JMX）、端口、是否独立进程 |
| 就绪/健康 | `health` `ready` `live` `startup` `probe` | 有无可供存在性判定的信号 |

**只找「用了什么」，不要预设「应该有什么」。** 一个项目完全可以没有指标 SDK（指标由外部 exporter 提供），或没有 trace（分段耗时写在日志字段里）。这些都是合法形态，写进画像即可。

### 0.4 暴露端点与默认开关（最贵的坑，必查）

**端点的路径、协议和默认状态一律不能假设。** 同名路径可能返回完全不同格式；同一能力可能在多个路径、多个开关后面。

判定方法是看两处源码证据：

1. **路由/handler 注册代码**：这个路径绑定的是哪个函数，返回什么格式；
2. **配置默认值**：argparse/`default`、config 文件、环境变量、Helm values、feature flag 里的默认值。

把每个可观测能力列成表（能力 / 开关 / 默认值 / 生效条件 / 如何验证数据流）。**默认关闭的能力，方案第一优先级是「怎么打开并验证」，不是「加什么埋点」**——否则后面所有告警规则都是空转。还要确认开关有没有暴露到用户可配置的入口（CLI 参数、配置文件、环境变量）；只在内部配置里存在的开关，要写清实际启用方式。

### 0.5 脚本能力矩阵与降级路径

内置脚本只覆盖一部分语言与框架。**先按矩阵判断，再决定跑脚本还是人工提取。**

| 语言 | 日志提取 | 指标提取 | 链路提取 |
|---|---|---|---|
| Python | AST + 正则（标准库 logging / loguru / structlog / `*_once` 变体） | AST（`prometheus_client` / OTel meter / 工厂别名 / 常量拼接 / 枚举 / f-string） | AST（OTel span / 装饰器 / NVTX / 自研 DSL / 阶段注册表） |
| C / C++ | 正则（glog / spdlog / 自定义与带前缀宏 / `CHECK_*` 断言） | 未内置 | 仅能识别 profiler range（NVTX / `record_function`） |
| Rust | 正则（`tracing::` / `log::` 宏 / `panic!` / `assert!`） | 未内置 | 未内置（`info_span!` / `#[instrument]` 需人工） |
| Java / Kotlin | 正则（slf4j / log4j2 fluent / JUL / `assert`；掩码全文 + 括号平衡参数解析） | 正则（Micrometer builder / Prometheus Java client 链式 builder / OTel builder / `@Timed` / 写入点变量溯源 / registry 与路径暴露点） | 正则（`spanBuilder` / `@WithSpan` / `setAttribute` / `addEvent`） |
| Go | 正则（slog / zap / logrus / zerolog / go-kit `level.X().Log` / stdlib / `panic`） | 正则（client_golang Opts 含 Namespace 前缀 / promauto / OTel meter / 写入点变量溯源 / promhttp 与路由暴露点） | 正则（`tracer.Start`（span 名取第 2 参）/ `SetAttributes` / `AddEvent`） |
| JS / TS（含 .jsx/.tsx/.mjs/.cjs） | 正则（pino / winston / bunyan / Nest Logger；`console.*` 默认排除，`--include-print` 开启） | 正则（prom-client 构造器 / OTel `create*` / `registerMetric` / 写入点溯源 / `/metrics` 暴露点） | 正则（`start(Active)Span` / `setAttributes` / `addEvent`） |
| C# / 其他 | 未内置 | 未内置 | 未内置 |

Java/Go/JS 三个后端共用 `scripts/lang_extract.py`（掩码全文 + 括号平衡的调用参数解析）与 `scripts/signal_patterns.json`（各 SDK 写法规则）。这三个后端的已知局限，判读结果时要注意：

1. **函数名追踪是行级近似**：多行签名的 Java/Kotlin 方法、TS 重载会导致 `function` 为空；`in_loop` / `in_except` / `conditional` 是大括号栈近似，方向可信、边界不精确。
2. **Go 的 `if err != nil` 不算异常分支**：`in_except` 只认 `catch`；Go 的错误处理日志带 `conditional="err != nil"` 证据，语义分类时靠它补。
3. **exposure 的路径字面量规则偏宽**：`/metrics`、`/prometheus` 字符串可能出现在客户端 URL 常量里，带 `evidence: "literal"` 标记的记录需人工核对路由注册代码（见强约束 11）。
4. **依赖声明不参与判断**：脚本只看源码调用点。模块级 sparse checkout 会让某平面显示为 0，扫描范围必须与 Step 0 画像一致。

遇到未覆盖的语言、框架或写法，走**降级路径**，不要假装脚本扫过了：

1. 用全文检索列出日志门面调用、指标构造器/注册器、span 创建点的**文件 + 行号 + 名称**，人工汇总成与脚本输出同构的清单（至少 `name / type / file / line / kind`）。
2. 优先做小幅扩展再重跑：往 `scripts/signal_patterns.json` 加该语言/该 SDK 的规则（Java/Go/JS 的日志、指标、链路、暴露点都在这里）；往 `scripts/logger_patterns.json` 加 C++/Rust 的日志模式；往 `scripts/extract_metrics.py` / `extract_traces.py` 的构造器集合里加 Python 侧写法。扩展后必须抽查验证。
3. 无法扩展时，明确标注「人工提取，覆盖率未知」，并在方案第 9 章列出未覆盖范围。
4. **脚本输出为 0 不等于项目没有该平面**：只有「检索过源码且确认无相关调用」才能下这个结论。

---

## Step 1：提取三类信号（确定性脚本优先，不要让 LLM 直接读源码）

能用脚本的语言就先跑脚本：LLM 逐文件读源码会漏，脚本不会。三个脚本都是纯标准库，无第三方依赖。

```bash
cd scripts/

# 1.1 日志埋点（自动识别 .py / C/C++ / .rs / .java .kt / .go / .js 系；单语言项目可加 --lang）
python extract_loggers.py <源码根目录> --output loggers_extracted.json

# 1.2 指标定义、写入点、暴露点（Python 走 AST；Java/Go/JS 走 lang_extract 正则后端）
python extract_metrics.py <源码根目录> --output metrics_extracted.json

# 1.3 链路 span（含 OTel / profiler range / 自研 DSL / 阶段注册表 / Java/Go/JS 后端）
python extract_traces.py <源码根目录> --output traces_extracted.json
```

通用参数：`--exclude GLOB`（可重复）、`--include-all`（不排除常见非生产目录）、`--no-summary`；`extract_loggers.py` 另有 `--lang auto|python|cpp|rust|java|go|js`、`--include-print`；`extract_metrics.py` / `extract_traces.py` 也有 `--lang auto|python|java|go|js`。Java/Go/JS 的写法规则集中在 `scripts/signal_patterns.json`，遇到新 SDK 写法优先往那里加规则再重跑。

### 验收：自洽性检查，不是数量门槛

**数量本身不构成合格标准。** 项目规模、形态、语言差异可以让埋点数相差几个数量级；不要拿某个项目的量级当验收线。只检查「提取结果是否自洽、是否可信」：

| 检查项 | 可信的表现 | 异常时怎么办 |
|---|---|---|
| 日志总数 | 与该项目语言/规模相称且 > 0 | 为 0 → 依次查路径、扩展名、`--lang`、排除目录、日志是否被封装；确认源码确实无日志后才记入结论 |
| 跨语言仓库 | 按 `language` 字段分语言看数量级，各语言口径与画像一致 | 某语言为 0 → 先确认该语言的文件是否在扫描范围内（模块级 sparse checkout 会让平面显示为 0），再下结论 |
| 按包/目录分布 | 能分辨出多个模块，且与画像里的生产代码结构一致 | 全是 `<root>` 或空 → 路径解析坏了；单个桶 > 80% → 包粒度太粗或前缀剥离没生效（脚本已对 `src-layout` 自适应，必要时按语言拆分再看） |
| 占位符风格 | 与项目实际格式化方式一致（printf / brace / fstring / 结构化字段） | 全为 `none` 但源码明显有 `%s` / `{}` / 结构化参数 → 解析未生效，后续无法区分变量与固定关键字 |
| 断言/abort 类 | 用 `is_assertion` 单独统计 | 断言占比高时，级别分布会被扭曲；必须在方案里说明「崩溃信号可能表现为日志断档 + 进程重启」 |
| 指标定义 vs wrapper | 定义数占多数 | `wrapper` 远多于定义 → 指标名由拼接常量、枚举、f-string、工厂或自定义 DSL 构造，解析没覆盖；补规则或转人工，**不能得出「指标很少」** |
| 定义 vs 写入点 | 两者大致同量级 | 大量定义无写入点 → 可能是预留指标，也可能是通过辅助函数写入（指标对象作为参数传入）未被识别 |
| 命名前缀 | 与画像里看到的命名约定一致 | 出现大片 `<no-prefix>` → 检查是 `ns:name`、`prefix_name` 还是其他风格 |
| span 类型 | 能区分分布式 span 与 profiler range | 数量畸高且全是 `range_push` / `record_function` → 那是 profiler，不是分布式 trace；名字为纯数字等非法值 → 误匹配，收紧识别规则 |
| 代码抽样 | `code_snippet` 是真实调用 | **必做**：随机抽 5~10 条，确认不是宏定义、注释或同名但语义无关的方法 |

### 静态提取的常见陷阱（已抽象为通用规则，具体案例见文末索引）

| 陷阱 | 通用判读规则 |
|---|---|
| **名字是符号引用，不是字面量** | span 名、指标名、事件名常来自枚举/常量/注册表（调用点只传变量）。必须去查定义处还原真实名称；「调用点拿不到字面量」≠「没有这个信号」 |
| **注册表优先于调用点** | 自研链路 DSL 常把阶段名集中在注册表类里。链路结构看注册表，埋点密度看调用点；注册表为空且大量动态名时，说明 DSL 未被识别，需补规则 |
| **分段耗时未必在子 span 里** | 可能在 span 属性、指标标签、响应头或日志字段里。span 数量少但属性很多、且值来自指标/计时结构时，链路是「指标的投影」——此时开 trace 往往依赖指标采集，查链路要看属性而非瀑布图 |
| **指标名有多种非标准构造** | 常量拼接、枚举成员、f-string、工厂函数、辅助函数写入都很常见。求不出值时标为动态/待确认，不要断言指标不存在 |
| **同名 API 语义可能完全不同** | 端点绑定不同 handler 就返回不同协议；`record_event` 之类可能是 profiler 事件而非 span 事件；裸方法名（如 `span(0)`）可能是缓冲区操作。识别规则要靠上下文，不能只靠名字 |
| **断言失败不一定落日志** | `CHECK_*` / `assert!` / `panic!` 失败即中止，可能只抛异常或直接 abort。它们不能做日志检索关键字，正确用途是启动期 smoke test 与崩溃归因 |
| **单测写在同文件里** | 某些语言把 `#[cfg(test)] mod tests` 之类写在生产文件内，按目录排除不掉，会污染级别分布。发现断言异常膨胀时先查这一点 |

---

## Step 2：语义分类

先读 `references/semantic_taxonomy.md`（日志 / 指标 / span 三套分类体系）。

脚本已输出**上下文证据字段**，分类必须基于证据，不要凭感觉：

| 字段 | 含义 | 分类用法 |
|---|---|---|
| `in_loop` | 在循环内 | 高频候选，**禁止做频率告警**，只能做趋势或采样 |
| `in_except` | 在异常/错误处理分支内 | 优先归为 error 类 |
| `once` | 只打印一次的方法变体 | 只做「是否出现」的存在性告警 |
| `conditional` | 在条件分支内，附带条件表达式 | 可能默认不启用，别当默认行为 |
| `is_assertion` | 断言/abort 类 | 单列，不作为可检索关键字 |
| `placeholder_style` | 占位符风格 | 有占位符的模板，变量部分不能当固定关键字做全文匹配 |

**分类 Prompt**（标签集可按运行形态扩展，例如批处理加 `job` / `checkpoint`、库加 `api_surface`）：

```
你是可观测性专家。对以下信号逐条打标签，必须基于给定字段做判断。

输入：loggers_extracted.json（按模块分批，每批 50~100 条）
输出：JSON 数组，每条含 {file, line, template, main_tag, sub_tag, reason, evidence}

规则：
1. main_tag 从 [lifecycle, request, dependency, resource, business_error, system_error, metric] 选一个；
   项目画像里登记的特殊形态可增加标签，但要在方案里注明
2. 有 in_except=true 且 level>=WARNING → business_error 或 system_error
3. 有 in_loop=true → 追加 sub_tag="high_volume"
4. 有 once=true → 追加 sub_tag="one_shot"
5. 有 conditional 非空 → 追加 sub_tag="feature_gated"，并在 reason 里写出条件
6. 调试级日志 → main_tag="dev_debug"，不进告警
7. 模板含凭据/标识符类字段 → 追加 sub_tag="sensitive"，给出脱敏建议
8. 模块路径只作辅助证据，路径语义按本项目画像解释，不预设目录名含义
9. 每条必须填 reason（判断依据）和 evidence（引用了哪个字段）
```

**分批策略**：埋点 > 300 条时按模块分批，每批 50~100 条；最后做一次全局合并，检查同一错误码/同一模板是否归到了不同类别。

---

## Step 3：信号仲裁（关键步骤，不能跳过）

这一步决定「某个监控需求，用哪个平面实现」。**必须先查已有什么，再谈缺什么。**

读 `references/signal_arbitration.md`，按里面的流程逐项过。核心是三个问题：

1. 这个监控需求，属于「事件」还是「度量」还是「分段耗时」？（需求清单由 Step 0 的运行形态决定）
2. 对应平面里，**源码中是否已经有信号**？（查三份 JSON 的 name / template / span_name 列表）
3. 如果没有，是「真缺」还是「我们没提取到」？（查 `kind=name_literal` 但未匹配定义的记录、动态命名记录、脚本未覆盖的语言）

**关键词要靠项目自身命名习惯构造，不要照搬领域词表**：先从提取结果里学出实际前缀/后缀与命名模式，再按语义维度扩展同义词去检索。

| 语义维度 | 通用词根（起点） | 领域扩展词（仅当画像确认属于该领域时才加） |
|---|---|---|
| 延迟/耗时 | `latency` `duration` `elapsed` `seconds` `time` `cost` | 如推理服务的 `ttft` / `itl` / `e2e` |
| 错误/拒绝 | `error` `failure` `rejected` `timeout` `exception` `abort` | 如 `preemption` / `admission_rejection` |
| 速率/吞吐 | `requests` `total` `count` `throughput` `qps` | 如 `tokens` / `messages` / `rows` |
| 排队/并发 | `queue` `waiting` `pending` `inflight` `backlog` `running` | 如 `batch` / `lag` |
| 缓存/状态 | `cache` `hit` `miss` `state` `status` | 如 `prefix_cache` / `checkpoint` |
| 资源水位 | `usage` `util` `memory` `pool` `connections` `bytes` `size` | 如 `gpu` / `kv_cache` |

输出一张仲裁表（格式见 `references/output_format.md` 第 4 章），每行必须写清：监控需求 / 归属平面 / 现有信号（含源码位置）/ 结论。

**禁止**在没有查完三个平面列表的情况下写「缺少 X 监控」。结论句式必须包含各平面的检索范围与数量，详见仲裁文档。

---

## Step 4：生成分层告警规则

先读 `references/alert_rules.md`（分层模板、等级定义、降噪白名单；其中的领域指标清单是**示例**，不可跨项目套用）。

**分层原则**：

| 层级 | 数据源 | 承载什么 | 典型等级 |
|---|---|---|---|
| 第一层 | **指标** | 延迟分位、错误率、资源水位、趋势劣化 | P0/P1 主力 |
| 第二层 | **日志（事件类）** | 崩溃、启动失败、OOM、连接断开、作业失败 | P0/P1 |
| 第三层 | 日志（频率类） | 某类错误计数突增 | P2/P3，**必须配白名单 + 相对基线** |
| 第四层 | 链路 | 分段耗时劣化、span 错误率 | P2（可选） |

各层占比**按项目实际信号密度决定**，不强求固定比例：指标平面贫乏的项目允许以日志事件为主，但必须在方案里写明这一取舍及其代价（阈值不可跨环境复用、噪声更高）。

**为什么频率类日志告警要降级**：日志量随流量线性增长，绝对阈值无法跨环境复用；且大量 ERROR 是预期内的（客户端断开、正常重试、可选特性不可用、降级路径）。直接按「ERROR 占比 > X%」告警通常持续误报。

**告警生成 Prompt**：

```
基于项目画像 + 指标清单 + 已分类日志 + span 清单，生成候选告警规则。

输出 JSON 数组，每条含：
{
  "alert_name": "简短描述",
  "level": "P0/P1/P2/P3",
  "signal_plane": "metric / log_event / log_rate / trace",
  "condition": "PromQL 或日志检索条件（用本项目真实的指标名/字段名）",
  "candidate_threshold": "候选阈值（必须标注待校准）",
  "source_ref": "源码文件:行号",
  "impact": "故障影响",
  "noise_reduction": "降噪策略",
  "whitelist": "需要排除的已知预期情况"
}

约束：
1. 能用指标表达的，优先用指标；不要用日志频率替代指标
2. 所有阈值标注「候选，待 SLO/压测/线上数据校准」
3. 每条必须溯源到具体源码位置，且指标名/字段名必须来自提取结果
4. 源码里的超时/重试/容量常量 ≠ 告警阈值，必须分别说明
5. 指标阈值要写明该指标在本项目的量纲与单位（如比率是 0~1 还是 0~100）
6. in_loop=true 的日志禁止做频率告警；once=true 的日志只做存在性告警
7. 已知预期内的错误（正常重试、客户端断开、可选特性不可用）必须进白名单
8. 需求清单必须匹配项目运行形态；库/SDK 类项目优先输出「暴露给宿主什么信号」，而非 P0 告警
```

---

## Step 5：输出方案文档

按 `references/output_format.md` 的 9 章结构输出。**不要写成散文**，用表格。

1. 项目画像 + 三平面总览（含语言、框架、暴露方式、默认开关）
2. 日志埋点明细（按语义分类）
3. 指标清单（名字、类型、标签、源码位置、是否有写入点、暴露名）
4. 信号仲裁表（每个监控需求的归属与结论）+ 三平面检查记录
5. 关键字库（告警关键字 / 统计字段 / 降噪白名单 / 检索语句）
6. 告警规则草稿（按层级 + P0~P3 分级）
7. 埋点缺口与优化建议（真缺口 / 已有但不可用 / 需人工确认，分开写）
8. 版本迭代方法
9. 局限性说明（含脚本未覆盖范围、人工提取部分、静态分析看不到的运行时行为）

---

## Step 6：版本迭代增量更新

```bash
# 对新版本重新提取（三个平面都要，命令同 Step 1，输出到 *_v2.json）
python extract_loggers.py <新源码> --output loggers_v2.json
python extract_metrics.py <新源码> --output metrics_v2.json
python extract_traces.py  <新源码> --output traces_v2.json

# 逐个对比（脚本自动识别类型）
python diff_signals.py loggers_v1.json loggers_v2.json --output diff_logs.json
python diff_signals.py metrics_v1.json metrics_v2.json --output diff_metrics.json
python diff_signals.py traces_v1.json  traces_v2.json  --output diff_traces.json
```

diff 结果的处理：

| 变化 | 动作 |
|---|---|
| 新增 ERROR/WARN 日志 | 评估是否新增日志事件告警 |
| 新增指标 | 评估是否新增指标告警；新指标往往意味着新的可观测能力 |
| 删除日志/指标 | 检查对应告警规则是否要下线（否则变成永不触发的僵尸规则） |
| 指标标签变更 | 更新查询语句和看板维度（标签改了，旧查询会静默失效） |
| 直方图桶变更 | 重新校准分位阈值 |
| 开关默认值变更 | 重新核对「打开并验证数据流」那一章，优先级最高 |

**注意**：diff 按「文件名 + 模板」模糊匹配，忽略行号偏移；同一文件内模板完全相同的多条日志会被折叠。这是刻意设计——行号变动是噪音，但意味着 diff 计数是近似值，不要拿它当精确埋点统计。人工提取的清单也可以整理成同构 JSON 后走同一套 diff。

---

## 强约束（防幻觉，必须遵守）

1. **三平面必查**：任何「缺少 X 监控」的结论，必须同时给出日志、指标、链路三个平面的检查结果与检索范围。只查一个平面就下结论 = 错误结论。
2. **所有结论溯源到源码**：每条告警、每个指标、每个关键字都要标注文件 + 行号。
3. **阈值都是候选**：必须标注「待 SLO / 压测 / 线上数据校准」。源码里的 `timeout=5s` 是业务配置，不是告警阈值。
4. **动态变量 ≠ 固定关键字**：模板里的占位符、变量字段要按结构化字段统计，不能当固定字符串全文匹配。
5. **调试级日志不默认进告警**：单独列出，由人工决定是否采集。
6. **禁止创造源码里没有的信号**：没有就写「需新增埋点」或「需外部 exporter」，并说明该在哪个文件/函数加，或接哪个组件。
7. **区分「源码里没有」和「没提取到」**：自定义 DSL、动态命名、封装函数、脚本未覆盖的语言都会造成遗漏；拿不准就标「需人工确认」，不要断言不存在。
8. **按语言清单全覆盖**：画像里列出的每种语言都要确认扫过或走了降级路径；目录语义先确认再排除。只扫主语言得出的「某层没有埋点」都是错的。
9. **断言不是日志**：`CHECK_*` / `assert!` / `panic!` 失败即中止，未必以日志落盘，不能做成日志检索关键字；正确用法是启动期 smoke test 与崩溃归因。
10. **名称符号化时查定义处**：阶段注册表 / 枚举 / 常量表优先于调用点；别把「span 少」「名字是变量」当成「没有链路」。
11. **暴露端点不可假设**：路径、协议、格式、端口、开关都要给出「注册代码 + 配置默认值」两处证据，再写任何抓取配置。
12. **默认开关单独列一章**：能力默认关闭时，方案第一优先级是「怎么打开并验证数据流」，不是「加什么埋点」。只写告警规则不写开关，等于交了一份空转的方案。
13. **名称从源码到暴露会变形**：SDK 会补后缀、改分隔符、注入前缀或命名空间（例如计数器补 `_total`）。源码构造名、文档名、抓取名可能三者都不同——面板/告警统一用**抓取名**，源码检索时几种写法都试。
14. **span 少 ≠ 没有分段能力**：先看 span 属性，再看指标标签、响应头、日志字段。分段信息可能在属性里，或在指标里以标签维度呈现；此时缺的往往不是 span，而是采集开关。

### 典型翻车案例（抽象版）

| 翻车 | 正确做法 |
|---|---|
| 只扫日志，断言「没有延迟监控」 | 先查指标平面：延迟分位通常在 Histogram 里 |
| 把带占位符的模板当固定关键字 | 按字段做数值提取与分位统计 |
| 把调试级日志全部纳入告警 | 单列，不进告警 |
| 找不到埋点就编一个指标名 | 标「需新增/需外部组件」+ 指定插入位置 |
| 对启动/一次性事件做 QPS 统计 | 只做「是否出现」的存在性告警 |
| 只看主语言目录，得出「某层没有日志/链路」 | 先按画像确认全部语言与代码根目录 |
| 把大量断言当成大量崩溃点 | `is_assertion` 单列；真正的崩溃信号在运行路径的少数几条上 |
| 看到同名端点就配抓取 | 先看 handler 返回什么协议、是否需开关 |
| 指标定义数畸少就断言「指标很少」 | 看 `wrapper` / 未匹配字面量：多半是构造方式没被解析 |
| 把三个样例项目的指标名、阈值、目录结构套到新项目 | 只复用方法；词表与阈值一律从本项目提取结果重新推导 |

---

## 案例索引（输出示例，不是适用边界）

`examples/` 下的九份方案是九个真实项目上的完整输出示例，覆盖四种运行形态（在线服务 / 库与框架 / 基础设施组件 / 多语言微服务集群），用来展示交付格式和静态分析的典型陷阱。它们证明了方法有效，但**不代表本 Skill 只适用于这几类项目**；其中的指标名、阈值、开关名称、目录结构一律不可跨项目复用。

| 样例 | 运行形态 | 它验证的通用教训 | 不要照搬什么 |
|---|---|---|---|
| `examples/vllm-monitoring-plan.md` | 在线推理服务 | 三平面互补；延迟分位属于指标而非日志；错误率可直接用带标签的请求结果计数器，不必统计日志 | `vllm:` 前缀的指标名、GPU 相关缺口结论 |
| `examples/sglang-monitoring-plan.md` | 在线推理服务 | 多语言仓库必须逐语言扫；可观测能力可能默认全关；分段耗时可同时存在于指标与自研链路 DSL | `--enable-metrics` 之类的具体开关、阶段注册表结构 |
| `examples/tensorrt-llm-monitoring-plan.md` | 在线推理服务 | 端点协议与路径歧义；多层默认开关且可能无 CLI 参数；断言污染统计；链路可能是指标的投影 | `/prometheus/metrics` 路径、`trtllm_` 前缀、开关表 |
| `examples/prometheus-monitoring-plan.md` | 基础设施组件 | 抓取时合成的样本（如 `up`）不是注册指标，脚本统计为 0 属预期；断言失败即进程消失，崩溃监控走「日志断档」而非关键字 | `prometheus_` 前缀与自监控指标清单 |
| `examples/dubbo-monitoring-plan.md` | 库 / RPC 框架 | sparse checkout 必须在画像里写明扫描范围与缺失模块；指标名走枚举 + 运行时拼接时定义数为 0 属预期，需人工降级提取；告警关键字围绕错误码构建 | `dubbo.error.code.count` 指标、`[类别]-[序号]` 错误码体系 |
| `examples/fastify-monitoring-plan.md` | 库 / Web 框架 | 库类项目方案重点是「暴露什么信号给宿主」；日志门面未配置时默认 no-op，第一优先级是宿主开启并验证 | pino 结构化字段约定、`LogController` 封装细节 |
| `examples/nest-monitoring-plan.md` | 库 / 框架（monorepo） | monorepo 必须先圈定生产代码根目录，否则测试与样例污染统计；框架的就绪字面量是宿主可用性的第一信号 | 内置 Logger 文案、就绪常量原文 |
| `examples/opentelemetry-demo-monitoring-plan.md` | 多语言微服务集群 | 13 种语言的多语言仓库逐语言降级提取的完整样例；主体 RED 指标可由 span 桥接（spanmetrics）而非独立埋点 | collector 配置分层、`demo.*` span 属性命名 |
| `examples/train-ticket-monitoring-plan.md` | 微服务集群（指标/链路贫乏） | 三平面指标/链路真空白时以日志事件为主并写明取舍；链路可由外部 Agent（字节码注入）在运行时提供，源码扫不到不代表没有 | Spring Boot 服务划分、SkyWalking 配置 |

---

## 文件清单

| 文件 | 用途 |
|---|---|
| `scripts/extract_loggers.py` | 日志埋点提取（Python AST + C++ / Rust 正则 + Java/Go/JS 后端） |
| `scripts/extract_metrics.py` | 指标定义 / 写入点 / 暴露点提取（Python AST + Java/Go/JS 后端） |
| `scripts/extract_traces.py` | 链路 span 提取（Python AST + Java/Go/JS 后端） |
| `scripts/lang_extract.py` | Java/Go/JS 通用正则后端：掩码全文 + 括号平衡参数解析 + 作用域近似 |
| `scripts/signal_patterns.json` | Java/Go/JS 三平面规则表（新增 SDK 写法从这里扩展） |
| `scripts/diff_signals.py` | 三类信号通用增量对比 |
| `scripts/diff_loggers.py` | 兼容入口，已转发到 diff_signals.py |
| `scripts/logger_patterns.json` | C++/Rust 日志库正则模式（新增语言从这里扩展） |
| `references/semantic_taxonomy.md` | 日志 / 指标 / span 分类体系（含可扩展的领域词表） |
| `references/signal_arbitration.md` | 信号仲裁流程 + 三平面检查清单 |
| `references/alert_rules.md` | 分层告警模板、等级定义、通用基线 + 领域基线示例 |
| `references/output_format.md` | 输出文档 9 章格式规范 |
| `examples/*.md` | 九个输出示例（覆盖四种运行形态；展示交付格式与典型陷阱，指标名/阈值不可复用） |
