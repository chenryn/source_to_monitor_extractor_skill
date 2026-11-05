# train-ticket 监控方案（源码 → 三平面）

- 分析对象：train-ticket（FudanSELab 开源微服务列车售票测试床），Maven 多模块 `ts-service-cluster`（根 `pom.xml` version 0.1.0）
- 运行形态：**在线服务 / API 微服务集群**（约 40 个独立部署的 Spring Boot 2.3.12 服务 + Node.js 服务 ×1 + Python 服务 ×2 + Go 服务 ×1 + React 前端 ×1）
- 源码语言：Java 635 文件（主体）、JS 73（`ts-ui-dashboard` 前端静态资源 + `ts-ticket-office-service` Node/Express 后端）、Python 6（`ts-avatar-service` Flask、`ts-voucher-service` Tornado、部署辅助脚本）、Go 2（`ts-news-service`，`hoisie/web` 框架）
- 扫描文件数：612 个源码文件（排除 `node_modules/`、`target/`、`.git/`；脚本默认排除目录未发现异常生产代码分布）
- 提取方式：三套脚本自动提取（Java/JS 后端）+ 指标/链路平面零结果时的人工全文检索确认；未覆盖范围见第 9 章

---

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面 | Java：SLF4J + `LoggerFactory`（Spring Boot 默认 logback，仓库内无自定义 logback.xml）；模板风格统一为 `[操作][动作][细节]` + `{}` 占位符（活跃 Java 模板 766/836 命中首括号风格）。JS：`console.*` 打印为主（前端调试输出 + Node 服务无结构化日志框架） | ts-order-service/src/main/java/order/controller/OrderController.java:28；ts-cancel-service/src/main/java/cancel/service/CancelServiceImpl.java:309 |
| 指标 SDK 与命名约定 | **源码级零指标定义**。根 pom 引入 `spring-boot-starter-actuator`（所有服务继承，传递引入 Micrometer，JVM/系统指标运行时自动注册），但无任何 `management.endpoints.*` 配置；全仓库 Java 源码检索 `MeterRegistry` / `Counter.builder` / `Timer.builder` / `@Timed` / `io.micrometer` / `prometheus` 均为 0 命中 | pom.xml:91；`application.yml` 无 management 配置（逐服务核对） |
| 链路 SDK | 源码级链路埋点仅 1 处：`@TraceCrossThread`（跨线程上下文传递标注）。链路能力主体来自 **SkyWalking Java Agent 8.8.0 运行时字节码注入**（静态源码中不可见）；依赖侧有 `apm-toolkit-logback-1.x`（根 pom）与 `apm-toolkit-trace`（仅 ts-travel-service）。另有归档遗留库 `old-docs/Lib/ms-monitoring-core`（自研 Sleuth 风格链路库，未被任何 pom 引用，非活跃代码） | ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java:219；pom.xml:147；ts-travel-service/pom.xml:44 |
| 暴露方式 | ① 日志：容器 stdout → Fluentd → Elasticsearch → Kibana（EFK 栈）；② 指标：actuator 端点（Spring Boot 2.3 默认 web 仅暴露 `/actuator/health`）；③ 链路：SkyWalking Agent gRPC → OAP `skywalking:11800` | deployment/kubernetes-manifests/efk-deployment/fluentd-es-ds.yaml:80；deployment/kubernetes-manifests/quickstart-k8s/yamls/sw_deploy.yaml.sample:38-49 |
| 默认开关 | 见 1.6 默认开关表 | — |

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|------|------|----------|-------------|
| 日志埋点 | 984 条活跃（Java 836 + JS 148；另有 67 条位于归档目录 `old-docs/`，不计入活跃范围） | 集中在订单/旅行/取消/保持等核心业务服务，见 1.3 | 中（业务事件丰富，但无结构化字段） |
| 指标定义 | **0**（源码级） | Micrometer 由 actuator 间接提供 JVM/系统指标，需开启暴露 | 低 |
| 链路 span | **0**（源码级 span 创建点） | SkyWalking Agent 运行时注入，静态分析不可见 | 低（依赖部署开关） |

### 1.2 日志按级别分布（活跃范围 984 条）

| 级别 | 数量 | 占比 | 是否纳入告警 |
|------|------|------|-------------|
| INFO | 676 | 68.7% | 否（生命周期事件类除外） |
| ERROR | 190 | 19.3% | 是（需白名单，业务拒绝类占多数） |
| WARNING | 111 | 11.3% | 部分（需白名单） |
| DEBUG | 7 | 0.7% | 否 |
| 断言类（is_assertion） | 0 | 0 | — |

### 1.3 日志按服务分布（Top 12）

| 服务目录 | 埋点数 | ERROR/WARN | 主要职责 |
|----|--------|-----------|----------|
| ts-ui-dashboard（前端 JS） | 124 | 15 | 浏览器端 console 打印（含 vue/echarts/angular 等第三方压缩库命中 18 条，应排除） |
| ts-order-other-service | 59 | 17 | 订单（非高铁） |
| ts-order-service | 58 | 19 | 订单（高铁） |
| ts-travel-service | 50 | 17 | 旅行查询/余票 |
| ts-travel2-service | 40 | 16 | 旅行查询（备用路径） |
| ts-cancel-service | 37 | 10 | 退票/退款 |
| ts-preserve-other-service / ts-preserve-service | 37+37 | — | 购票 |
| ts-food-service | 29 | 11 | 食物订单（含 RabbitMQ 发送） |
| ts-basic-service | 28 | — | 基础查询聚合 |
| ts-assurance-service | 26 | 10 | 保险 |
| ts-food-delivery-service | 25 | — | 送餐（循环内 ERROR） |
| ts-ticket-office-service（Node.js） | 24 | 0 | 售票处（console 打印） |

### 1.4 指标按类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| 源码显式定义（Counter/Gauge/Histogram） | 0 | 全仓库无 Micrometer/Prometheus client 调用点 |
| 运行时自动注册（Micrometer via actuator） | 未静态可数 | JVM 内存/GC/线程、Tomcat 连接池等，仅 JMX bean 形式默认可用；web 端点未暴露 |

### 1.5 链路 span 按阶段分布

| 阶段 | 数量 | 说明 |
|------|------|------|
| 源码级 span | 0 | 无 OTel/brave/opentracing 源码调用点 |
| Agent 注入 span（运行时） | 未静态可数 | SkyWalking Agent 拦截 RestTemplate/Feign/Servlet 等插件点，span 存在与否取决于部署是否挂载 agent |

### 1.6 上下文证据摘要（活跃范围）

| 证据 | 数量 | 占比 | 对方案的影响 |
|------|------|------|-------------|
| in_loop（循环内） | 28 | 2.8% | 禁止频率告警，只能趋势/采样 |
| in_except（异常分支） | 34 | 3.5% | error 类优先 |
| conditional（条件分支） | 285 | 29.0% | 本项目典型形态为 `if (response.getStatus() != 1)` 业务响应校验（`!op.isPresent(` ×27、`result.getStatus(` ×17 等），多为业务拒绝路径而非特性开关 |
| once（仅一次） | 0 | 0 | 无存在性告警候选 |
| is_assertion | 0 | 0 | 无断言污染 |

### 1.7 默认开关与能力启用表

| 能力 | 开关/载体 | 默认值 | 启用方式与验证 |
|---|---|---|---|
| 日志输出 | Spring Boot logback 默认（console） | 开启 | 无需操作；验证：`docker logs <svc>` 可见 `[op][action]` 格式日志 |
| 日志集中采集 | EFK（k8s manifests） | 随 k8s-with-efk 部署启用 | 验证：Kibana 中按服务名检索 |
| JVM/系统指标 | actuator（pom.xml:91） | web 仅暴露 `/actuator/health`（Spring Boot 2.3 默认，无 management 配置） | **需在每服务 application.yml 加 `management.endpoints.web.exposure.include: health,metrics,prometheus` 并引入 `micrometer-registry-prometheus`，或改用 JMX**；验证：`curl :<port>/actuator/metrics` |
| 业务指标 | 无源码埋点 | 不存在 | 需新增埋点（见第 7 章） |
| 链路 | SkyWalking Agent（sw_deploy.yaml.sample:16-49） | 默认 compose 快速启动不挂 agent；仅 k8s skywalking 样例挂载 | initContainer 拷贝 agent + `-javaagent:/skywalking/agent/skywalking-agent.jar`（行 49）+ `SW_AGENT_COLLECTOR_BACKEND_SERVICES=skywalking:11800`（行 38-39）；验证：SkyWalking UI 出现服务拓扑 |
| 链路日志关联 | apm-toolkit-logback-1.x（pom.xml:147）+ `SW_GRPC_LOG_SERVER_HOST/PORT`（sw_deploy.yaml.sample:40-43） | 依赖 agent 挂载 | 验证：SkyWalking 日志页可见 trace 关联日志 |
| 备选链路栈 | 预构建 `-with-jaeger` 镜像 | compose 变体提供 | deployment/docker-compose-manifests/docker-compose-with-jaeger.yml:36 起；jaeger collector 未在该 compose 内定义（zipkin 段被注释，行 21-27），需外部提供 |

---

## 2. 日志埋点明细（按语义分类）

Java 836 条按 704 个唯一模板聚合分类；分类基于脚本证据字段（in_except / in_loop / conditional / level / placeholder_style），非目录名猜测。模板风格说明：`{}` 占位符内容为变量，不可当固定关键字全文匹配（见第 5 章）。

### 2.1 生命周期类 (lifecycle) — 9 模板 / 11 条

| 文件:行 | 级别 | 模板 | 触发条件 |
|---------|------|------|--------|
| ts-consign-price-service/.../init/InitData.java:25 | INFO | `[InitData.run][Consign price service][Init data operation]` | 服务启动初始化（存在性判定的就绪信号候选） |
| ts-order-service/.../OrderServiceImpl.java（initOrder 系列） | INFO/ERROR | `[initOrder][Init Order Success/Fail][OrderId: {}]` | 启动期数据初始化 |
| ts-auth-service/.../TokenServiceImpl.java（createDefaultAuthUser） | ERROR | `[createDefaultAuthUser][Create default auth user][UserOperationException][message: {}]` | 启动建默认账号失败 |

### 2.2 请求/事务类 (request) — 455 模板 / 543 条

本项目主体埋点：每个 Controller/Service 方法入口与成功出口各打一条 INFO，形如 `[getTickets][Calculate distance][calculate time：{} time: {}]`（ts-travel-service）、`[getRouteByRouteId][Get Route By Id][Route ID：{}]`。代表位置：ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java、ts-order-service/src/main/java/order/service/OrderServiceImpl.java（44 模板）。用途：请求流量存在性观测与业务链路审计；**不适合做频率告警**（量随流量线性增长）。

### 2.3 下游依赖类 (dependency) — 12 模板 / 12 条（全 ERROR，跨服务调用失败）

| 文件:行 | 级别 | 模板 |
|---------|------|------|
| ts-admin-order-service/.../AdminOrderServiceImpl.java:61 | ERROR | `[getAllOrders][receive response][Get Orders From ts-order-service fail!]` |
| ts-admin-order-service（同文件邻近） | ERROR | `[getAllOrders][receive response][Get Orders From ts-order-other-service fail!]` |
| ts-admin-travel-service、ts-admin-basic-info-service 等同模式 | ERROR | `[getAllRoutes][receive response][Get routes error][response code: {}]`、`[getAllTravels][receive response][Get Travel From ts-travel-service fail!]` 等 |
| ts-food-service/.../FoodServiceImpl.java:106 | ERROR | `[createFoodOrders(InBatch)][AddFoodOrder][send delivery info to mq error][exception: {}]`（RabbitMQ 发送失败，in_loop=true） |
| ts-preserve-other-service/.../PreserveOtherServiceImpl.java:293 | ERROR | `[sendEmail][Send email to mq error] exception is: {}` |

统一命名习惯：**`[...][receive response][... fail!]`** 与 **`[...][... to mq error]`**，是日志平面仅有的跨服务/MQ 依赖失败信号。

### 2.4 资源指标类 (resource) — 0 条

三平面检查后确认：日志平面无连接池/队列深度/内存类埋点（检索范围见 4.1）。

### 2.5 业务逻辑异常 (business_error) — 195 模板 / 238 条

绝大多数 ERROR/WARNING 属于此类：业务规则拒绝（订单不存在、已存在、余额/座位不足、验证码错误等）。代表：

| 文件:行 | 级别 | 模板 | 预期内？ |
|---------|------|------|--------|
| ts-order-other-service/.../OrderOtherServiceImpl.java:364 | ERROR | `[payOrder][Pay order Fail][Order not found][OrderId: {}]` | 是（用户侧输入错误） |
| ts-cancel-service/.../CancelServiceImpl.java:90 | ERROR | `[cancelOrder][Draw Back Money Failed][loginId: {}, orderId: {}]` | **否，涉及资金，需告警** |
| ts-preserve-other-service/.../PreserveOtherServiceImpl.java:95 | WARNING | `[preserve][Step 3][Check seat][Check seat is Not enough][TripId: {}]` | 是（正常售罄） |
| ts-auth-service/.../TokenServiceImpl.java:86 | WARNING | `[getToken][Incorrect username or password][username: {}, password: {}]` | 是（且**含明文 password 占位符，敏感**） |

### 2.6 系统异常 (system_error) — 22 模板 / 24 条

| 文件:行 | 级别 | 模板 |
|---------|------|------|
| ts-common/.../security/jwt/JWTUtil.java:102 | ERROR | `[validateToken][getClaims][Token expired][ExpiredJwtException: {}]`（预期内，客户端常态） |
| ts-common/.../security/jwt/JWTUtil.java:105-114 | ERROR | Token format error / not properly constructed / Signature failure / Illegal parameter（**签名失败与格式错误为攻击或严重配置错误信号**） |
| ts-food-delivery-service/.../FoodDeliveryServiceImpl.java:65 | ERROR | `{}:{} have no such food: {}`（in_loop=true，禁止频率告警） |
| ts-travel-service/.../TravelServiceImpl.java:284 | ERROR | `[queryInParallel][Query error] {}`（in_loop=true，并发查询异常） |

### 2.7 调试日志（DEBUG，5 模板 / 6 条，不纳入告警）

ts-security-service `[check][Get Order Num Info]` 等；ts-travel2-service `[call][Start to query][tripId: {}, routeId: {}]`。默认不采集，由人工决定。

### 2.8 JS 平面（148 条，需区别对待）

| 范围 | 数量 | 说明 |
|------|------|------|
| ts-ui-dashboard 前端（浏览器） | 124 | `console.log` 调试打印；第三方压缩库（vue/echarts/angular/oss）误命中 18 条应排除。不构成服务端可观测信号 |
| ts-ticket-office-service（Node 后端） | 24 | `console.log`（ts-ticket-office-service/bin/db.js:25 `Table created` 等）；无日志级别、无结构化字段，仅可按文本检索 |

---

## 3. 指标清单

### 3.1 已定义的指标

**源码级显式指标定义：0 个。**

| 检索动作 | 范围 | 结果 |
|---|---|---|
| `extract_metrics.py` 脚本（Java/Go/JS 后端） | 612 个源码文件 | 定义 0 / 写入点 0 / 暴露点 0 |
| 全文检索 `MeterRegistry\|Counter\.builder\|Timer\.builder\|@Timed\|io\.micrometer\|prometheus`（glob **/*.java） | 全仓库 Java | 0 命中 |
| 全文检索 `prometheus\|metrics\|otlp\|opentelemetry`（ts-ticket-office-service、ts-news-service、ts-voucher-service、ts-avatar-service） | 非 Java 服务 | 0 命中 |

**结论：业务指标平面为真空白**（三平面检查记录见 4.1）。可用指标仅有两类间接来源：① actuator/Micrometer 运行时自动注册的 JVM/Tomcat 指标（需开启暴露，见 1.7）；② SkyWalking Agent 提供的端到端指标（需挂 agent）。

### 3.2 指标质量风险

| 风险 | 影响 |
|------|------|
| 无任何业务指标（无请求计数/错误计数/耗时直方图） | RED 三要素全部缺失；错误率、P99 只能由外部平台（SkyWalking/actuator）或新增埋点承担 |
| actuator web 端点默认未暴露 | 不做配置变更，指标采集配置全是空转 |

### 3.3 指标暴露方式

| 方式 | 位置 | 说明 |
|------|------|------|
| HTTP 拉取（潜在） | `/<port>/actuator/*`，各服务端口见各自 application.yml（如 ts-order-service 12031、ts-food-service 18856） | Spring Boot 2.3 默认仅 health；开启 metrics/prometheus 需改配置（候选方案，需逐服务验证） |
| Agent 上报 | SkyWalking OAP `skywalking:11800`（gRPC） | sw_deploy.yaml.sample:38-39；指标含服务级 QPS/响应时间/端点 P99 |

---

## 4. 信号仲裁

运行形态为在线微服务集群，需求清单按「率 / 分位 / 水位 / 事件 / 分段耗时」构造；关键词从本项目实际命名习惯推导（`[op][action]` 首括号风格、`receive response`、`mq error`、`Fail`/`error` 后缀），未套用外部词表。

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---------|---------|---------|---------|------|
| 1 | 服务是否活着/启动成功 | 日志 | `[InitData.run][Consign price service][Init data operation]` 等初始化日志 | ts-consign-price-service/.../InitData.java:25 | 已覆盖（存在性告警）；各服务均有启动初始化日志可供存在性判定 |
| 2 | 请求错误率 | 指标 | 无（源码 0 定义，检索 0 命中） | — | **真缺口**：由 SkyWalking Agent 端点级错误率承接，或新增 Micrometer 埋点 |
| 3 | 请求延迟分位（P95/P99） | 指标 | 无源码定义；日志 `calculate time：{} time: {}`（ts-travel-service）为业务计算耗时打印，非请求级分位 | — | **真缺口**：首选 SkyWalking Agent 端点 P99；不建议日志做分位 |
| 4 | 跨服务调用失败 | 日志 | 12 个 `[...][receive response][... fail!]` ERROR 模板 | ts-admin-order-service/.../AdminOrderServiceImpl.java:61 | 已覆盖（L2 事件告警，全项目最集中的依赖失败信号） |
| 5 | MQ 发送失败 | 日志 | `[createFoodOrders(InBatch)][AddFoodOrder][send delivery info to mq error]`、`[sendEmail][Send email to mq error]` | ts-food-service/.../FoodServiceImpl.java:106；ts-preserve-other-service/.../PreserveOtherServiceImpl.java:293 | 已覆盖（L2；food 侧 in_loop=true 禁频率告警） |
| 6 | 资金操作失败 | 日志 | `[cancelOrder][Draw Back Money Failed][loginId: {}, orderId: {}]` | ts-cancel-service/.../CancelServiceImpl.java:90 | 已覆盖（L2/P1，涉及资金不建议进白名单） |
| 7 | 认证系统异常 | 日志 | `[validateToken][getClaims][...]` 5 个模板 | ts-common/.../security/jwt/JWTUtil.java:102-114 | 部分覆盖：Token expired 进白名单；Signature failure/format error 做事件告警 |
| 8 | 资源水位（JVM/连接池/线程池） | 指标 | actuator/Micrometer 运行时指标（潜在） | pom.xml:91 | **已有但不可用**：需先开启 `/actuator/metrics` 暴露（1.7） |
| 9 | 一次请求慢在哪一段 | 链路 | 源码 0 span；SkyWalking Agent 运行时注入 | ts-travel-service/.../TravelServiceImpl.java:219（仅 @TraceCrossThread） | **部署依赖型**：挂 agent 后由 SkyWalking 拓扑/瀑布图承接；不挂 agent 则三平面均无分段耗时信号 |
| 10 | 服务拓扑 / 依赖健康 | 链路 | Agent 注入（部署开关后） | sw_deploy.yaml.sample:16-49 | 同上，先解决启用与验证 |
| 11 | Node/Python/Go 服务的可观测 | 日志 | console.print / 无日志框架（ts-news-service main.go 全部逻辑被注释） | ts-ticket-office-service/bin/db.js:25 | 部分覆盖：仅文本日志存在性；指标/链路为真缺口（第 9 章标注降级路径） |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|------|-----------|------|
| 日志 | 活跃埋点 984 条（Java 836 + JS 148）；检索关键词：`receive response`、`mq`、`fail`、`error`、`latency/duration/time/cost`、`queue/pool/connection/memory`、`init/start` | 有依赖失败与生命周期事件；无延迟/资源水位类埋点 |
| 指标 | 脚本提取 612 文件（0 定义）+ 全文检索 `MeterRegistry/Counter.builder/Timer.builder/@Timed/io.micrometer/prometheus`（Java）与 `prometheus/metrics/otlp`（Node/Go/Python 服务） | 源码级业务指标 0；唯一潜在来源为 actuator 运行时指标 |
| 链路 | 脚本提取 0 span；全文检索 `@Trace|TraceContext|Tracer\.|startSpan|opentracing|brave\.span`（Java）——唯一命中为 old-docs 归档库 ms-monitoring-core（未被任何 pom 引用）与 ts-travel-service 的 `@TraceCrossThread` 1 处 | 源码级 span 真空白；链路能力取决于 SkyWalking Agent 部署 |

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|--------|------|------|------|
| `[receive response]` AND `fail!` | P1 | 跨服务调用失败（12 模板族） | ts-admin-order-service/.../AdminOrderServiceImpl.java:61 |
| `send delivery info to mq error` | P1 | RabbitMQ 发送失败 | ts-food-service/.../FoodServiceImpl.java:106 |
| `Send email to mq error` | P2 | 通知 MQ 发送失败 | ts-preserve-other-service/.../PreserveOtherServiceImpl.java:293 |
| `Draw Back Money Failed` | P1 | 退款失败（资金） | ts-cancel-service/.../CancelServiceImpl.java:90 |
| `Signature failure` / `Token format error` / `not properly constructed` | P1 | JWT 签名/格式异常 | ts-common/.../security/jwt/JWTUtil.java:105-114 |
| `Init data operation` / `Init Order Success` | P0（存在性） | 启动初始化完成信号 | ts-consign-price-service/.../InitData.java:25 |
| `Query error`（`[queryInParallel]` 前缀限定） | P2 | 并发查询异常（in_loop，仅趋势） | ts-travel-service/.../TravelServiceImpl.java:284 |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在模板 | 统计方式 | 维度 |
|------|---------|---------|------|
| `{}`（orderId / tripId / RouteId 等） | 全部 `[op][action][detail: {}]` 模板 | 按值分组计数（topk 定位热点对象） | orderId、tripId 等高基数字段不可做告警 `by` 维度 |
| `calculate time：{}` | `[getTickets][Calculate distance][calculate time：{} time: {}]`（ts-travel-service） | 数值提取 → 分布趋势（非分位） | service=ts-travel |
| `response code: {}` | `[getAllRoutes][receive response][Get routes error][response code: {}]` | 按值分组计数 | 下游服务 |

> ⚠ 上述 `{}` 均为变量，不能写成 `orderId: abc123` 之类的固定关键字。

### 5.3 降噪白名单（必须显式配置）

| 关键字/条件 | 原因 | 处理 |
|------------|------|------|
| `[payOrder][Pay order Fail][Order not found]`、`Order already exists`、`Assurance not clear` 等 ~150 个业务拒绝模板 | 用户侧输入错误/重复提交，属正常业务拒绝 | 从 ERROR 频率告警中排除；只统计趋势 |
| `[getToken][Incorrect username or password]` | 登录失败为常态 | 排除（另需脱敏，见 7.3） |
| `[validateToken][getClaims][Token expired]` | 客户端令牌过期是常态 | 排除；只保留 Signature/format 类 |
| `[preserve][Step 3][Check seat][Check seat is Not enough]` | 正常售罄 | 排除 |
| `[query][Query trip error][Tickets not found]`（ts-travel-service:192、ts-travel2-service:203，in_loop） | 查询无结果是正常路径 | 排除 |
| `[initContacts/createContacts][Init Contacts, Already Exists]` | 启动期幂等初始化 | 排除启动窗口期 |
| ts-ui-dashboard 全部 console 输出 | 浏览器端调试打印，非服务端信号 | 不采集 |
| conditional 非空的 285 条 | 多为 `getStatus() != 1` 业务分支，运行路径依赖输入 | 分类时按「预期内业务拒绝」处理，不直接告警 |

### 5.4 检索语句示例

```logql
# 跨服务调用失败（按服务分组；模板已含固定字面量，可直接匹配）
sum(rate({job=~"ts-.*"} |= "receive response" |= "fail!" [5m])) by (job)

# MQ 发送失败
sum(rate({job="ts-food-service"} |= "send delivery info to mq error" [5m]))

# ERROR 频率（排除白名单后，相对基线）
sum(rate({job=~"ts-.*"} | level="ERROR" != "Order not found" != "Already Exists" != "Incorrect username or password" != "Token expired" != "Check seat is Not enough" [5m])) by (job)
```

```promql
# JVM 堆水位（前提：开启 /actuator/metrics 暴露；候选阈值待校准）
jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"}
# SkyWalking 端点 P99（agent 挂载后；指标名以 SkyWalking 实际暴露为准）
```

---

## 6. 告警规则草稿（所有阈值均为候选，待 SLO/压测/线上数据校准）

**分层取舍说明**：本项目指标平面源码级为 0，链路平面依赖部署开关，因此**以 L2 日志事件告警为主**。代价：阈值不可跨环境复用、噪声高于指标告警、无法计算分位。L1 仅覆盖「开启暴露后」的平台层指标；L4 在挂载 SkyWalking Agent 后才可启用。此取舍在接入 SkyWalking/actuator 指标后应逐步向 L1 迁移。

### 6.1 指标告警（L1，前提条件见 1.7，未开启前为空转）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|--------|------|---------|------|------|--------|
| JVM 堆水位过高 | `jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 阈值` 持续 10m | > 0.85（量纲 0~1，候选，待压测拐点校准） | P1 | 按实例分组；持续 2 周期 | 发布后 15 分钟预热窗口 |
| 服务不可达 | `up{job="ts-*"} == 0` 持续 1m | 0（Prometheus 抓取失败即 0） | P0 | 按实例去重 | 计划内重启 |
| Tomcat 线程池饱和 | `tomcat_threads_busy / tomcat_threads_config_max > 阈值` 持续 5m | > 0.8（量纲 0~1，候选） | P2 | 按服务分组 | 压测窗口 |
| SkyWalking 端点 P99 劣化 | SkyWalking OAP 端点指标 P99 环比基线上升超阈值 | 待 SkyWalking 实际指标名与基线确认后定义 | P2 | 仅观察核心端点（preserve/cancel/pay/order） | 冷启动 |

### 6.2 日志事件告警（L2，主力）

| 告警名 | 条件 | 等级 | 降噪 | 白名单 |
|--------|------|------|------|--------|
| 服务启动初始化未完成 | 启动后 N 秒内未出现该服务 `Init` 类 INFO（如 `[InitData.run][Consign price service][Init data operation]`） | P0 | 只在重启事件后判定；按实例去重；N 按各服务实测启动耗时分别校准 | 计划内维护 |
| 跨服务调用失败 | 出现 `[...][receive response][... fail!]` ERROR | P1 | 按服务对聚合；持续 2 周期；失败次数阈值用相对基线 | 下游计划内重启窗口 |
| RabbitMQ 发送失败 | 出现 `send delivery info to mq error` / `Send email to mq error` | P1（food）/ P2（email） | food 侧 in_loop=true **禁止频率告警**，用「出现即告警 + 持续窗口」；按队列分组 | MQ 计划内维护 |
| 退款失败 | 出现 `[cancelOrder][Draw Back Money Failed]` | P1 | 单次即告；按 orderId 聚合去重 | 无（资金类不设白名单） |
| JWT 签名/格式异常 | 出现 `Signature failure` / `Token format error` / `not properly constructed` | P1 | 按来源 IP/实例聚合；排除 Token expired | 密钥轮换窗口 |

### 6.3 日志频率告警（L3，谨慎；仅用于无对应指标的异常）

| 告警名 | 条件 | 基线来源 | 等级 |
|--------|------|---------|------|
| 服务 ERROR 计数突增 | `sum(rate({job} \| level="ERROR" [5m]))` 排除 5.3 白名单后 > 3× 近 1h 基线，持续 3 周期 | 自身近 1 小时基线（候选） | P2 |
| `[queryInParallel][Query error]` 趋势上升 | in_loop=true，**禁止频率告警**；仅做日级趋势报表 | 7 日基线 | P3 |

### 6.4 链路告警（L4，可选；仅当 SkyWalking Agent 挂载后）

| 告警名 | span | 条件 | 等级 |
|--------|------|------|------|
| 核心购票链路分段耗时劣化 | Agent 注入（preserve→order→inside-payment→execute 拓扑） | 端点 P95 环比上升 > 50%（候选） | P2 |
| 跨服务调用断链 | 拓扑边消失 | 某服务→下游的调用边在拓扑中消失持续 5m | P1 |

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，检查记录见 4.1）

| 场景 | 风险 | 建议做法 | 优先级 |
|------|------|---------|--------|
| 业务级 RED 指标（请求计数/错误率/耗时直方图） | 无法感知错误率与延迟趋势，只能靠日志文本 | 优先启用 SkyWalking Agent（已具备全部部署物料）；如需 Prometheus 体系，在核心服务（order/preserve/cancel/pay/travel）Controller 层新增 Micrometer `@Timed`/Counter 埋点 | P1 |
| actuator 指标 web 暴露 | JVM/连接池指标存在但采不到 | 每服务 application.yml 增加 `management.endpoints.web.exposure.include: health,prometheus` + `micrometer-registry-prometheus` 依赖，并确认 management 端口与防火墙 | P1 |
| 非 Java 服务（Node/Python/Go）指标与链路 | ts-ticket-office-service、ts-avatar-service、ts-voucher-service、ts-news-service 三平面仅剩文本日志 | Node 服务接入 express 级中间件日志（替换 console）+ 走 SkyWalking NodeJS agent 或 nginx 侧指标；ts-news-service 源码主体为注释状态，建议确认其是否仍需部署 | P2 |
| 请求级延迟 | 日志仅有业务计算耗时打印 | 由 SkyWalking 端点 P99 承接（见 6.4），不建议日志做分位 | P1 |

### 7.2 已有但不可用（信号存在，质量问题）

| 信号 | 问题 | 建议 |
|------|------|------|
| actuator/Micrometer 运行时指标 | web 端点默认未暴露（无 management 配置） | 见 7.1 第 2 行；先开启再谈告警 |
| SkyWalking 链路/指标 | Agent 挂载仅存在于 k8s skywalking 样例；快速启动 compose 不含 agent，且 with-jaeger 变体的 collector 未在 compose 内定义 | 统一链路后端选型（SkyWalking 或 Jaeger），补全 collector 服务定义 |
| 日志-链路关联 | apm-toolkit-logback-1.x 已在依赖中，但无自定义 logback.xml 配置 pattern | 若走 SkyWalking，为各服务添加含 `%tid` 的 logback 配置，使日志可按 traceId 检索 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|------|------|------|
| **明文密码进日志** | ts-auth-service/.../TokenServiceImpl.java:86 `[getToken][Incorrect username or password][username: {}, password: {}]` | 立即移除 password 占位符，改打固定文案 |
| Token 明文进日志 | ts-auth-service TokenServiceImpl `[getToken][success][USER TOKEN: {} USER ID: {}]` | 脱敏：只打 userId 与 token 前 8 位 |
| 循环内打 ERROR/INFO | ts-food-service/.../FoodServiceImpl.java:65,106；ts-food-delivery-service/.../FoodDeliveryServiceImpl.java:65 | 批量操作改为聚合摘要一条（处理 N 条、失败 M 条） |
| 无结构化字段（纯文本 `{}` 拼接） | 全部 Java 服务 | 若引入 Loki/ES 检索，改 JSON encoder（logback-logstash-encoder），占位符变字段 |
| 同一业务双服务重复模板 | ts-order-service 与 ts-order-other-service、ts-travel-service 与 ts-travel2-service 模板几乎相同 | 告警规则需带 `job` 维度区分，否则无法定位服务 |

### 7.4 需人工确认

| 项 | 原因 |
|----|------|
| old-docs/Lib/ms-monitoring-core（自研链路库，67 条日志 + span 实现） | 归档代码，未被任何活跃 pom 引用；确认是否为历史监控方案遗存，避免误配告警 |
| ts-ui-dashboard 前端第三方压缩库命中的 18 条「日志」 | min.js 内 console 调用，应从扫描范围排除 |
| Go 服务 ts-news-service 的实际运行状态 | main.go 主体逻辑被注释，静态分析无法判断是否部署使用 |
| with-jaeger 镜像内部的实际 instrumentation 方式 | 预构建镜像（codewisdom/ts-*-with-jaeger:v1），源码中无对应依赖，需在运行时验证其上报行为 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
cd /Users/rizhiyi/Downloads/gitdir/source-log-to-monitor/scripts
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

（人工降级提取的 Node/Python/Go 清单可整理成同构 JSON 后走同一 diff。）

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---------|-------------|------|
| 新增 ERROR 日志 | 可能有新故障模式 | 对照 5.3 白名单归类；非业务拒绝类评估新增 L2 告警 |
| 新增 Micrometer/actuator 指标 | 新的可观测能力 | 评估新增 L1 告警 |
| 删除日志/指标 | 对应告警静默失效 | **必须下线对应规则** |
| application.yml 新增/变更 management 配置 | 指标暴露面变化 | 重新核对 1.7 开关表，优先级最高 |
| 部署编排变更（agent 挂载、SW_* 环境变量） | 链路平面可用性变化 | 重新验证 SkyWalking 数据流 |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配，忽略行号偏移；同文件同模板多条会被折叠，计数为近似值
- 本项目 order/order-other、travel/travel2 模板高度雷同，diff 需带服务目录维度人工复核

---

## 9. 局限性说明

1. **阈值为候选值**：第 6 章全部阈值需结合 SLO、压测基线、线上历史校准；本项目为测试床，无真实流量基线，白名单外的绝对阈值慎用。

2. **源码常量 ≠ 告警阈值**：application.yml 中的 MySQL/RabbitMQ 地址、端口（如 ts-food-service:18856、rabbitmq:5672）是部署配置，不是告警阈值。

3. **提取覆盖率不是 100%**：
   - 指标：脚本与全文检索双确认 0 定义，但「运行时由 actuator 自动注册」的指标静态不可数
   - 日志：JS 平台的 `console.*` 调用被脚本归类为结构化框架（logger_type=pino_winston_bunyan），实际均为无级别打印，已按 code_snippet 回源纠正；Python 服务（Flask/Tornado）的 `print`/`logging` 调用不在本次活跃清单内（脚本对 `ts-avatar-service/app.py`、`ts-voucher-service/server.py` 的覆盖有限，属脚本未覆盖路径，需人工补充，覆盖率未知）
   - Go 服务 ts-news-service：主体逻辑被注释，提取结果 0 条按「源码确认」处理

4. **静态分析看不到运行时**：
   - SkyWalking Agent 是否挂载、OAP 是否可达、采样率，均由部署编排决定（1.7），需上线后验证数据流
   - actuator 端点实际暴露面需在目标环境 `curl` 验证
   - conditional 非空的 285 条日志是否触发取决于运行路径输入

5. **未覆盖的场景**：MySQL/Redis/RabbitMQ/Nacos 等基础设施层指标需外部 exporter（mysqld_exporter、redis_exporter、rabbitmq_exporter），不修改本项目源码；浏览器端用户体验指标不在本方案范围。

6. **脚本能力边界**：Java/JS 走脚本正则后端（函数边界为大括号栈近似，in_loop/in_except 方向可信、边界不精确）；Python/Go 仅完成画像与人工检索，未纳入结构化提取清单；old-docs/ 归档目录已扫描但显式排除在活跃范围外。
