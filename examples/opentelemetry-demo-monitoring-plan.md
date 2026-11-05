# opentelemetry-demo 监控方案（源码 → 三平面可观测性分析）

- 分析对象：opentelemetry-demo（OpenTelemetry 官方微服务演示应用），IMAGE_VERSION=3.1.0（`.env:2`）
- 运行形态：多语言在线服务集群（电商 gRPC/HTTP 微服务）+ 浏览器前端 + Kafka 事件链 + 压测器 + 完整可观测栈（OTel Collector / Jaeger / Prometheus / Grafana / OpenSearch）
- 提取方式：脚本提取（Python / C++ / Rust / Java / Kotlin / Go / JS/TS）+ 人工降级提取（C# / PHP / Ruby、C++/Rust 的指标与链路），未覆盖范围见第 9 章

---

## 1. 项目画像与三平面总览

### 1.0 服务 × 语言矩阵（生产代码根目录均为 `src/`）

| 服务 | 语言 | 可观测承载 | 证据 |
|---|---|---|---|
| frontend | TS/TSX | Web OTel SDK（span→OTLP） | `src/frontend/utils/telemetry/FrontendTracer.ts:32-45` |
| frontend-proxy | Envoy（配置） | http_check receiver + OTLP 转发 | `otel-config.yml`；`.env:40` |
| checkout | Go | OTel Go SDK（otelgrpc/otelhttp/otelslog） | `src/checkout/main.go:53-66` |
| product-catalog | Go | OTel Go SDK + OpAMP | `src/product-catalog/main.go:114-119` |
| ad | Java | OTel Java agent + Prometheus client | `compose.yaml:36-53`；`src/ad/src/main/java/oteldemo/AdService.java:76,109` |
| recommendation | Java | OTel Java agent | `src/recommendation/src/main/java/oteldemo/recommendation/RecommendationService.java:72` |
| fraud-detection | Kotlin | OTel Java agent | `src/fraud-detection/main/kotlin/frauddetection/main.kt` |
| cart | C# | OTel .NET SDK + LoggerMessage 源生成 | `src/cart/src/cart.csproj`；`src/cart/src/Log.cs` |
| accounting | C# | OTel .NET SDK + LoggerMessage 源生成 | `src/accounting/Log.cs` |
| payment | JS | OTel JS SDK | `src/payment/charge.js:28` |
| currency | C++ | OTel C++ SDK（tracer/meter 手工初始化） | `src/currency/src/tracer_common.h:73-102`；`server.cpp:357` |
| shipping | Rust | OTel Rust SDK（OTLP exporter） | `src/shipping/src/telemetry_conf.rs:4-10` |
| email | Ruby | OTel Ruby SDK（logs+metrics+Sinatra 插桩） | `src/email/email_server.rb:13-45` |
| quote | PHP | OTel PHP SDK | `src/quote/public/index.php:15-19` |
| agent / chatbot / mcp | Python | OTel Python + OpAMP | `src/chatbot/src/chat_interface/chat_interface.py:19` |
| load-generator | Python（Locust） | OTel span 上报压测行为 | `src/load-generator/locustfile.py:151-259` |
| react-native-app | TS | OTel RN tracer | `src/react-native-app/hooks/useTracer.ts:128` |
| kafka / valkey / postgres / opensearch | 基础设施 | 由 Collector 接收器采集，不在应用源码内插桩 | `src/otel-collector/otelcol-config.yml:47-146` |

语言分布（find 统计）：ts 92、js 14、go 14、py 13、cs 13、java 9、h 9、rs 7、php 6、cc 4、cpp 1、rb 1、kt 1。

**脚本能力矩阵核对**：`.py / .go / .java / .kt / .js .ts / .cc .cpp .h` 日志+指标+链路可脚本提取；`.rs` 仅日志；**`.cs / .php / .rb` 三平面全部不在能力矩阵内 → 走人工降级路径**（结果已并入第 2、3 章，覆盖率未知）。

### 1.1 可观测技术栈

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面 | 按语言各异：Go=otelslog 桥接 OTel（`checkout/main.go:62`）、Java=JUL/java.util.logging（`AdService.java:255`）、C#=LoggerMessage 源生成（`cart/src/Log.cs:11`）、Python=logging、JS=pino（`frontend/utils/telemetry/logger.ts:3`）| 各文件行号 |
| 指标 SDK | 自定义指标极少且统一 `demo` 语义前缀（两种风格并存：`demo.name` 与 `demo_name_total`）；主体指标来自 OTel 自动插桩 + Collector 接收器 | `metrics.json`；`otelcol-config.yml:264` |
| 链路 SDK | 全服务 OTel 标准化 span→OTLP；span 属性采用 `demo.*` 命名空间（40 处） | `traces.json`；`payment/charge.js:44-66` |
| 信号汇聚 | 单点 OTel Collector（OTLP grpc 4317 / http 4318），trace→Jaeger、metric→Prometheus、log→OpenSearch | `otelcol-config-observability.yml:64-74`；`.env:32-39` |
| span→metric 桥 | spanmetrics connector（`span_metrics`）把 span 聚合为调用/错误/时长指标后并入 metrics 管道 | `otelcol-config.yml:254-255,262-264` |

### 1.2 暴露端点与默认开关（注册代码 + 配置默认值两处证据）

| 能力 | 注册代码 | 配置默认值 | 默认状态 |
|---|---|---|---|
| OTLP 三平面接收 | `otelcol-config.yml:10-25`（otlp receiver grpc/http） | `.env:33-34` 4317/4318 | **开启**（collector 启动即监听） |
| span→metric 桥 | `otelcol-config.yml:262`（traces 导出到 span_metrics） | `otelcol-config.yml:254-255` connectors 声明 | **开启** |
| trace→Jaeger | `otelcol-config-observability.yml:20-23,68` | 仅加载 observability 层时生效 | 取决于 compose 组合 |
| metric→Prometheus | `otelcol-config-observability.yml:26-29,70`（otlp 写入 prometheus `/api/v1/otlp`） | 同上 | 取决于 compose 组合 |
| log→OpenSearch | `otelcol-config-observability.yml:32-38,72` | 同上 | 取决于 compose 组合 |
| ad 服务 Prometheus 拉取端点 | `AdService.java:109`（/metrics HTTP 服务启动日志） | `.env:59` AD_PROMETHEUS_PORT=9465；`otelcol-config.yml:138-147`（prometheus/ad receiver 抓 `ad:9465`） | **开启** |
| Collector 自身配置分层 | `otelcol-config.yml:3-7`（注释：base 只导出 debug；layered via --config） | `.env:35-38` 指向 base 配置；Makefile/compose 组合 full+observability | base 单跑时**仅 debug 导出** |
| frontend 浏览器 span | `FrontendTracer.ts:36-43`（BatchSpanProcessor→OTLP http） | `.env:40` PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8080/otlp-http/v1/traces（经 frontend-proxy 转发） | **开启** |
| 容器/主机/Redis/PG/对象存储指标 | `otelcol-config.yml:41-146`（docker_stats、host_metrics、redis、postgresql、nginx、http_check 接收器） | 同文件内 enabled:true | **开启** |
| Kafka 指标 | `otelcol-config-full.yml:23`（kafkametrics receiver） | 仅 full 层 | 取决于 compose 组合 |
| 敏感数据脱敏 | `otelcol-config.yml:241-252`（redaction processor：掩码卡号、阻断 card_pin/password/secret） | processor 在 traces/logs 管道生效（:261,269） | **开启** |
| 故障注入（problempattern） | `ad/src/main/java/oteldemo/problempattern/CPULoad.java:48`；`checkout/main.go:719`（kafkaQueueProblems）；`main.kt:69,89`（kafkaConsumerDead/kafkaQueueProblems） | flagd 特性开关控制，默认关闭 | **默认关闭** |

compose 组合说明（`compose.yaml:4-9`）：默认全量 = compose.yaml + full + observability，此时上述三套导出全部生效；仅 `docker compose -f compose.yaml up` 时 collector **只打印 debug，任何后端都没有数据**。

### 1.3 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|---|---|---|---|
| 日志埋点 | 脚本 134 条 + 人工降级 C# 19 个 LoggerMessage 定义 + Ruby/PHP 少量 | checkout 31、product-catalog 26、load-generator 16、ad 13；ERROR 占 30.6% | 高 |
| 指标定义 | 脚本 6 个 + 人工降级 2 个；另有 SDK 自动插桩指标与 spanmetrics 桥接指标（运行时命名） | 全部为 Counter；无自定义 Gauge/Histogram | 中 |
| 链路 span | 脚本 19 个 span（含压测 span 13 个）+ 40 处 span 属性 + 7 处 span_event；人工降级 C++ 2 个 span、Rust span 事件/属性 | 属性集中在 `demo.*` 命名空间 | 高 |

### 1.4 日志按级别分布（脚本提取 134 条）

| 级别 | 数量 | 占比 | 是否纳入告警 |
|---|---|---|---|
| FATAL | 3 | 2.2% | 是（P0，其中 2 条为 panic/abort 断言类，见 1.7） |
| ERROR | 41 | 30.6% | 是（P1，需白名单） |
| WARNING | 13 | 9.7% | 部分（需白名单） |
| INFO | 74 | 55.2% | 否（启动/就绪事件类除外） |
| DEBUG | 3 | 2.2% | 否 |

### 1.5 日志按服务分布（Top 10）

| 服务/目录 | 埋点数 | ERROR/WARN | 主要职责 |
|---|---|---|---|
| src/checkout | 31 | 20 | 下单编排、Kafka 生产、gRPC 下游调用 |
| src/product-catalog | 26 | 17 | 商品目录、DB、OpAMP 生命周期 |
| src/load-generator | 16 | 2 | 压测行为日志（**非生产路径**，监控应剔除） |
| src/ad | 13 | 2 | 广告服务、Prometheus 端点、故障注入 |
| src/shipping | 10 | 1 | 运费报价、追踪号、特性开关降速 |
| src/payment | 6 | 2 | 支付扣款 |
| src/agent | 7 | 2 | LLM agent 服务 |
| src/chatbot | 7 | 3 | LLM 聊天 + OpAMP 客户端 |
| src/frontend | 6 | 3 | 结账 API、请求中间件 |
| src/mcp | 3 | 0 | MCP server 生命周期 |

其余：fraud-detection 4、recommendation 3、react-native-app 2；人工降级：cart(C#) 13、accounting(C#) 6、email(RB) 若干、quote(PHP) 配置型日志。

### 1.6 指标按类型分布

| 类型 | 数量 | 指标 |
|---|---|---|
| Counter | 8（脚本 6 + 人工 2） | 见第 3 章 |
| Gauge / Histogram（自定义） | 0 | 由 SDK 自动插桩与 spanmetrics 桥接承载 |

### 1.7 上下文证据摘要

| 证据 | 数量 | 占比 | 对方案的影响 |
|---|---|---|---|
| in_loop（循环内） | 8 | 6.0% | 禁止频率告警（如 `checkout/kafka/producer.go:64`、`main.kt:69-93`） |
| in_except（异常分支） | 17 | 12.7% | error 类主力 |
| once（仅一次） | 0 | 0 | 无存在性告警特殊候选 |
| conditional（条件分支） | 68 | 50.7% | 大量为故障注入/特性开关分支，进白名单 |
| is_assertion（断言） | 2 | 1.5% | `checkout/main.go:293`（panic 风格 fatal）、`money.go:73`（panic）——不做日志检索关键字，做崩溃归因 |

---

## 2. 日志埋点明细（按语义分类）

> 说明：模板中 `{}`/`{0}`/`%v`/`%s` 为占位符，变量部分不可作固定关键字。C# 条目来自人工降级提取（LoggerMessage 定义处），覆盖率未知。

### 2.1 生命周期类 (lifecycle)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| checkout/main.go:273 | INFO | `starting to listen on tcp: %q` | 就绪事件（存在性判定候选） |
| checkout/main.go:287 | INFO | `Checkout gRPC server stopped` | 停机事件 |
| ad/.../AdService.java:128 | INFO | `Ad service started, listening on {}` | 就绪事件 |
| ad/.../AdService.java:358 | INFO | `Ad service starting.` | 启动事件 |
| ad/.../AdService.java:108 | INFO | `Prometheus metrics endpoint started, listening on {}` | 指标端点就绪 |
| product-catalog/main.go:190 | INFO | `Product Catalog gRPC server started on port: %s` | 就绪事件 |
| payment/index.js:66 | INFO | `payment gRPC server started on {}` | 就绪事件 |
| mcp/run.py:33 / mcp_server.py:39 | INFO | `Starting (Fast)MCP Server on {}:{}` | 启动事件 |
| shipping/main.rs:21 | INFO | `Successfully configured OTel` | SDK 就绪 |
| product-catalog/main.go:99 | INFO | `Database connection established` | 依赖就绪 |
| chatbot/opamp.py:162 / product-catalog/main.go:174 区段 | INFO | `Started OpAMP client` / `Failed to start OpAMP client: %v` | OpAMP 生命周期 |
| checkout/main.go:101,117 | ERROR | `new otlp (trace|metric) http exporter failed: %v` | **SDK 初始化失败 → P0 候选** |
| product-catalog/main.go:114 | ERROR | `Failed to initialize OpenTelemetry SDK: %v` | 同上 |
| checkout/main.go:293 | FATAL | `environment variable %q not set` | 启动即中止（存在性告警） |
| shipping/main.rs:25 | FATAL | `Couldn't start OTel: {0}` | 同上 |
| checkout/main.go:280 / product-catalog/main.go:215 | ERROR | `Failed to serve gRPC server(, err: %v)` | 端口监听失败 → P0 候选 |
| product-catalog/main.go:194 | ERROR | `TCP Listen: %v` | 同上 |

### 2.2 请求/事务类 (request)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| ad/.../AdService.java:210 | INFO | `Targeted ad request received for {}` | conditional（有 context keys） |
| ad/.../AdService.java:218 | INFO | `Non-targeted ad request received, preparing random response.` | 降级路径事件 |
| recommendation/.../RecommendationService.java:84 | INFO | `Receive ListRecommendations for product ids:{}` | 请求入口 |
| payment/index.js:20 | INFO | `Charge request received.` | 请求入口 |
| payment/charge.js:101 | INFO | `Transaction complete.` | 事务完成 |
| frontend/.../InstrumentationMiddleware.ts:23,35 | INFO/ERROR | `API request completed` / `API request failed` | 前端 API 网关层 |
| checkout/main.go:414,419 | INFO | `order confirmation email sent` / `sending to postProcessor` | 订单后处理事件 |

### 2.3 下游依赖类 (dependency)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| checkout/main.go:698 | ERROR | `Failed to write message: %v` | Kafka 生产失败 → P1 |
| checkout/main.go:713 | ERROR | `Failed to send message to Kafka within context deadline: %v` | Kafka 超时 → P1 |
| checkout/kafka/producer.go:64 | ERROR | `Failed to write message: %+v` | **in_loop=true**，禁频率告警 |
| checkout/main.go:691 | INFO | `Successful to write message. offset: %v, duration: %v` | duration 为统计字段 |
| checkout/main.go:705 | WARNING | `Context canceled before success message received: %v` | 白名单（主动取消） |
| checkout/main.go:479 | ERROR | `could not connect to %s service, err: %+v` | 下游 gRPC 不可达 → P1 |
| checkout/main.go:412 | WARNING | `failed to send order confirmation: %+v` | 邮件失败（降级） |
| product-catalog/main.go:132,138,145 | ERROR | `Error (initializing database|closing database connection|unregistering database metrics): %v` | DB 生命周期 |
| product-catalog/main.go:470,476 | ERROR | `failed to (begin lock contention transaction|acquire lock for lock contention scenario)` | 故障注入路径（白名单） |
| cart/src/Log.cs:14,29,32（人工） | ERROR/INFO | `Wasn't able to connect to redis` / `Connection failed. Disposing the object` / `Redis internal error` | Valkey 连接状态 → P1 |
| cart/src/Log.cs:26（人工） | INFO | `Connection to redis was restored successfully.` | 恢复事件（P3） |
| chatbot/opamp.py:145 | ERROR | `OpAMP client disabled: %s` | OpAMP 断连 |
| agent/src/agents/mcp_client.py:29 | ERROR | `Error closing connection : {}` | MCP 连接清理失败 |

### 2.4 资源指标类 (resource)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| ad/.../CPULoad.java:48 | INFO | `High CPU-Load problempattern enabled` | **故障注入开关**，conditional |
| ad/.../GarbageCollectionTrigger.java:45,58 | INFO | `Triggering a manual garbage collection...` / `The artificially triggered GCs took: ` | 故障注入（白名单） |
| ad/.../MemoryUtils.java:46,54 | DEBUG | `No maximum heap is set` / `Current heap usage is {0} percent` | 调试级，不进告警 |

### 2.5 业务逻辑异常 (business_error)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| frontend/pages/api/checkout.ts:46 | ERROR | `Checkout failed to place order` | **in_loop + in_except**，禁频率告警 |
| frontend/pages/api/checkout.ts:38 | INFO | `Checkout payment declined` | in_except；预期内业务拒绝（白名单，P3 趋势） |
| ad/.../AdService.java:255 | WARNING | `GetAds Failed with status {}` | in_except，gRPC 状态为变量（按字段统计） |
| chatbot/chat_interface/chat_interface.py:64 | ERROR | `Error : {}` | LLM 调用失败 |
| accounting/Log.cs:OrderParsingFailed（人工，Log.cs 内） | ERROR | `Order parsing failed:` | Kafka 消息解析失败 |
| accounting/Log.cs:ConsumeError（人工） | ERROR | `Consume error: {reason}` | Kafka 消费错误 |
| shipping/shipping_service.rs:79 | WARNING | （模板为空，上下文为报价异常分支） | 需人工确认 |
| frontend/gateways/Session.gateway.ts:29 | WARNING | `Failed to parse session from localStorage` | 浏览器端，可忽略 |

### 2.6 系统异常 (system_error)

| 文件:行 | 级别 | 模板 | 备注 |
|---|---|---|---|
| checkout/money/money.go:73 | FATAL（panic） | — | **is_assertion**，崩溃归因用 |
| checkout/main.go:170,177,184 | ERROR | `Error shutting down (tracer|meter|logger) provider: %v` | 停机清理失败，P3 |
| checkout/main.go:201,206 / product-catalog/main.go:155,160 | ERROR | `Error creating flagd provider` / `Failed to set flagd as the provider` | 特性开关依赖失败 → P1 |
| checkout/main.go:667 | ERROR | `Failed to marshal message to protobuf: %+v` | 序列化失败 |
| checkout/main.go:361 → product-catalog/main.go:361 | ERROR | `Environment Variable Not Set: %q` | 配置缺失 |
| product-catalog/main.go:108,173,179 | ERROR | `Failed to prepare OpAMP identity / start / stop OpAMP client` | OpAMP 生命周期 |
| react-native-app/hooks/useTracer.ts:128 | WARNING | `failed to setup tracer` | 移动端 SDK 初始化失败 |

### 2.7 调试日志（不纳入告警）

| 文件:行 | 模板 |
|---|---|
| ad/.../MemoryUtils.java:46,54 | 堆使用调试信息 |
| chatbot/opamp.py:132 | `Failed to close OpAMP WebSocket` |
| cart/src/Log.cs:11,20,23（人工） | Redis connecting/small_test 系列（LogLevel.Debug） |

### 2.8 特性开关/降级事件（白名单主体，conditional 证据）

| 文件:行 | 模板（节选） | 条件 |
|---|---|---|
| fraud-detection/main.kt:69,77,89 | `FeatureFlag 'kafkaConsumerDead' is (enabled|disabled)...` / `FeatureFlag 'kafkaQueueProblems' is enabled, sleeping ...` | in_loop + conditional，**故障注入** |
| checkout/main.go:719,726 | `Warning: FeatureFlag 'kafkaQueueProblems' is activated...` / `Done with #%d messages for overload simulation.` | 故障注入 |
| shipping/shipping_service.rs:87 | `Delaying international shipment due to intlShippingSlowdown feature` | 降速特性 |
| recommendation/.../RecommendationService.java:146,155 | `get_product_list: cache (miss|hit)` | 缓存状态事件（可统计比率） |
| payment/index.js:55 | `Overwriting Localhost IP: {}` | conditional |
| load-generator/locustfile.py:281,283,305,307 | 浏览器压测行为/错误 | browser_traffic_enabled 条件，**非生产路径** |

---

## 3. 指标清单

### 3.1 已定义的指标（源码显式定义）

| 指标名（源码构造名） | 类型 | 语义角色 | 标签 | 源码位置 | 有写入点 | 备注 |
|---|---|---|---|---|---|---|
| `demo_ad_served_total` | Counter | 业务：广告投放量 | — | `AdService.java:76`（写入 :267,:288） | 是 | Prometheus Java client，暴露于 /metrics |
| `demo.ad.requests` | Counter | RED.Traffic | — | `AdService.java:88` | 是 | OTel API |
| `demo.recommendation.requests` | Counter | RED.Traffic | — | `RecommendationService.java:72` | 是 | OTel API |
| `demo.payment.transactions` | Counter | 业务：支付笔数 | — | `payment/charge.js:15` | 是 | OTel API |
| `demo.chatbot.requests` | Counter | 业务：聊天请求数 | — | `chatbot/src/chat_interface/chat_interface.py:19`（写入 :41） | 是 | 缺 documentation |
| `demo.tool.calls` | Counter | 业务：LLM 工具调用数 | — | `shared/tools.py:20`（写入 :32） | 是 | 缺 documentation |
| `demo.exchange.conversions`（人工） | Counter | 业务：货币换算次数 | to_code | `currency/src/server.cpp:357`（写入 :244,:271） | 是 | C++ OTel SDK，人工降级提取 |
| `demo.notification.confirmations`（人工） | Counter | 业务：确认邮件数 | — | `email/email_server.rb:45`（写入 :52） | 是 | Ruby OTel SDK，人工降级提取 |

### 3.2 SDK 自动插桩与平台指标（非源码显式定义，运行时命名）

| 来源 | 承载内容 | 证据 |
|---|---|---|
| OTel 自动插桩（otelgrpc/otelhttp/AspNetCore/Runtime/Process/StackExchangeRedis/Sinatra 等） | RPC/HTTP 调用计数、时长直方图、运行时指标 | `checkout/main.go:63-64`；`cart/src/cart.csproj`（Instrumentation.* 包） |
| spanmetrics connector | 由 span 聚合的调用/错误/时长指标（RED 主力） | `otelcol-config.yml:254-255,262` |
| docker_stats / host_metrics / redis / postgresql / nginx / http_check | 容器与基础设施 USE 指标 | `otelcol-config.yml:41-146` |
| kafkametrics | Kafka broker/lag 指标 | `otelcol-config-full.yml:23` |

> spanmetrics 与自动插桩指标的实际暴露名由 SDK/connector 版本决定，需在 Prometheus 运行时确认后再写 PromQL（SKILL.md 强约束 13）。

### 3.3 指标质量风险

| 风险 | 指标数 | 具体指标 | 影响 |
|---|---|---|---|
| 缺 documentation | 2 | `demo.chatbot.requests`、`demo.tool.calls` | 语义靠猜，告警易设错 |
| 命名风格不统一 | 2 种 | `demo.ad.requests` vs `demo_ad_served_total` | 检索需两种写法都试；Prometheus 转义后 `demo.ad.requests`→`demo_ad_requests` |
| 高基数风险 | 1 | `demo.exchange.conversions` 标签 `to_code`（受控币种集合，风险低） | 可接受 |
| 无写入点 | 0 | — | — |

### 3.4 指标暴露方式

| 方式 | 位置 | 说明 |
|---|---|---|
| OTLP push（主体） | 所有服务 → `otel-collector:4317/4318`（`.env:32-39`） | 三平面统一通道；服务端注册见 `otelcol-config.yml:10-25` |
| Prometheus 拉取（仅 ad） | `ad:9465/metrics`，collector `prometheus/ad` receiver 抓取 | `AdService.java:109` + `otelcol-config.yml:138-147` + `.env:59` |
| Collector → Prometheus | `otlp_http/prometheus` exporter 写 `prometheus:9090/api/v1/otlp` | `otelcol-config-observability.yml:26-29` |
| 验证方法 | 打开 Grafana(3000)/Prometheus(9090)（`.env:175,188`）查询 `demo_` 前缀指标；或 `curl ad:${AD_PROMETHEUS_PORT}/metrics` | — |

---

## 4. 信号仲裁

需求清单按运行形态（多语言在线微服务 + 事件链）制定。

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---|---|---|---|---|
| 1 | 服务是否活着/启动成功 | 日志+指标 | 就绪日志（checkout:273、ad:128、pc:190、payment:66）；指标 `up`（Prometheus 对每个 OTLP 服务/抓取目标自动生成） | 第 2.1 节 | 已覆盖（存在性告警） |
| 2 | 请求错误率 | 指标 | spanmetrics connector 桥接的调用/错误计数；无自定义 error counter | `otelcol-config.yml:254-255` | 已覆盖（依赖 trace 全量上报与 observability 层加载） |
| 3 | 请求延迟分位 | 指标 | spanmetrics 时长直方图 + 各语言插桩直方图；无自定义 Histogram | 同上 | 已覆盖（桥接型，采样率影响精度，见 7.2） |
| 4 | 一次下单慢在哪一段 | 链路 | 全链路分布式 span（11 服务→Jaeger）+ `demo.*` 属性 | `traces.json` 19 span/40 属性 | 已覆盖 |
| 5 | Kafka 链路健康（生产失败/超时/积压） | 日志+指标 | 日志：`Failed to write message`（checkout:698, producer.go:64）、`within context deadline`（:713）；指标：kafkametrics receiver | `otelcol-config-full.yml:23` | 已覆盖 |
| 6 | 缓存有效性（推荐缓存命中率） | 日志+span 属性 | `cache hit/miss` 日志对 + `demo.recommendation.cache_hit` span 属性 | `RecommendationService.java:145,154` | 已覆盖（比率需日志聚合） |
| 7 | 业务量（订单/支付/广告/换算/邮件） | 指标 | 8 个自定义 Counter（第 3.1 章） | 各指标行 | 已覆盖 |
| 8 | 资源水位（CPU/内存/连接） | 指标 | docker_stats+host_metrics receiver；Go runtime/C# Runtime/Process 插桩 | `otelcol-config.yml:41-77` | 已覆盖（平台侧采集） |
| 9 | 存储层健康（PG 死锁、Valkey 连接） | 指标+日志 | postgresql receiver（含 deadlocks）；cart Redis 日志族 | `otelcol-config.yml:104-125`；`cart/src/Log.cs` | 已覆盖 |
| 10 | 敏感数据防泄漏 | 处理器 | redaction processor（掩码卡号/阻断 pin/password/secret） | `otelcol-config.yml:241-252` | 已覆盖（正向能力） |
| 11 | LLM 服务（agent/chatbot）健康 | 日志+指标 | `Error : {}`、`OpAMP client disabled`；`demo.chatbot.requests`、`demo.tool.calls` | `chat_interface.py:64`；`chat_interface.py:19` | 已覆盖（LLM token/延迟维度无自定义指标，见 4.1） |
| 12 | 前端真实用户延迟 | 链路 | 浏览器 span（fetch 插桩 + `demo.synthetic_request` 区分压测流量） | `FrontendTracer.ts:61-71` | 部分覆盖（有 span 无 RUM 指标聚合） |
| 13 | LLM 调用 token 用量/延迟 | 指标 | 日志平面无；指标平面无自定义定义；trace 平面存在 gen_ai_normalizer 处理器说明 gen_ai span 已被规范化 | `otelcol-config.yml:261`（gen_ai_normalizer） | **真缺口（低优先级）**：需在 agent/chatbot 增加 token/延迟直方图，或在 collector 侧从 gen_ai span 派生 |
| 14 | quote(PHP)/email(RB) 服务细粒度指标 | 指标 | email 有 `demo.notification.confirmations`；quote 无任何自定义指标 | `email_server.rb:45` | 部分缺口：quote 仅 SDK 自动插桩，无业务指标（低优先级） |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

**需求 13（LLM token/延迟）**：

| 平面 | 已检查内容 | 结果 |
|---|---|---|
| 日志 | 检索 134 条模板 + 人工清单，关键词 token/usage/prompt/completion/ttft/latency | 无相关埋点（agent 6 条、chatbot 7 条全部为生命周期/异常类） |
| 指标 | 检索 8 个自定义指标名 + SDK 自动指标 | 无 token/延迟类定义 |
| 链路 | 检索 19 span + 40 属性，`gen_ai` 前缀 | collector 有 gen_ai_normalizer 处理器（`otelcol-config.yml:261`），说明 gen_ai span 属性在管道中被规范化，但源码内无显式 token 指标 |

结论：token/延迟度量属真缺口；建议位置 `src/agent/src/agents/` 与 `src/chatbot/src/chat_interface/`，接 OTel Python meter 新增 Histogram，不改 collector。

**需求 14（quote 业务指标）**：日志平面（人工检索 quote 全部 6 个 .php 文件）无 logger 显式调用点，仅 `app/settings.php:22-25` 配置 stdout logger；指标平面无自定义定义；链路平面经 OTel PHP SDK 自动插桩（`quote/public/index.php:15-19`）。结论：除自动插桩外无业务信号，属低优先级缺口。

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|---|---|---|---|
| `new otlp trace http exporter failed` / `new otlp metric http exporter failed` | P0 | SDK 初始化失败 → 全平面数据丢失 | checkout/main.go:101,117 |
| `Failed to initialize OpenTelemetry SDK` | P0 | 同上 | product-catalog/main.go:114 |
| `Couldn't start OTel: ` | P0 | Rust 服务 OTel 启动失败 | shipping/main.rs:25 |
| `environment variable ` + `not set` | P0 | 配置缺失启动中止 | checkout/main.go:293（FATAL） |
| `Failed to serve gRPC server` / `TCP Listen: ` | P0 | 端口监听失败 | checkout/main.go:280；pc/main.go:215,194 |
| `could not connect to ` + `service` | P1 | 下游 gRPC 不可达 | checkout/main.go:479 |
| `Failed to write message` / `within context deadline` | P1 | Kafka 生产失败/超时 | checkout/main.go:698,713 |
| `Error creating flagd provider` / `Failed to set flagd as the provider` | P1 | 特性开关依赖失败 | checkout/main.go:201,206；pc/main.go:155,160 |
| `Checkout failed to place order` | P1 | 下单失败 | frontend/pages/api/checkout.ts:46 |
| `Wasn't able to connect to redis` / `Connection failed. Disposing the object` | P1 | Valkey 连接失败 | cart/src/Log.cs:14,29（人工） |
| `Order parsing failed` / `Consume error: ` | P1 | Kafka 消息解析/消费失败 | accounting/Log.cs（人工） |
| `Connection to redis was restored successfully.` | P3 | 恢复事件（确认自愈） | cart/src/Log.cs:26（人工） |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在模板 | 统计方式 | 维度 |
|---|---|---|---|
| duration（Kafka 写入耗时） | `Successful to write message. offset: %v, duration: %v`（checkout/main.go:691） | 数值提取→分位 | service=checkout |
| gRPC status | `GetAds Failed with status {}`（AdService.java:255） | 按值分组计数 | status |
| cache hit/miss | `get_product_list: cache (hit\|miss)`（RecommendationService.java:146,155） | 命中率计算 | — |
| orderId | `Consumed record with orderId: ${...}`（main.kt:93） | 幂等/去重参考 | orderId |
| sleepMs | `sleeping ${sleepMs}ms`（main.kt:89） | 故障注入时长（白名单内） | — |

> 以上均为占位符变量，禁止写成 `cost=123ms` 类固定关键字。

### 5.3 降噪白名单（预期内事件）

| 关键字/条件 | 原因 | 处理 |
|---|---|---|
| `FeatureFlag 'kafkaQueueProblems'` / `'kafkaConsumerDead'`（checkout:719,726；main.kt:69,77,89） | 故障注入特性，flagd 控制，默认关闭 | 白名单；开启期间静音对应告警 |
| `problempattern`（CPULoad:48、GarbageCollectionTrigger:45,58） | 人工故障注入 | 白名单 |
| `Checkout payment declined`（checkout.ts:38） | 预期内业务拒绝 | P3 趋势统计，不告警 |
| `Overwriting Localhost IP`（payment/index.js:55、shipping/main.rs:39） | 开发环境分支，conditional | 白名单 |
| `no baggage found in context`（AdService.java:201） | 正常降级路径 | 白名单 |
| `Context canceled before success message received`（checkout:705） | 主动取消 | 白名单 |
| `intlShippingSlowdown`（shipping_service.rs:87） | 特性降速 | 白名单 |
| `VCR: not recording response`（agent/patch_vcr.py:57,69） | 录制回放特性分支 | 白名单 |
| `failed to (begin lock contention|acquire lock)`（pc/main.go:470,476） | 锁竞争故障注入场景 | 白名单 |
| load-generator 全部日志（locustfile.py） | 压测器自身，非生产路径 | 整目录排除出监控 |
| `Instrumentation complete...`（locustfile.py:92） | 压测器启动 | 同上 |

### 5.4 检索语句示例

```logql
# checkout 错误日志占比（排除白名单）
sum(rate({service_name="opentelemetry-demo/checkout"} |~ "Failed to write message|could not connect to|Failed to marshal" [5m]))
/
sum(rate({service_name="opentelemetry-demo/checkout"} [5m]))
```

```promql
# RED 指标来自 spanmetrics 桥接（实际指标名需在 Prometheus 运行时确认，通常为 calls/duration/errors 族）
histogram_quantile(0.99, sum(rate(<spanmetrics_duration_bucket>[5m])) by (le, service_name))
# 自定义业务指标（源码名 demo.payment.transactions → Prometheus 转义暴露名 demo_payment_transactions_total）
rate(demo_payment_transactions_total[5m])
```

---

## 6. 告警规则草稿（候选阈值，待 SLO/压测/线上数据校准）

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 | 源码位置 |
|---|---|---|---|---|---|---|
| 服务下线 | `up{service_name=~"opentelemetry-demo/.*"} == 0` 持续 2m | 0（确定语义，无校准需求） | P0 | 按实例去重 | 计划内重启 | `.env:32-39`；`otelcol-config.yml:10-25` |
| OTLP 数据断流 | `sum(rate(calls[5m])) by (service_name) == 0` 且服务 up=1，持续 10m | 0（候选，待校准压测基线） | P1 | 排除 load-generator 自身 | 低流量演示时段 | `otelcol-config.yml:254-255` |
| 服务错误率超标 | spanmetrics 错误调用数 / 总调用数 > 5% 持续 5m（量纲 0~1） | 5%（候选） | P1 | 按服务分组；排除压测流量（`demo.synthetic_request` 维度） | 故障注入开启期间 | `otelcol-config.yml:254-255,262` |
| 请求 P99 劣化 | `histogram_quantile(0.99, rate(spanmetrics duration_bucket[5m])) by (service_name) > 2s` | 2 秒（候选，单位 s） | P1 | 持续 2 周期 | 冷启动窗口 | 同上 |
| 容器内存逼近上限 | `container.memory.rss / container.memory.hierarchical_memory_limit > 0.9`（量纲 0~1）持续 10m | 0.9（候选；compose 内存限额见各服务 limits，如 ad 300M） | P1 | 与 OOMKill 事件联动 | — | `otelcol-config.yml:46-56`；`compose.yaml:27-29` |
| 容器 CPU 持续打满 | `rate(container.cpu.usage.system[5m])` 换算利用率 > 90% 持续 15m | 90%（候选，量纲 0~100） | P2 | 排除 problempattern 开启期 | 故障注入 | `otelcol-config.yml:43-45` |
| frontend-proxy 探测失败 | `http_check` probe 失败持续 5m | 0 成功（候选） | P1 | — | — | `otelcol-config.yml:27-31` |
| ad /metrics 抓取失败 | Prometheus `up{job="ad"} == 0` 持续 5m | 0 | P2 | — | — | `otelcol-config.yml:138-147`；`AdService.java:109` |
| PostgreSQL 死锁 | `increase(postgresql.deadlocks[10m]) > 0` | 0（候选） | P2 | 与锁竞争故障注入联动 | 锁竞争场景开启期 | `otelcol-config.yml:116` |
| 支付量断流 | `rate(demo_payment_transactions_total[5m]) == 0` 且 locust 流量正常，持续 10m | 0（候选） | P2 | 与压测开启状态联动 | 压测停止期 | `payment/charge.js:15` |
| 换算次数异常跌落 | `rate(demo_exchange_conversions_total[5m])` 相对基线跌幅 > 80% 持续 15m | 80%（候选，相对基线） | P3 | 相对基线 | — | `currency/src/server.cpp:357` |

### 6.2 日志事件告警（L2）

| 告警名 | 条件 | 等级 | 降噪 | 白名单 |
|---|---|---|---|---|
| OTel SDK 初始化失败 | 出现 `new otlp trace http exporter failed` / `Failed to initialize OpenTelemetry SDK` / `Couldn't start OTel` | P0 | 只在重启事件后判定 | — |
| 启动中止 | 出现 FATAL `environment variable ... not set` 或 `Failed to serve gRPC server` | P0 | 按实例去重 | 计划内配置变更 |
| Kafka 生产失败 | 出现 `Failed to write message` / `within context deadline`（非 in_loop 调用点 :698/:713） | P1 | kafkaQueueProblems 开启期静音 | 故障注入 |
| 下游服务不可达 | 出现 `could not connect to %s service` | P1 | 按目标服务聚合 | — |
| Valkey 连接丢失 | 出现 `Wasn't able to connect to redis` / `Connection failed. Disposing the object`（cart Log.cs，人工提取） | P1 | 按实例去重；恢复日志关联 | — |
| 下单失败 | 出现 `Checkout failed to place order` | P1 | in_loop 日志只做存在性+聚合计数，不做单条频率 | payment declined 白名单 |
| flagd 依赖失败 | 出现 `Error creating flagd provider` | P1 | 按服务去重 | — |

### 6.3 日志频率告警（L3，谨慎）

| 告警名 | 条件 | 基线来源 | 等级 | 白名单 |
|---|---|---|---|---|
| GetAds 错误突增 | `GetAds Failed with status` 计数 > 基线×3 持续 3 周期 | 近 7 天同小时基线 | P2 | 依赖不可用已被 #2 覆盖时静音 |
| LLM 调用错误突增 | chatbot `Error : {}` 计数 > 基线×3 | 近 7 天基线 | P2 | — |

> 依据 SKILL 约束：`in_loop=true`（producer.go:64、main.kt 系列、checkout.ts:46）一律不做频率告警。

### 6.4 链路告警（L4，可选）

| 告警名 | span | 条件 | 等级 |
|---|---|---|---|
| checkout 主链路分段劣化 | `prepareOrderItemsAndShippingQuoteFromCart`（checkout/main.go:434） | P95 环比上升 > 50%（候选，待校准） | P2 |
| 支付扣款劣化 | `charge`（payment/charge.js:28） | P95 环比上升 > 50%（候选） | P2 |

---

## 7. 埋点缺口与优化建议

### 7.1 真缺口（三平面均无信号）

| 场景 | 风险 | 建议做法 | 优先级 |
|---|---|---|---|
| LLM token 用量/延迟度量（agent/chatbot） | LLM 成本与体验不可量化 | 在 `src/agent/src/agents/`、`src/chatbot/src/chat_interface/` 新增 OTel Histogram（token、ttft）；gen_ai span 属性已由 `otelcol-config.yml:261` 规范化，可优先从 span 属性派生 | P2 |
| quote(PHP) 业务指标 | 报价业务量不可见 | 在 `quote/src/` 仿照 email（`email_server.rb:45`）新增 Counter | P3 |
| 前端 RUM 聚合指标 | 只有浏览器 span，无用户侧延迟分位 | 用 collector spanmetrics 按 `session.id` 属性聚合（属性已存在于 `ad/.../AdService.java:193` 与前端 processor） | P3 |

### 7.2 已有但不可用（质量问题）

| 信号 | 问题 | 建议 |
|---|---|---|
| spanmetrics 桥接 RED 指标 | 依赖 trace 上报链路；若上游采样开启则错误率/分位失真 | 确认各服务采样率配置（OTEL_TRACES_SAMPLER）并在告警文档登记 |
| `demo.chatbot.requests`、`demo.tool.calls` | 缺 documentation | 补描述（`chat_interface.py:19`、`tools.py:20`） |
| 自定义指标命名两种风格 | `demo.ad.requests` 与 `demo_ad_served_total` 并存 | 统一为 OTel 命名；检索时两种写法都要试 |
| quote 的 stdout logger | `settings.php:22-25` 配置了 logger 但源码内无显式调用点，实际是否输出需运行时确认 | 人工确认 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|---|---|---|
| 循环内 ERROR（高频） | checkout/kafka/producer.go:64 | 已由 main.go:698 非循环版本承载告警；此条降为 DEBUG 或聚合 |
| 结账失败日志在循环+异常分支内 | frontend/pages/api/checkout.ts:46 | 移出循环，改为计数器 |
| 压测日志与业务日志同管道上报 | load-generator/locustfile.py 全部 | OTLP 上报加 `service.criticality=none` 或直接关闭其 exporter |
| 空模板日志 | checkout/kafka/producer.go:30,33,36、payment/index.js:26,62、shipping_service.rs:44,79 | 补全消息文本，否则告警无法检索 |
| 故障注入日志 INFO 级易与真实事件混淆 | checkout/main.go:719 | 前缀加 `[problempattern]`（现有前缀不统一） |

### 7.4 需人工确认（脚本未能确定）

| 项 | 原因 |
|---|---|
| C# cart/accounting 的全部运行时调用点覆盖率 | .cs 不在脚本能力矩阵，LoggerMessage 定义为人工全量提取（19 个），调用点抽样验证 |
| Ruby email / PHP quote 三平面完整性 | .rb/.php 未覆盖，人工检索为主，覆盖率未知 |
| C++ currency 指标与 span、Rust shipping span | 指标/链路提取脚本不支持 C++/Rust，人工提取 |
| spanmetrics/自动插桩指标在 Prometheus 中的最终暴露名 | 运行时行为，静态分析不可见 |
| 采样率、batch 处理器队列等运行时参数 | 需结合部署配置与压测验证 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
cd scripts/
python3 extract_loggers.py <新版本源码> --output loggers_v2.json
python3 extract_metrics.py <新版本源码> --output metrics_v2.json
python3 extract_traces.py  <新版本源码> --output traces_v2.json
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

人工降级清单（C#/PHP/Ruby）整理成与脚本输出同构的 JSON（含 name/type/file/line/kind）后同样可入 diff。

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---|---|---|
| 新增 ERROR 日志 | 新故障模式 | 评估新增 L2 告警 |
| 新增 `demo.*` 指标 | 新业务维度 | 评估新增 L1 告警与看板 |
| 新增 span 属性（`demo.*`） | 新的桥接维度 | 评估 spanmetrics dimensions 扩展 |
| compose 默认开关变更（如 observability 层默认不加载） | 三平面后端全部无数据，**最高优先级** | 重跑 1.2 开关表并重新验证数据流 |
| collector 配置分层变更 | 指标/日志/trace 导出路径变化 | 同上 |
| 删除指标/日志 | 僵尸规则 | 下线对应告警 |

### 8.3 diff 局限

- 按「文件名+模板」模糊匹配，忽略行号偏移；同文件同模板多条会被折叠，计数是近似值。
- C#/PHP/Ruby 人工清单的 diff 依赖人工维护质量。

---

## 9. 局限性

1. **阈值为候选值**：全部阈值需结合 SLO、压测基线与线上历史校准；compose 内的内存 limits（如 ad 300M）是部署配置，不是告警阈值。
2. **源码常量 ≠ 告警阈值**：`checkout/main.go` 中 Kafka deadline 等是业务超时配置。
3. **提取覆盖率不是 100%**：
   - C#（cart/accounting）、PHP（quote）、Ruby（email）不在脚本能力矩阵内，走人工降级，**覆盖率未知**（第 9.4 项清单）。
   - C++/Rust 的指标与链路平面脚本不支持，`demo.exchange.conversions`、currency span、shipping span 事件为人工提取。
   - 拼接构造的消息（如 `fmt.Sprint(v...)` checkout/kafka/producer.go:33）无法还原完整模板。
4. **静态分析看不到运行时**：
   - collector 配置分层（base 仅 debug 导出）意味着「有插桩」≠「有后端数据」，1.2 开关表必须与实际 compose 组合核对。
   - spanmetrics 指标最终暴露名、采样率、batch 队列行为需运行时确认。
   - span 属性脱敏（redaction processor）在管道内执行，源码里看到的敏感属性（`session.id`、`enduser.id`）在落库时已被处理，但不能假设覆盖所有字段。
5. **脚本输出为 0 不代表平面缺失**：本项目的指标平面大量依赖 SDK 自动插桩与 collector 接收器（非源码显式构造器），仅看自定义指标数会得出「指标贫乏」的错误结论。
6. **人工提取部分**：第 2.3/2.5 节 C# 条目、第 3.1 章最后两行、第 4 章 currency/shipping 相关行均为人工降级提取，未经脚本交叉验证。
