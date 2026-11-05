# Prometheus 自身监控方案（源码 → 监控方案）

- 分析对象：prometheus（Prometheus 服务端本体，Go 项目）
- 运行形态：基础设施组件 + 在线 HTTP 服务（时序数据库 TSDB、抓取代理、规则引擎、查询引擎于一身的常驻进程）
- 特殊性说明：被分析对象是 Prometheus 自己——它用 client_golang 为自身埋点，再通过自己的 `/metrics` 端点暴露自监控指标。「源码里注册的指标」与「运行时暴露的指标」在本项目中是一套体系，但存在第三类「抓取时合成的 per-target 样本」（`up`、`scrape_samples_scraped` 等），三者在源码中的注册方式不同，第 3.3 节单独区分。
- 提取方式：脚本提取（extract_loggers / extract_metrics / extract_traces，Go 正则后端），中间产物位于 `/tmp/sltm-agent-prometheus/`。

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面 | promslog（log/slog 封装），结构化键值对参数；`logger := promslog.New(&cfg.promslogConfig)` 统一创建并 `slog.SetDefault` | cmd/prometheus/main.go:693-694 |
| 指标 SDK | prometheus/client_golang v1.25.0；标准构造器（NewCounter/NewGauge/NewHistogramVec 等）+ promauto 风格注册；命名约定 `Namespace: "prometheus"` + Subsystem（如 `rules/group.go:921` `const namespace = "prometheus"`、`promql/engine.go:61-62` `namespace="prometheus", subsystem="engine"`） | go.mod:68；rules/group.go:921；promql/engine.go:61-62 |
| 链路 SDK | OpenTelemetry（go.opentelemetry.io/otel v1.47.0），`otel.Tracer("").Start(...)` 显式调用；由 tracing.Manager 管理 | go.mod:86-92；tracing/tracing.go:64-92 |
| 暴露方式 | HTTP 拉取：`router.Get("/metrics", promhttp.Handler().ServeHTTP)`，与 UI/API 同端口 | web/web.go:506 |
| 默认开关 | 见 1.6 表 | — |
| 运行模式 | 常规 Server（TSDB）与 Agent 模式（WAL + remote write），`--agent` 切换，影响存储组件与指标集 | cmd/prometheus/main.go:671,951 |

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|------|------|----------|-------------|
| 日志埋点 | 800 条（断言 232，实际日志 568；按模板去重 496） | tsdb 126、cmd/prometheus 119、storage/remote 73、promql 52、discovery/kubernetes 45 | 高 |
| 指标定义 | 263 个定义 / 405 写入点 / 10 暴露点 | tsdb 74、storage/remote 47、scrape 25、tsdb/wlog 17、discovery 17 | 高（自监控指标体系完整） |
| 链路 span | 23 个 span + 15 处属性 | scrape 7、rules 6、storage/remote 4、promql 4、web/api 2 | 中（span 齐全但默认禁用） |

### 1.2 日志按级别分布（实际日志 568 条，断言单列）

| 级别 | 数量 | 占比 | 是否纳入告警 |
|------|------|------|-------------|
| ERROR | 203 | 35.7% | 是（P1 事件类，需白名单） |
| INFO | 166 | 29.2% | 否（lifecycle 事件类除外） |
| WARNING | 118 | 20.8% | 部分（需白名单） |
| DEBUG | 78 | 13.7% | 否 |
| FATAL | 3 | 0.5% | 否（全部位于 web/ui 的文档生成工具 gen_functions_docs，非运行时路径） |
| 断言（panic，is_assertion） | 232 | — | 不是可检索日志；失败即中止，表现为「日志断档 + 进程退出」，用进程存活监控承接 |

注：FATAL 级别总数 235 中 232 条是 panic 断言（如 cmd/prometheus/main.go:2137 `panic(fmt.Sprintf("unknown storage type %T", db))`），真正的运行时 FATAL 日志为 0——崩溃信号需靠进程存活 + `/metrics` 断档判定，不能做日志关键字。

### 1.3 日志按包分布（Top 10）

| 包 | 埋点数 | 主要职责 |
|----|--------|----------|
| tsdb | 126 | 本地存储引擎（WAL/compaction/head） |
| cmd/prometheus | 119 | 主程序生命周期、配置加载 |
| storage/remote | 73 | remote write/read 客户端与 handler |
| promql | 52 | 查询引擎与 query log |
| discovery/kubernetes | 45 | K8s 服务发现 |
| tsdb/chunkenc | 44 | chunk 编解码 |
| rules | 28 | 规则评估 |
| tsdb/wlog | 28 | WAL 读写与 repair |
| discovery/aws | 26 | AWS 服务发现 |
| tsdb/agent | 21 | Agent 模式存储 |

### 1.4 指标按类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| Counter | 147 | 累计计数（`*_total`） |
| Gauge | 75 | 瞬时水位（head_series、shards、queue_length 等） |
| Histogram | 23 | 分布（query_duration、scrape_duration、compaction 等） |
| Summary | 18 | 客户端分位（query_duration_seconds、target_interval_length_seconds 等） |

命名前缀分布：`prometheus_tsdb_*` 79、`prometheus_target_*` 25、`prometheus_sd_*` 24、`prometheus_rule_*` 15、`prometheus_agent_*` 12、`prometheus_query_*` 9、`prometheus_http_*` 5。命名高度统一。

### 1.5 链路 span 按阶段分布

| 阶段 | 数量 | 典型 span |
|------|------|-----------|
| request（查询 API） | 2 | promqlInstantQuery、promqlRangeQuery（web/api/v1/api.go:655、1255） |
| request（抓取链） | 7 | scrapeRequest → scrape → newAppender → scrapeRead → scrapeAppend → scrapeCommit → scrapeReport（scrape/scrape.go:837、1556-1598） |
| background（规则评估链） | 6 | rule group → rule → newAppender → ruleAppendResults → ruleCommit（rules/group.go:515-574、rules/manager.go:93） |
| external_call（远端存储） | 4 | Remote Store、Remote Read、Remote Send Batch、Remote Metadata Send Batch（storage/remote/client.go:294、406；queue_manager.go:664、2809） |
| request（查询执行内段） | 4 | Querier、querierSelect、promqlExpandSeries、eval（promql/engine.go:1273、1578、1641、2683） |

span 属性：result_series（api.go:619）、samples_scraped（scrape.go:1703）、request_size/exemplars/histograms（queue_manager.go:2810-2821）、num_series（engine.go:1650）等。

### 1.6 上下文证据摘要（日志平面）

| 证据 | 数量 | 占比 | 对方案的影响 |
|------|------|------|-------------|
| in_loop（循环内） | 271 | 33.9%（占全部 800） | 禁止做频率告警 |
| in_except（异常分支） | 1 | 0.1% | Go 的 `if err != nil` 不算异常分支，错误语义靠 conditional 字段 |
| once（仅一次） | 0 | 0% | 无存在性告警特例 |
| conditional（条件分支） | 531 | 66.4%（占全部 800） | 大量错误日志在 `if err != nil` 内，是正常错误路径而非特性开关 |
| is_assertion（断言） | 232 | 29.0%（占全部 800） | 单列，不进日志告警 |

### 1.7 暴露端点与默认开关（每项两处源码证据）

| 能力 | 开关 | 默认值 | 注册代码证据 | 配置默认值证据 |
|------|------|--------|--------------|----------------|
| `/metrics` 自监控指标 | 无开关，随 web 服务始终注册 | 默认开 | web/web.go:506 `router.Get("/metrics", promhttp.Handler().ServeHTTP)` | cmd/prometheus/main.go:442-443（web 服务默认监听） |
| `/-/healthy`（存活） | 无开关 | 默认开 | web/web.go:612,616（GET/HEAD） | 同上 |
| `/-/ready`（就绪） | 无开关 | 默认开 | web/web.go:619,623（GET/HEAD，受 readyf 门控） | 同上；ready 状态源 web/web.go:180 `prometheus_ready` Gauge |
| `/-/reload`（HTTP 热加载） | `--web.enable-lifecycle` | **false** | web/web.go:585-589（受 EnableLifecycle 分支） | cmd/prometheus/main.go:480-481 `Default("false")` |
| Admin API（删除 series/snapshot） | `--web.enable-admin-api` | **false** | web/web.go:449-450（features.API 记录） | cmd/prometheus/main.go:483-484 `Default("false")` |
| OTLP 接收端点 | `--web.enable-otlp-receiver` | **false** | web/web.go:450 | cmd/prometheus/main.go:495-496 `Default("false")` |
| OTel 链路导出 | 配置文件 `tracing_config.endpoint` | **空 = 禁用** | tracing/tracing.go:64-92（ApplyConfig：endpoint 为空则装 noop TracerProvider） | tracing/tracing.go:86 注释 "If no endpoint is set, assume tracing should be disabled" |
| Agent 模式 | `--agent` | false | cmd/prometheus/main.go:951（mode 分支） | cmd/prometheus/main.go:671 `BoolVar(&agentMode)` |
| 实验特性 | `--enable-feature`（逗号分隔：exemplar-storage、otlp-native-delta-ingestion、native-histograms 等 26 项） | 空 | cmd/prometheus/main.go:236-375（setFeatureListOptions 分支） | cmd/prometheus/main.go:668（flag 定义列出全部合法值） |
| 日志格式/级别 | `--log.format`（logfmt 默认）/ `--log.level`（info 默认） | logfmt / info | cmd/prometheus/main.go:693-694（promslog.New） | cmd/prometheus/main.go:417 `promslogConfig: promslog.Config{}`（零值默认） |
| Web 监听地址 | `--web.listen-address` | 0.0.0.0:9090 | web/web.go Listeners() | cmd/prometheus/main.go:442-443 `Default("0.0.0.0:9090")` |

**方案第一优先级**：`/metrics`、`/-/healthy`、`/-/ready` 默认开启无需操作；OTel trace 与 OTLP receiver 默认关闭，若要启用必须显式配置并验证数据流（见第 6.4、7.1 节）。

## 2. 日志埋点明细（按语义分类）

模板均为结构化键值对（slog），变量在键值参数中而非模板内，模板本身可直接做检索关键字（占位符风格 `none`=759 条的原因）。以下列监控相关代表条目；DEBUG 类 78 条不进告警，略。

### 2.1 生命周期类 (lifecycle)

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| cmd/prometheus/main.go:954 | INFO | `Starting {}` | — | 进程启动 |
| cmd/prometheus/main.go:1532 | INFO | `Server is ready to receive web requests.` | 就绪判据 | 全部组件启动完成后 |
| cmd/prometheus/main.go:1547 / 1571 | INFO | `Starting TSDB ...` / `TSDB started` | — | TSDB 打开 |
| cmd/prometheus/main.go:1609 / 1633 | INFO | `Starting WAL storage ...` / `Agent WAL storage started` | agent 模式 | Agent 模式启动 |
| cmd/prometheus/main.go:1770 / 1812 | INFO | `Loading configuration file` / `Completed loading of configuration file` | — | 启动与热加载 |
| cmd/prometheus/main.go:1459 | ERROR | `Error reloading config` | in_loop（reload 循环） | 配置重载失败 |
| cmd/prometheus/main.go:789 | ERROR | `Error loading config (--config.file=%s)` | conditional | 启动期配置加载失败 |
| cmd/prometheus/main.go:1306 | ERROR | `Unable to start web listener` | conditional | 端口被占/TLS 配错 |
| cmd/prometheus/main.go:1704 | ERROR | `Fatal error` | — | 主循环致命错误 |
| cmd/prometheus/main.go:1327 / 1330 | WARNING | `Received an OS signal, exiting gracefully...` / `Received termination request via web service, exiting gracefully...` | — | 优雅退出触发 |
| cmd/prometheus/main.go:1566 | WARNING | `This filesystem is not supported and may lead to data corruption and data loss. ...` | conditional | 非本地/NFS 文件系统 |

### 2.2 请求/事务类 (request)

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| web/api/v1/api.go:2816 / 2824 / 2854 | ERROR | `error marshaling response` / `error writing response` / `error marshaling json response` | — | API 响应编码/写出失败 |
| web/federate.go:271 / 275 | ERROR/WARNING | `federation failed` / `Ignoring nameless metric during federation` | — | federation 出口失败 |
| storage/remote/read_handler.go:183 / 263 | WARNING | `Warnings on remote read query` / `Warnings on chunked remote read query` | — | remote read 带警告返回 |

### 2.3 下游依赖类 (dependency)——抓取目标、服务发现、Alertmanager、远端存储

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| discovery/refresh/refresh.go:80 | ERROR | `Unable to refresh target groups` | in_loop | SD 刷新失败（通用） |
| discovery/dns/dns.go:348 | WARNING | `DNS resolution failed` | conditional | DNS 解析失败 |
| discovery/kubernetes/endpoints.go:121 等（k8s 包合计 45 条） | ERROR | `converting to Service object failed` / `... informer unable to sync cache` 等 | — | K8s API 对象转换/同步失败 |
| discovery/aws/ecs.go:379 等 | ERROR | `Failed to describe clusters/services/tasks...` | in_loop | AWS SD API 调用失败 |
| discovery/consul/consul.go:291 / 392 | ERROR | `Error retrieving datacenter name` / `Error refreshing service list` | — | Consul SD 失败 |
| notifier/manager.go:250 | ERROR | `couldn't sync alert manager set` | in_loop | Alertmanager 发现集合同步失败 |
| notifier/sendloop.go:239 | ERROR | `Error sending alerts` | conditional | 告警投递失败 |
| notifier/sendloop.go:97 / 89 | WARNING | `Alert notification queue full, dropping alerts` / `Alert batch larger than queue capacity, dropping alerts` | in_loop | **告警队列满，通知丢失** |
| scrape/manager.go:290 / 297 / 454 | ERROR | `error reloading target set` / `error creating new scrape pool` / `error reloading scrape pool` | — | 抓取池建立/重载失败 |
| storage/remote/queue_manager.go:2173 / 648 | ERROR | `non-recoverable error` / `non-recoverable error while sending metadata` | — | remote write 不可恢复错误（分片将重启） |
| storage/remote/queue_manager.go:2542 | WARNING | `Failed to send batch, retrying` | in_loop | remote write 批次失败重试（可恢复） |
| storage/remote/write_handler.go:112 / 137 | ERROR | `Error decoding v1 remote write request` / `Error decoding v2 remote write request` | — | 接收端解码失败 |

### 2.4 资源指标类 (resource)

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| tsdb/head_append.go:2254 | ERROR | `Too many OOO chunks, dropping data` | conditional | OOO 缓存耗尽，**丢数据** |
| cmd/prometheus/main.go:905 | WARNING | `storage.tsdb.retention.size is ignored, ...` | — | 保留配置冲突 |
| tsdb/db.go:1637 | WARNING | `Head compaction took longer than the block time range, compactions are falling behind and ...` | — | compaction 落后于数据生成速度 |
| cmd/prometheus/main.go:956 | WARNING | `This Prometheus binary has not been compiled for a 64-bit architecture. ...` | — | 32 位构建内存受限 |

### 2.5 业务逻辑异常 (business_error)——对 Prometheus 而言是「配置/规则/接收数据不合法」

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| rules/group.go:548 | WARNING | `Evaluating rule failed` | in_loop | 单条规则评估失败 |
| rules/manager.go:286 | ERROR | `loading groups failed` | — | 规则文件加载失败 |
| rules/group.go:628 / 631 / 634 | WARNING | `Error on ingesting out-of-order result ...` / `too old result` / `different value but same timestamp` | in_loop | 规则结果写入被拒 |
| storage/remote/write_handler.go:183 / 187 | WARNING | `Invalid metric names or labels` / `Invalid labels for series.` | — | 接收端拒绝非法样本 |
| storage/remote/write_handler.go:242 / 264 | ERROR | `Out of order sample from remote write` / `Out of order histogram from remote write` | conditional | 接收到乱序样本 |
| tsdb/record/record.go:610 | WARNING | `skipping histogram with unknown schema in WAL record` | in_loop | WAL 中未知 schema 直方图被跳过 |
| config/config.go:103 | WARNING | `Empty environment variable` | — | 配置环境变量为空 |
| cmd/prometheus/main.go:848 | WARNING | `Failed to set GOMAXPROCS automatically` | — | 容器 CPU 配额探测失败 |

### 2.6 系统异常 (system_error)——存储引擎与 IO

| 文件:行 | 级别 | 模板 | 附加标签 | 触发条件 |
|---------|------|------|----------|----------|
| tsdb/wlog/wlog.go:413 | WARNING | `Starting corruption repair` | — | **WAL 损坏，进入修复** |
| tsdb/wlog/wlog.go:421 / 443 | WARNING | `Deleting all segments newer than corrupted segment` / `Rewrite corrupted segment` | — | 修复中**丢弃损坏段之后的数据** |
| tsdb/db.go:1239 / tsdb/agent/db.go:370 | WARNING | `Encountered WBL read error, attempting repair` / `encountered WAL read error, attempting repair` | — | 读损坏自动修复 |
| tsdb/db.go:827 / 2202 | WARNING | `Overlapping blocks found during opening` / `during reloadBlocks` | — | 块重叠（数据完整性问题） |
| tsdb/db.go:1350 | ERROR | `compaction failed` | conditional | 压缩失败 |
| tsdb/db.go:1338 | ERROR | `immediate stale series compaction failed` | conditional | 陈旧序列清理失败 |
| tsdb/head.go:850 / 1151 | ERROR | `Loading on-disk chunks failed` / `Loading on-disk chunks failed, discarding chunk files completely` | — | chunk 加载失败（后者**丢弃全部 chunk 文件**） |
| tsdb/head.go:902 | ERROR | `Fast startup: WAL scan failed, skipping fast startup` | — | 回退慢启动 |
| tsdb/wlog/watcher.go:288 | ERROR | `error tailing WAL` | conditional | WAL 尾随失败（远程读跟随者） |
| tsdb/wlog/watcher.go:383 / 385 | WARNING | `Ignoring error reading to end of segment, may have dropped data` / `Expected to have read whole segment, may have dropped data` | — | **明确提示可能丢数据** |
| tsdb/compact.go:645 | ERROR | `Failed to write 'Deletable' to meta file after compaction` | conditional | 压缩标记落盘失败 |
| promql/engine.go:1767 | ERROR | `runtime panic during query evaluation` | — | 查询期间 runtime panic（被恢复） |
| promql/query_logger.go:94-161（6 条） | ERROR | `Failed to open/read query log file` / `Failed to mmap` 等 | — | query log 初始化/写入失败 |
| tsdb/head_wal.go:2033-2046（3 条） | ERROR | `Failed to create temp series state file` 等 | — | 关机快照失败（影响快速启动） |
| util/treecache/treecache.go:146 / 180 / 196 | ERROR | `Error during initial read of Zookeeper` / `during processing of Zookeeper event` / `during Zookeeper resync` | — | ZK 依赖异常 |

### 2.7 高频/白名单候选（不直接告警）

| 文件:行 | 级别 | 模板 | 处理 |
|---------|------|------|------|
| cmd/prometheus/main.go:250-369（约 8 条） | WARNING | `This option for --enable-feature is deprecated/no-op/being phased out...` | 配置提示，忽略；用于升级检查 |
| discovery 各包 `Skipping invalid...` 类 | WARNING | SD 源数据异常跳过 | P3 趋势统计 |
| tsdb/db.go:795 / head.go:922 / agent/db.go:441 | WARNING | `Error while closing the wal segments reader` / `Closing block failed` | 关闭路径噪音，白名单 |

## 3. 指标清单

### 3.1 核心已定义指标（暴露名，均带 `prometheus_` 前缀）

| 指标名（暴露名） | 类型 | 语义角色 | 标签 | 源码位置 | 有写入点 | 备注 |
|--------|------|----------|------|----------|---------|------|
| prometheus_engine_query_duration_seconds | Summary | RED.Duration | slice（inner/evaluate/prepare 等） | promql/engine.go:573-581 | 是 | 客户端分位 0.5/0.9/0.99，**不可跨实例聚合分位** |
| prometheus_engine_query_duration_histogram_seconds | Histogram | RED.Duration | slice | promql/engine.go:583-594 | 是 | 桶 .01/.1/1/10 + 原生直方图；分位告警用这个 |
| prometheus_engine_queries | Gauge | USE.Saturation | — | promql/engine.go:597-602 | 是 | 当前并发查询数 |
| prometheus_http_requests_total | Counter | RED.Rate | code/handler | web/web.go（instrumented） | 是 | API 请求计数 |
| prometheus_http_request_duration_seconds | Histogram | RED.Duration | handler | web/instrumented handler | 是 | API 延迟 |
| prometheus_ready | Gauge | 健康枚举 | — | web/web.go:180 | 是 | 1=就绪 |
| prometheus_config_last_reload_successful | Gauge | 健康枚举 | — | cmd/prometheus/main.go:135 | 是 | 1=最近一次配置加载成功 |
| prometheus_config_last_reload_success_timestamp_seconds | Gauge | 健康枚举 | — | cmd/prometheus/main.go:135 | 是 | 配置加载时间 |
| prometheus_tsdb_head_series | Gauge | USE.Utilization | — | tsdb/head.go:494 | 是 | head 内存中活跃序列数（内存主驱动） |
| prometheus_tsdb_head_chunks | Gauge | USE.Utilization | — | tsdb/head.go:538 | 是 | head chunk 数 |
| prometheus_tsdb_lowest_timestamp | Gauge | 容量 | — | tsdb/db.go:449 | 是 | 最早数据时间 |
| prometheus_tsdb_out_of_order_samples_total | Counter | RED.Errors | type | tsdb/head.go:578 | 是 | 乱序样本累计 |
| prometheus_tsdb_head_out_of_order_samples_appended_total | Counter | RED.Errors | — | tsdb/head.go:570 | 是 | 实际接收的乱序样本 |
| prometheus_tsdb_wal_corruptions_total | Counter | RED.Errors | — | tsdb/head.go:558 | 是 | WAL 损坏计数 |
| prometheus_tsdb_compactions_failed_total | Counter | RED.Errors | — | tsdb/db.go:437 | 是 | 压缩失败计数 |
| prometheus_tsdb_head_truncations_failed_total | Counter | RED.Errors | — | tsdb/head.go:586 | 是 | head 截断失败 |
| prometheus_tsdb_retention_limit_seconds | Gauge | 配置水位 | — | tsdb/db.go:479 | 是 | 保留期上限 |
| prometheus_target_scrape_duration_seconds | Histogram | RED.Duration | interval 等 | scrape/metrics.go:256 | 是 | 每目标抓取耗时 |
| prometheus_target_interval_length_seconds | Summary | RED.Duration | interval | scrape/metrics.go:195 | 是 | 实际抓取间隔 |
| prometheus_target_scrapes_sample_out_of_order_total | Counter | RED.Errors | — | scrape/metrics.go:226 | 是 | 抓取样本乱序拒绝 |
| prometheus_target_scrapes_exemplar_out_of_order_total | Counter | RED.Errors | — | scrape/metrics.go:250 | 是 | exemplar 乱序拒绝 |
| prometheus_target_scrape_pools_total / failed | Counter | RED.Rate/Errors | type | scrape/metrics.go | 是 | 抓取池计数 |
| prometheus_sd_*_failures_total / refresh（约 24 个） | Counter/Gauge | RED.Errors | name | discovery/manager.go:99,120 及各 SD 包 | 是 | 服务发现失败 |
| prometheus_rule_evaluations_total | Counter | RED.Rate | rule_group | rules/group.go:994（namespace="prometheus"） | 是 | 规则评估计数 |
| prometheus_rule_evaluation_failures_total | Counter | RED.Errors | rule_group | rules/group.go:1002 | 是 | 规则评估失败 |
| prometheus_rule_group_iterations_missed_total | Counter | RED.Errors | rule_group | rules/group.go:978 | 是 | **错过的评估迭代（调度落后）** |
| prometheus_rule_group_duration_seconds | Summary | RED.Duration | rule_group | rules/group.go:963 | 是 | 规则组耗时 |
| prometheus_rule_group_duration_histogram_seconds | Histogram | RED.Duration | rule_group | rules/group.go:969 | 是 | 分位告警用 |
| prometheus_notifications_alertmanagers_discovered | Gauge | 依赖状态 | — | notifier/manager.go | 是 | 发现的 AM 数（0 = 断联） |
| prometheus_notifications_errors_total | Counter | RED.Errors | — | notifier/manager.go | 是 | 告警投递失败 |
| prometheus_notifications_queue_length / capacity | Gauge | USE.Saturation | — | notifier/manager.go | 是 | 通知队列水位（对应 sendloop.go:97 丢告警日志） |
| prometheus_remote_storage_samples_total / failed_total / retries_total | Counter | RED.Rate/Errors | remote 等 | storage/remote/queue_manager.go:175 附近 | 是 | remote write 成功/失败/重试 |
| prometheus_remote_storage_shards | Gauge | USE.Saturation | — | storage/remote/queue_manager.go:316 | 是 | 分片数（自动 reshard） |
| prometheus_remote_storage_queue_length / highest_sent_timestamp_seconds | Gauge | USE.Saturation/滞后 | remote | storage/remote/queue_manager.go | 是 | 队列深度与发送滞后 |
| prometheus_remote_read_handler_queries / gate_wait_duration_seconds | Gauge/Histogram | USE | — | storage/remote/read_handler.go | 是 | remote read 并发与门控等待 |
| prometheus_agent_out_of_order_samples_total | Counter | RED.Errors | — | tsdb/agent/db.go:171 | 是 | agent 模式乱序 |
| prometheus_otlp_out_of_order_exemplars_total | Counter | RED.Errors | — | storage/remote/write_otlp_handler.go:252 | 是 | OTLP 接收乱序 exemplar（需 otlp receiver 开启） |
| prometheus_notification_active_subscribers / updates_sent_total / updates_dropped_total | Gauge/Counter | RED | — | web（live notifications） | 是 | UI 实时订阅 |
| prometheus_tsdb_head_min_time / max_time | Gauge | 容量 | — | tsdb/head.go:691（GaugeFunc） | 是 | **条件定义**（见 3.2） |

### 3.2 抓取时合成的 per-target 样本（不是 client_golang 注册指标）

这类指标由 Prometheus 在每次抓取后为每个 target 合成并写回自身 TSDB，源码形态是 `reportSample` 结构而非构造器，因此脚本统计为 0 属预期：

| 指标名 | 类型 | 语义 | 源码位置 |
|--------|------|------|----------|
| up | Gauge | 目标抓取健康（1 成功 / 0 失败） | scrape/scrape.go:2295 |
| scrape_duration_seconds | Gauge | 最近一次抓取耗时 | scrape/scrape.go:2302-2309 |
| scrape_samples_scraped | Gauge | 最近一次抓取样本数 | scrape/scrape.go:2310-2317 |
| scrape_samples_post_metric_relabeling | Gauge | relabel 后样本数 | scrape/scrape.go:2318-2325 |

这是「self-scrape 自举」通道：`up{job="prometheus"}` 同时回答「Prometheus 活着吗」和「它自己能否被抓取」。

### 3.3 指标质量风险

| 风险 | 指标数 | 具体指标 | 影响 |
|------|--------|----------|------|
| 条件定义（可能未启用） | 8 | prometheus_tsdb_head_min_time/max_time、prometheus_tsdb_isolation_low/high_watermark、prometheus_tsdb_head_chunks_storage_size_bytes、query_rejected_total、query_series_touched、query_samples_read（query 前缀 3 个为 per-step-stats 特性指标） | 需确认注册条件（GaugeFunc 条件注册 / `--enable-feature=promql-per-step-stats`） |
| Summary 类型（分位不可跨实例聚合） | 18 | prometheus_engine_query_duration_seconds、prometheus_target_interval_length_seconds、prometheus_rule_group_duration_seconds | 多副本查询分位需改用对应 Histogram 版本 |
| Histogram 桶较少 | 1 类 | prometheus_engine_query_duration_histogram_seconds 桶 .01/.1/1/10 | 大于 10s 的查询全落 +Inf，P99 超过 10s 时失真 |
| 写入点误报 | 少量 | `s.enqueuedSamples.Inc()`（storage/remote/queue_manager.go:1707,1819）实为 `atomic.Int64` 非 prometheus 指标 | 统计口径噪音，不影响告警设计 |

### 3.4 指标暴露方式

| 方式 | 位置 | 说明 |
|------|------|------|
| HTTP 拉取 `/metrics` | web/web.go:506 | promhttp.Handler，与 API 同端口（默认 0.0.0.0:9090），文本协议，默认开启。验证：`curl -s localhost:9090/metrics \| head` |
| per-target 合成样本 | scrape/scrape.go:2295-2325 | 写入自身 TSDB，可通过 `/api/v1/query` 或再次抓取 `up` 类指标验证 |

## 4. 信号仲裁

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---------|---------|---------|---------|------|
| 1 | 进程活着吗 | 指标 + 日志 + 合成样本 | `up{job="prometheus"}`；`Server is ready to receive web requests.` | scrape/scrape.go:2295；cmd/prometheus/main.go:1532 | 已覆盖（三层互证） |
| 2 | 就绪/启动完成 | 指标 + 日志 + 端点 | prometheus_ready；`/-/ready`；`Server is ready...` | web/web.go:180；web/web.go:619；main.go:1532 | 已覆盖 |
| 3 | 启动失败原因 | 日志 | `Error loading config`、`Unable to start web listener`、`Fatal error` | main.go:789、1306、1704 | 已覆盖（P0 事件） |
| 4 | 配置热加载是否成功 | 指标 + 日志 | prometheus_config_last_reload_successful；`Error reloading config` | main.go:135、1459 | 已覆盖 |
| 5 | 查询错误率/延迟分位 | 指标 | prometheus_http_requests_total{code}、prometheus_engine_query_duration_histogram_seconds | web/web.go:506 注册链；promql/engine.go:583-594 | 已覆盖（用 Histogram 非 Summary） |
| 6 | 一次查询慢在哪一段 | 链路 + 指标标签 | promqlInstantQuery/promqlRangeQuery span + `slice` 标签分解（inner/prepare/evaluate） | web/api/v1/api.go:655,1255；promql/engine.go:573-594 | 部分覆盖：指标 slice 维度默认可用；span 需开 tracing（默认禁用） |
| 7 | 抓取目标健康 | 合成样本 + 指标 | up、scrape_duration_seconds、prometheus_target_scrape_duration_seconds | scrape/scrape.go:2295-2309；scrape/metrics.go:256 | 已覆盖 |
| 8 | 抓取失败率/乱序 | 指标 | prometheus_target_scrapes_sample_out_of_order_total、prometheus_tsdb_out_of_order_samples_total | scrape/metrics.go:226；tsdb/head.go:578 | 已覆盖 |
| 9 | SD 刷新失败 | 指标 + 日志 | prometheus_sd_*_failures_total；`Unable to refresh target groups` | discovery/manager.go:99,120；discovery/refresh/refresh.go:80 | 已覆盖 |
| 10 | 规则评估健康 | 指标 + 日志 | prometheus_rule_evaluation_failures_total、prometheus_rule_group_iterations_missed_total；`Evaluating rule failed` | rules/group.go:1002、978、548 | 已覆盖 |
| 11 | 告警能否送达 Alertmanager | 指标 + 日志 | prometheus_notifications_alertmanagers_discovered、prometheus_notifications_errors_total、queue_length/capacity；`Alert notification queue full, dropping alerts` | notifier/manager.go；notifier/sendloop.go:97 | 已覆盖 |
| 12 | remote write 积压/滞后/失败 | 指标 + 日志 | prometheus_remote_storage_samples_failed_total、shards、queue_length、highest_sent_timestamp_seconds；`non-recoverable error` | storage/remote/queue_manager.go:175、316、2173 | 已覆盖 |
| 13 | TSDB 数据完整性（WAL 损坏/块重叠/丢数据） | 指标 + 日志 | prometheus_tsdb_wal_corruptions_total、compactions_failed_total；`Starting corruption repair`、`may have dropped data`、`Overlapping blocks found` | tsdb/head.go:558；tsdb/wlog/wlog.go:413；tsdb/wlog/watcher.go:383；tsdb/db.go:827 | 已覆盖（**自监控核心价值点**） |
| 14 | 内存/容量压力 | 指标 + 日志 | prometheus_tsdb_head_series、head_chunks、retention_limit_seconds；`compactions are falling behind` | tsdb/head.go:494,538；tsdb/db.go:479,1637 | 已覆盖（head_series 上升趋势 + compaction 落后日志） |
| 15 | Go 运行时（GC/线程/内存） | 指标 | GoCollector 默认注册（go_goroutines、go_memstats_* 等） | web/web.go:506 promhttp 默认 handler 自带 | 已覆盖 |
| 16 | 宿主机/磁盘硬件层 | 指标 | 三平面均无（已检索 262 个指标名无 node/disk/filesystem 类，568 条日志无相关埋点，23 个 span 无相关） | — | **真缺口：需外部 exporter（node_exporter 等），不改源码**；磁盘满会先表现为 WAL 写失败日志与抓取 up 断档 |
| 17 | 各 SD 源发现的目标数趋势 | 指标 | prometheus_sd_discovered_targets（discovery/manager.go） | discovery/manager.go | 已覆盖 |

### 4.1 三平面检查记录（#16 真缺口结论的依据）

| 平面 | 已检查内容 | 结果 |
|------|-----------|------|
| 日志 | 568 条实际日志（去重 496 模板），关键词：disk/filesystem/space/no space/device | 无宿主磁盘水位埋点（仅 main.go:1566 文件系统类型警告） |
| 指标 | 262 个唯一指标名，关键词：disk/filesystem/space/storage_size | 仅 prometheus_tsdb_head_chunks_storage_size_bytes（head 目录，条件注册），无宿主层 |
| 链路 | 23 个 span 名 + 15 处属性 | 无相关 |

## 5. 关键字库

### 5.1 告警关键字（事件类，模板无占位符可直接全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|--------|------|------|------|
| `Server is ready to receive web requests.` | 就绪基准 | 启动成功判据 | cmd/prometheus/main.go:1532 |
| `Fatal error` | P0 | 主循环致命错误 | cmd/prometheus/main.go:1704 |
| `Unable to start web listener` | P0 | 起不来 | cmd/prometheus/main.go:1306 |
| `Error loading config` | P0 | 配置加载失败 | cmd/prometheus/main.go:789 |
| `Error reloading config` | P1 | 热加载失败 | cmd/prometheus/main.go:1459 |
| `Starting corruption repair` | P1 | WAL 损坏修复 | tsdb/wlog/wlog.go:413 |
| `may have dropped data` | P1 | 明确的丢数据提示 | tsdb/wlog/watcher.go:383,385 |
| `Overlapping blocks found` | P1 | 块重叠 | tsdb/db.go:827,2202 |
| `Too many OOO chunks, dropping data` | P1 | OOO 丢数据 | tsdb/head_append.go:2254 |
| `discarding chunk files completely` | P1 | 丢弃全部 chunk | tsdb/head.go:1151 |
| `compactions are falling behind` | P1 | 压缩落后 | tsdb/db.go:1637 |
| `non-recoverable error` | P1 | remote write 分片重启 | storage/remote/queue_manager.go:2173 |
| `Alert notification queue full, dropping alerts` | P1 | 告警通知丢失 | notifier/sendloop.go:97 |
| `couldn't sync alert manager set` | P2 | AM 发现失败 | notifier/manager.go:250 |
| `runtime panic during query evaluation` | P2 | 查询 panic（已恢复） | promql/engine.go:1767 |
| `compaction failed` | P2 | 压缩失败 | tsdb/db.go:1350 |

### 5.2 结构化字段（不可全文匹配，按字段统计）

| 字段 | 所在模板/日志 | 统计方式 | 维度 |
|------|---------|---------|------|
| err | 全部 ERROR/WARN 键值对（slog 结构化） | 按 err 值归类计数 | 组件/包 |
| file | `Error loading config (--config.file=%s)` | 按文件分组 | 配置文件 |
| option | `Unknown option for --enable-feature`（main.go:369） | 按值分组计数 | 特性名 |

> slog 格式下模板与变量分离，logfmt 解析后按字段聚合即可；不要把 `err="..."` 的具体值写进关键字。

### 5.3 降噪白名单

| 关键字/条件 | 原因 | 处理 |
|------------|------|------|
| `This option for --enable-feature is deprecated/no-op/being phased out` | 配置兼容提示 | 忽略；用于升级评审 |
| `Failed to send batch, retrying`（queue_manager.go:2542） | 可恢复重试，有 prometheus_remote_storage_retries_total 指标承接 | 日志不告警，走指标 |
| `Out of order sample from remote write`（write_handler.go:242） | 预期内拒绝路径，有 out_of_order 指标承接 | 指标趋势告警 |
| `skipping histogram with unknown schema`（record.go:610） | in_loop 高频 | 白名单 |
| SD 包 `Skipping invalid...` | 源数据问题非本进程故障 | P3 统计 |
| `Error while closing the wal segments reader` / `Closing block failed` | 关闭路径噪音 | 白名单 |
| `checkpoint already exists`（tsdb/agent/checkpoint.go:98） | agent 重启正常路径 | 白名单 |
| UI 组件日志（web/ui 下 6 条 js/tsx） | 浏览器端，不进服务端日志管道 | 排除 |
| `A lockfile from a previous execution already existed`（tsdbutil/dir_locker.go:75） | 异常退出后重启的正常恢复 | 白名单 |

### 5.4 检索语句示例

```logql
# 系统错误事件（排除白名单）
sum(rate({job="prometheus"} |~ "Starting corruption repair|may have dropped data|Overlapping blocks found|compactions are falling behind" [5m]))

# remote write 不可恢复错误
sum(rate({job="prometheus"} |= "non-recoverable error" [5m]))
```

```promql
# 查询 P99（Histogram 版本；不要用 Summary 版本跨实例聚合）
histogram_quantile(0.99, sum(rate(prometheus_engine_query_duration_histogram_seconds_bucket[5m])) by (le, slice))

# 配置加载失败
prometheus_config_last_reload_successful == 0

# remote write 滞后（秒）
time() - max(prometheus_remote_storage_highest_sent_timestamp_seconds) by (remote)
```

## 6. 告警规则草稿（候选阈值，全部待 SLO/压测/线上数据校准）

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|--------|------|---------|------|------|--------|
| PrometheusSelfDown | `up{job="prometheus"} == 0` 或 absent(up{job="prometheus"}) | 0 持续 2m | P0 | 按 instance 去重 | 计划内重启窗口 |
| PrometheusNotReady | `prometheus_ready == 0` 且进程存活 | 0 持续 5m | P0 | 与启动耗时联动（启动期 2m 内不判） | 计划内重启 |
| ConfigReloadFailed | `prometheus_config_last_reload_successful == 0` | ==0 持续 5m | P1 | 与 `Error reloading config` 日志联动 | 人工修正期间 |
| QueryP99High | `histogram_quantile(0.99, sum(rate(prometheus_engine_query_duration_histogram_seconds_bucket[5m])) by (le, slice)) > 10` | >10s（候选；注意最大桶 10s，超出落 +Inf） | P2 | 按 slice 分组；持续 10m | 大查询维护窗口 |
| HeadSeriesGrowth | `deriv(prometheus_tsdb_head_series[1h]) > 0 持续 6h` 且 `prometheus_tsdb_head_series > <基线×2>` | 相对基线 2 倍（候选） | P2 | 只做趋势不做绝对值 | 迁移/合并任务期 |
| WALCorruptions | `increase(prometheus_tsdb_wal_corruptions_total[1h]) > 0` | >0 | P1 | 按实例 | 无（出现即异常） |
| CompactionFailing | `increase(prometheus_tsdb_compactions_failed_total[1h]) > 0` | >0 | P1 | 按实例 | 磁盘已满告警去重 |
| HeadTruncationFailed | `increase(prometheus_tsdb_head_truncations_failed_total[1h]) > 0` | >0 | P1 | 按实例 | 无 |
| RuleEvalFailures | `sum(increase(prometheus_rule_evaluation_failures_total[5m])) by (rule_group) / sum(increase(prometheus_rule_evaluations_total[5m])) by (rule_group) > 0.05` | >5%（候选，0~1 量纲） | P2 | 按 rule_group；持续 15m | 已知缺失数据源的规则组 |
| RuleIterationsMissed | `increase(prometheus_rule_group_iterations_missed_total[10m]) > 0` | >0 | P2 | 按组；持续 20m | 规则组刚变更后 5m |
| ScrapeP99Slow | `histogram_quantile(0.95, sum(rate(prometheus_target_scrape_duration_seconds_bucket[5m])) by (le)) > 0.8×<scrape_interval>` | 0.8 倍抓取间隔（候选） | P2 | 按 job 分组 | 大目标 job 单列 |
| SDFailuresRising | `sum(increase(prometheus_sd_failures_total[10m])) by (name) > 0` 且非零目标数下降 | 相对基线 5 倍（候选） | P2 | 按 SD 名称 | SD 源计划内维护 |
| AlertmanagerDiscovered | `prometheus_notifications_alertmanagers_discovered < <预期副本数>` | <1 持续 10m（候选） | P1 | 按 cluster | AM 滚动升级 |
| NotificationQueueFull | `prometheus_notifications_queue_length / prometheus_notifications_queue_capacity > 0.8` | >0.8（0~1 量纲，候选） | P1 | 持续 10m | AM 不可用已单独告警时降噪 |
| RemoteWriteLag | `time() - max(prometheus_remote_storage_highest_sent_timestamp_seconds) by (remote) > 120` | >120s（候选） | P1 | 按 remote/name；持续 5m | 远端计划内停写 |
| RemoteWriteFailures | `sum(increase(prometheus_remote_storage_samples_failed_total[5m])) by (remote) / sum(increase(prometheus_remote_storage_samples_total[5m])) by (remote) > 0.05` | >5%（候选，0~1 量纲） | P1 | 按 remote | 远端限流返回 429 短时波动 |
| RemoteShardsMaxed | `prometheus_remote_storage_shards >= <配置上限>` 持续 30m | 达到上限（候选） | P2 | 按 remote | 已知流量洪峰窗口 |
| APIServerErrors | `sum(rate(prometheus_http_requests_total{code=~"5.."}[5m])) by (handler) / sum(rate(prometheus_http_requests_total[5m])) by (handler) > 0.02` | >2%（候选，0~1 量纲） | P2 | 按 handler；排除 /metrics 自抓取 | federation 大范围拉取 |
| SelfScrapeAbsent | `absent(up{job="prometheus", instance="<自身>"})` | 5m | P0 | — | 无 |

### 6.2 日志事件告警（L2）

| 告警名 | 条件 | 等级 | 降噪 | 源码 |
|--------|------|------|------|------|
| PrometheusFatal | 出现 `Fatal error` 或进程在无 SIGTERM 下消失（`Received an OS signal` 缺失 + up 断档） | P0 | 按实例去重；崩溃时无日志属预期，需配合日志断档判定（232 条 panic 断言不落盘） | main.go:1704 |
| StartFailed | 启动后 120s 内未出现 `Server is ready to receive web requests.` 或出现 `Error loading config`/`Unable to start web listener` | P0 | 只在重启事件后判定 | main.go:1532,789,1306 |
| WALRepairStarted | 出现 `Starting corruption repair` | P1 | 按实例去重，1h 内不重复 | tsdb/wlog/wlog.go:413 |
| DataLossWarning | 出现 `may have dropped data` / `discarding chunk files completely` / `Too many OOO chunks, dropping data` | P1 | 按实例+类型去重 | watcher.go:383,385；head.go:1151；head_append.go:2254 |
| OverlappingBlocks | 出现 `Overlapping blocks found` | P1 | 按实例去重 | tsdb/db.go:827,2202 |
| CompactionFallingBehind | 出现 `compactions are falling behind` | P1 | 按实例；与 CompactionFailing 指标联动 | tsdb/db.go:1637 |
| RemoteWriteNonRecoverable | 出现 `non-recoverable error` | P1 | 按 remote 去 | queue_manager.go:2173,648 |
| AlertsDropped | 出现 `Alert notification queue full, dropping alerts` | P1 | 与 NotificationQueueFull 指标联动，二者同源取一 | sendloop.go:97 |

### 6.3 日志频率告警（L3，谨慎——仅无指标承接的项）

| 告警名 | 条件 | 基线来源 | 等级 | 白名单 |
|--------|------|---------|------|--------|
| RuleEvalErrorSpike | `sum(rate({job="prometheus"} \|= "Evaluating rule failed" [10m])) > 5×近 7 天同时段基线` | 相对基线（候选） | P3 | 已有 RuleEvalFailures 指标时用指标版，日志版仅作补充 |
| SDSyncCacheErrorSpike | `sum(rate({job="prometheus"} \|~ "informer unable to sync cache" [10m])) > 5×基线` | 相对基线（候选） | P3 | K8s API 计划内重启 |

注：`Failed to send batch, retrying`（in_loop）与各类 out-of-order 拒绝日志全部由指标承接，禁止做日志频率告警（SKILL.md 强约束 6）。

### 6.4 链路告警（L4，可选——**前提是先开启 tracing**）

开关：配置文件 `tracing_config.endpoint` 非空（tracing/tracing.go:86-92，默认空 = noop）。未开启前本节规则空转。

| 告警名 | span | 条件 | 等级 |
|--------|------|------|------|
| ScrapePipelineSlow | scrapeRead / scrapeAppend（scrape/scrape.go:1640,1679） | span P95 时长环比升 >50% 持续 15m（候选） | P2 |
| RemoteSendSlow | Remote Send Batch（queue_manager.go:2809） | span P95 > 5s 持续 15m（候选） | P2 |
| RuleEvalSlow | rule group（rules/manager.go:93） | span P95 接近 group interval（候选） | P2 |

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，见 4.1 检查记录）

| 场景 | 风险 | 建议做法 | 优先级 |
|------|------|---------|--------|
| 宿主磁盘容量/水位 | 磁盘满是 TSDB 故障的头号诱因，源码内只有失败后的 ERROR（`compaction failed` 等），无水位预警 | 接入 node_exporter（或容器编排层指标），对挂载 TSDB 的分区做阈值告警；不改源码 | P1 |
| 宿主 CPU/内存/网络 | OOM kill 时进程直接消失，无任何日志 | 同上，平台层采集 | P1 |
| 集群内多实例的 bronze 一致性 | 单机系统，无副本状态信号（属架构特性，非埋点缺失） | 靠对象存储层（若用 Thanos/Mimir 下游）监控 | P3（记录即可） |

### 7.2 已有但不可用（需先打开/确认）

| 信号 | 问题 | 建议 |
|------|------|------|
| 全部 23 个 span（promqlInstantQuery、scrape 链等） | tracing 默认禁用：`tracing_config.endpoint` 为空即 noop | 在 prometheus.yml 配置 endpoint 与 sampler，验证 Tempo/Jaeger 收到数据后再启用 6.4 规则 |
| prometheus_otlp_out_of_order_exemplars_total | 需 `--web.enable-otlp-receiver`（默认 false） | 启用 OTLP 接收后纳入 8 号需求 |
| prometheus_tsdb_head_min_time/max_time、isolation_watermark、head_chunks_storage_size_bytes | 条件定义，注册条件需人工确认 | 上线前 curl /metrics 确认存在，不存在则不建告警 |
| query_rejected_total 等 3 个 query 指标 | 需 `--enable-feature=promql-per-step-stats` | 同上 |
| Summary 类分位（engine/rule group/target_interval 的 *_seconds） | 分位不可跨实例聚合 | 告警一律改用对应 *_histogram_seconds 版本 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|------|------|------|
| panic 断言 232 条失败不落盘 | 全仓库（如 cmd/prometheus/main.go:2137） | 崩溃归因依赖 Go runtime stderr + 日志断档告警（6.2 PrometheusFatal），不要找日志关键字 |
| `Evaluating rule failed` 在循环内高频（rules/group.go:548，in_loop） | 同左 | 保持白名单，用指标承接 |
| WAL 修复类日志级别为 WARNING 但语义严重（wlog.go:413-443） | 同左 | 监控侧按 P1 事件处理，级别仅作参考 |

### 7.4 需人工确认（脚本未能确定）

| 项 | 原因 |
|----|------|
| 合成样本 up/scrape_* 的 relabel 行为 | 非构造器注册（scrape/scrape.go:2295-2325 reportSample），抓取侧配置影响其标签 |
| 8 个条件定义指标的注册分支 | 需人工确认运行时注册条件 |
| TS/JS UI 层 6 条日志 | 浏览器端信号，是否采集由前端监控体系决定 |

## 8. 版本迭代

### 8.1 标准流程

```bash
cd /Users/rizhiyi/Downloads/gitdir/source-log-to-monitor/scripts
python3 extract_loggers.py <新版源码> --output /tmp/sltm-agent-prometheus/loggers_v2.json
python3 extract_metrics.py <新版源码> --output /tmp/sltm-agent-prometheus/metrics_v2.json
python3 extract_traces.py  <新版源码> --output /tmp/sltm-agent-prometheus/traces_v2.json

python3 diff_signals.py /tmp/sltm-agent-prometheus/loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py /tmp/sltm-agent-prometheus/metrics.json metrics_v2.json --output diff_metrics.json
python3 diff_signals.py /tmp/sltm-agent-prometheus/traces.json   traces_v2.json   --output diff_traces.json
```

### 8.2 变化处理矩阵（Prometheus 特有项加粗）

| 变化类型 | 对监控的影响 | 动作 |
|---------|-------------|------|
| 新增 ERROR/WARN 日志 | 新故障模式 | 评估新增 L2 告警；**注意 --enable-feature 废弃类 WARN 应白名单** |
| 新增 prometheus_* 指标 | 新的可观测能力 | 评估新增 L1 告警与看板 |
| 删除指标 | 对应告警静默失效 | **必须下线对应告警**（Prometheus 版本升级频繁删除/重命名自监控指标） |
| 指标标签变更 | PromQL 静默失效 | 更新查询与看板 |
| Histogram 桶变更 | 分位精度变化 | 重新校准 6.1 阈值 |
| **--enable-feature 列表变化** | 特性开关增删 | 重新核对 7.2 的开关与指标存在性，优先级最高 |
| **web.* 开关默认值变化** | 端点暴露面变化 | 重新核对 1.7 表 |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配，忽略行号偏移；同文件内相同模板折叠，计数为近似值
- 指标名双重拼接伪影（如 `rule_evaluations_total_rule_evaluations_total`）需先归一化再 diff，否则 diff 数虚高

## 9. 局限性

1. **阈值为候选值**：6 章全部阈值需结合 SLO、压测基线、线上历史校准后才能上线。
2. **源码常量 ≠ 告警阈值**：如 `--web.read-timeout` 默认 5m（main.go:459-461）是业务配置；查询 P99 告警阈值须另行推导。
3. **提取覆盖率不是 100%**：
   - 指标：双重拼接伪影已人工归一化；`atomic.Int64` 被误报为写入点（queue_manager.go:1707,1819）已在 3.3 说明；8 个条件定义指标需人工确认
   - 日志：slog 封装调用（如 util/treecache/treecache.go:61 `zl.logger.Info(s, i...)` 动态模板）无法提取模板，覆盖率存在少量遗漏
   - 合成样本（up、scrape_*）不是构造器注册，脚本统计为 0 属预期，已按人工提取单列 3.2
4. **静态分析看不到运行时**：tracing endpoint、feature flags、agent 模式的实际生效组合取决于部署配置；条件定义指标是否注册需在目标环境 curl /metrics 验证。
5. **未覆盖的场景**：宿主机/磁盘/网络层指标（需 node_exporter）、Go runtime 指标的具体集合随版本变化、UI（TS/JS）端信号、业务语义判断需真实流量验证。
6. **脚本能力边界**：Go 正则后端函数名追踪为行级近似（`function` 字段可能为空）；`if err != nil` 不算异常分支（in_except 仅 1 条），错误语义靠 conditional 字段（66.4%）补充判读；JS 后端仅扫到 UI 层 6 条日志，不代表服务端可观测水平。
