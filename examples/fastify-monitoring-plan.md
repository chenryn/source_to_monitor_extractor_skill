# fastify 监控方案（源码 → 三平面信号）

- 分析对象：fastify `6.0.0-alpha.6`（commit `8c0f699144187dab3b600a626829c31368762256`）
- 分析日期：2026-11-04
- 提取方式：内置脚本（JS 后端）+ 全文检索复核；未覆盖范围见第 9 章

---

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 运行形态 | **库 / Web 框架**（被宿主应用集成，本身不是在线服务） | package.json（main: fastify.js）；无自带常驻进程入口 |
| 源码语言 | JavaScript 为主：生产代码 = `lib/`（27 个模块）+ `fastify.js`，共 264 个 .js；33 个 .ts 为纯类型声明（`types/`、`fastify.d.ts`），不含运行时逻辑 | `find` 统计 + `types/` 目录内容 |
| 日志门面 | **pino**（可选），内部经自研 `LogController` 封装请求生命周期日志；**未配置 logger 时默认挂 abstract-logging 空实现** | lib/logger-pino.js:9,40；lib/log-controller.js:10-165；lib/logger-factory.js:87-91 |
| 日志风格 | pino 结构化：首参为对象（req/res/err/responseTime 等），消息取最后一个字符串参数；对象首参无消息时模板为空（msg 取 err.message） | lib/log-controller.js:48,64,66 等；lib/logger-pino.js:46-63（serializers：req/res/err） |
| 指标 SDK | **无**。全仓库检索 `prom-client / prometheus / opentelemetry / createCounter / createHistogram / createGauge / registerMetric / meter / tracer` 均零命中（含 .js/.mjs/.ts，排除 .d.ts） | 全文检索证据见 4.1 |
| 链路 SDK | **无**。检索 `startActiveSpan / startSpan / @opentelemetry / tracer / sampler` 零命中 | 全文检索证据见 4.1 |
| 暴露方式 | 无 /metrics 端点、无 OTLP exporter。全部信号经**注入的 logger 实例**交由宿主输出 | fastify.js:926-940（createLogger）；lib/logger-pino.js:15-44 |
| 默认开关 | **日志默认关闭**：`options.logger` 与 `options.loggerInstance` 均未传时挂 abstract-logging 空实现（`hasLogger: false`），所有 log 调用为 no-op | lib/logger-factory.js:87-91；fastify.js:926 |
| 请求日志开关 | `disableRequestLogging`（默认 `false`，可传布尔或函数），关闭后抑制 incoming/completed/errored 三类请求日志；`serviceUnavailable`（503 关闭期拒绝）**不受**此开关抑制 | lib/log-controller.js:21,32-36,45-49,60-68,162-164 |
| 自定义入口 | 宿主可经 `logController` 选项注入 LogController 子类定制内部日志行；子 logger 绑定 `requestIdLogLabel`（默认 `reqId`） | lib/logger-factory.js:132-140,38-41；lib/log-controller.js:23 |
| 依赖声明 | package.json 含 `pino`、`abstract-logging`；不含任何指标/链路 SDK | package.json dependencies |

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|------|------|----------|-------------|
| 日志埋点 | 生产路径 28 条（另 17 条来自 scripts/ 开发脚本，不计入监控方案） | lib/ 27 + fastify.js 1；请求生命周期集中在 log-controller.js | 中（日志平面完整，但依赖宿主开启） |
| 指标定义 | 0 个 | 无任何指标 SDK 与定义 | 无 |
| 链路 span | 0 个 | 无任何 trace SDK | 无 |

### 1.2 日志级别分布（生产路径 28 条）

| 级别 | 数量 | 占比 | 是否纳入告警 |
|------|------|------|-------------|
| ERROR | 8 | 28.6% | 部分（4xx 归为业务类，进白名单） |
| WARNING | 11 | 39.3% | 部分（框架误用类为主，需白名单） |
| INFO | 7 | 25.0% | 否（事件类除外：启动完成、关闭期拒绝） |
| DEBUG/TRACE | 2 | 7.1% | 否 |
| FATAL | 0 | — | — |

### 1.3 日志按模块分布（生产路径）

| 模块 | 埋点数 | ERROR/WARN | 主要职责 |
|----|--------|-----------|----------|
| lib/log-controller.js | 10 | 3 | 请求生命周期日志统一封装（宿主可覆盖） |
| lib/reply.js | 8 | 7 | 响应发送、流式响应、reply.then |
| lib/head-route.js | 2 | 2 | HEAD 路由流处理 |
| lib/four-oh-four.js | 2 | 2 | 默认 404 兜底 |
| lib/hooks.js | 1 | 1 | hook 执行错误 |
| lib/route.js | 1 | 1 | onRequestAbort hook 失败 |
| lib/wrap-thenable.js | 1 | 1 | Promise 包装错误 |
| lib/server.js | 1 | 0 | listen 地址打印 |
| fastify.js | 1 | 0 | 客户端连接错误（trace 级） |
| scripts/validate-ecosystem-links.js | 17（不纳入） | 1 | 文档链接校验开发脚本，非生产代码 |

### 1.4 上下文证据摘要（生产路径）

| 证据 | 数量 | 占比 | 对方案的影响 |
|------|------|------|-------------|
| in_loop | 2 | 7.1% | server.js:393（逐地址打印）、reply.js:940（流回调）禁止频率告警 |
| in_except | 2 | 7.1% | head-route.js:23、reply.js:593 优先归 error 类 |
| once | 0 | 0% | 无存在性告警候选 |
| conditional | 12 | 42.9% | 多数为错误分支条件（statusCode>=500、error 非空等），非特性开关 |
| is_assertion | 0 | 0% | 无断言类 |
| structured（对象首参） | 16 | 57.1% | pino 结构化风格；12 条无消息模板（msg 取 err.message 或动态文本） |

---

## 2. 日志埋点明细（按语义分类，生产路径 28 条）

> pino 首参为对象时消息取最后一个字符串参数；首参为 Error 对象时无消息模板（记 structured，运行时 msg = err.message）。

### 2.1 请求/事务类 (request)

| 文件:行 | 级别 | 模板 | 结构化字段 | 触发条件 |
|---------|------|------|-----------|----------|
| lib/log-controller.js:48 | INFO | `incoming request` | req (method/url/version/host/remoteAddress/remotePort) | 每个请求进入时；受 disableRequestLogging 控制 |
| lib/log-controller.js:66 | INFO | `request completed` | res.statusCode, responseTime | 请求正常完成；responseTime 为**响应耗时毫秒数** |
| lib/log-controller.js:64 | ERROR | `request errored` | res.statusCode, err, responseTime | 请求以错误结束 |
| lib/log-controller.js:120 | INFO | `Route {method}:{url} not found` | 占位符 style=template | 默认 404 handler |

### 2.2 业务/框架误用类 (business_error)

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| lib/log-controller.js:85 | INFO | （无模板，msg=error.message） | 默认错误处理器处理 4xx；**预期内，进白名单** |
| reply.js:162 | WARN | （无模板，err=FST_ERR_REP_ALREADY_SENT） | reply 已发送后再次 send；使用错误 |
| reply.js:593 | WARN | `Reply was already sent, did you forget to "return reply" in the "{url}" ({method}) route?` | 经典使用错误提示 |
| reply.js:1055 | WARN | `Trying to send a NotFound error inside a 404 handler. Sending basic 404 response.` | 404 handler 内二次发送，已自动兜底 |
| reply.js:794 / reply.js:893 | WARN | `response will send, but you shouldn't use res.writeHead in stream mode` | 流模式误用 writeHead（两处同模板） |
| four-oh-four.js:175 | WARN | `the default handler for 404 did not catch this, this is likely a fastify bug, please report it` | 框架自身 bug 兜底路径，不受 disableRequestLogging 抑制 |
| four-oh-four.js:176 | WARN | （无模板，router.prettyPrint() 路由表） | 伴随上一条输出 |

### 2.3 系统/IO 异常类 (system_error)

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| lib/log-controller.js:83 | ERROR | （无模板，msg=error.message） | 默认错误处理器处理 5xx；条件 statusCode>=500 |
| lib/log-controller.js:104 | WARN | `response terminated with an error with headers already sent` | 响应头已发出后流出错 |
| lib/log-controller.js:134 | WARN | （无模板，msg=error.message） | 错误处理阶段 writeHead 抛错 |
| lib/log-controller.js:152 | ERROR | `The serializer for the given status code failed` | 序列化失败（serialize_error） |
| lib/head-route.js:13 | ERROR | `Error on Stream found for HEAD route` | HEAD 路由流错误（payload.resume 分支） |
| lib/head-route.js:23 | ERROR | `Error on Stream found for HEAD route` | 同上（payload.getReader 分支，in_except） |
| lib/hooks.js:193 | ERROR | （无模板，err 对象直传） | hook 链 next(err) 透传 |
| lib/route.js:621 | ERROR | `onRequestAborted hook failed` | 请求中断 hook 失败 |
| lib/wrap-thenable.js:64 | ERROR | `Promise errored, but reply.sent = true was set` | 异步 handler 抛错但响应已发出 |
| reply.js:503 | WARN | `unhandled rejection on reply.then` | reply.then 未处理 rejection（排除 premature close） |
| reply.js:879 | WARN | `stream payload does not end properly` | 流未正常结束 |
| lib/log-controller.js:102 | INFO | `stream closed prematurely` | 客户端提前断开，**预期内，进白名单** |

### 2.4 生命周期类 (lifecycle)

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| lib/server.js:393 | INFO | （listen 地址文本，动态构造，in_loop） | 监听成功；in_loop 逐地址打印，禁频率告警 |
| lib/log-controller.js:163 | INFO | `request aborted - refusing to accept new requests as server is closing` | 关闭期拒绝新请求（503）；不受 disableRequestLogging 抑制 |

### 2.5 调试日志（不纳入告警）

| 文件:行 | 级别 | 模板 | 说明 |
|---------|------|------|------|
| fastify.js:1009 | TRACE（脚本记 DEBUG） | `client {errorLabel}` | 客户端连接错误；`defaultClientErrorHandler` |
| reply.js:940 | DEBUG | （err 对象直传，in_loop） | 流错误回调调试 |

---

## 3. 指标清单

### 3.1 已定义的指标

**无。** 提取脚本指标定义 0、写入点 0、暴露点 0；全文检索确认无任何指标 SDK 调用（证据见 4.1）。

fastify 不内置指标平面，属库类项目的合法形态：RED/USE 信号由宿主侧的 APM/指标中间件提供。**但源码中存在一处「可作为指标数据源的字段」**：

| 字段 | 所在位置 | 语义 | 说明 |
|------|---------|------|------|
| `responseTime` | lib/log-controller.js:64,66 | 请求耗时（毫秒，来自 reply.elapsedTime） | 以 pino 结构化字段随 `request completed` / `request errored` 日志输出；宿主可从日志提取分位，或经 LogController 子类改写上报 |

### 3.2 指标暴露方式

| 方式 | 位置 | 说明 |
|------|------|------|
| 无 | — | 无 /metrics、无 OTLP、无 push；唯一对外通道是注入的 logger 实例 |

---

## 4. 信号仲裁

### 4.0 运行形态决定的需求清单

fastify 是库，不是在线服务。它**不自带告警**；监控需求的主语是「宿主应用」。方案回答三个问题：
1. fastify 向宿主**暴露**哪些信号；
2. 宿主如何**打开并验证**采集（默认开关是第一优先级）；
3. 哪些信号 fastify **不提供**，需宿主侧补齐。

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|------|-----------|------|
| 日志 | 提取 45 条（生产 28 条），检索关键词：`log.info/error/warn/debug/trace`、`request.log`、`reply.log`、`server.log`、`this.log`；lib/ + fastify.js 命中 28 处调用，与脚本输出一致 | 请求生命周期、流错误、关闭拒绝均有埋点 |
| 指标 | 脚本 0 条 + 全文检索 `prom-client|prometheus|opentelemetry|createCounter|createHistogram|createGauge|registerMetric|collectDefaultMetrics|\.histogram\(|\.counter\(|\.gauge\(` 于 lib/ fastify.js types/（排除 .d.ts）→ **0 命中**；package.json 无指标 SDK 依赖 | 确认「真没有」，非提取遗漏（JS 后端在能力矩阵内） |
| 链路 | 脚本 0 条 + 全文检索 `startActiveSpan|startSpan|@opentelemetry|tracer|sampler` 于 lib/ fastify.js → **0 命中** | 确认「真没有」，非提取遗漏 |

另：`console.*` 于 lib/ 与 fastify.js 零命中，无需 `--include-print` 重跑。分段耗时的**唯一**源码级信号是日志字段 `responseTime`（log-controller.js:64,66），无 span、无指标。

### 4.2 仲裁表

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---------|---------|---------|---------|------|
| 1 | 宿主是否开启了 fastify 日志 | 配置（前置条件） | 默认 abstract-logging no-op | lib/logger-factory.js:87-91 | **默认关闭**：宿主须传 `logger: true` / pino options / logger 实例；所有日志信号的采集以此为前提 |
| 2 | 请求量 / 流量 | 日志 | `incoming request`（每请求一条，结构化 req 字段） | lib/log-controller.js:48 | 已覆盖（计数式，前提 #1 满足且 disableRequestLogging=false） |
| 3 | 错误率（5xx） | 日志 | `request errored`（ERROR）+ defaultErrorLog 5xx 分支（msg=error.message） | lib/log-controller.js:64,83 | 已覆盖，按 res.statusCode 或级别统计；4xx 进白名单 |
| 4 | 请求耗时分布 | 日志字段（非指标） | `responseTime` 结构化字段 | lib/log-controller.js:64,66 | 部分覆盖：仅日志平面，数值提取算分位，精度与成本劣于 Histogram；指标平面为真缺口 |
| 5 | 请求失败原因 | 日志 | `request errored` 的 err 字段 + 各错误点模板 | lib/log-controller.js:64；2.3 节全表 | 已覆盖 |
| 6 | 一次请求慢在哪一段（分段耗时） | 链路 | 无 span，无属性投影 | — | **真缺口**：库不提供，需宿主接 OTel HTTP instrumentation 或路由级埋点 |
| 7 | 优雅关闭期间的行为 | 日志 | `request aborted - refusing to accept new requests as server is closing`（503） | lib/log-controller.js:163 | 已覆盖（存在性判断，不受 disableRequestLogging 抑制） |
| 8 | 启动成功 | 日志 | listen 地址打印 | lib/server.js:393 | 已覆盖（存在性告警；in_loop，禁频率统计） |
| 9 | 客户端提前断开 | 日志 | `stream closed prematurely`（INFO） | lib/log-controller.js:102 | 已覆盖，**预期内，白名单**，不做告警 |
| 10 | 资源水位（内存/连接/队列） | 指标 | 无 | — | 真缺口：属宿主进程/平台层，需宿主采集（Node 进程指标），非 fastify 职责 |
| 11 | 框架自身 bug 兜底触发 | 日志 | `the default handler for 404 did not catch this, this is likely a fastify bug` | lib/four-oh-four.js:175 | 已覆盖；存在性告警候选（P2，出现即值得上报 issue） |

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|--------|------|------|------|
| `request errored` | P1 | 请求以错误结束（结构化，按 err/res 字段下钻） | lib/log-controller.js:64 |
| `response terminated with an error with headers already sent` | P1 | 响应头已发出后流失败 | lib/log-controller.js:104 |
| `The serializer for the given status code failed` | P1 | 序列化失败 | lib/log-controller.js:152 |
| `Promise errored, but reply.sent = true was set` | P2 | 异步 handler 错误被吞 | lib/wrap-thenable.js:64 |
| `onRequestAborted hook failed` | P2 | 中断 hook 失败 | lib/route.js:621 |
| `Error on Stream found for HEAD route` | P2 | HEAD 路由流错误 | lib/head-route.js:13,23 |
| `request aborted - refusing to accept new requests as server is closing` | P2（存在性） | 关闭期拒绝请求，非故障 | lib/log-controller.js:163 |
| `the default handler for 404 did not catch this, this is likely a fastify bug` | P2 | 框架 bug 兜底 | lib/four-oh-four.js:175 |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在日志 | 统计方式 | 维度 |
|------|---------|---------|------|
| responseTime | `request completed` / `request errored` | 数值提取 → P50/P95/P99（单位 ms） | 路由/method（低基数） |
| res.statusCode | 同上 | 按值分组计数 → 错误率 | method（低基数） |
| reqId | 全部请求日志（child logger 绑定） | 请求串联（不进聚合 by） | — |
| err (pino stdSerializers.err) | ERROR/WARN 结构化日志 | err.type / err.code 分组计数 | err.type（低基数） |

> ⚠ `Route {method}:{url} not found` 中的 method/url 是占位符，不能当固定关键字；按结构化字段统计。

### 5.3 降噪白名单

| 关键字/条件 | 原因 | 处理 |
|------------|------|------|
| `stream closed prematurely`（INFO） | 客户端主动提前断开，正常现象 | 忽略，不计错误率 |
| 4xx 的 defaultErrorLog（log-controller.js:85） | 业务拒绝（校验失败、404 等），预期内 | 只对 5xx 分支（:83）告警 |
| `Route ... not found` | 扫描器/探测流量常态 | P3 趋势观察；突增提示路由配置问题 |
| `Reply was already sent...` / `Trying to send a NotFound error inside a 404 handler` | 框架已自动兜底 | 频率突增反映宿主代码误用，P3 |
| `request aborted - refusing...`（:163） | 优雅关闭的预期行为 | 只做存在性/趋势，不做告警 |
| 框架误用类 WARN（writeHead in stream mode 等） | 宿主代码问题，服务仍可用 | P3 统计 |
| disableRequestLogging=true 的宿主 | 请求级日志被有意关闭 | 前提核查项，不是异常 |

### 5.4 检索语句示例（宿主侧 Loki，假设已开启 pino JSON 日志）

```logql
# 请求错误率分子（排除白名单）
sum(rate({job="<host-app>"} |= "request errored" [5m]))
/
sum(rate({job="<host-app>"} |= "request completed" [5m]))

# 耗时 P95：responseTime 为 JSON 数值字段，需 logql 数值提取
quantile_over_time(0.95,
  {job="<host-app>"} |= "request completed"
    | json | unwrap responseTime [5m]
) by (route)
```

---

## 6. 告警规则草稿（候选，待校准）

> 库类项目：不设计 P0；以下规则全部是**宿主应用侧**的候选规则，前提是第 6.0 节的开关核查通过。所有阈值均为候选值，待 SLO/压测/线上数据校准。

### 6.0 前置核查（第一优先级，非告警）

| # | 核查项 | 判定标准 | 证据 |
|---|--------|---------|------|
| 1 | 宿主是否传入 logger | 构造参数含 `logger: true` / pino options / logger 实例；未传 = 所有信号为空转 | lib/logger-factory.js:87-91 |
| 2 | pino level 是否覆盖 info | 默认 `info`（options.logger 为对象时，logger-factory.js:125）；level 高于 info 则请求生命周期日志不可见 | lib/logger-factory.js:125 |
| 3 | disableRequestLogging 状态 | 默认 false；为 true 时 #2~#5 全部失效 | lib/log-controller.js:21 |
| 4 | 数据流验证 | 触发一次请求，确认能看到 `incoming request` 与 `request completed` 两条 JSON 日志 | lib/log-controller.js:48,66 |

### 6.1 日志事件告警（L2，本方案主力层）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|--------|------|---------|------|------|--------|
| 请求错误率突增 | `request errored`（log-controller.js:64）速率 / `request completed` 速率 | 比值 > 5% 持续 5 分钟（**候选，待校准**） | P1 | 相对基线；按 err.type 分组 | 客户端断开、4xx、计划内压测 |
| 响应流失败（头已发出） | 出现 `response terminated with an error with headers already sent` | 出现即告警 | P1 | 按实例去重 | 无（该路径无预期来源） |
| 序列化失败 | 出现 `The serializer for the given status code failed` | 出现即告警 | P1 | 按 statusCode 分组 | 无 |
| 异步错误被吞 | 出现 `Promise errored, but reply.sent = true was set` | 5 分钟内 > N 次（N 候选，待校准） | P2 | 按路由聚合 | 无 |
| HEAD 路由流错误 | 出现 `Error on Stream found for HEAD route` | 出现即告警 | P2 | 按 url 聚合 | 无 |
| 框架 bug 兜底触发 | 出现 `the default handler for 404 did not catch this` | 出现即告警 | P2 | 按实例去重，附带路由表日志上报 | 无 |
| 中断 hook 失败 | 出现 `onRequestAborted hook failed` | 5 分钟内 > N 次（候选） | P2 | 按 hook 名聚合 | 客户端高频断开的宿主 |

### 6.2 日志频率告警（L3，谨慎）

| 告警名 | 条件 | 基线来源 | 等级 |
|--------|------|---------|------|
| 框架误用类 WARN 突增 | `Reply was already sent` / `writeHead in stream mode` / `unhandled rejection on reply.then` 速率 > 基线 × 3（候选） | 该宿主近 7 天历史 | P3 |
| 404 未命中突增 | `Route .* not found` 速率 > 基线 × 5（候选） | 同上 | P3（可能提示路由回归或扫描） |

> 注：`request completed` 里 responseTime 字段可承载耗时趋势（P3 看板，非告警），但日志算分位成本高，仅作指标平面补齐前的过渡。

### 6.3 指标告警（L1）与链路告警（L4）

| 层 | 结论 |
|----|------|
| L1 指标 | fastify 无指标平面（4.1 节已核实）。宿主接入指标后，RED 规则（请求速率 / 5xx 错误率 / 耗时 P99）应建立在宿主侧指标上，本方案不虚构指标名 |
| L4 链路 | fastify 无链路平面。宿主开启 OTel 后按通用 HTTP server span 规则设计，不属本方案范围 |

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，主语为「宿主需补齐」）

| 场景 | 风险 | 建议做法 | 优先级 |
|------|------|---------|--------|
| 请求耗时分布（指标） | 只有 responseTime 日志字段，无法低成本算分位/做趋势告警 | 宿主接入指标层（如 OTel HTTP instrumentation）后建 Histogram；fastify 侧不改源码 | P1 |
| 分段耗时（链路） | 无法回答「这一次慢在哪一段」 | 宿主开启 OTel HTTP server span + handler 级 span | P2 |
| 进程资源水位 | 库不提供，属宿主进程层 | 宿主采集 Node 进程指标（内存/句柄/事件循环延迟） | P1 |

### 7.2 已有但不可用（信号存在质量问题）

| 信号 | 问题 | 建议 |
|------|------|------|
| 全部日志（28 条） | 默认挂在 abstract-logging no-op 上，未配置时零输出 | 宿主必须显式开启（第 6.0 节核查项），方案优先级最高 |
| 请求生命周期日志 | 受 disableRequestLogging 开关控制，宿主关闭后无请求级信号 | 开启前确认；或依赖 defaultErrorLog 与 streamError（同受该开关控制，无法单独保留） |
| `request errored` / defaultErrorLog 5xx | msg = error.message（动态文本），不可全文匹配 | 按 err.type / res.statusCode 结构化字段统计 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|------|------|------|
| `Route {method}:{url} not found` 未结构化（模板拼接） | lib/log-controller.js:120 | 同类其他日志均为对象首参；此处若改为 `{ method, url }` 字段更利于统计 |
| HEAD 路由两处 ERROR 同模板不同分支 | lib/head-route.js:13,23 | 可合并；err 字段已区分根因 |
| 框架 bug 类与误用类均为 WARN | lib/four-oh-four.js:175 | 前者实为框架异常，出现即需上报，级别语义与误用类混在一起；宿主告警需分开对待 |

### 7.4 需人工确认

| 项 | 原因 |
|----|------|
| `router.prettyPrint()`（four-oh-four.js:176）的输出形态 | 多行文本落日志，采集端解析行为需实测 |
| 宿主自定义 LogController 子类的行为 | logController 选项可整体替换内部日志（logger-factory.js:132-140），替换后本章全部模板可能变化 |
| fastify.js:1009 实际为 trace 级 | 脚本归入 DEBUG 档；对监控无影响 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本重新提取（三平面都要）
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json

# 对比
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---------|-------------|------|
| 新增 ERROR/WARN 日志 | 可能有新故障模式 | 评估新增 6.1/6.2 告警 |
| 新增指标/trace | fastify 当前无此两平面；一旦出现即重大能力变化 | 重新走 Step 0 画像，补 L1/L4 规则 |
| 日志模板变更 | 5.1 关键字与 6.x 条件静默失效 | 更新关键字库与告警条件 |
| LogController 方法增删 | 宿主可覆盖的日志面变化 | 重新核对 6.0 前置核查 |
| 默认开关变更（logger 默认值、disableRequestLogging 默认值） | 采集前提变化，**优先级最高** | 重跑 6.0 核查并验证数据流 |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配，忽略行号偏移；同文件同模板多条会被折叠（如 reply.js:794 与 :893），计数是近似值。

---

## 9. 局限性

1. **阈值为候选值**：所有阈值需结合宿主 SLO、压测基线、线上历史校准。
2. **源码常量 ≠ 告警阈值**：fastify 源码中的 `requestIdLogLabel`、level 默认值等是配置项，不是告警阈值。
3. **提取覆盖率不是 100%**：
   - 日志：动态构造的 listen 文本（server.js:393）、err 对象直传（无模板）的运行时 msg 无法静态还原；
   - 类型声明（.ts/.d.ts）只读签名不扫调用，`fastify.d.ts` 中的可选配置不构成运行时证据；
   - scripts/ 下 17 条为开发脚本日志，已排除，不构成监控结论。
4. **静态分析看不到运行时**：
   - 默认 logger 为 no-op（logger-factory.js:87-91），宿主是否真正开启日志只有运行时验证可知；
   - pino 采样/transport/红切（redact）配置属宿主侧，静态不可见；
   - disableRequestLogging 支持函数形式（log-controller.js:22,32-35），按请求条件关闭，静态无法判定实际范围。
5. **形态边界**：本方案面向「库」的运行形态——fastify 自身无进程、无端口、无 /metrics；一切指标与链路能力由宿主提供。对 fastify 直接套用在线服务 RED 告警清单是错误的。
6. **脚本能力边界**：JS 后端在能力矩阵内，三平面输出经全文检索复核（4.1 节），指标/链路为 0 属「真没有」而非提取遗漏；但 pino child logger 的间接调用、宿主传入的自定义 logger 行为不在静态覆盖范围内。
