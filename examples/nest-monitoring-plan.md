# nest（NestJS 框架）源码可观测性分析方案

- 分析对象：nest（`@nestjs/core` v12.0.0 monorepo，见 `/tmp/sltm-validation/nest/package.json`）
- 运行形态：**库 / 框架**（npm workspaces + lerna monorepo，10 个发布包）
- 源码语言：TypeScript 2193 个 .ts 文件（99.6%）、JavaScript 8 个（1.4%，仓库根 scripts/ 工具）
- 扫描范围：生产代码根目录为 `packages/`（core、common、microservices、platform-*、websockets、testing 共 10 个包）
- 已排除目录与理由：
  - `packages/*/test/`（约 1200 个 .ts）——单元测试，目录名经确认均为 vitest spec（如 `packages/core/test/`），非生产路径
  - `integration/`——集成测试工程（`vitest.config.integration.mts` 专用）
  - `sample/`——40+ 个样例应用（如 `sample/01-cats-app`），是文档演示代码，不是框架本体
  - `tools/`、`scripts/`、`.github/`——构建脚本与 CI 配置
  - 排除后实际扫描 714 个 .ts 文件。注：脚本 `--exclude` 采用 fnmatch 对仓库相对路径匹配，`**/test/**` 形式的 glob 无法匹配以目录名开头的路径（如 `sample/...`），故采用 `test/*`、`sample/*` 等形式（目录级排除另由脚本内置 DEFAULT_EXCLUDE_DIRS 兜底）
- 提取方式：确定性脚本（`extract_loggers.py` / `extract_metrics.py` / `extract_traces.py`），JS/TS 走 lang_extract 正则后端

---

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面 | 内置 `Logger` 类（`@nestjs/common`），默认实现 `ConsoleLogger`，输出到 stdout/stderr；宿主可通过 `LoggerService` 接口整体替换（pino/winston 等）；支持实例方法与静态方法两种调用形态；支持日志缓冲（bootstrap 期间缓存、init 完成后 flush） | `packages/common/services/logger.service.ts:79`（Logger 类）、`:14-50`（LoggerService 接口）、`:291`（attachBuffer）、`:279`（flush）、`:307`（overrideLogger） |
| 指标 SDK 与命名约定 | **无**。生产代码无任何 prom-client / OTel meter / statsd / micrometer 引用 | 全文检索 `prom-client\|prometheus\|new Counter\|new Gauge\|new Histogram\|collectDefaultMetrics\|registerMetric` 在 `packages/` 非测试代码 0 命中；脚本提取指标定义 0 / 写入点 0 / 暴露点 0 |
| 链路 SDK | **无**。无 OTel span / startActiveSpan / 装饰器式埋点 | 全文检索 `opentelemetry\|startActiveSpan\|startSpan\|@WithSpan\|traceId\|spanContext` 在 `packages/` 非测试代码 0 命中；脚本提取 span 定义 0 |
| 暴露方式 | 无 HTTP 指标端点。日志走 `console.log`/`process.stdout.write`/`process.stderr.write`（ConsoleLogger）。框架另暴露 `GraphInspector`（依赖图序列化，供宿主/调试工具消费）与生命周期钩子（`onApplicationBootstrap`、`onApplicationShutdown` 等）供宿主挂接自定义埋点 | `packages/common/services/console-logger.service.ts:723-731`（printStackTrace 落 stderr）；`packages/core/inspector/graph-inspector.ts:10`；`packages/core/nest-factory.ts:99`（createGraphInspector 注入） |
| 默认开关 | ① `NEST_LOG_LEVEL` 环境变量控制级别（未设 = 全级别输出）；② `logger: false` 可完全关闭内置日志；③ `ConsoleLogger({ json: true })` JSON 结构化输出，**默认关闭**；④ 日志缓冲默认在 bootstrap 期间开启、init 成功后自动 flush | `packages/common/services/utils/get-env-log-levels.util.ts:8`（NEST_LOG_LEVEL）；`packages/common/services/logger.service.ts:324-336`（isLevelEnabled，无级别设置时全放行）；`packages/common/services/console-logger.service.ts:59`（json 选项）；`packages/core/nest-application.ts:216-220`（init 后 flush 注释） |

**结论（决定后续需求清单）**：nest 是框架类项目，自身不运行、不对外暴露服务端点。它的可观测性契约是「暴露给宿主什么信号」：可替换的日志门面 + 异常兜底日志 + 生命周期钩子 + 依赖图检查器。**指标与链路两平面为真零**（检索证据见 4.1），延迟/错误率类监控责任在设计上由宿主应用承担。

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|------|------|----------|-------------|
| 日志埋点 | 171 条 | microservices 传输层占 53.8%（92 条，client 44 + server 38 + 其他 10），core 框架生命周期占 28.1%（48 条） | 中：事件类信号完整（启动/就绪/断连/异常兜底），但 77.8% 模板为变量消息、无指标平面配套 |
| 指标定义 | 0 | 无任何 SDK 与自定义注册 | 无（框架形态下属设计取舍，非缺陷；缺口见第 7 章） |
| 链路 span | 0 | 无任何 span 埋点 | 无（同上） |

### 1.2 日志按级别分布

| 级别 | 数量 | 占比 | 是否纳入告警 |
|------|------|------|-------------|
| ERROR | 103 | 60.2% | 是（P0/P1，需白名单，见 5.3） |
| WARNING | 29 | 17.0% | 部分（配置类警告进白名单） |
| INFO | 34 | 19.9% | 否（就绪事件类除外） |
| DEBUG | 5 | 2.9% | 否 |

注：断言类（`is_assertion`）为 0，级别分布无断言污染。

### 1.3 日志按包分布

| 包 | 埋点 | ERROR | WARN | INFO | DEBUG | 主要职责 |
|----|--------|-------|------|------|-------|----------|
| microservices | 92 | 60 | 10 | 17 | 5 | 传输层（RMQ/Redis/NATS/MQTT/Kafka/gRPC/TCP）连接与消息收发 |
| core | 48 | 20 | 14 | 14 | 0 | 应用生命周期、DI 注入、路由注册、异常兜底 |
| common | 9 | 6 | 1 | 2 | 0 | 日志门面实现本身、文件流 |
| platform-ws | 7 | 6 | 1 | 0 | 0 | WebSocket adapter（含序列化失败兜底） |
| platform-node | 5 | 4 | 1 | 0 | 0 | 原生 HTTP adapter |
| platform-express | 3 | 2 | 1 | 0 | 0 | Express adapter |
| websockets | 3 | 2 | 0 | 1 | 0 | WS 网关注册与订阅事件 |
| platform-fastify | 2 | 1 | 1 | 0 | 0 | Fastify adapter |
| platform-socket.io | 2 | 2 | 0 | 0 | 0 | Socket.io adapter |

microservices 包内部：client 44 条、server 38 条、nest-microservice.ts 8 条、exceptions 2 条。

### 1.4 链路 span 按阶段分布

无 span 定义（见 4.1 检查记录）。框架的分段信息不以 span 形式存在。

### 1.5 上下文证据摘要

| 证据 | 数量 | 占比 | 对方案的影响 |
|------|------|------|-------------|
| in_loop（循环内） | 23 | 13.5% | 禁止做频率告警（如 client-nats.ts:189/207/223 的 NatsStatus 事件流日志、client-mqtt.ts:108 回调循环错误） |
| in_except（异常分支） | 36 | 21.1% | error 类主力：传输回调失败、序列化失败（ws-adapter.ts:255） |
| once（仅一次） | 0 | 0 | 无存在性告警候选来自该字段 |
| conditional（条件分支） | 69 | 40.4% | 高比例：多为 `this.isInitialized`（注册时序守卫）、`this.routingMap.size > 0`（断连清理）、重试次数判断（`times > getOptionsProp(...)`）——默认运行时是否触发需按配置确认 |
| is_assertion（断言） | 0 | 0 | 无 |
| 占位符风格 | template=37 / brace=1 / printf=0 / **none=133** | 77.8% 模板为变量 | 大量 `this.logger.error(err)` 形态，消息在运行时由 Error 对象决定，不能按固定模板全文匹配；可匹配的是集中在 `constants.ts` 的字面量常量 |

---

## 2. 日志埋点明细（按语义分类）

> 库类项目画像下，一级标签沿用 taxonomy 七类，另按「框架暴露面」将配置/用法告警归入 `business_error`（子标签 `misconfig_hint`，扩展标签，理由：这类 WARNING 的语义是「宿主用法不正确」，属业务侧错误而非框架故障）。

### 2.1 生命周期类 (lifecycle)

| 文件:行 | 级别 | 模板/常量 | 占位符 | 触发条件 | 说明 |
|---------|------|------|--------|------|------|
| packages/core/nest-factory.ts:245 | INFO | `Starting Nest application...`（MESSAGES.APPLICATION_START，core/constants.ts:4） | 无 | createApp 时 | 启动开始标记 |
| packages/core/nest-application.ts:215 | INFO | `Nest application successfully started`（MESSAGES.APPLICATION_READY，core/constants.ts:5） | 无 | init 完成且 isInitialized=true | **HTTP 应用就绪信号，全仓库最重要的存在性事件** |
| packages/microservices/nest-microservice.ts:323 | INFO | `Nest microservice successfully started`（MESSAGES.MICROSERVICE_READY，core/constants.ts:6） | 无 | microservice listen 完成 | 微服务就绪信号 |
| packages/core/nest-application-context.ts:424-428 | ERROR | `Error happened during shutdown`（MESSAGES.ERROR_DURING_SHUTDOWN，core/constants.ts:8） | 无 | 信号处理/关闭流程抛异常（in_except） | 优雅关闭失败，随后 `process.exit(1)`（:429） |
| packages/core/hooks/before-app-shutdown.hook.ts:68,80 | ERROR | `Logger.error(result.reason)` / `Logger.error(err)` | 变量 | 宿主 `beforeApplicationShutdown` 钩子抛错（:80 在 in_except） | 关闭钩子失败 |
| packages/core/hooks/on-app-shutdown.hook.ts:66,79 | ERROR | 同上 | 变量 | `onApplicationShutdown` 钩子抛错 | 同上 |
| packages/core/hooks/on-module-destroy.hook.ts:62 | ERROR | `Logger.error(result.reason)` | 变量 | `onModuleDestroy` 钩子抛错 | 同上 |
| packages/core/nest-application-context.ts:537-540 | WARNING | `Application is running in the PREVIEW mode!` 等 4 行 | 无 | preview 模式启动 | 配置态标记，非故障 |

### 2.2 请求/事务类 (request)

| 文件:行 | 级别 | 模板 | 占位符 | 说明 |
|---------|------|------|--------|------|
| packages/core/router/router-explorer.ts:306 | INFO | `ROUTE_MAPPED_MESSAGE(path, requestMethod)` | path/method（变量） | 路由映射，high-volume 候选（随路由数线性） |
| packages/core/router/routes-resolver.ts:122,131 | INFO | `logMessage`（Route mapped） | 变量 | 同上；:122 受 conditional（相关路由不存在才打） |
| packages/websockets/web-sockets-controller.ts:689 | INFO | `{} subscribed to the "{}" message` | 客户端/消息名 | WS 订阅事件，客户端可控的高基数变量 |

框架不记录请求级 received/complete 日志——请求平面完全留给宿主。

### 2.3 下游依赖类 (dependency)——microservices 传输层，本仓库最大信号簇

| 文件:行 | 级别 | 模板/常量 | 说明 |
|---------|------|------|------|
| packages/microservices/client/client-rmq.ts:411 | INFO | `Successfully connected to RMQ broker` | RMQ 连接成功 |
| packages/microservices/server/server-rmq.ts:190 | ERROR | `CONNECTION_FAILED_MESSAGE` = `Connection to transport failed. Trying to reconnect...`（microservices/constants.ts:65-66） | 连接失败，自动重连 |
| packages/microservices/client/client-rmq.ts:403, server/server-rmq.ts:237 | ERROR | `DISCONNECTED_RMQ_MESSAGE` = `Disconnected from RMQ. Trying to reconnect.`（constants.ts:64） | 断连 |
| packages/microservices/client/client-rmq.ts:426, server/server-rmq.ts:245 | WARNING | `RMQ broker has blocked the connection (flow control). Reason: ${reason}`（constants.ts:67-68） | broker 侧流控，积压领先指标 |
| packages/microservices/client/client-rmq.ts:434, server/server-rmq.ts:252 | INFO | `RMQ broker has unblocked the connection.`（constants.ts:69） | 流控解除 |
| packages/microservices/server/server-rmq.ts:349 | ERROR | `RMQ broker has cancelled the consumer. Trying to consume again.`（constants.ts:70-71） | 消费者被取消 |
| packages/microservices/client/client-redis.ts:207, server/server-redis.ts:301 | INFO | `Connected to Redis. Subscribing to channels...` | Redis 就绪 |
| packages/microservices/client/client-redis.ts:188, server/server-redis.ts:290 | INFO | `Reconnecting to Redis...`（conditional：未达重连上限） | 重连中 |
| packages/microservices/client/client-redis.ts:228, server/server-redis.ts:324 | ERROR | `Disconnected from Redis.`（server 侧为 `...No further reconnection attempts will be made.`） | 最终断连 |
| packages/microservices/client/client-redis.ts:291,344 | ERROR | `Redis connection closed and retry attempts not specified` | 未配置重试即永久断开 |
| packages/microservices/client/client-redis.ts:297, server/server-redis.ts:350 | ERROR | `Retry time exhausted` | **重试耗尽——传输层最终不可用信号** |
| packages/microservices/client/client-mqtt.ts:274 | INFO | `Connected to MQTT broker` | MQTT 就绪 |
| packages/microservices/client/client-mqtt.ts:240, server/server-mqtt.ts:356 | INFO | `MQTT connection lost. Trying to reconnect...` | 断连重连 |
| packages/microservices/client/client-mqtt.ts:231 | ERROR | `MQTT broker went offline.` | broker 下线 |
| packages/microservices/client/client-nats.ts:162,174; server/server-nats.ts:289,295 | ERROR | `NatsError: type: "{}", error: "{}".` | NATS 协议错误（type 为变量，可分组统计） |
| packages/microservices/server/server-nats.ts:359 | ERROR | `The connection to the NATS server is closed and will not be reopened (see the "maxReconnectAttempts" option)` | NATS 重连耗尽 |
| packages/microservices/client/client-nats.ts:189,207,223; server/server-nats.ts:317,327,343 | INFO/DEBUG | `NatsStatus: type: "{}"` 等状态流 | **in_loop=true**，随集群事件高频，禁止频率告警 |

另有各 transport 收发路径上的错误对象直传（模板为 none）：`this.logger.error(err)` 形态约 40 处（如 client-grpc.ts:312、server-grpc.ts:713、client-kafka.ts:152、server-kafka.ts:530 等），归类 dependency/system_error，按 5.2 字段统计而非关键字匹配。

### 2.4 资源指标类 (resource)

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| packages/platform-ws/adapters/ws-adapter.ts:326 | WARNING | `Closing a client with {} bytes queued for it, more than "maxSendBufferSize" ({})` | WS 发送背压超限，客户端被关闭 |
| packages/core/router/router-response-controller.ts:286 | WARNING | `The client is not reading its events: {} character(s) of event data are queued for it, which exceeds...` | SSE/流式响应消费端背压 |
| packages/core/injector/opaque-key-factory/deep-hashed-module-opaque-key-factory.ts:63 | WARNING | `The module "{}" is taking {}ms to serialize, this may be caused by larger objects statically assigned...` | 序列化耗时（框架自带的耗时类信号，单位 ms） |

### 2.5 业务逻辑异常 (business_error)——含扩展子标签 misconfig_hint（配置/用法告警）

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| packages/core/router/route-conflict-detector.ts:281 | WARNING | `logger.warn(message)`（冲突描述为变量） | 路由冲突检测 |
| packages/core/middleware/middleware-module.ts:119 | WARNING | `warningMessage`（中间件未生效原因，变量） | 中间件注册无效 |
| packages/microservices/nest-microservice.ts:167,182,207,232,252,275 | WARNING | `Cannot apply WebSocket adapter/global exception filters/global pipes/global interceptors/global guards/global preRequest hooks: registration must occur before initialization.` | **初始化后注册全局组件不生效**（conditional=`this.isInitialized`，6 条同型） |
| packages/core/nest-application.ts:394 | WARNING | `Your HTTP Adapter does not support '.useBodyParser'.` | adapter 能力缺失 |
| packages/core/nest-application.ts:538 | WARNING | `useWebSocketAdapter() was called after WebSocket gateways were already initialized...` | 同型时序问题 |
| packages/core/router/router-exception-filters.ts:92 | WARNING | `Global exception filters (APP_FILTER) with request or transient scope are not applied to exceptions...` | 全局过滤器作用域陷阱 |
| packages/core/router/legacy-route-converter.ts:77,86 | ERROR/WARNING | `*` 通配路由迁移提示（变量） | 兼容性警告 |
| packages/core/router/router-explorer.ts:405 | WARNING | `{}. Requests for other hosts are answered with 404 instead of...` | 多主机路由回退 404 |
| packages/core/repl/native-functions/debug-repl-fn.ts:23 | ERROR | `"{}" has not been found in the modules registry` | REPL 查询对象不存在（预期内的用户输入错误，白名单候选） |
| packages/common/pipes/file/file-type.validator.ts:204 | WARNING | `Failed to load the "file-type" package for magic number validation.` | 可选依赖缺失，降级路径 |
| packages/core/helpers/safe-instance-decorator.ts:21 | WARNING | `The "instanceDecorator" function threw an error while decorating an instance ({}). The undecorated instance...` | 装饰器失败已降级 |
| packages/platform-*/adapters/*:231/823/295 | WARNING | `Content-Type doesn't match Reply body, you might need a custom ExceptionFilter for non-JSON response` | 3 个 adapter 同型 |

### 2.6 系统异常 (system_error)

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| packages/core/errors/exception-handler.ts:7 | ERROR | `ExceptionHandler.logger.error(exception)` | 未处理异常最终兜底 |
| packages/core/exceptions/base-exception-filter.ts:77, external-exception-filter（同目录） | ERROR | `BaseExceptionFilter.logger.error(exception)`（conditional：非 IntrinsicException） | HTTP 异常过滤器兜底 |
| packages/microservices/exceptions/base-rpc-exception-filter.ts:44, rpc-exceptions-handler.ts:36 | ERROR | 同型 | RPC 异常兜底 |
| packages/websockets/exceptions/base-ws-exception-filter.ts:119 | ERROR | 同型 | WS 异常兜底 |
| packages/platform-ws/adapters/ws-adapter.ts:255（另见 :135,:231） | ERROR | `this.logger.error(err)`（in_except：JSON.stringify 抛错） | **响应序列化失败**，注释明确：抛出会经 RxJS 异步重抛杀死进程（:247-250） |
| packages/microservices/server/server-grpc.ts:910 | ERROR | `invalidPackageError`（变量） | gRPC 包数据非法 |
| packages/microservices/server/server-rmq.ts:424 | ERROR | `Dropped a reply whose "replyTo" or "correlationId" is longer than {} bytes.` | 消息丢弃（协议违规） |
| packages/core/nest-application-context.ts:515 | ERROR | `this.logger.error(error)`（close/init 流程） | 容器级错误 |

### 2.7 调试日志（DEBUG，不纳入告警）

| 文件:行 | 模板 |
|---------|------|
| packages/microservices/client/client-mqtt.ts:311, client-redis.ts:312,334,337 | 报文非 JSON 格式绕过/无匹配回调提示 |
| packages/microservices/client/client-nats.ts:200 | `NatsStatus: pending pings` |
| packages/core/injector/instance-wrapper.ts:704,717 | `{} introspected as durable`（conditional=isString(name)，受 isDebugMode() 守卫） |
| packages/core/injector/injector.ts:1733,1749 | `Looking for {}` / `Found {}`（DI 解析过程） |

INFO 中另有两处仅调试态输出：instance-wrapper.ts:704,717（conditional 非空）。

---

## 3. 指标清单

### 3.1 已定义的指标

**无。** 脚本提取：定义 0、写入点 0、暴露点 0（扫描 714 个生产 .ts 文件）。

这不是提取遗漏（检索证据见 4.1）：nest 生产代码不依赖 prom-client / OTel / 任何指标 SDK，`packages/core/package.json` 的 dependencies 仅有 `fast-safe-stringify`、`iterare`、`path-to-regexp`、`tslib`、`uid` 五项，均非可观测组件。框架的度量责任按设计转移给宿主应用（通过 interceptor/middleware/OTel 集成实现）。

### 3.2 指标质量风险

不适用（无指标）。

### 3.3 指标暴露方式

| 方式 | 位置 | 说明 |
|------|------|------|
| 无 | — | 框架不提供任何指标端点。宿主进程的运行时指标（heap/GC/event loop）由外部采集（Node.js exporter / OTel runtime instrumentation）负责，与框架源码无关 |

---

## 4. 信号仲裁

需求清单按「库/框架」运行形态构造：框架自身关注的不是 QPS/P99，而是「宿主能否感知框架发生了什么」。

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---------|---------|---------|---------|------|
| 1 | HTTP 应用是否启动成功 | 日志（事件） | `Nest application successfully started` | packages/core/nest-application.ts:215；core/constants.ts:5 | 已覆盖（存在性告警，P0） |
| 2 | 微服务是否启动成功 | 日志（事件） | `Nest microservice successfully started` | packages/microservices/nest-microservice.ts:323；core/constants.ts:6 | 已覆盖（存在性告警） |
| 3 | 优雅关闭是否失败 | 日志（事件） | `Error happened during shutdown` + 3 个 shutdown hook 的 ERROR | packages/core/nest-application-context.ts:424-428；hooks/on-app-shutdown.hook.ts:66 等 | 已覆盖（P1；进程可能以 exit(1) 终止） |
| 4 | 传输层（RMQ/Redis/NATS/MQTT）断连与恢复 | 日志（事件） | `Connection to transport failed...` / `Disconnected from RMQ...` / `Retry time exhausted` / `Connected to Redis...` 等 | microservices/constants.ts:64-71；client-redis.ts:297；server-rmq.ts:190 等 | 已覆盖（事件完整）；**缺重连次数/断连时长的度量**（见 #9） |
| 5 | 未捕获/兜底异常 | 日志（事件） | ExceptionHandler、BaseExceptionFilter、RPC/WS 过滤器兜底 ERROR | core/errors/exception-handler.ts:7；core/exceptions/base-exception-filter.ts:77 等 | 已覆盖（P1，需白名单：被业务过滤器正常处理的异常也会走到部分兜底路径） |
| 6 | 响应序列化失败（进程崩溃风险） | 日志（事件） | ws-adapter JSON.stringify catch | packages/platform-ws/adapters/ws-adapter.ts:255（注释 ：247-250 明确崩溃路径） | 已覆盖（P1；platform-* 各 adapter 同型） |
| 7 | 依赖注入失败 / 循环依赖 | 日志（事件）+ 图检查器 | `Nest encountered an undefined dependency...`（injector.ts:630）+ GraphInspector.registerPartial（graph 状态置 partial 并记录 cause） | packages/core/injector/injector.ts:630；packages/core/inspector/graph-inspector.ts:39-50 | 已覆盖（启动期失败随启动日志断档判定） |
| 8 | 路由冲突 / 全局组件注册不生效 | 日志（事件） | route-conflict-detector WARNING、6 条 `Cannot apply ... registration must occur before initialization.` | core/router/route-conflict-detector.ts:281；microservices/nest-microservice.ts:167-275 | 已覆盖（P3 配置审计；均为宿主用法问题） |
| 9 | 传输层重连次数 / 断连时长 / 消息吞吐 | 指标 | 无 | —（检索记录见 4.1） | **真缺口**：事件日志有、度量没有。需新增埋点（在 client/server 的 reconnect 回调处计数）或由宿主/平台侧补；框架形态下优先「宿主侧定义」 |
| 10 | 请求延迟 / 请求错误率 | 指标 | 无 | —（检索记录见 4.1） | **真缺口（设计使然）**：建议宿主集成 OTel interceptor / APM agent；不建议在框架内强加 |
| 11 | 进程资源水位（heap/GC/event loop） | 指标 | 无（框架与运行时指标均无） | — | 真缺口（框架形态合理）：由 Node.js runtime exporter / OTel runtime 指标在宿主环境补齐 |
| 12 | 一次请求慢在哪一段 | 链路 | 无 span；宿主可通过 interceptor 自行分段 | — | 真缺口（设计使然）：宿主集成 OTel SDK |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|------|-----------|------|
| 日志 | 检索全部 171 条埋点的 template/code_snippet；关键词 `latency\|duration\|elapsed\|ms\|time\|cost\|count\|counter\|bytes\|queue\|size` | 仅 3 条与耗时/容量相关且为事件告警（deep-hashed-module-opaque-key-factory.ts:63 序列化 ms、ws-adapter.ts:326 排队字节、router-response-controller.ts:286 排队字符）；无任何计数/分位类打点 |
| 指标 | 脚本提取定义 0/写入点 0/暴露点 0；全文检索 `prom-client\|prometheus\|new Counter\|new Gauge\|new Histogram\|collectDefaultMetrics\|registerMetric\|createObservable` 于 packages/ 非测试代码 | 0 命中（唯一命中在 packages/microservices/test/ 的测试变量 `writeCounter`，已排除）→ 指标平面为真零，非提取遗漏 |
| 链路 | 脚本提取 span 0；全文检索 `opentelemetry\|startActiveSpan\|startSpan\|@WithSpan\|traceId\|spanContext\|Tracer` 于 packages/ 非测试代码 | 0 命中 → 链路平面为真零 |

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配；均来自源码字面量常量）

| 关键字 | 级别 | 说明 | 来源 |
|--------|------|------|------|
| `Nest application successfully started` | P0（存在性） | HTTP 应用就绪判定：启动后 N 秒内未出现即告警 | core/constants.ts:5，触发于 nest-application.ts:215 |
| `Nest microservice successfully started` | P0（存在性） | 微服务就绪判定 | core/constants.ts:6，触发于 nest-microservice.ts:323 |
| `Error happened during shutdown` | P1 | 优雅关闭失败，进程将以 exit(1) 终止 | core/constants.ts:8，触发于 nest-application-context.ts:424-429 |
| `Connection to transport failed. Trying to reconnect...` | P1 | 传输层连接失败 | microservices/constants.ts:65-66，触发于 server-rmq.ts:190 |
| `Retry time exhausted` | P1 | Redis 重试耗尽，最终断连 | client-redis.ts:297、server-redis.ts:350 |
| `No further reconnection attempts will be made` | P1 | Redis/NATS 最终断连 | server-redis.ts:324；server-nats.ts:359（NATS 版含 `maxReconnectAttempts` 字样） |
| `Disconnected from RMQ. Trying to reconnect.` | P2 | RMQ 断连（有自动重连，频繁出现才升级） | microservices/constants.ts:64 |
| `RMQ broker has blocked the connection (flow control)` | P2 | broker 流控，积压领先指标 | microservices/constants.ts:67-68 |
| `RMQ broker has cancelled the consumer` | P2 | 消费者被取消 | microservices/constants.ts:70-71 |
| `MQTT broker went offline.` | P1 | MQTT broker 下线 | client-mqtt.ts:231 |
| `An unsupported message was received. It has been negative acknowledged` | P2 | 收到无 handler 消息被 nack（可能丢消息） | microservices/constants.ts:49-53 |
| `There is no matching message handler defined in the remote service.` | P2 | 远端无 handler | microservices/constants.ts:63 |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在模板/形态 | 统计方式 | 维度 |
|------|---------|---------|------|
| 异常对象（约 40 处 `logger.error(err)`） | 模板为 none，内容为 Error 消息 | 按异常类型/文件分组计数，不做关键字匹配 | file、transport |
| NatsError type | `NatsError: type: "{}", error: "{}".` | 占位符提取 → 按 type 分组计数 | type |
| 序列化耗时 | `The module "{}" is taking {}ms to serialize` | 数值提取 → P95（单位 ms） | module |
| 排队字节数 | `Closing a client with {} bytes queued... more than "maxSendBufferSize" ({})` | 数值提取 → 与上限比值 | 无 |
| 路由映射 | `ROUTE_MAPPED_MESSAGE(path, requestMethod)` | 按事件计数（审计用，非告警） | path, method |
| WS 订阅 | `{} subscribed to the "{}" message` | 计数；client/message 为高基数，禁止进告警分组维度 | client, message |

### 5.3 降噪白名单（必须显式列出）

| 关键字/条件 | 原因 | 处理 |
|------------|------|------|
| `... Trying to reconnect...`（RMQ/Redis/MQTT 重连中） | 自动重连路径，单次出现是预期内抖动 | 单次忽略；升级为「持续 N 分钟未出现 Connected 确认」或出现 `Retry time exhausted` 时告警 |
| `Application is running in the PREVIEW mode!` | 配置态标记（preview 模式本来就不实例化 provider） | 忽略；如需监控配置漂移，走 P3 审计 |
| `Cannot apply ... registration must occur before initialization.`（6 条同型） | 宿主开发期用法错误，属配置审计而非运行故障 | P3 日报汇总 |
| `... has not been found in the modules registry`（REPL） | REPL 交互中用户查询不存在的对象，预期内 | 忽略 |
| `Failed to load the "file-type" package` | 可选依赖缺失的降级路径，功能仍可用 | P3 统计降级次数 |
| `Content-Type doesn't match Reply body` | 宿主返回非 JSON 时的提示，服务仍正常 | 忽略 |
| `Your HTTP Adapter does not support '.useBodyParser'` | adapter 能力差异提示 | 忽略 |
| `NatsStatus: ...` / `Looking for...` / `Found...`（in_loop / debug 守卫） | 高频状态流与 DI 过程日志 | 仅趋势观察，禁止频率告警 |
| conditional=`this.isInitialized` 系列警告 | 仅在宿主错误使用 API 时触发，默认运行不出现 | 触发即代表宿主代码 bug，适合 P3 审计而非 P1 告警 |

### 5.4 检索语句示例（字段值替换为实际部署的采集配置）

```logql
# 就绪判定（存在性）：启动 120s 内未出现
{job="my-nest-app"} |~ "Nest (application|microservice) successfully started"

# 传输层最终不可用（白名单先行排除重连中事件）
{job="my-nest-app"} |~ "Retry time exhausted|No further reconnection attempts"

# 优雅关闭失败
{job="my-nest-app"} |= "Error happened during shutdown"

# 传输连接失败频率（趋势观察，非直接告警）
sum(rate({job="my-nest-app"} |= "Connection to transport failed" [10m]))
```

```promql
# 进程存活（宿主侧采集，非框架指标）
up{job="my-nest-app"} == 0
# 传输层错误计数趋势（由 5.2 字段经 logcli 转换为 metric 后）
sum(rate(nest_transport_error_total[5m])) by (transport)
```

---

## 6. 告警规则草稿（候选阈值，全部待校准）

> 库/框架形态：**本框架不自带指标平面，L1 缺失是形态使然**。因此以 L2（日志事件）为主力——这一取舍的代价是阈值不可跨环境复用、噪声高于指标告警；凡能由宿主侧补指标的需求（#9/#10/#11）均应优先在宿主环境补齐后再迁移到 L1。L1 层仅给出宿主侧通用建议。

### 6.1 指标告警（L1）——宿主环境采集，非框架内置信号

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|--------|------|---------|------|------|--------|
| 宿主进程不可达 | `up{job="my-nest-app"} == 0` 持续 1m | 0（确定值，来自采集器语义） | P0 | 按实例去重；区分滚动发布窗口 | 计划内重启 |
| 传输层错误速率（由日志字段转换的 metrics） | `sum(rate(nest_transport_error_total[5m])) by (transport) > 基线*5` | 相对基线 5 倍（待用历史数据确定基线） | P2 | 相对基线 + 持续 3 周期 | 重连成功后的瞬时错误 |

### 6.2 日志事件告警（L2，本方案主力）

| 告警名 | 条件 | 等级 | 降噪 | 白名单 |
|--------|------|------|------|--------|
| 应用启动失败/就绪超时 | 启动后 N 秒内未出现 `Nest application successfully started` 或 `Nest microservice successfully started`；N 取启动 P95 的 3 倍，候选 120s（待按本项目启动耗时校准） | P0 | 只在重启事件后判定；按实例去重 | 计划内发布 |
| 优雅关闭失败 | 出现 `Error happened during shutdown` | P1 | 按实例去重；与进程退出事件关联 | 无（触发即真实故障，进程随后 exit(1)） |
| 传输层最终不可用 | 出现 `Retry time exhausted` / `No further reconnection attempts will be made` / `MQTT broker went offline.` | P1 | 按 transport 维度去重；重连成功日志（`Connected to Redis...` 等）到达后自动恢复 | 无 |
| RMQ 消费者被取消 | 出现 `RMQ broker has cancelled the consumer` | P2 | 5m 内同 transport 折叠 | 消费者主动下线窗口 |
| RMQ broker 流控 | 出现 `RMQ broker has blocked the connection (flow control)` | P2 | 持续 >10m 未出现 UNBLOCKED 升级 P1 | 短时流控（<1m，broker 正常背压） |
| 响应序列化失败 | `platform-*` adapter 序列化 catch 路径 ERROR（ws-adapter.ts:255 同型） | P1 | 按 adapter 去重；关注是否伴随进程重启 | 无（注释明确该路径失败可致死进程） |
| 收到无 handler 消息被 nack | 出现 `An unsupported message was received... negative acknowledged` | P2 | 按 pattern 维度分组；同 pattern 频率超基线才升级 | 发布过渡期新旧 pattern 并存 |

### 6.3 日志频率告警（L3，谨慎；仅用于无指标对应的传输层错误）

| 告警名 | 条件 | 基线来源 | 等级 |
|--------|------|---------|------|
| NatsError 突增 | `NatsError: type:` 计数 > 基线 * 5 持续 3 周期（相对基线，非绝对阈值，待历史数据校准） | 过去 7 天同窗口均值 | P2 |
| RMQ/Redis 断连频繁 | `Disconnected from RMQ` / `Reconnecting to Redis` > 基线 * 3 | 同上 | P2 |

注：`NatsStatus` 类 in_loop 日志禁止进入本层。

### 6.4 链路告警（L4）

不适用：框架无 span（4.1 检查记录）。宿主集成 OTel 后按宿主方案另行定义。

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，检查记录见 4.1）

| 场景 | 风险 | 建议做法 | 优先级 |
|------|------|---------|--------|
| 请求级延迟/错误率（HTTP 与 RPC） | 宿主对核心 SLI 无内建来源 | 宿主侧集成 OTel / APM（框架已暴露 interceptor 机制，无需改框架源码）；若确需框架级信号，可在 `RoutesResolver`（core/router/routes-resolver.ts）挂全局 interceptor 计数 | P1（宿主侧） |
| 传输层重连计数 / 断连时长度量 | 只有事件日志，无法量化可用性 | 需新增埋点：在各 client/server 的 reconnect 回调与 Connected 事件处累计计数（如 client-redis.ts:188、client-rmq.ts:411） | P2 |
| 进程资源水位（heap/GC/event loop lag） | 框架与源码均无，Node 运行时问题不可见 | 宿主环境接 Node.js runtime exporter / OTel runtime instrumentation，不修改源码 | P1（宿主侧） |
| 分段耗时 | 无 span | 宿主 OTel SDK 自行分段 | P2 |

### 7.2 已有但不可用（信号存在、采集受限）

| 信号 | 问题 | 建议 |
|------|------|------|
| 约 40 处传输层 `logger.error(err)`（模板 none，占 77.8%） | 消息由 Error 对象决定，无法按模板全文匹配/聚合 | 启用 `ConsoleLogger({ json: true })`（console-logger.service.ts:59）或替换结构化 LoggerService（logger.service.ts:14-50），按 err.name/message 字段统计 |
| `NatsError: type: "{}"`、`{} bytes queued` 等含变量模板 | 变量部分不可当固定关键字 | 按 5.2 字段提取，不做全文匹配 |
| 全部日志走 stdout/stderr（console-logger.service.ts:723-731） | 无直接结构化输出，默认彩色非 JSON | 采集 agent 按 JSON 模式解析；彩色码需在采集侧剥离（json 模式自动禁色） |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|------|------|------|
| `NatsStatus` 状态流在事件循环内逐条打印（in_loop） | client-nats.ts:189,207,223；server-nats.ts:317,327,343 | 高流量集群下量大，建议降级 DEBUG 或采样 |
| `Cannot apply ... before initialization` 6 条同型警告分开硬编码 | nest-microservice.ts:167,182,207,232,252,275 | 可合并常量；对监控无阻塞（P3 审计） |
| DI 过程日志 `Looking for/Found`（INFO）默认级别即输出 | injector.ts:1733,1749 | 建议降为 DEBUG，减少默认噪声 |

### 7.4 需人工确认（脚本未能确定）

| 项 | 原因 |
|----|------|
| `GraphInspector` 依赖图的实际消费方式 | inspector/graph-inspector.ts 仅做序列化；是否随 WebSocket 输出、是否默认开启，属运行时行为，静态分析不可见 |
| 日志缓冲（attachBuffer）在异常启动路径下的最终 flush 行为 | nest-application.ts:216-220 注释说明失败时也会打印，具体时机需运行时验证 |
| `conditional` 证据中的重试上限默认值（如 Redis `retryAttempts`） | 默认值由 options 传入（client-redis.ts `getOptionsProp`），需结合宿主配置确认 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本三平面提取（命令同 Step 1，输出 *_v2.json；排除目录与本方案第 1 章一致）
python extract_loggers.py <新版nest源码> --output loggers_v2.json --exclude 'sample/*' --exclude 'integration/*' --exclude 'tools/*' --exclude 'scripts/*'
python extract_metrics.py <新版nest源码> --output metrics_v2.json --exclude 'sample/*' ...
python extract_traces.py  <新版nest源码> --output traces_v2.json --exclude 'sample/*' ...

# 逐平面 diff
python diff_signals.py loggers.json loggers_v2.json --output diff_logs.json
python diff_signals.py metrics.json metrics_v2.json --output diff_metrics.json
python diff_signals.py traces.json traces_v2.json --output diff_traces.json
```

### 8.2 变化处理矩阵（本项目特化关注点）

| 变化类型 | 对监控的影响 | 动作 |
|---------|-------------|------|
| 新增 ERROR 日志 | 新故障模式 | 评估新增 L2 告警；核对 constants.ts 是否新增字面量常量（关键字库要同步） |
| 新增指标定义 | **重大信号**：框架历史上从未有指标平面 | 逐个确认类型/标签/写入点/暴露端点，并按强约束 11 验证注册代码与默认开关 |
| 常量文本修改（如 `MESSAGES.APPLICATION_READY` 变更） | 关键字告警静默失效 | diff 到 constants.ts 附近变化时必须人工核对全部告警关键字 |
| 删除日志 | 对应告警变僵尸规则 | 下线对应 L2/L3 规则 |
| 开关默认值变更（NEST_LOG_LEVEL、json、preview） | 数据流整体变化 | 最高优先级：重查第 1.0 节开关表 |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配、忽略行号偏移；同文件同模板多条会折叠，计数为近似值
- 本项目 77.8% 模板为 none（变量消息），diff 对该部分不敏感，需配合 `constants.ts` 的常量变更人工审阅

---

## 9. 局限性

1. **阈值为候选值**：第 5、6 章全部阈值（就绪超时 120s、基线倍数 3/5 倍、流控 10m 等）均需结合宿主 SLO、启动耗时基线与线上历史校准。
2. **源码常量 ≠ 告警阈值**：microservices/constants.ts:73 `NATS_DEFAULT_GRACE_PERIOD = 10000`、Redis 重试配置等是业务/重连配置，不是告警阈值。
3. **提取覆盖率不是 100%**：
   - 日志：`logger.error(err)` 形态（模板 none，133 条）的消息内容静态不可得；封装调用（如经 `this.logger` 间接转发）仅提取到调用点
   - 路径字面量：无指标/链路暴露点，未涉及该风险
4. **静态分析看不到运行时**：
   - `NEST_LOG_LEVEL` 未设置时全级别放行（get-env-log-levels.util.ts:18-28）意味着 **ERROR/WARN 默认可达**，但宿主 `logger: false` 会整体关闭日志——采集前必须确认宿主日志配置
   - `conditional` 证据（69 条，40.4%）的实际触发依赖宿主配置与运行状态（如重试上限、isInitialized 时序）
   - GraphInspector 的数据流与默认开关需运行时确认（7.4）
5. **未覆盖的场景**：Node.js 运行时指标（heap/GC/event loop）、宿主 HTTP 层 RED 指标、基础设施层（broker/Redis 服务端）指标——均需宿主环境或平台侧采集组件，不属于框架源码可推导的范围。
6. **脚本能力边界**：JS/TS 后端基于正则 + 括号平衡解析，多行签名/重载会导致 `function` 字段为空、`conditional` 边界为近似（如 client-mqtt.ts:108 记录的 `routingMap.size > 0` 是外层 if 而非 catch 条件本身）；这些方向可信、边界不精确的记录已在分类时按保守语义处理。脚本输出 0 的两平面（指标/链路）均已经全文检索二次确认为真零，非提取遗漏。
