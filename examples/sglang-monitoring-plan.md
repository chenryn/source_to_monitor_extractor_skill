# SGLang 监控方案（源码逆向生成）

- 分析对象：sglang（SGLang 在线 LLM 推理服务引擎，Python + Rust 网关多语言仓库，src-layout：`python/sglang/...`）
- 运行形态：在线服务（HTTP/gRPC 推理 API）+ 基础设施组件（Rust KV 感知路由网关）
- 提取方式：三套确定性脚本（`extract_loggers.py` / `extract_metrics.py` / `extract_traces.py`）+ Rust 网关链路平面人工降级核查；未覆盖范围见第 9 章
- 所有指标名、开关名、日志模板均从本项目源码提取，阈值为候选值，待 SLO/压测/线上数据校准

---

## 1. 项目画像与三平面总览

### 1.0 项目画像（决定后续需求清单）

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面（Python） | 标准库 `logging`，`logger = logging.getLogger(__name__)`；一次性日志封装 `python/sglang/_logging_once.py`；格式混用 printf 与 f-string | `python/sglang/srt/observability/metrics_collector.py:56` |
| 日志门面（Rust） | `tracing` + `tracing-subscriber`（env-filter + json feature） | `sgl-router/Cargo.toml:76-77` |
| 日志门面（C++/CUDA） | 自定义 `CHECK_*` / glog 风格宏，FATAL 级断言为主 | `python/sglang/kernels/aot/csrc/infllm_v2/flash_attn/flash_api.cpp:131` |
| 指标 SDK | Python：`prometheus_client`（Counter/Gauge/Histogram/GaugeHistogram，延迟导入）；统一命名空间前缀 `sglang:`（冒号分隔，183 个定义全部带此前缀）。Rust 网关：不引入 metrics crate，**手写 counter/histogram/gauge + Prometheus 0.0.4 文本曝光**，前缀 `sgl_router_` | `python/sglang/srt/observability/metrics_collector.py:342-346`；`sgl-router/src/server/metrics.rs:5-8` |
| 链路 SDK | 四套并存：① OTel（OTLP exporter，gRPC/HTTP，`trace.py`，默认关）② 自研阶段注册表 `RequestStageConfig`（请求级分段耗时）③ 自研 PD 分离 trace DSL（`trace_req_start/trace_req_finish/trace_slice`）④ NVTX profiler range（`record_function`） | `python/sglang/srt/observability/trace.py:242-255`；`python/sglang/srt/observability/req_time_stats.py:138`；`python/sglang/srt/disaggregation/common/conn.py`；`python/sglang/srt/managers/scheduler_pp_mixin.py:138-1664` |
| 暴露方式（Python 引擎） | HTTP `/metrics`（ASGI Mount，`make_asgi_app`，多进程 registry）；gRPC server 另有 `/metrics`（`generate_latest`）；多模态 runtime 独立 `start_http_server(port)`。均受 `--enable-metrics` 门控 | `python/sglang/srt/utils/common.py:2864`；`python/sglang/srt/entrypoints/grpc_server.py:56,65`；`python/sglang/multimodal_gen/runtime/observability/metrics.py:54` |
| 暴露方式（Rust 网关） | `/metrics` GET 路由，**默认挂载、不受 readiness 门控**（有意暴露预热期指标）；另有 `/healthz` `/readyz` | `sgl-router/src/server/app.rs:197-199`；`sgl-router/src/server/routes/metrics.rs:5-9` |
| 健康检查 | `/health` 与 `/health_generate`（发一条真实生成请求做深度探活） | `python/sglang/srt/entrypoints/http_server.py:726-733` |
| 分段耗时实现 | 三层：① 指标投影——`sglang:time_to_first_token_seconds` / `sglang:inter_token_latency_seconds` / `sglang:e2e_request_latency_seconds` / `sglang:per_stage_req_latency_seconds`（stage 标签）/ `sglang:scheduler_stage_seconds_total`（调度器 9 阶段 wall time，stage 标签）② 自研阶段注册表 `RequestStageConfig`（tokenize→dispatch→prefill_waiting→forward→…，经 `--enable-request-time-stats-logging` 输出日志）③ OTel span（默认关） | `python/sglang/srt/observability/metrics_collector.py:2048,2058,2075,889,1081`；`python/sglang/srt/observability/req_time_stats.py:157-214`；`python/sglang/srt/arg_groups/fields/observability.py:140-143` |
| Rust 网关画像 | `sgl-router`（axum，独立二进制）+ `rust/` 工作区（sglang-server、sglang-grpc、sglang-radix-tree 等）+ `sgl-model-gateway`。指标面 27+ 个 `sgl_router_*` 序列（请求/响应计数、TTFT 直方图、worker 健康/熔断状态、策略决策计数），自带 Grafana dashboard | `sgl-router/src/server/metrics.rs:23-50`；`sgl-router/monitoring/grafana-dashboard.json` |

### 1.0.1 默认开关（最贵的坑，必查——本项目可观测采集默认全关）

| 能力 | 开关 | 默认值 | 生效条件 / 启用方式 | 证据 |
|---|---|---|---|---|
| Prometheus 指标注册与 /metrics 暴露 | `--enable-metrics` | **False（关）** | 启动参数显式打开 | `python/sglang/srt/arg_groups/fields/observability.py:70` |
| 非 TP0 调度器分别记指标 | `--enable-metrics-for-all-schedulers` | **False** | dp_attention 场景需打开，否则指标全记在 TP0 | 同上 `:82-85` |
| OTel 链路 | `--enable-trace` | **False（关）** | 打开后经 `--otlp-traces-endpoint`（默认 `localhost:4317`）上报，`--trace-modules` 默认 `request` | 同上 `:164-172` |
| MFU 指标 | `--enable-mfu-metrics` | **False** | 显式打开 | 同上 `:78-81` |
| 前向逐迭代指标（ZMQ IPC） | `--enable-forward-pass-metrics` | **False** | 外部消费者（如 Dynamo planner）订阅 | 同上 `:152-155` |
| 请求级分段耗时日志 | `--enable-request-time-stats-logging` | **False** | 打开后输出 `RequestStageConfig` 注册的阶段耗时 | 同上 `:140-143` |
| 逐请求元数据/输入输出日志 | `--log-requests` | **False** | 级别由 `--log-requests-level`（默认 2）控制 | 同上 `:36-46` |
| 逐请求指标落文件 | `--export-metrics-to-file` | **False** | 需同时给 `--export-metrics-to-file-dir` | 同上 `:179-186` |
| KV 事件发布 | `--kv-events-config` | **None（关）** | 给出 ZMQ 配置即启用 | 同上 `:144-147` |
| 直方图桶 | `--bucket-time-to-first-token` / `--bucket-inter-token-latency` / `--bucket-e2e-request-latency` / `--prompt-tokens-buckets` / `--generation-tokens-buckets` | **None（默认桶）** | 长延迟场景必须自定义桶，否则 P99 落 +Inf | 同上 `:104-131` |
| Rust 网关指标 | 无开关 | **默认开** | `/metrics` 路由无条件挂载，且不 gate 在 readiness 上 | `sgl-router/src/server/app.rs:199`；`routes/metrics.rs:5-9` |
| Rust 网关日志级别 | 环境变量（env-filter） | 未设时默认 | `RUST_LOG` 控制 | `sgl-router/Cargo.toml:77` |

> **方案第一优先级：打开并验证数据流。** 引擎侧 `--enable-metrics` 默认关，不打开则第 6 章所有 L1 指标告警空转。最小验证序列：
> 1. `--enable-metrics` 启动后 `curl http://<engine>:<port>/metrics | grep '^sglang:'`（应出现 `sglang:num_running_reqs` 等）；
> 2. 发一条请求后确认 `sglang:http_requests_total` 递增；
> 3. 需要链路时加 `--enable-trace --otlp-traces-endpoint <collector>:4317`，在 collector 侧确认 `service.name`（默认回退 `sglang`，`observability.py:173-177`）；
> 4. 网关侧无开关，`curl http://<router>:<port>/metrics | grep '^sgl_router_'` 直接验证。

### 1.1 语言构成与扫描范围

| 语言 | 文件数（约） | 日志埋点提取数 | 说明 |
|---|---|---|---|
| Python | 5006（python/） | 5529 | 生产代码根 `python/sglang/` |
| Rust | 657（rust/ 202、sgl-router/ 202、sgl-model-gateway/ 253） | 1560 | `#[cfg(test)] mod tests` 已由脚本内置排除；`sgl-model-gateway/e2e_test`（197 条）目录名未命中默认排除规则，统计口径见第 9 章 |
| C/C++/CUDA | 108（.cpp 44、.cu 56、.cc 8、其余 .h） | 602 | 集中在 `python/sglang/kernels/`，几乎全为 FATAL 断言 |

### 1.2 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|---|---|---|---|
| 日志埋点 | 7691 条（Python 5529 / Rust 1560 / C++ 602） | 见 1.4 | 高（级别分明、封装统一） |
| 指标 | 836 条记录：定义 183（写入点 187、name_literal 213、stats_field 245、暴露点 3） | 见 1.5 | 高（Python 侧）/ 中（Rust 侧手写 registry，脚本未覆盖、人工核对） |
| 链路 | 126 条：span 95、阶段注册表 30、attribute 1 | 见 1.6 | 中（结构丰富但默认关闭 + 分布式 span 仅 PD 分离 DSL 承载） |

### 1.3 日志按级别分布（含语言拆分）

| 级别 | 数量 | 占比 | Python / Rust / C++ | 是否纳入告警 |
|---|---|---|---|---|
| FATAL | 1133 | 14.7% | 4 / 528 / 601 | **不直接纳入**：Rust 侧为 `panic!`+`assert!`（528=403+125），C++ 侧为 `CHECK_*` 宏，均失败即中止、未必落盘；崩溃用「日志断档 + 进程重启」判定（见 6.2） |
| ERROR | 957 | 12.4% | 735 / 221 / 1 | 是（L2 事件类，需白名单） |
| WARNING | 2229 | 29.0% | 1917 / 312 / 0 | 部分（retract 等恢复路径必须白名单，见 5.3） |
| INFO | 2355 | 30.6% | 2171 / 184 / 0 | 否（就绪/启动等存在性事件除外） |
| DEBUG | 1017 | 13.2% | 702 / 315 / 0 | 否（dev_debug，人工决定是否采集） |

### 1.4 日志按包分布（Top 10）

| 包 | 埋点数 | ERROR/WARN | 主要职责 |
|---|---|---|---|
| sglang/srt | 3708 | ~2600 | 引擎核心：scheduler、tokenizer_manager、disaggregation、entrypoints |
| sglang/multimodal_gen | 1332 | ~350 | 多模态生成 runtime（diffusion pipeline） |
| sglang/kernels | 773 | 601（几乎全 FATAL 断言） | C++/CUDA kernel 与 AOT 编译 |
| sgl-model-gateway/src | 692 | ~180 | Rust 模型网关 |
| sgl-router/src | 362 | ~90 | Rust KV 感知路由器 |
| rust/sglang-server | 250 | ~60 | Rust 服务器 crate |
| sgl-model-gateway/e2e_test | 197 | ~50 | 端到端测试（非生产路径，见第 9 章） |
| rust/sglang-radix-tree | 129 | ~20 | Rust 基数树库 |
| rust/sglang-api-codegen | 45 | ~5 | API 代码生成 |
| rust/sglang-grpc | 42 | ~10 | gRPC crate |

### 1.5 指标按类型与语义分布

| 类型 | 数量 | 说明 |
|---|---|---|
| Gauge | 76 | 水位/瞬时值：`sglang:token_usage`、`sglang:num_queue_reqs` 等，多进程模式 `mostrecent` |
| Counter | 61 | 累计计数：`sglang:prompt_tokens_total`、`sglang:num_retracted_requests_total` 等 |
| Histogram | 45 | 延迟分布：TTFT/ITL/E2E/queue_time/per_stage，桶可由启动参数自定义 |
| Summary | 1 | 极少（分位不可跨实例聚合，本项目不依赖） |

命名约定：全部 183 个定义均带 `sglang:` 前缀（冒号合法，曝光名=源码构造名）；Rust 网关侧 `sgl_router_` 前缀下划线风格，两套前缀并存。

### 1.6 链路 span 按阶段分布

| 阶段/形态 | 数量 | 典型 span | 说明 |
|---|---|---|---|
| 自研阶段注册表（stage_registry） | 30 | `RequestStageConfig`：tokenize、api_server_dispatch、dpc_dispatch、request_process、prefill_waiting、decode_forward、decode_loop、prefill_forward、chunked_prefill、mm_encode、prefill_prepare、prefill_bootstrap 等 | `python/sglang/srt/observability/req_time_stats.py:157-214`；另有 `mooncake_trace.py`、`multimodal_gen/runtime/utils/trace_wrapper.py` 注册块。这是本项目请求级分段耗时的事实标准 |
| NVTX / profiler range（record_function） | 55 | `recv_requests`、`get_next_batch_to_run`、`run_batch`、`process_batch_result`、`sglang.vlm.mm_embedding` | `python/sglang/srt/managers/scheduler_pp_mixin.py:138-1664`；属 profiler 信号而非分布式 trace |
| 自研 PD 分离 trace DSL | 32 | `trace_req_start` / `trace_req_finish` / `trace_slice`（conn.py） | 跨 prefill/decode 实例的请求级跟踪，`python/sglang/srt/disaggregation/common/conn.py:2179,2424` |
| OTel span（start_span） | 5 | `f'{role} Req {rid[:8]}'`、slice span | `python/sglang/srt/observability/trace.py:596,670`；**默认关闭** |
| range_push | 3 | NVTX 包装 | `nvtx_pytorch_hooks.py:251` |

### 1.7 上下文证据摘要

| 证据 | 数量 | 占比 | 对方案的影响 |
|---|---|---|---|
| in_loop（循环内） | 819 | 10.6% | 禁止频率告警（如 decode 循环内日志） |
| in_except（异常分支） | 1170 | 15.2% | error 类主力 |
| once（仅一次） | 153 | 2.0% | 只做存在性告警 |
| conditional（条件分支） | 3282 | 42.7% | 大量日志在特性分支内，默认配置下可能不触发 |
| is_assertion（断言/abort） | 1004 | 13.1% | 不是可检索日志关键字；崩溃归因用 |

---

## 2. 日志埋点明细（按语义分类）

分类方法：7691 条按证据字段（in_except / is_assertion / level / once / conditional / in_loop）+ 模板关键词规则化打标，按模板去重聚合（7691 → 6641 个唯一模板）。领域子标签按画像扩展：`preemption`（retract）、`model_load`、`kv_cache`、`tokenize`、`batch_schedule`。

### 2.1 生命周期类（lifecycle）

| 文件:行 | 级别 | 模板 | 说明 | 附加标签 |
|---|---|---|---|---|
| python/sglang/srt/entrypoints/http_server.py:2598 | INFO | `The server is fired up and ready to roll!` | 就绪锚点，P0 存在性告警依据 | one_shot |
| python/sglang/srt/entrypoints/http_server.py:726 | INFO | `/health` `/health_generate` 路由 | 深度探活 | — |
| python/sglang/srt/observability/startup_func_log_and_timer.py | INFO | `Engine startup timings (s): load_weight=%.2f, kv_cache_allocation=%.2f...` | 启动耗时分解 | — |
| python/sglang/srt/managers/scheduler.py（SIGTERM 处理） | WARNING | `SIGTERM received on node_rank {}...` | 计划内关闭 | — |
| sgl-router/src/server/routes/health.rs | — | `/healthz` `/readyz` handler | 网关存活/就绪探针 | — |

聚合计数：lifecycle 共约 400 条（startup、shutdown、health_check 子类）。

### 2.2 请求/事务类（request）

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| python/sglang/srt/managers/scheduler.py:5777-5834 | INFO | `#queue-req={} #running-req={} ...`（Decode batch / Prefill batch 周期日志，`--decode-log-interval` 默认 40 迭代） | in_loop，量随负载线性，只做字段统计不做告警 |
| python/sglang/srt/entrypoints/grpc_server.py 等 | ERROR | `Error in parse_streaming_increment: {}`（8 处同模板） | 流式解析失败 |

聚合计数：request 类约 1800 条，其中 high_volume 660+、feature_gated 1200+。

### 2.3 下游依赖类（dependency）

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| python/sglang/srt/disaggregation/common/conn.py:2145 | ERROR | `Lost connection with prefill instance (bootstrap_addr: {}), {} requests affected` | PD 分离断连，P1 候选 |
| python/sglang/srt/disaggregation/common/conn.py（多处） | WARNING | `Failed to sync status {} of room {} to {}: {}`、`Some requests timed out when bootstrapping...` | bootstrap/同步超时 |
| sgl-router（tokenizer/discovery） | — | worker 发现与 tokenize 失败由 `sgl_router_ingress_tokenize_errors_total` 指标承载 | Rust 侧依赖错误优先用指标 |

### 2.4 资源指标类（resource）

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| python/sglang/srt/managers/schedule_batch.py:2944 | **WARNING** | `retract_decode: aborted request %s: %s` | **OOM 类日志的真实级别是 WARNING**：retract 是内存压力下的恢复机制而非错误，常规 retract 不告警，用 `sglang:num_retracted_requests_total` 指标看趋势（见 4-#9） |
| python/sglang/srt/managers/schedule_batch.py:2971 | **WARNING** | `retract_decode: aborted last request %s due to OOM` | **例外**：连最后一个请求都放不下，属硬性容量耗尽，P1 事件告警 |
| python/sglang/multimodal_gen/runtime/managers/gpu_worker.py:923 | WARNING | `OOM detected. Possible solutions: ...` | 多模态 OOM 恢复路径（有降级建议），P2 |
| python/sglang/srt/managers/scheduler_components/metrics_reporter.py（EmbeddingPool 等） | WARNING | `EmbeddingPool full: {}/{}MB in-flight...queueing` | 池满排队，P2 |

### 2.5 业务逻辑异常（business_error）

聚合计数约 560 条。代表：`Parameter {} not found in params_dict`（WARNING ×35，权重加载容错）、`Max running requests is reset to 48 for speculative decoding...`（配置修正提示）、`Prompt #{} has {} tokens, exceeds max_length={}`（超长截断）。

### 2.6 系统异常（system_error）

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| python/sglang/srt/managers/scheduler_components（FlashInfer workspace） | ERROR | `Failed to setup FlashInfer workspace: %s` | 后端初始化失败 |
| python/sglang/srt/disaggregation/common/conn.py:1261 | ERROR | `PD retract rebootstrap could not resolve the prefill /generate URL...` | PD 恢复失败 |
| rust/sglang-server 等 | FATAL | `panic!` 系列（125 处）+ `assert!`（403 处） | 断言，不做检索关键字 |
| python/sglang/kernels/** | FATAL | `CHECK_*` 宏（601 处，模板多为空/表达式） | 同上 |

### 2.7 调试日志（DEBUG，1017 条，不纳入告警）

DEBUG 1017 条（Python 702 / Rust 315）单列，由人工决定采集策略；含 `GPU memory: peak=%.2f GB...`（gpu_worker.py:572）等有价值的水位观测，若采集建议升为结构化字段。

---

## 3. 指标清单

### 3.1 已定义的指标（核心集，全部带写入点验证；曝光名 = 源码构造名，`sglang:` 前缀）

| 指标名 | 类型 | 语义角色 | 关键标签 | 源码位置 | 备注 |
|---|---|---|---|---|---|
| `sglang:time_to_first_token_seconds` | Histogram | RED.Duration（TTFT） | model_name, engine_type, tp_rank… | metrics_collector.py:2048 | 桶可自定义（`--bucket-time-to-first-token`） |
| `sglang:inter_token_latency_seconds` | Histogram | RED.Duration（ITL） | 同上 | metrics_collector.py:2058 | 桶可自定义 |
| `sglang:e2e_request_latency_seconds` | Histogram | RED.Duration（端到端） | 同上 | metrics_collector.py:2075 | 桶可自定义 |
| `sglang:queue_time_seconds` | Histogram | USE.Saturation（排队时长） | 同上 | metrics_collector.py:842 | |
| `sglang:per_stage_req_latency_seconds` | Histogram | 分段耗时（请求级） | + `stage` 标签 | metrics_collector.py:889 | stage 取值=阶段注册表条目 |
| `sglang:scheduler_stage_seconds_total` | Counter | 调度器分段耗时 | + `stage` 标签（9 类） | metrics_collector.py:1081-1094 | 类别集见 scheduler_stage_metrics.py:31-51 |
| `sglang:num_running_reqs` | Gauge | USE.Saturation | 同上 | metrics_collector.py:364 | mostrecent 多进程模式 |
| `sglang:num_queue_reqs` | Gauge | USE.Saturation（等待队列） | 同上 | metrics_collector.py:370 | |
| `sglang:token_usage` | Gauge | USE.Utilization（KV 池，0~1） | 同上 | metrics_collector.py:404 | FIXME 注释自认名字有误导（实为跨池瓶颈比） |
| `sglang:full_token_usage` / `sglang:swa_token_usage` / `sglang:mamba_usage` | Gauge | USE.Utilization | 同上 | metrics_collector.py:410-422 | 混合模型分池 |
| `sglang:kv_available_tokens` / `kv_used_tokens` / `kv_evictable_tokens` | Gauge | USE.Utilization（绝对量） | 同上 | metrics_collector.py（Memory pool 段） | |
| `sglang:num_retracted_requests_total` | Counter | USE.Saturation/Errors（retract） | 同上 | metrics_collector.py:557 | 旧 Gauge `num_retracted_reqs`(:552) 并存，取 Counter |
| `sglang:num_retracted_input_tokens_total` / `num_retracted_output_tokens_total` | Counter | retract 规模 | 同上 | metrics_collector.py:563,568 | |
| `sglang:num_aborted_requests_total` | Counter | RED.Errors（中止） | 同上 | metrics_collector.py:1967 | |
| `sglang:http_requests_total` | Counter | RED.Rate（HTTP 层） | path, method | http_request_metrics.py:28 | 中间件注册 http_server.py:521 |
| `sglang:http_responses_total` | Counter | RED.Errors（HTTP 状态码） | path, method, status_code | http_request_metrics.py:34 | 错误率分母/分子 |
| `sglang:http_requests_active` | Gauge | 并发在途 | path | http_request_metrics.py:40 | |
| `sglang:prompt_tokens_total` / `sglang:generation_tokens_total` | Counter | 吞吐 | 同上 | metrics_collector.py:1863,1868 | |
| `sglang:gen_throughput` | Gauge | 吞吐（token/s，周期窗口） | 同上 | metrics_collector.py:382 | |
| `sglang:cache_hit_rate` | Gauge | 前缀缓存命中（0~1） | 同上 | metrics_collector.py:388 | |
| `sglang:num_bootstrap_failed_reqs_total` / `num_transfer_failed_reqs_total` | Counter | PD 分离失败 | 同上 | metrics_collector.py:672,677 | |
| `sglang:weight_load_duration_seconds` | Gauge | 权重加载/更新耗时 | + `source` | metrics_collector.py:496 | 事件型，用 changes() 检测 |
| `sglang:startup_latency_breakdown_seconds_max` | Gauge | 启动耗时分解 | context | startup_func_log_and_timer.py:27 | |
| `sglang:num_grammar_queue_reqs` | Gauge | 语法约束队列 | 同上 | metrics_collector.py:376 | |
| `sglang:estimated_flops_per_gpu_total` | Counter | MFU 估算 | 同上 | metrics_collector.py（`--enable-mfu-metrics`） | 条件定义 |

**Rust 网关指标（人工提取，脚本未覆盖 Rust 指标；来自 `sgl-router/src/server/metrics.rs:23-50` 官方表面表）**：

| 指标名 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `sgl_router_requests_total` / `sgl_router_responses_total` | Counter | route, method(, status_code) | 网关 RED |
| `sgl_router_request_duration_seconds` / `sgl_router_ttft_seconds` | Histogram | model_id | 网关视角延迟（含排队+路由） |
| `sgl_router_active_load` | Gauge | worker_url, kind | 每 worker 负载 |
| `sgl_router_worker_health` / `sgl_router_worker_cb_state` | Gauge | worker_url | 健康与熔断状态 |
| `sgl_router_worker_inflight_requests` | Gauge | worker_url | worker 在途数 |
| `sgl_router_stale_requests_total` / `sgl_router_retries_total` / `sgl_router_stream_outcome_total` | Counter | outcome 等 | 重试/陈旧/流结果 |
| `sgl_router_policy_decisions_total` / `sgl_router_policy_selection_failures_total` | Counter | policy, reason | 路由策略决策与失败 |
| `sgl_router_cache_aware_decisions_total` | Counter | model_id, decision | cache 命中/驱逐决策 |
| `sgl_router_ingress_tokenize_errors_total` | Counter | model_id | tokenize 失败 |

### 3.2 指标质量风险

| 风险 | 数量 | 具体指标 | 影响 |
|---|---|---|---|
| 条件定义（默认不注册） | 约 20 | 全部 183 个定义都在 `enable_metrics` 分支内（metrics_collector.py:1380）；`estimated_flops_per_gpu_total` 等还有二级开关 | **不开 `--enable-metrics` 时指标平面整体为空**，第 6 章 L1 全部空转 |
| 无 documentation | 0 | 抽样未见空 documentation | 质量好 |
| 无写入点（预留） | 少量 | name_literal 213 条中未匹配到构造器的引用需人工确认（如 `sglang:routing_key_running_req_count`） | 可能是跨模块写入，勿断言不存在 |
| 高基数标签 | 低 | labelnames 均为 model_name/rank/stage/outcome 等低基数字段；`priority` 标签需配 `--default-priority-value`（metrics_collector.py:66-70 注释） | 风险受控 |
| 双写并存 | 2 | `sglang:num_retracted_reqs`（Gauge 旧）与 `sglang:num_retracted_requests_total`（Counter 新） | 告警统一用 Counter，避免语义混淆 |
| 命名误导 | 1 | `sglang:token_usage` 实为跨池瓶颈比（源码 FIXME，metrics_collector.py:93） | 看板注释写明 |

### 3.3 指标暴露方式

| 方式 | 位置 | 说明 |
|---|---|---|
| HTTP 拉取（引擎） | `/metrics`，ASGI Mount | `utils/common.py:2864`；`--enable-metrics` 门控（默认关）；支持 `PROMETHEUS_MULTIPROC_DIR` 多进程模式 |
| HTTP 拉取（gRPC server） | `/metrics`，`generate_latest` | `entrypoints/grpc_server.py:56,65` |
| HTTP 拉取（多模态 runtime） | 独立端口 `start_http_server(port)` | `multimodal_gen/runtime/observability/metrics.py:54` |
| HTTP 拉取（Rust 网关） | `/metrics`，Prometheus 0.0.4 文本 | `sgl-router/src/server/app.rs:199`；默认开、预热期即可抓取 |
| OTLP | `--otlp-traces-endpoint`（默认 localhost:4317） | 仅 trace 平面，默认关 |

---

## 4. 信号仲裁

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---|---|---|---|---|
| 1 | 服务活着/就绪 | 日志 + 探针 | `The server is fired up and ready to roll!`；`/health`、`/health_generate`；网关 `/healthz` `/readyz` | http_server.py:2598,726；sgl-router app.rs:197 | 已覆盖（存在性告警 + 探针） |
| 2 | 请求速率 | 指标 | `sglang:http_requests_total`、`sgl_router_requests_total` | http_request_metrics.py:28；metrics.rs:24 | 已覆盖（默认关，先开开关） |
| 3 | HTTP 错误率 | 指标 | `sglang:http_responses_total{status_code}` | http_request_metrics.py:34 | 已覆盖 |
| 4 | TTFT 分位 | 指标 | `sglang:time_to_first_token_seconds` | metrics_collector.py:2048 | 已覆盖（需自定义桶） |
| 5 | ITL 分位 | 指标 | `sglang:inter_token_latency_seconds` | metrics_collector.py:2058 | 已覆盖 |
| 6 | 端到端延迟分位 | 指标 | `sglang:e2e_request_latency_seconds` | metrics_collector.py:2075 | 已覆盖 |
| 7 | 排队/并发 | 指标 | `sglang:num_queue_reqs`、`sglang:queue_time_seconds`、`sglang:num_running_reqs`、`sglang:http_requests_active` | metrics_collector.py:370,842,364 | 已覆盖 |
| 8 | KV 缓存水位 | 指标 | `sglang:token_usage`、`full_token_usage`、`kv_available_tokens` 系 | metrics_collector.py:404-422 | 已覆盖（0~1 比率） |
| 9 | 内存压力/抢占 | 指标 + 日志 | `sglang:num_retracted_requests_total`（趋势）+ `retract_decode` WARNING（归因） | metrics_collector.py:557；schedule_batch.py:2944 | 已覆盖；**retract 是 WARNING 级恢复机制，常规 retract 只做指标趋势，不设日志告警** |
| 10 | 一次请求慢在哪一段 | 链路/指标标签 | ① `sglang:per_stage_req_latency_seconds`（stage 标签）② `sglang:scheduler_stage_seconds_total`（调度器 9 阶段）③ `RequestStageConfig` 注册表 13+ 阶段（日志）④ OTel span（默认关）⑤ PD 分离 DSL（conn.py） | metrics_collector.py:889,1081；req_time_stats.py:157-214；trace.py:596 | **部分覆盖**：调度器内部分段完整；跨实例分布式 span 仅 PD 分离 DSL + OTel 承载，OTel 需开 `--enable-trace` |
| 11 | 网关层延迟与 worker 健康 | 指标（Rust） | `sgl_router_ttft_seconds`、`sgl_router_worker_health`、`sgl_router_worker_cb_state` 等 27+ 序列 | sgl-router/src/server/metrics.rs:23-50 | 已覆盖（默认开） |
| 12 | 崩溃/进程异常终止 | 日志断档 + 外部 | FATAL/断言 1004 处**不落盘或不可检索**；`panic!`/`CHECK_*` 失败即中止 | 第 2.6 节 | **部分覆盖**：用「进程消失 + 日志断档 + 容器重启」判定，不能做日志关键字 |
| 13 | GPU/硬件层（util/temp/ECC） | 指标 | 三平面均无（检索见 4.1） | — | **真缺口：接 DCGM/NVIDIA exporter，不改源码** |
| 14 | tokenizer/预处理延迟 | 指标 | `sglang:per_stage_req_latency_seconds{stage="tokenize"}`、注册表 TOKENIZE 条目；网关 `sgl_router_ingress_tokenize_errors_total` | req_time_stats.py:157 | 已覆盖 |
| 15 | 前缀缓存命中 | 指标 | `sglang:cache_hit_rate`（0~1）、`sgl_router_cache_aware_decisions_total` | metrics_collector.py:388 | 已覆盖 |
| 16 | 权重更新事件 | 指标 | `sglang:weight_load_duration_seconds`（source 标签，changes() 检测） | metrics_collector.py:496 | 已覆盖 |

### 4.1 三平面检查记录（#13「GPU 硬件层」缺口结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|---|---|---|
| 日志 | 检索 7691 条埋点，关键词 `gpu util\|temperature\|power\|ecc\|throttle`（模板列） | 仅有启动期 GPU 型号/显存探测 INFO，无运行时硬件水位日志 |
| 指标 | 检索 183 个定义名，关键词 `gpu\|util\|temperature\|power\|flops` | 仅有 `graph_memory_usage_gb`、`kv_cache_memory_usage_gb`、`weight_memory_usage_gb`（进程内存占用，非硬件利用率）；无 GPU util/temp 定义 |
| 链路 | 检索 95 个 span 名 + 30 注册表条目 | 无硬件相关 span |
| 结论 | GPU 硬件层属**真缺口**，需外部 exporter（DCGM），在抓取配置层接入，与引擎 `/metrics` 同一 Prometheus | |

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|---|---|---|---|
| `The server is fired up and ready to roll!` | P0（缺失判定） | 就绪锚点，启动 N 秒未出现即告警 | http_server.py:2598 |
| `retract_decode: aborted last request` | P1 | 连最后请求都无法保留的容量耗尽 | schedule_batch.py:2971 |
| `Lost connection with prefill instance` | P1 | PD 分离断连 | conn.py:2145 |
| `PD retract rebootstrap could not resolve` | P1 | PD 恢复失败 | conn.py:1261 |
| `OOM detected` | P2 | 多模态 OOM（有恢复路径） | gpu_worker.py:923 |
| `Failed to setup FlashInfer workspace` | P1 | attention 后端初始化失败 | 第 2.6 节 ERROR 族 |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在模板 | 统计方式 | 维度 |
|---|---|---|---|
| `#queue-req` / `#running-req` | scheduler.py:5777-5834 周期日志 | 数值提取 → 时序趋势 | 实例 |
| `gen throughput (token/s)` | 同上周期日志 | 数值提取 | 实例 |
| `load_weight` / `kv_cache_allocation`（秒） | startup_func_log_and_timer.py | 数值提取 → 启动耗时基线 | context |
| `%s requests affected` | conn.py:2145 | 数值提取 → 断连影响面 | bootstrap_addr |

### 5.3 降噪白名单（必须显式列出）

| 关键字/条件 | 为什么不告警 | 处理 |
|---|---|---|
| `retract_decode: aborted request %s`（非 last） | 常规内存压力恢复机制，WARNING，有指标承载 | 忽略单条；看 `sglang:num_retracted_requests_total` 速率 |
| `client disconnected` / 流中断 | 客户端行为正常 | 忽略，不计错误率 |
| `Parameter {} not found in params_dict` | 权重加载容错路径 | 仅趋势统计 |
| 可选后端不可用（`FlashInfer not supported for world size`、`falling back to`） | 降级路径 | P3 统计降级次数 |
| `Max running requests is reset...`、`using fallback...` | 配置修正提示，once 类 | 存在性记录 |
| conditional 非空的 3282 条 | 特性分支内，默认不触发 | 先确认配置再启用监控 |
| once=true 的 153 条 | 频率无意义 | 只做存在性判断 |
| FATAL/断言 1004 条 | 失败即中止，不可检索 | 崩溃归因用，改「日志断档+重启」判定 |

### 5.4 检索语句示例

```promql
# TTFT P99（桶为默认值时先确认 +Inf 占比）
histogram_quantile(0.99, sum(rate(sglang:time_to_first_token_seconds_bucket[5m])) by (le, model_name))

# HTTP 5xx 错误率
sum(rate(sglang:http_responses_total{status_code=~"5.."}[5m]))
  / sum(rate(sglang:http_requests_total[5m]))

# KV 池水位（0~1）
max(sglang:token_usage)

# retract 趋势（恢复机制，看速率不看单条日志）
sum(increase(sglang:num_retracted_requests_total[10m]))

# 调度器 run_batch 阶段耗时占比
sum(rate(sglang:scheduler_stage_seconds_total{stage="run_batch"}[5m]))
  / sum(rate(sglang:scheduler_stage_seconds_total[5m]))
```

```logql
# 启动就绪存在性判定
{job="sglang"} |= "The server is fired up and ready to roll!"

# PD 断连事件
{job="sglang"} |= "Lost connection with prefill instance"
```

---

## 6. 告警规则草稿（候选阈值，待 SLO/压测/线上数据校准）

> 前置条件：`--enable-metrics` 已打开并按 1.0.1 验证数据流；否则本章 L1 全部空转。

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪/白名单 | 源码 |
|---|---|---|---|---|---|
| 引擎实例下线 | `up{job="sglang"} == 0` 持续 1m | 1 分钟 | P0 | 计划内重启窗口白名单 | http_server.py:726 |
| HTTP 5xx 错误率 | 5xx rate / 总 rate > 阈值，持续 5m | >5%（0~1 比率，待校准） | P1 | 排除客户端断连 4xx；冷启动 5m | http_request_metrics.py:34,28 |
| TTFT P99 劣化 | `histogram_quantile(0.99, …time_to_first_token_seconds_bucket) > 阈值` 持续 10m | 按模型基线 ×2 定（秒） | P1 | 按 model_name 分组；排除首桶未充盈期 | metrics_collector.py:2048 |
| ITL P99 劣化 | 同上（inter_token_latency_seconds） | 秒；待压测校准 | P1 | 按 model_name 分组 | metrics_collector.py:2058 |
| E2E P99 劣化 | 同上（e2e_request_latency_seconds） | 秒 | P2 | 区分 chunked prefill 场景 | metrics_collector.py:2075 |
| KV 池水位过高 | `sglang:token_usage > 0.95` 持续 10m | 0.95（0~1 量纲） | P1 | 与 num_queue_reqs 联动；分池模型优先看 full_token_usage | metrics_collector.py:404,410 |
| 队列积压 | `sglang:num_queue_reqs > 阈值` 持续 10m | 绝对值待容量基线校准 | P2 | 排除批量离线流量窗口 | metrics_collector.py:370 |
| 调度卡死（吞吐归零） | `sglang:gen_throughput ≈ 0` 且 `sglang:num_running_reqs > 0` 持续 5m | 5 分钟 | P1 | 排除全部请求已完成的瞬间 | metrics_collector.py:382,364 |
| retract 速率异常 | `increase(sglang:num_retracted_requests_total[10m]) > 阈值` | 待基线；常规 retract 是恢复机制 | P2 | 单条 WARNING 日志不告警；只看聚合速率 | metrics_collector.py:557 |
| 请求中止率 | `rate(sglang:num_aborted_requests_total[5m]) / rate(sglang:http_requests_total[5m]) > 阈值` | 待校准 | P2 | 白名单：客户端主动取消 | metrics_collector.py:1967 |
| PD 传输失败 | `increase(sglang:num_bootstrap_failed_reqs_total[10m]) > 0` 或 `num_transfer_failed_reqs_total` | 出现即告警（计数型） | P1 | PD 分离部署才有该序列 | metrics_collector.py:672,677 |
| 网关 worker 不可用 | `sgl_router_worker_health == 0` 持续 2m | 2 分钟 | P1 | 与熔断状态 `sgl_router_worker_cb_state` 联动 | sgl-router/src/server/metrics.rs:32-34 |
| 网关策略选路失败 | `rate(sgl_router_policy_selection_failures_total[5m]) > 阈值` | 待校准 | P2 | 按 policy, reason 分组 | metrics.rs:41 |
| 网关 TTFT P99 | `histogram_quantile(0.99, …sgl_router_ttft_seconds_bucket)` | 秒；含路由+排队 | P2 | 与引擎侧区分 | metrics.rs:27 |

### 6.2 日志事件告警（L2）

| 告警名 | 条件 | 等级 | 降噪 | 源码 |
|---|---|---|---|---|
| 启动失败/就绪超时 | 启动后 N 秒未出现 `The server is fired up and ready to roll!` 或出现启动期 ERROR | P0 | 只在重启事件后判定；按实例去重；N 待按启动耗时基线校准（参考 `sglang:startup_latency_breakdown_seconds_max`） | http_server.py:2598 |
| 进程异常终止 | 实例日志断档 + 容器重启（**断言/panic 不落盘，不按关键字匹配**） | P0 | 按实例去重；连续多次升级 | 第 2.6 节 FATAL 族 |
| 容量耗尽（最后请求 OOM） | 出现 `retract_decode: aborted last request` | P1 | 与 token_usage 联动确认 | schedule_batch.py:2971 |
| PD 分离断连 | 出现 `Lost connection with prefill instance` | P1 | 按 bootstrap_addr 聚合；区分计划内摘除 | conn.py:2145 |
| attention 后端初始化失败 | 出现 `Failed to setup FlashInfer workspace` | P1 | 启动窗口外判定 | 第 2.6 节 |

### 6.3 日志频率告警（L3，谨慎）

| 告警名 | 条件 | 基线来源 | 等级 |
|---|---|---|---|
| 解析类错误突增 | `Error in detect_and_parse / parse_streaming_increment` 速率 > 5×7 天基线，持续 3 周期 | 历史日志基线 | P3 |
| 权重容错提示突增 | `Parameter {} not found in params_dict` 速率突增 | 历史基线 | P3 |

> 通用 ERROR 占比告警不设置：大量 ERROR 属预期内（客户端断开、可选后端降级），绝对阈值必然误报。in_loop 的 819 条（decode 周期日志等）禁止做频率告警。

### 6.4 链路告警（L4，可选）

| 告警名 | 条件 | 等级 | 前置 |
|---|---|---|---|
| 调度器 run_batch 占比劣化 | `run_batch` 阶段耗时占比环比上升 > 阈值 | P2 | 用指标 `sglang:scheduler_stage_seconds_total`（不需要 trace） |
| 请求级分段劣化 | `sglang:per_stage_req_latency_seconds{stage=…}` P95 环比上升 | P2 | 同上，指标投影优先于 span |
| OTel span 错误率 | span `f'{role} Req …'` error rate 上升 | P3 | **需先开 `--enable-trace`** 并确认采样率 |

---

## 7. 埋点缺口与优化建议

### 7.1 真缺口（三平面均无信号）

| 场景 | 风险 | 建议做法 | 优先级 |
|---|---|---|---|
| GPU 硬件层（util/temp/power/ECC） | 无法感知硬件瓶颈与降频 | 接 DCGM exporter，不改源码（三平面检查记录见 4.1） | P1 |
| Rust 网关分布式 span | 网关→引擎跨进程无法还原调用拓扑（人工核查：sgl-router 无 `info_span!`/`#[instrument]`/otel 依赖） | 如需拓扑，在 sgl-router 引入 tracing+otel span；短期以网关指标（网关 TTFT − 引擎 TTFT）替代 | P2 |

### 7.2 已有但不可用（信号存在，质量/默认状态问题）

| 信号 | 问题 | 建议 |
|---|---|---|
| 全部 `sglang:*` 指标 | `--enable-metrics` 默认关 | 部署清单强制带该参数并加 1.0.1 验证步骤 |
| OTel trace | 默认关 + endpoint 默认 localhost:4317 | 显式配 collector 地址；确认采样率 |
| 延迟直方图 | 默认桶对长上下文场景过粗（P99 落 +Inf） | 按模型延迟分布配 `--bucket-*` 参数 |
| `sglang:token_usage` | 命名误导（实为跨池瓶颈比，源码 FIXME） | 看板注释；混合模型改用 `full_token_usage` |
| `sglang:num_retracted_reqs`（Gauge） | 与 Counter 双写并存 | 告警统一用 `num_retracted_requests_total` |
| dp_attention 部署 | 非 TP0 调度器指标默认不上报 | dp 场景加 `--enable-metrics-for-all-schedulers` |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|---|---|---|
| decode 周期日志承载水位（`#queue-req` 等）与指标重复 | scheduler.py:5777-5834 | 监控一律取指标，日志仅排障 |
| 42.7% 日志在条件分支内（feature_gated） | 全仓 | 采集策略需与特性配置对齐，避免「默认不触发」的空告警 |
| 多模态 OOM 模板含多行修复建议 | gpu_worker.py:923 | 关键字匹配截断到首行 `OOM detected` |

### 7.4 需人工确认（脚本未能确定）

| 项 | 原因 |
|---|---|
| name_literal 213 条中未匹配构造器的引用（如 `sglang:routing_key_running_req_count`） | 指标名跨模块传递或动态构造，需人工核对写入点 |
| `sgl-model-gateway`（Rust 模型网关）的完整指标面 | 指标脚本未覆盖 Rust；仅人工核对 sgl-router，gateway 侧覆盖率未知 |
| C++/CUDA 侧指标 | 能力矩阵未内置 C++ 指标提取；按画像确认该层不承载业务指标（kernel 层） |
| Python `placeholder_style=none` 的 1401 条 | 多为结构化/拼接消息，模板抽取受限，分类按文件与级别近似 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本重新提取（三平面，命令同本次）
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json

# 逐个对比
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

### 8.2 变化处理矩阵（本项目特有关注点）

| 变化类型 | 对监控的影响 | 动作 |
|---|---|---|
| 新增 ERROR 日志 | 新故障模式 | 评估新增 L2 告警（先过白名单） |
| 新增 `sglang:*` 指标 | 新可观测能力 | 评估新增 L1；确认是否又被新开关门控 |
| **开关默认值变更** | 可观测面整体变化 | **优先级最高**：重跑 1.0.1 开关表与验证序列 |
| `sglang:` 指标标签变更 | PromQL 静默失效 | 更新查询/看板（如 `priority` 标签引入） |
| 直方图桶变更 | 分位精度变化 | 重新校准 6.1 阈值 |
| 阶段注册表条目变更（req_time_stats.py） | 分段耗时维度变化 | 更新 stage 维度看板与 6.4 规则 |
| Rust `sgl_router_*` 表面变更 | 网关指标面变化 | 人工对照 metrics.rs 头部文档表（脚本不覆盖） |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配，忽略行号偏移；同文件同模板多条被折叠，计数是近似值
- Rust/C++ 平面脚本覆盖不完整（见第 9 章），diff 前需保持同一提取口径

---

## 9. 局限性

1. **阈值为候选值**：所有阈值需结合 SLO、压测基线、线上历史校准；源码常量（如 `--decode-log-interval` 默认 40、`--load-snapshot-publish-interval` 默认 15）是业务配置，不是告警阈值。
2. **静态分析看不到运行时**：`--enable-metrics` 等开关的运行时生效状态、OTel 采样率、多进程 mode 行为、抓取器实际路径，均需按 1.0.1 序列实测。
3. **提取覆盖率不是 100%**：
   - Rust 指标/链路平面脚本未内置（能力矩阵：Rust 指标「未内置」、链路「未内置」）；sgl-router 指标面为人工提取自 `metrics.rs:23-50` 官方文档表；sgl-model-gateway 与 rust/ 各 crate 未逐一人工核对，覆盖率未知
   - C++/CUDA 指标未内置；该层日志几乎全为断言（602 条中 601 条 FATAL）
   - Python 侧 1401 条 `placeholder_style=none`、213 条 name_literal 未匹配构造器，均不作「不存在」结论
4. **统计口径**：`sgl-model-gateway/e2e_test`（197 条）、`benchmark/`、`tools/sglang-simulator` 等非生产目录未在默认排除规则内，已计入总数；占比小（<5%），不影响结论方向，但做精确对比时建议 `--exclude` 重跑。
5. **断言不是日志**：1004 条 FATAL/断言（panic!/assert!/CHECK_*）失败即中止，不能做日志检索关键字；崩溃检测依赖「日志断档 + 进程重启」外部判定。
6. **链路平面是复合体**：本项目「分段耗时」同时存在于指标标签、自研阶段注册表、PD DSL 日志与 OTel span 四处；监控优先用指标投影（`per_stage_req_latency_seconds`、`scheduler_stage_seconds_total`），OTel 仅在显式开启后有数据。
7. **业务语义**：指标与日志的业务含义（如 retract 策略合理性、cache_hit_rate 目标值）需真实流量验证，静态分析无法给出 SLO。
