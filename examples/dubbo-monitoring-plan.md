# Apache Dubbo 监控方案（源码 → 监控方案，三平面）

## 1. 项目画像与三平面总览

- 分析对象：Apache Dubbo（RPC 框架，Java）
- 运行形态：**库 / SDK**（被宿主应用集成的 RPC 框架；监控重点 = 框架暴露什么信号给宿主、宿主如何开启与采集，而非框架自带告警）
- 源码语言：Java 100%（2064 个 `.java`；其中 `src/main` 1113 个、`src/test` 951 个，脚本已自动排除 test）
- 扫描范围与 sparse checkout 影响：本仓库为 sparse checkout 子集，实际存在模块仅有：
  - `dubbo-common`（公共层：logger、threadpool、deploy、utils）
  - `dubbo-config`（dubbo-config-api / spring / spring6）
  - `dubbo-metrics`（api / default / event / prometheus / otlp / netty / registry / metadata / config-center / **dubbo-tracing**）
  - `dubbo-rpc`（api / dubbo / injvm / triple）
  - **缺失模块**：dubbo-registry、dubbo-remoting、dubbo-cluster、dubbo-filter、dubbo-qos、dubbo-plugin 等。凡涉及注册中心连接、网络传输层、集群容错的「缺失 X」结论均**不成立为真缺口**，只能标注「未扫描，sparse checkout 无法确认」。
- 提取方式：脚本提取（日志）+ 人工降级提取（指标、链路），未覆盖范围见第 9 章

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面 | 自研双门面：`Logger` / `ErrorTypeAwareLogger` / `FluentLogger`，由 `LoggerFactory` 按 SPI 适配 log4j / log4j2 / slf4j / jcl / jdk | dubbo-common/src/main/java/org/apache/dubbo/common/logger/LoggerFactory.java；ErrorTypeAwareLogger.java |
| 日志结构化约定 | ERROR/WARN 统一带 **错误码参数**（格式 `[类别]-[序号]`，如 `0-1`），错误码集中定义 193 个 | dubbo-common/src/main/java/org/apache/dubbo/common/constants/LoggerCodeConstants.java:40-193 |
| 指标 SDK | 自研事件驱动 metrics API（`MetricsCollector` + `MetricSample`），底座 Micrometer；指标名**不是静态构造器字面量**，而是集中在 `MetricsKey` 枚举注册表，运行时 `String.format(name, side)` 拼接 | dubbo-metrics/dubbo-metrics-event/src/main/java/org/apache/dubbo/metrics/model/key/MetricsKey.java:19-152 |
| 链路 SDK | Micrometer Observation API（非裸 OTel/Brave 直接调用）：`ObservationReceiverFilter`（provider）/`ObservationSenderFilter`（consumer），可经 exporter 落 OTLP 或 Zipkin | dubbo-metrics/dubbo-tracing/src/main/java/org/apache/dubbo/tracing/filter/ObservationReceiverFilter.java:43；ObservationSenderFilter.java:68 |
| 暴露方式 | ① Prometheus **拉取**：独立端口默认 `20888`、路径默认 `/metrics`（`PROMETHEUS_DEFAULT_METRICS_PORT=20888`、`PROMETHEUS_DEFAULT_METRICS_PATH="/metrics"`）；② Prometheus **pushgateway** 推送（默认 30s 间隔）；③ OTLP 推送（`OtlpMetricsReporter`）；④ QoS 命令 `metrics`（复用 qos 通道返回文本格式指标） | dubbo-common/src/main/java/org/apache/dubbo/common/constants/MetricsConstants.java:90-96；dubbo-metrics/dubbo-metrics-prometheus/.../PrometheusMetricsReporterCmd.java:37 |
| 默认开关 | 见 1.6 开关表（**关键结论：链路默认关闭，Prometheus pushgateway 默认关闭，JVM 指标默认关闭**） | MetricsConstants.java / TracingConfig.java / AbstractMetricsReporter.java |

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|---|---|---|---|
| 日志埋点 | 649 条（断言 38 条单列，实际日志调用 611 条） | 集中于 dubbo-common（282，含 logger 适配器自身）、dubbo-config-api（108）、dubbo-rpc-triple（72）；级别 ERROR 157 / WARNING 200 / INFO 172 / DEBUG 82 | 高（且具备错误码体系，是全项目最有价值的检索维度） |
| 指标定义 | 脚本 definition=0 / wrapper=3 / 暴露点 13；**人工提取：MetricsKey 枚举注册表 87 个指标键**（含 `%s` 占位，按 provider/consumer 两侧展开） | 名字全部带 `dubbo.` 前缀（netty 4 个除外），写入点在 `AggregateMetricsCollector` / `HistogramMetricsCollector` / 各 sampler | 高（RED 体系完整：requests/succeed/failed 分类计数 + RT p50/p90/p95/p99 + histogram） |
| 链路 span | 脚本 0 条；人工提取：2 个 Observation 入口（server/client 两个 filter），span 名 = `service/method` 动态生成，导出经 OTLP/Zipkin | 全局仅一条请求主链路 span，无内部子阶段 span | 中（能力存在但**默认关闭**，且依赖宿主引入 micrometer-observation） |

### 1.2 日志按级别分布

| 级别 | 数量 | 占比 | 是否纳入告警 |
|---|---|---|---|
| FATAL | 38（全部为断言 `is_assertion`） | 5.9% | 否——不以日志落盘；用于启动期 smoke test 与崩溃归因 |
| ERROR | 157 | 24.2% | 是（L2 事件类，按错误码聚合） |
| WARNING | 200 | 30.8% | 部分（需白名单） |
| INFO | 172 | 26.5% | 否（lifecycle 事件类除外） |
| DEBUG | 82 | 12.6% | 否（dev_debug；其中 32 条来自 logger 适配器自身，属框架自举日志） |

### 1.3 日志按包分布（Top 10）

| 包/模块 | 埋点数 | ERROR/WARN | 主要职责 |
|---|---|---|---|
| dubbo-common/src | 282 | 约 130 | 公共层：deploy 状态机、threadpool、logger 适配器、extension |
| dubbo-config/dubbo-config-api | 108 | 约 40 | 应用/模块部署器、配置加载 |
| dubbo-rpc/dubbo-rpc-triple | 72 | 约 35 | Triple(HTTP/2+Protobuf) RPC 客户端/服务端 |
| dubbo-rpc/dubbo-rpc-api | 59 | 约 25 | RPC 抽象层 |
| dubbo-config/dubbo-config-spring | 50 | 约 15 | Spring 集成 |
| dubbo-rpc/dubbo-rpc-dubbo | 30 | 约 10 | Dubbo 协议 |
| dubbo-metrics/dubbo-metrics-api | 21 | 少量 | metrics 事件总线 |
| dubbo-metrics/dubbo-metrics-prometheus | 10 | 少量 | Prometheus reporter |
| dubbo-metrics/dubbo-metrics-default | 6 | 少量 | 默认 collector/reporter |
| dubbo-metrics/dubbo-tracing | 6 | 少量 | 链路集成 |

### 1.4 指标按类型分布（人工提取，源 = MetricsKey 枚举）

| 类型 | 数量（约） | 代表指标 | 说明 |
|---|---|---|---|
| Counter | 45+ | `dubbo.%s.requests.total`、`dubbo.%s.requests.failed.total`、`dubbo.registry.register.requests.total` | `%s` 运行时替换为 provider/consumer；写入点 AggregateMetricsCollector.java:134-247 |
| Gauge | 15+ | `dubbo.%s.rt.milliseconds.p99`、`dubbo.thread.pool.active.size`、`dubbo.registry.directory.num.all` | RT 分位经 TimeWindowCounter 聚合后以 Gauge 暴露；AggregateMetricsCollector.java:282-304 |
| Histogram | 1 | `dubbo.%s.rt.milliseconds.histogram` | 受 `histogram.enabled` 开关控制；HistogramMetricsCollector.java:110 |
| 元信息 | 1 | `git.commit.id` | 版本指纹 |

标签体系（低基数，见 MetricsConstants.java:24-50）：`application.name`、`application.module.id`、`interface`、`method`、`group`、`version`、`ip`、`pid`、`hostname`、`error`（错误码维度）。

### 1.5 上下文证据摘要（日志平面）

| 证据 | 数量 | 占比 | 对方案的影响 |
|---|---|---|---|
| in_loop（循环内） | 89 | 13.7% | 禁止频率告警，只做趋势/采样 |
| in_except（异常分支） | 239 | 36.8% | error 类主力 |
| once（仅一次） | 0 | 0 | 无存在性告警候选 |
| conditional（条件分支） | 322 | 49.6% | 大量日志特性门控（如 debug 判断），默认可能不触发 |
| is_assertion（断言） | 38 | 5.9% | 失败即抛异常，不做可检索关键字 |
| 占位符风格 | none=532 / brace=108 / printf=9 | — | Java 侧大量模板来自字符串拼接（`getIdentifier() + " has stopped."`），模板提取为空属预期，溯源以 code_snippet 为准 |

### 1.6 默认开关表（方案第一优先级：怎么打开并验证）

| 能力 | 开关 | 默认值 | 启用方式 | 证据 |
|---|---|---|---|---|
| 链路追踪（Observation） | `dubbo.tracing.enabled` | **false** | `<dubbo:tracing enabled="true"/>` 或 yml `dubbo.tracing.enabled: true` + classpath 引入 micrometer-observation 与 exporter | dubbo-common/src/main/java/org/apache/dubbo/config/TracingConfig.java:33 |
| Prometheus exporter（拉取） | `prometheus.exporter.enabled` | 未显式默认（`PrometheusConfig.enabled` 为 null，需宿主显式配置） | `dubbo.metrics.prometheus.exporter.enabled=true`；端口 `prometheus.exporter.metrics.port`（默认 20888）、路径默认 `/metrics` | dubbo-common/.../config/nested/PrometheusConfig.java:63；MetricsConstants.java:74-92 |
| Prometheus pushgateway（推送） | `prometheus.pushgateway.enabled` | **false** | 同上置 true + `base.url`；间隔默认 30s | PrometheusMetricsReporter.java:73-95；MetricsConstants.java:94 |
| JVM 指标 | `enable.jvm` | **false** | `dubbo.metrics.enable-jvm: true` | AbstractMetricsReporter.java:111 |
| collector 同步任务 | `enable.collector.sync` | **true**（周期默认 `DEFAULT_SCHEDULE_PERIOD`） | 默认已开 | AbstractMetricsReporter.java:145 |
| 聚合指标（RT p99 等） | `aggregation.enabled` | 需确认（`AggregationConfig`） | `dubbo.metrics.aggregation.enabled: true` | MetricsConstants.java:60 |
| RT histogram | `histogram.enabled` | 需确认 | `dubbo.metrics.histogram.enabled: true` | MetricsConstants.java:66 |
| QoS `metrics` 命令 | QoS 开启即用 | 跟随 QoS 端口（默认 22222） | `@Cmd(name="metrics")` 注册 | PrometheusMetricsReporterCmd.java:37 |

> 抽取配置生效验证：打开开关后，先 `curl http://<host>:20888/metrics` 确认返回 Prometheus 文本格式（至少包含一行 `text/plain; version=0.0.4`），再配置抓取——见 PrometheusMetricsReporterCmd.java:70 的注释与 `PrometheusMetricsReporter.getResponse()`。

---

## 2. 日志埋点明细（按语义分类）

> 611 条日志调用按证据字段分类汇总；ERROR/WARN 全量见提取产物 `/tmp/sltm-agent-dubbo/loggers.json`。Dubbo 特殊形态：**ERROR/WARN 的固定可检索特征是错误码**（`[类别]-[序号]`，第一个参数），不是消息模板——模板多为运行时拼接，静态提取为空。类别 `api_surface`（框架对外 API 行为日志）为本项目扩展标签。

### 2.1 生命周期类 (lifecycle)

| 文件:行 | 级别 | 语义 | 错误码/关键字 | 触发条件 |
|---|---|---|---|---|
| config/deploy/DefaultApplicationDeployer.java:1402 | INFO | 应用已停止 | `has stopped.` | shutdown 流程 |
| config/deploy/DefaultApplicationDeployer.java（deployer 系列 INFO 12 条） | INFO | 启动/停止/初始化事件 | `Starting` / `started` / `Stopping` | 部署状态机迁移 |
| common/deploy/AbstractDeployer.java:118,134,150,166,182,199 | ERROR | 状态监听器抛异常 | `0-14`（COMMON_MONITOR_EXCEPTION） | listener.onStarting 等异常（in_except） |
| config/deploy/DefaultModuleDeployer.java（ERROR 8 条） | ERROR | 模块启动失败 | `0-12` 等 | 模块 deploy 异常 |

**监控价值**：启动失败、启动超时判定。就绪信号 = `DefaultModuleDeployer` 的 started 级 INFO；失败信号 = 同文件 ERROR。

### 2.2 请求/事务类 (request)

| 文件:行 | 级别 | 语义 | 证据 |
|---|---|---|---|
| rpc/protocol/tri/call/TripleClientCall.java:108 | ERROR | triple 请求失败 | 错误码 `3-x` 系（PROTOCOL_FAILED_RESPONSE 类）；in_except |
| rpc/protocol/tri/TripleInvoker.java:200 | ERROR | 取消 triple 请求失败 | `"Cancel triple request failed"` + `PROTOCOL_FAILED_REQUEST`；in_except |
| dubbo-rpc-api 各 Invoker/Filter | ERROR/WARN | 调用失败、降级 | 多数 in_except |

### 2.3 下游依赖类 (dependency)

| 文件:行 | 级别 | 语义 | 证据 |
|---|---|---|---|
| tracing/DubboObservationRegistry.java:74 | WARNING | Observation 注册异常 | sparse 范围内 registry 相关日志有限；**注册中心（zookeeper/nacos）连接类日志在 dubbo-registry 模块，本次未扫描** |
| metadata / config-center 相关 | INFO/WARN | 元数据推送、配置中心变更 | `dubbo-metrics-metadata`、`dubbo-metrics-config-center` 各事件类 |

### 2.4 资源指标类 (resource)

| 文件:行 | 级别 | 语义 | 错误码 | 证据 |
|---|---|---|---|---|
| common/threadpool/manager/DefaultExecutorRepository.java:206 | WARNING | 找不到可用线程池（不应发生） | `0-16`（COMMON_EXECUTORS_NO_FOUND） | snippet 已回源核对 |
| common/threadpool/manager/DefaultExecutorRepository.java:290 | ERROR | 线程池执行异常 | `0-18`（COMMON_ERROR_USE_THREAD_POOL） | snippet 已回源核对 |
| common/threadpool/serial/SerializingExecutor.java:113 | ERROR | 任务执行异常 | `0-19`（COMMON_ERROR_RUN_THREAD_TASK） | snippet 已回源核对 |
| common/threadpool/manager/DefaultExecutorRepository.java:241 | WARNING | 线程池创建/覆盖告警 | — | — |

### 2.5 业务逻辑异常 (business_error)

| 语义 | 典型位置 | 说明 |
|---|---|---|
| 服务不可用（invoker 空列表、服务禁用） | dubbo-rpc-api 目录（`RpcException` 处理分支） | 对应指标 `dubbo.%s.requests.failed.service.unavailable.total` |
| 限流拒绝 | cluster filter 路径（sparse 范围内仅 MetricsClusterFilter） | 对应 `dubbo.%s.requests.limit.total` |
| 业务失败 | `dubbo.%s.requests.business.failed.total` 对应的日志分支 | 区别于框架级失败 |

### 2.6 系统异常 (system_error)

| 语义 | 数量 | 典型错误码 | 位置示例 |
|---|---|---|---|
| 扩展加载失败 | 9（ExtensionLoader ERROR） | `0-15`（COMMON_ERROR_LOAD_EXTENSION） | common/extension/ExtensionLoader.java |
| 编解码/协议异常 | triple 模块约 35 条 | `3-x` 系 | rpc/protocol/tri/* |
| 注册中心操作失败 | LoggerCodeConstants 定义 `1-1`~`1-20` | `1-17`（刷新地址失败）等 | 大部分消费点在未扫描的 dubbo-registry 模块 |
| 指标采集异常 | — | `0-13`（COMMON_METRICS_COLLECTOR_EXCEPTION） | AbstractMetricsReporter.java:169、PrometheusMetricsReporter.java:117 |

### 2.7 调试日志（dev_debug，不纳入告警）

DEBUG 82 条，其中 32 条来自 logger 适配器自身（`JclLogger` / `JdkLogger` / `Slf4jLogger` / `FailsafeLogger`——框架日志门面的自举实现），其余为部署器与配置调试输出。由人工决定是否采集，不进告警。

### 2.8 断言类（is_assertion，单列）

38 条，全部为 Java `assert`。失败即抛 AssertionError，**不能作为日志检索关键字**；用途限于启动期 smoke test 与崩溃归因（进程消失 + 日志断档）。

---

## 3. 指标清单

### 3.1 指标提取方式说明（重要）

脚本输出 definition=0 / wrapper=3 / 暴露点 13，**不能据此断言「指标很少」**。原因：Dubbo 指标名不走 Micrometer 静态构造器，而是运行时经 `MetricsKey.getNameByType(side)`（`String.format`）拼出后，由 `AbstractMetricsReporter.registerCounterSample/registerGaugeSample` 用 `sample.getName()` 动态注册（AbstractMetricsReporter.java:200,208 —— 即脚本识别到的 3 条 wrapper）。真实指标名注册表 = `MetricsKey` 枚举（87 个键），写入点 = 各 collector/sampler。以下为人工提取结果（降级路径，覆盖 MetricsKey 全部字面量）。

### 3.2 已定义的指标（按语义角色归组，节选核心，全量 87 键见源码）

| 指标名（源码构造名） | 类型 | 语义角色 | 关键标签 | 源码位置 | 写入点 |
|---|---|---|---|---|---|
| `dubbo.{provider\|consumer}.requests.total` | Counter | RED.Rate | application.name/interface/method | MetricsKey.java:25 | AggregateMetricsCollector.java:134 |
| `dubbo.%s.requests.succeed.total` | Counter | RED.Rate | 同上 | MetricsKey.java:26 | AggregateMetricsCollector.java:150 |
| `dubbo.%s.requests.failed.total` | Counter | RED.Errors | 同上 | MetricsKey.java:33 | AggregateMetricsCollector.java:162 |
| `dubbo.%s.requests.business.failed.total` | Counter | RED.Errors（业务） | 同上 | MetricsKey.java:27 | AggregateMetricsCollector.java:150 分支 |
| `dubbo.%s.requests.timeout.total` | Counter | RED.Errors | 同上 | MetricsKey.java:30 | DefaultSubDispatcher.java:83 起 |
| `dubbo.%s.requests.limit.total` | Counter | RED.Errors（限流） | 同上 | MetricsKey.java:31 | 同上 |
| `dubbo.%s.requests.failed.network.total` | Counter | RED.Errors | 同上 | MetricsKey.java:34 | 同上 |
| `dubbo.%s.requests.failed.codec.total` | Counter | RED.Errors | 同上 | MetricsKey.java:37 | 同上 |
| `dubbo.%s.requests.failed.service.unavailable.total` | Counter | RED.Errors | 同上 | MetricsKey.java:35 | DefaultMetricsCollector.java:52 |
| `dubbo.%s.requests.processing.total` | Gauge | USE.Saturation（在途请求） | 同上 | MetricsKey.java:29 | DefaultSubDispatcher.java:77 |
| `dubbo.%s.rt.milliseconds.p50/p90/p95/p99` | Gauge | RED.Duration | 同上 | MetricsKey.java:60-63 | AggregateMetricsCollector.java:282-304 |
| `dubbo.%s.rt.milliseconds.last/min/max/sum/avg` | Gauge | RED.Duration | 同上 | MetricsKey.java:55-59 | 同上 |
| `dubbo.%s.rt.milliseconds.histogram` | Histogram | RED.Duration | 同上 | MetricsKey.java:72 | HistogramMetricsCollector.java:110 |
| `dubbo.%s.qps.total` | Gauge（窗口均值） | RED.Rate | 同上 | MetricsKey.java:54 | AggregateMetricsCollector.java:267 |
| `dubbo.registry.register.requests.total` | Counter | RED.Rate（注册） | application.name | MetricsKey.java:69 | registry collector（dubbo-metrics-registry） |
| `dubbo.registry.subscribe.num.total` | Counter | RED.Rate（订阅） | 同上 | MetricsKey.java:79 | 同上 |
| `dubbo.registry.directory.num.all/valid/to_reconnect/disable` | Gauge | USE（服务目录健康） | 同上 | MetricsKey.java:84-87 | RegistryStatComposite.java |
| `dubbo.metadata.push.num.total` 等 | Counter | RED（元数据推送） | 同上 | MetricsKey.java:101-109 | metadata collector |
| `dubbo.thread.pool.core/max/active.size` 等 7 键 | Gauge | USE（线程池水位） | application.name | MetricsKey.java:92-98 | ThreadPoolMetricsSampler.java |
| `dubbo.thread.pool.reject.thread.count` | Counter | USE.Errors（拒绝） | 同上 | MetricsKey.java:98 | ThreadRejectMetricsCountSampler.java:33 |
| `dubbo.error.code.count` | Counter | RED.Errors（按错误码计数） | application.name + `error`（错误码） | MetricsKey.java:137 | ErrorCodeSampler.java:43 |
| `dubbo.consumer.invoker.no.available.count` | Counter | RED.Errors（无可用提供者） | 同上 | MetricsKey.java:134 | api 层 listener |
| `dubbo.application.info.total` | Counter | lifecycle | 同上 | MetricsKey.java:20 | init 事件 |
| `dubbo.configcenter.total` | Counter | dependency（配置中心变更） | config.center/change.type | MetricsKey.java:22 | config-center collector |
| `netty.allocator.*`（8 键） | Gauge | USE（内存分配器水位） | — | MetricsKey.java:140-151 | dubbo-metrics-netty |
| `git.commit.id` | Gauge | 版本指纹 | git.commit.id | MetricsKey.java:130 | — |

### 3.3 指标质量风险

| 风险 | 影响 | 说明 |
|---|---|---|
| 名称含 `%s`（side 维度） | 告警 PromQL 必须按 `{provider,consumer}` 或实际标签写，不能照抄源码名 | Micrometer 注册时 `%s` 被替换为 provider/consumer；`_total` 后缀是否追加由 Micrometer 决定（Counter 注册会补 `_total`），**抓取名以 `/metrics` 实际输出为准** |
| 多个关键指标受开关控制 | `aggregation.enabled`/`histogram.enabled` 未开时 RT 分位与 histogram 不存在，告警永不触发 | MetricsConstants.java:60,66 |
| `enable.jvm=false` 默认关 | JVM 内存/GC 指标默认缺失 | AbstractMetricsReporter.java:111 |
| RT 分位是 TimeWindowCounter 窗口聚合而非原生 Histogram 分位 | p99 语义 = 窗口内聚合，跨实例聚合需谨慎 | dubbo-metrics-api/aggregate/TimeWindowCounter.java |
| 指标注册在 `resetIfSamplesChanged` 同步任务里 | 首次请求前部分指标不存在（按需注册） | AbstractMetricsReporter.java:157-178 |

### 3.4 指标暴露方式

| 方式 | 位置 | 说明 |
|---|---|---|
| Prometheus 拉取（独立 HTTP 端口） | `PrometheusMetricsReporter`；默认端口 20888、路径 /metrics | 需 `prometheus.exporter.enabled=true`；文本协议 `text/plain; version=0.0.4` |
| Prometheus pushgateway | `PrometheusMetricsReporter.schedulePushJob()` | 默认关闭；间隔 30s；支持 BasicAuth |
| OTLP 推送 | `OtlpMetricsReporter`（dubbo-metrics-otlp） | 需配置 otlp endpoint |
| QoS 命令 | `PrometheusMetricsReporterCmd`（`@Cmd(name="metrics")`） | 经 QoS 端口（默认 22222）telnet/http 拉取文本格式；`application_all` 返回 JSON |
| （指标 API 查询） | `DefaultMetricsService`（dubbo-metrics-default） | 宿主内编程式读取 |

---

## 4. 信号仲裁（库/SDK 运行形态的需求清单）

需求清单按「库/SDK」形态选取：框架暴露什么信号给宿主、宿主如何采集、错误如何暴露；宿主应用层的业务 RED 告警不在本方案范围内。

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---|---|---|---|---|
| 1 | 框架启动/停止事件（宿主感知部署状态） | 日志 | `Starting/started/has stopped.` 系 INFO + deploy ERROR | DefaultApplicationDeployer.java:1402 系；DefaultModuleDeployer ERROR 系 | 已覆盖（事件类） |
| 2 | 请求错误率 | 指标 | `dubbo.%s.requests.failed.total` / `requests.total` | MetricsKey.java:25,33；写入 AggregateMetricsCollector.java:134-162 | 已覆盖（指标优先） |
| 3 | 请求延迟分位 | 指标 | `dubbo.%s.rt.milliseconds.p50/p90/p95/p99` + histogram | MetricsKey.java:60-63,72 | 已覆盖（需 `aggregation.enabled` / `histogram.enabled`，候选阈值待校准） |
| 4 | 超时/限流/无可用提供者/编解码失败分类 | 指标 | timeout/limit/codec/service.unavailable 四类专用 Counter | MetricsKey.java:30-37 | 已覆盖（比日志更可靠） |
| 5 | 在途请求/饱和度 | 指标 | `dubbo.%s.requests.processing.total` | MetricsKey.java:29 | 已覆盖 |
| 6 | 线程池耗尽/拒绝 | 指标 + 日志 | `dubbo.thread.pool.reject.thread.count` + 日志错误码 `0-1`/`0-16`/`0-18` | MetricsKey.java:98；LoggerCodeConstants.java:44; DefaultExecutorRepository.java:206,290 | 已覆盖（指标为主，日志码为辅） |
| 7 | 注册中心/服务目录健康 | 指标 | `dubbo.registry.directory.num.to_reconnect.total`、subscribe/register 失败计数 | MetricsKey.java:71,81,86 | 部分覆盖；**注册中心连接细节在未扫描的 dubbo-registry 模块，无法确认** |
| 8 | 一次调用慢在哪一段（分段耗时） | 链路 | Observation server/client span（名字 `service/method`，标签 rpc.system/service/method） | ObservationReceiverFilter.java:56-68；AbstractDefaultDubboObservationConvention.java:49-56 | 能力存在但**默认关闭**（TracingConfig.java:33 enabled=false），且只有单层请求 span、无内部子阶段——「哪一段慢」主要靠日志错误码 + 指标，不是 span 瀑布 |
| 9 | 框架内部故障归因 | 日志 | 193 个错误码（`0-x` common / `1-x` registry / `2-x` config / `3-x` protocol 等） | LoggerCodeConstants.java | 已覆盖，且 `dubbo.error.code.count` 指标可按码聚合（MetricsKey.java:137） |
| 10 | 宿主 JVM 状态 | 指标 | Micrometer JVM binder | AbstractMetricsReporter.java:110-128 | 已有但默认关闭（`enable.jvm=false`） |
| 11 | 网络传输层（连接数、字节量） | 指标 | 仅 netty allocator 内存 8 键 | MetricsKey.java:140-151 | 部分覆盖；**dubbo-remoting 未扫描，连接级指标无法确认** |
| 12 | 宿主业务方法级错误率 | 指标 | interface/method 标签的 requests/failed 系 | MetricsKey.java:25-37 | 已覆盖（标签下钻） |
| 13 | 安全/漏洞事件 | 日志 | 错误码 `0-28`（VULNERABILITY_WARNING） | LoggerCodeConstants.java（`0-28`） | 已覆盖（存在性告警） |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|---|---|---|
| 日志 | 611 条调用 + 38 断言；按模板关键词（registry/zookeeper/nacos/thread/timeout/failed）与模块分布检索 | registry/remoting 连接级日志不在扫描范围 |
| 指标 | MetricsKey 枚举全量 87 键 + 13 个暴露点 + 3 条动态封装 wrapper；检索词根：requests/rt/failed/timeout/limit/pool/directory/notify | 无 CPU/内存（JVM 关默认）、无网络字节量指标 |
| 链路 | 脚本 0 条 → 人工核查 dubbo-tracing 全模块：2 个 filter 入口、2 个 convention、2 个 exporter；确认 Observation API 写法不在 signal_patterns.json 覆盖内（Java 后端只识别 OTel spanBuilder/@WithSpan） | 脚本 0 属**提取遗漏（SDK 未覆盖）**，非「无链路」 |

---

## 5. 关键字库

### 5.1 告警关键字（日志事件类，可全文匹配）

Dubbo 日志的可靠检索锚点是**错误码**（首个参数，固定格式 `\d+-\d+`），不是消息文本：

| 关键字（错误码） | 级别 | 说明 | 来源 |
|---|---|---|---|
| `0-1`（COMMON_THREAD_POOL_EXHAUSTED） | P1 | 线程池耗尽 | LoggerCodeConstants.java:44 |
| `0-12`（COMMON_UNEXPECTED_EXCEPTION） | P1 | 框架未预期异常 | LoggerCodeConstants.java:60 |
| `0-13`（COMMON_METRICS_COLLECTOR_EXCEPTION） | P2 | 指标采集异常（监控自身失明） | LoggerCodeConstants.java:64 |
| `0-15`（COMMON_ERROR_LOAD_EXTENSION） | P2 | 扩展加载失败 | LoggerCodeConstants.java:68 |
| `0-16` / `0-17` / `0-18` / `0-19` | P1 | 线程池缺失/被关/异常 | LoggerCodeConstants.java:70-76 |
| `0-28`（VULNERABILITY_WARNING） | P0 | 漏洞告警 | LoggerCodeConstants.java（0-28） |
| `1-1` / `1-17`（REGISTRY_ADDRESS_INVALID / FAILED_REFRESH_ADDRESS） | P1 | 注册中心地址/刷新失败 | LoggerCodeConstants.java:99,135 |
| `3-x` 系（protocol/triple） | P1 | triple 协议失败（PROTOCOL_FAILED_REQUEST/RESPONSE） | TripleClientCall.java:108、TripleInvoker.java:200 |

### 5.2 统计字段（不可全文匹配，需按字段/标签提取）

| 字段 | 所在 | 统计方式 | 维度 |
|---|---|---|---|
| `dubbo.error.code.count` 的 `error` 标签 | 指标 | 按码分组计数突增检测 | error code |
| interface/method 标签 | 指标 | 服务级 RED 下钻 | interface, method（**中基数**，随服务数线性增长，注意基数控制） |
| 日志占位符（`{}` × 108 条） | 日志 | 结构化提取后再统计 | — |

> ⚠ 源码中的常量（如 `PROMETHEUS_DEFAULT_PUSH_INTERVAL=30`）是业务配置，不是告警阈值。

### 5.3 降噪白名单

| 关键字/条件 | 原因 | 处理 |
|---|---|---|
| `1-13`（REGISTRY_EXECUTE_RETRYING_TASK） | 注册中心重试属正常自愈 | 只在重试持续超阈值后告警 |
| 客户端主动断开 / `Cancel triple request failed`（TripleInvoker.java:200） | 客户端取消，常见 | 忽略或 P3 统计 |
| `0-29`（COMMON_NOT_FOUND_TRACER_DEPENDENCY） | 未集成 tracer 的降级提示 | feature_gated，忽略 |
| conditional 非空的 322 条日志 | 特性门控，可能默认不触发 | 先确认配置再决定监控 |
| DEBUG 82 条（含 logger 适配器自举） | dev_debug | 不进告警 |
| in_loop 的 89 条 | 高频 | 禁止频率告警 |

### 5.4 检索语句示例

```logql
# 框架错误日志占比（按错误码锚定；job 换成宿主实际值）
sum(rate({job="<dubbo-host>"} |~ `"[0-9]+-[0-9]+"` [5m]))
/
sum(rate({job="<dubbo-host>"} [5m]))

# 线程池耗尽事件（存在性）
sum(count_over_time({job="<dubbo-host>"} |= "0-1" [5m]))
```

```promql
# 请求错误率（抓取名以 /metrics 实际输出为准，Micrometer Counter 会追加 _total）
sum(rate(dubbo_provider_requests_failed_total[5m])) by (interface)
/
sum(rate(dubbo_provider_requests_total[5m])) by (interface)

# RT P99（窗口聚合 Gauge，直接读值）
dubbo_provider_rt_milliseconds_p99
```

---

## 6. 告警规则草稿（候选阈值，待 SLO/压测/线上数据校准）

> 库/SDK 形态：以下是**宿主应用启用 Dubbo 指标暴露后**可建的告警，前提是第 1.6 章开关已打开并验证数据流。所有阈值均为候选。

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|---|---|---|---|---|---|
| RPC 错误率超阈值 | `sum(rate(dubbo.%s.requests.failed.total[5m])) / sum(rate(dubbo.%s.requests.total[5m])) > 阈值`（量纲 0~1） | > 0.01 持续 5m | P1 | by (interface)；持续 2 周期 | 计划内发布窗口 |
| RT P99 劣化 | `dubbo.%s.rt.milliseconds.p99 > 阈值`（单位 ms，窗口聚合值） | > 500ms | P1 | 按 interface 分组；区分冷启动 | 压测窗口 |
| 在途请求积压 | `dubbo.%s.requests.processing.total > 阈值` | > 线程池 max × 2（与 `dubbo.thread.pool.max.size` 联动） | P2 | 持续 5m | — |
| 线程池拒绝发生 | `increase(dubbo.thread.pool.reject.thread.count[5m]) > 0` | 任何非零 | P1 | 按实例去重 | — |
| 无可用提供者 | `increase(dubbo.consumer.invoker.no.available.count[5m]) > 0` | 任何非零 | P1 | — | 计划内发布 |
| 服务目录重连积压 | `dubbo.registry.directory.num.to_reconnect.total > 0 持续 10m` | 非零持续 | P2 | — | 注册中心维护窗口 |
| 某错误码计数突增 | `sum(increase(dubbo.error.code.count[10m])) by (error) > 基线 × N` | 3 倍基线（待定基线） | P2 | 相对基线；排除白名单码 | 1-13（重试类） |

### 6.2 日志事件告警（L2）

| 告警名 | 条件 | 等级 | 降噪 |
|---|---|---|---|
| 模块/应用启动失败 | 出现 DefaultModuleDeployer/DefaultApplicationDeployer 的 ERROR（`0-12` 系） | P1 | 只在重启事件后判定；按实例去重 |
| 线程池耗尽日志 | 出现错误码 `0-1` | P1 | 指标 `reject.thread.count` 已覆盖时以指标为准 |
| 注册中心地址无效 | 出现 `1-1` / `1-17` | P1 | 按注册中心类型聚合 |
| 漏洞告警 | 出现 `0-28` | P0 | 存在性判断 |

### 6.3 日志频率告警（L3，谨慎）

| 告警名 | 条件 | 基线来源 | 等级 |
|---|---|---|---|
| triple 协议错误突增 | `rate({job} \|~ "3-[0-9]+" [5m]) > 基线 × 3` | 历史日志数据 | P2 |

> 仅当未启用 `dubbo.error.code.count` 指标时使用；启用后以 6.1 的错误码指标告警替代。

### 6.4 链路告警（L4，可选——仅当 `dubbo.tracing.enabled=true` 且 exporter 已配置）

| 告警名 | span | 条件 | 等级 |
|---|---|---|---|
| RPC span 错误率 | `service/method`（server + client 两个 span） | span error 比例 > 阈值（候选 1%，待校准） | P2 |

> span 无内部子阶段，分段耗时告警价值有限；优先用 L1。

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，且在本扫描范围内确认）

| 场景 | 风险 | 建议做法 | 优先级 |
|---|---|---|---|
| JVM 内存/GC 默认无指标 | 无法感知宿主内存水位 | 打开 `enable.jvm`（AbstractMetricsReporter.java:111），无需改源码 | P1 |
| 网络字节量/连接数指标缺失 | 传输层容量规划缺数据 | sparse 范围内 netty 仅 allocator 内存指标；连接级需确认 dubbo-remoting（未扫描）或宿主侧采集 | P2 |
| 内部子阶段 span 缺失 | 「一次调用慢在哪一段」只能到 service/method 粒度 | 框架设计如此（单层 Observation span）；深挖需宿主接入 OTel 扩展或看日志错误码 | P3 |

### 7.2 已有但不可用（信号存在，质量问题）

| 信号 | 问题 | 建议 |
|---|---|---|
| 全部指标 | 默认不暴露：需 `prometheus.exporter.enabled=true` 或 OTLP/pushgateway 配置 | 按 1.6 开关表启用并验证 `curl :20888/metrics` |
| RT 分位 / histogram | 受 `aggregation.enabled` / `histogram.enabled` 控制 | 启用时一并打开 |
| `dubbo.%s.*` 指标名 | `%s` 在注册时替换为 side；Prometheus 抓取名另加 `_total` 后缀（Counter） | 面板/告警统一用**抓取名**；上线前以 `/metrics` 实际输出为准 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|---|---|---|
| 日志消息大量运行时字符串拼接（模板提取为空 532 条） | 全仓库 | 检索依赖错误码而非消息；保持错误码规范即可，非缺陷 |
| `0-14`（listener 异常）在状态机各处重复出现 | AbstractDeployer.java:118-199 | 聚合时按错误码去重 |
| logger 适配器自身日志混入统计 | common/logger/*（32 条 DEBUG） | 采集侧按包过滤 |

### 7.4 需人工确认（脚本未能确定 / sparse checkout 未扫描）

| 项 | 原因 |
|---|---|
| `prometheus.exporter.enabled` 的最终默认值 | 常量已定义（MetricsConstants.java:68）但消费点不在扫描范围（疑在未扫描的 dubbo-qos/plugin 模块） |
| 注册中心连接、重连、通知类日志与指标的实际密度 | dubbo-registry / dubbo-remoting / dubbo-cluster 模块未在 sparse checkout 中 |
| `aggregation.enabled` / `histogram.enabled` 的运行时默认 | 由 nested config 决定，需结合完整仓库与宿主配置确认 |
| 集群容错（failover 等）日志 | dubbo-cluster 未扫描 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本重新提取（三平面同命令，输出 *_v2.json）
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json

# diff
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

> 注：metrics.json 中脚本提取的 wrapper/暴露点可参与 diff；`MetricsKey` 人工清单可整理成同构 JSON（name/type/file/line/kind）后走同一套 diff。

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---|---|---|
| `LoggerCodeConstants` 新增错误码 | 新故障模式 | 评估新增 L2/L3 告警；更新 5.1 关键字库 |
| `MetricsKey` 新增指标键 | 新的可观测能力 | 评估新增 L1 告警与看板 |
| `MetricsKey` 删除/改名 | 告警静默失效 | **必须下线对应规则** |
| 指标标签变更（如 error 标签） | PromQL 静默失效 | 更新查询与看板 |
| 开关默认值变更（tracing/jvm/pushgateway） | 数据流变化 | 重新核对 1.6 开关表，优先级最高 |
| Observation filter/convention 变更 | span 名/标签变化 | 更新 L4 规则 |

### 8.3 diff 局限性

- 按「文件名 + 模板」模糊匹配，行号偏移忽略；同文件同模板多行折叠 → 计数是近似值
- Dubbo 大量拼接式消息模板为空，diff 区分度低；**错误码清单（LoggerCodeConstants/MetricsKey）才是本项目最可靠的 diff 维度**

---

## 9. 局限性

1. **阈值为候选值**：全部需结合 SLO、压测基线、线上历史校准；源码常量（如 push 间隔 30s）≠ 告警阈值。
2. **sparse checkout 范围限制**：仅扫描 dubbo-common / dubbo-config / dubbo-metrics / dubbo-rpc 四大模块（2064 个 java 文件）。dubbo-registry、dubbo-remoting、dubbo-cluster、dubbo-qos、dubbo-filter、dubbo-plugin 等模块未扫描，凡涉这些层的「缺失」结论均标注「未扫描，无法确认」，**不构成真缺口结论**。
3. **指标平面为人工降级提取**：脚本 definition=0 是因为 Dubbo 指标名走 `MetricsKey` 枚举 + `String.format` 动态构造、经 `sample.getName()` 注册，不在 signal_patterns.json 覆盖内。人工清单覆盖 MetricsKey 全部 87 个字面量，但各 collector 的运行时注册条件（开关、首次请求后才注册）是静态分析无法完全确认的。
4. **链路平面为人工降级提取**：Micrometer Observation API 不在脚本 Java 规则覆盖内；span 名运行时动态生成（`service/method`），无法静态枚举。
5. **静态分析看不到运行时**：tracing 默认关闭、JVM 指标默认关闭、pushgateway 默认关闭；Prometheus 抓取名与源码构造名可能不同（Micrometer 会追加 `_total`、替换 `%s`）——抓取名以 `/metrics` 实际输出为准。
6. **日志模板提取覆盖率有限**：532 条模板为空（字符串拼接/可变参数），语义分类以错误码 + code_snippet 抽样回源（6 条已核对：DefaultApplicationDeployer.java:1402、SerializingExecutor.java:113、AbstractDeployer.java:118、DefaultExecutorRepository.java:206,290、TripleClientCall.java:108）为依据。
7. **宿主业务语义**：本方案只覆盖框架自身信号；宿主应用的业务 RED、第三方 SDK（注册中心客户端等）信号需在各组件自身方案中处理。
8. **断言单列**：38 条 assert 失败不以日志落盘，崩溃信号 = 日志断档 + 进程重启。
