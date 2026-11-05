# vLLM 监控方案（源码 → 三平面可观测性分析）

- **分析对象**：vLLM `main` 分支快照（`/tmp/vllm_probe/vllm-main`，2026-11）
- **运行形态**：在线服务（OpenAI HTTP API + gRPC）｜Python 为主 + Rust 前端 + CUDA/C++ 内核
- **分析方法**：确定性脚本提取三平面信号 → 证据驱动语义分类 → 信号仲裁 → 分层告警
- **提取范围**：全仓库；非生产目录见 9.3

---

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据（文件:行号） |
|---|---|---|
| 日志门面 | Python：标准库 `logging`，统一封装 `init_logger`，带 `debug_once/info_once/warning_once/error_once` 变体与 `scope=global/local` 过滤；格式 `VLLM_LOGGING_PREFIX(进程名 pid) LEVEL 时间 [文件:行] 消息`，支持 JSON formatter | `vllm/logger.py:54-195`、`vllm/logger.py:28-32` |
| 日志门面（Rust） | `tracing`/`log` 宏（`info!`/`warn!`/`tracing::info!`，结构化字段） | `rust/src/bench/src/tokenizer.rs:289`、`rust/src/cmd/src/main.rs:75` |
| 指标 SDK | `prometheus_client`，全部指标统一 `vllm:` 命名空间前缀（98/98）；标准标签 `model_name, engine`；多进程模式经 `PROMETHEUS_MULTIPROC_DIR`（未设置时自动创建临时目录） | `vllm/v1/metrics/loggers.py:499`、`vllm/v1/metrics/prometheus.py:17-53`、`prometheus.py:68` |
| 指标 SDK（Rust 前端） | `prometheus-client` crate（`vllm-metrics` crate），与 Python 侧同名指标 1:1 复刻；counter 编码时自动补 `_total` 后缀 | `rust/src/metrics/src/lib.rs:28-31,43-99`、`rust/src/metrics/src/request.rs:73-229` |
| 链路 SDK | OpenTelemetry，OTLP 导出（grpc / http-protobuf 二选一）；`@instrument` 装饰器 + `instrument_manual` 手工 span；trace 上下文经 HTTP header 与 `os.environ` 向 worker 进程传播 | `vllm/tracing/otel.py:18-102,132-177,180-210`、`vllm/v1/engine/output_processor.py:995-1001` |
| 暴露方式（Python） | `GET /metrics`（Prometheus 文本协议，prometheus-fastapi-instrumentator + ASGI mount），随 API server 无条件挂载 | `vllm/entrypoints/serve/instrumentator/metrics.py:56-81`、`vllm/entrypoints/serve/__init__.py:19-22`、`vllm/entrypoints/launchers/api_server/routers.py:20` |
| 暴露方式（Rust） | `GET /metrics` → `METRICS.render()`（OpenMetrics 文本） | `rust/src/server/src/routes.rs:147`、`rust/src/server/src/routes/metrics.rs:13` |
| 暴露方式（离线批处理） | `vllm serve` 之外的 run_batch 路径经 `start_http_server(port, addr)` 独立端口拉取 | `vllm/entrypoints/cli/run_batch.py:42`、`vllm/entrypoints/launchers/run_batch.py:1148`、`vllm/entrypoints/openai/run_batch.py:33` |
| 健康/就绪 | `GET /health`：引擎存活检查，`EngineDeadError` 返回 503（无条件挂载）；Rust 侧另有 gRPC liveness 健康服务 | `vllm/entrypoints/serve/instrumentator/health.py:22-34`、`rust/src/server/src/grpc/health.rs:125` |
| 请求级统计日志 | 周期性吞吐/并发日志（`Engine %03d: Avg prompt throughput: %.1f tokens/s ...`），引擎空闲时降为 DEBUG | `vllm/v1/metrics/loggers.py:276-283` |

### 1.0.1 默认开关（默认关闭的能力，第一优先级是「打开并验证数据流」）

| 能力 | 开关 | 默认值 | 源码证据 | 打开与验证方式 |
|---|---|---|---|---|
| 请求统计日志 + 核心指标写入 | `--disable-log-stats` | `False`（默认开启） | `vllm/engine/arg_utils.py:630`、`vllm/v1/engine/async_llm.py:297` | 默认即有；抓取 `/metrics` 验证 `vllm:` 指标出现 |
| OTel 链路导出 | `--otlp-traces-endpoint` | `None`（关闭） | `vllm/config/observability.py:38`、`vllm/engine/arg_utils.py:742` | 显式设 endpoint（协议默认 grpc，可经 `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf` 切换，`tracing/otel.py:95-101`）；到 collector 验证 span |
| 模型/worker 详细分段 trace | `--collect-detailed-traces [model/worker/all]` | `None`（关闭），且强制要求已设 endpoint | `vllm/config/observability.py:41-48,216-222` | 两个开关都设后验证 `collect_model_forward_time` 相关 span |
| KV cache 驻留指标 | `kv_cache_metrics`（`--kv-cache-metrics`） | `False`（关闭），采样率 `kv_cache_metrics_sample=0.01` | `vllm/config/observability.py:62-68` | 打开后验证 `vllm:kv_block_lifetime_seconds` 等 3 个直方图 |
| CUDA graph 指标 | `cudagraph_metrics` | `False` | `vllm/config/observability.py:82-84` | 打开后验证 padded/unpadded tokens 指标 |
| 层级 NVTX 追踪 | `enable_layerwise_nvtx_tracing` | `False`（与 CUDA graph 互斥） | `vllm/config/observability.py:86-89` | 打开后经 Nsight Systems 验证 range |
| MFU 指标 | `enable_mfu_metrics` | `False` | `vllm/config/observability.py:91-92` | 打开后验证 `vllm:estimated_flops_per_gpu_total` 等写入 |
| 迭代明细日志 | `enable_logging_iteration_details` | `False` | `vllm/config/observability.py:99-103` | 打开后 EngineCore 日志出现迭代明细 |
| 每请求 spec decode 指标（响应体） | `per_request_spec_decode_metrics` | `"none"` | `vllm/config/observability.py:50-60` | 设 `summary`/`detailed` 验证响应 `metrics.speculative_decoding` 字段 |
| 已弃用指标（隐藏期） | `--show-hidden-metrics-for-version` | `None` | `vllm/config/observability.py:23-36` | 迁移期临时打开；确认旧告警迁移后关闭 |
| 自定义直方图桶 | `--custom-histogram-buckets`（JSON） | `None`（用默认桶） | `vllm/config/observability.py:70-80`、`vllm/v1/metrics/buckets.py:161-169` | 按桶族覆盖后抓取 `_bucket` 序列核对边界 |
| KV connector 传输指标 | 随 connector 配置启用 | 未启用即不注册 | `vllm/distributed/kv_transfer/kv_connector/v1/mooncake/stats.py:204` 等 | 仅当使用对应 connector 时抓取 |

### 1.1 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|------|------|----------|-------------|
| 日志埋点 | 6615 条记录，其中断言 2445（is_assertion 单列）、实际日志调用 4170 | Python 3678 + Rust tracing 296（另有 rust/c++ 断言 2445）；生产路径 3974 条 | 高 |
| 指标定义 | 98 个定义（counter 46 / histogram 36 / gauge 16），写入点 99，另有 42 个字面量引用走常量注册表（见 7.4） | 全部 `vllm:` 前缀；核心引擎 52 个在 `vllm/v1`，KV connector 族 38 个 | 高 |
| 链路 span | 脚本提取 37（27 个 `@instrument` 装饰器 + profiler range）；人工核对补充请求级 span `llm_request`（SpanKind.SERVER） | 加载/初始化阶段为主；请求级 span 属性承载 TTFT/e2e/排队/prefill/decode 全套分段 | 中（默认关闭） |

### 1.2 日志按级别分布（实际日志调用，不含断言）

| 级别 | 数量 | 占比 | 是否纳入告警 |
|------|------|------|-------------|
| FATAL | 193 | 4.6% | 是（P0；注意非断言 FATAL 主要在 Rust 启动路径） |
| ERROR | 373 | 8.9% | 是（P1，需白名单） |
| WARNING | 1339 | 32.1% | 部分（需白名单） |
| INFO | 1369 | 32.8% | 否（事件类除外） |
| DEBUG | 896 | 21.5% | 否 |

断言单列：`is_assertion=2445`（C++ `TORCH_CHECK/CHECK_*` 644、Rust `assert!`/`panic!` 1933，其中 1563 条位于 `tests.rs` 单测文件，见 9.3）。失败即 abort，不以日志落盘，**不能当可检索关键字**；正确用途是启动期 smoke test 与崩溃归因（配合日志断档 + 进程重启判定）。

### 1.3 日志按包分布（Top 10，含断言）

| 包 | 埋点数 | ERROR/WARN/FATAL（非断言） | 主要职责 |
|----|--------|-----------|----------|
| rust/src | 2229 | 283 | Rust 前端服务（HTTP/gRPC/编排）；其中 1933 条为断言（1563 在单测） |
| vllm/distributed | 765 | 352 | 分布式通信（NCCL/并行/connector） |
| vllm/v1 | 728 | 304 | V1 引擎核心（调度/输出处理/指标） |
| vllm/model_executor | 663 | 278 | 模型加载/执行/warmup |
| csrc/cpu | 644 | 0（全为 TORCH_CHECK 断言） | CPU 算子内核 |
| vllm/entrypoints | 253 | 106 | API server/CLI 入口 |
| vllm/models | 188 | 68 | 模型实现 |
| vllm/config | 177 | 97 | 配置校验 |
| vllm/platforms | 139 | 54 | 平台适配（CUDA/ROCm/TPU/CPU/XPU） |
| vllm/tool_parsers | 134 | 70 | 工具调用解析器 |

### 1.4 上下文证据摘要（实际日志调用 4170 条）

| 证据 | 数量 | 占比 | 对方案的影响 |
|------|------|------|-------------|
| in_loop（循环内） | 536 | 12.9% | 禁止做频率告警，只能做趋势或采样 |
| in_except（异常分支） | 634 | 15.2% | error 类主力 |
| once（仅打印一次） | 1072 | 25.7% | 只做存在性告警 |
| conditional（条件分支） | 2196 | 52.7% | 特性开关相关，默认配置下可能不触发 |
| is_assertion（断言） | 2445 | 37.0% | 不是可检索日志，单列 |
| 占位符风格 | printf 2319 / brace 165 / fstring 49 / none 1771 | — | 模板变量不能当固定关键字全文匹配 |

---

## 2. 日志埋点明细（按语义分类）

分类基于证据字段（in_except/level/template 关键词/once/conditional），全量 4170 条按 `包+级别+模板` 聚合后打标；明细数据见提取结果，此处按类别给汇总与代表模板。

### 2.1 分类分布

| main_tag | 记录数 | 占比 | 唯一模板数 | 说明 |
|---|---|---|---|---|
| lifecycle | 1840 | 44.1% | 1663 | 启动/加载/关闭/就绪（含 Rust 前端编排） |
| dev_debug | 896 | 21.5% | 819 | DEBUG 级，不进告警 |
| system_error | 800 | 19.2% | 609 | 框架/系统异常（in_except 或 ERROR/FATAL） |
| resource | 247 | 5.9% | 238 | KV cache/GPU 内存/水位 |
| request | 189 | 4.5% | 173 | 请求处理路径 |
| dependency | 124 | 3.0% | 121 | 下载/连接/外部存储 |
| metric | 46 | 1.1% | 39 | 耗时/计数打点（大多已被指标替代） |
| business_error | 28 | 0.7% | 28 | 拒绝/参数非法类 |

附加标签分布（记录数）：`one_shot` 1072、`feature_gated` 2196、`high_volume` 536、`sensitive`（模板含 token/key 等字样）若干——脱敏要求见 7.3。

### 2.2 生命周期类代表模板（告警价值最高）

| 文件:行 | 级别 | 模板 | once | 监控用途 |
|---------|------|------|------|----------|
| vllm/v1/engine/core.py:1709 | ERROR | `EngineCore encountered a fatal error.` | 否 | P0 引擎致命错误 |
| vllm/v1/executor/multiproc_executor.py:380 | ERROR | `Worker proc %s died unexpectedly (exit code: %s), shutting down executor.` | 否 | P0 worker 崩溃 |
| vllm/v1/executor/ray_executor_v2.py:522 | ERROR | `RayWorkerProc rank=%s died unexpectedly, shutting down executor.` | 否 | P0（Ray 部署） |
| vllm/entrypoints/launchers/launcher.py:188 | INFO | `Available routes are:` | 否 | 启动成功就绪标志（存在性告警） |
| vllm/entrypoints/cli/serve.py:271 | INFO | `Shutting down.` | 否 | 计划内关停（白名单） |
| vllm/v1/core/kv_cache_utils.py:1171 | INFO | `%s KV cache size: %s tokens, Maximum concurrency for %s tokens per request: %.2fx` | 否 | 启动期容量快照 |
| rust/src/cmd/src/main.rs:75-76 | INFO | `received shutdown signal (Ctrl-C/SIGTERM), shutting down...` | 否 | Rust 前端信号关停 |
| vllm/entrypoints/cli/preload.py:399 | ERROR | `Weight cache daemon rank(s) exited during %s (exitcodes=%s); shutting down.` | 否 | P1 权重缓存守护进程退出 |

### 2.3 错误类代表模板（非断言 ERROR，生产路径）

| 文件 | 次数 | 模板 | 归类 |
|---|---|---|---|
| vllm/tool_parsers（13+10 处） | 23 | `Error in extracting tool call from response.` / `Error trying to handle streaming tool call.` | system_error（解析失败降级为不调用工具） |
| vllm/entrypoints | 5 | `Error with model %s` | system_error |
| vllm/distributed | ~10 | NCCL communicator 创建/销毁失败类 | system_error（依赖故障） |
| vllm/v1 | 2 | `EngineCore encountered a fatal error.` | system_error（P0） |
| vllm/kernels | 2 | `Failed to load config file %s: %s` | system_error |

### 2.4 其余类别

| 类别 | 数量 | 代表信号 |
|---|---|---|
| resource | 247 | KV cache/GPU 内存告警与水位提示（多数已由 `vllm:kv_cache_usage_perc` 承载） |
| request | 189 | 请求参数校验/流式处理提示 |
| dependency | 124 | HF 下载、外部存储连接、KV connector 传输失败 |
| metric（日志打点） | 46 | `Avg prompt throughput: %.1f tokens/s`（loggers.py:283）等，已被指标替代 |
| business_error | 28 | 拒绝/参数非法（多数在响应体返回 4xx，日志仅辅助） |

---

## 3. 指标清单

### 3.1 核心引擎指标（`vllm/v1/metrics/loggers.py`，标签 `model_name, engine`）

| 指标名（暴露名） | 类型 | 语义角色 | 标签 | 源码位置 | 有写入点 | 备注 |
|--------|------|----------|------|----------|---------|------|
| vllm:request_success | Counter | RED.Rate/Errors | +`finished_reason`(stop/length/abort/error/repetition) | loggers.py:808-815,1290 | 是 | 错误率 = `finished_reason="error"` 分量；abort 为客户端断开（白名单） |
| vllm:time_to_first_token_seconds | Histogram | RED.Duration | 基础 | loggers.py:930 | 是 | 秒；TTFT 桶 1ms~2560s，亚秒段最密（buckets.py:57-80） |
| vllm:inter_token_latency_seconds | Histogram | RED.Duration | 基础 | loggers.py:940 | 是 | 秒；每 decode 步 10ms~80s（buckets.py:84-104） |
| vllm:e2e_request_latency_seconds | Histogram | RED.Duration | 基础 | loggers.py:960 | 是 | 秒；0.3s~7680s（buckets.py:30-52） |
| vllm:request_queue_time_seconds | Histogram | USE.Saturation | 基础 | loggers.py:970 | 是 | 排队时间分位 |
| vllm:request_prefill_time_seconds | Histogram | RED.Duration | 基础 | loggers.py:990 | 是 | prefill 阶段 |
| vllm:request_decode_time_seconds | Histogram | RED.Duration | 基础 | loggers.py:1000 | 是 | decode 阶段 |
| vllm:request_inference_time_seconds | Histogram | RED.Duration | 基础 | loggers.py:980 | 是 | prefill+decode 合计 |
| vllm:request_time_per_output_token_seconds | Histogram | RED.Duration | 基础 | loggers.py:950 | 是 | 每 token 出速（TPOT） |
| vllm:num_requests_running | Gauge | USE.Saturation | 基础 | loggers.py:527 | 是 | 并发运行数 |
| vllm:num_requests_waiting | Gauge | USE.Saturation | 基础 | loggers.py:537 | 是 | 排队数 |
| vllm:num_requests_waiting_by_reason | Gauge | USE.Saturation | +`reason` | loggers.py:547 | 是 | 按等待原因细分 |
| vllm:kv_cache_usage_perc | Gauge | USE.Utilization | 基础 | loggers.py:619 | 是 | **量纲 0~1**，1=100% |
| vllm:num_preemptions | Counter | USE.Saturation | 基础 | loggers.py:738 | 是 | 引擎累计抢占 |
| vllm:request_num_preemptions | Histogram | USE.Saturation | 基础 | loggers.py:1010 | 是 | 每请求抢占分布，桶 1~20 |
| vllm:admission_rejections | Counter | RED.Errors | +`reason` | loggers.py:847 | 是 | 准入拒绝（队列上限等） |
| vllm:corrupted_requests | Counter | RED.Errors | 基础 | loggers.py:630 | 是 | 条件定义：`VLLM_COMPUTE_NANS_IN_LOGITS` 开启才写入 |
| vllm:generation_tokens | Counter | RED.Rate | 基础 | loggers.py:798 | 是 | 吞吐 |
| vllm:prompt_tokens | Counter | RED.Rate | 基础 | loggers.py:747 | 是 | 吞吐 |
| vllm:prompt_tokens_cached | Counter | RED.Rate | 基础 | loggers.py:772 | 是 | 缓存命中 token |
| vllm:prefix_cache_hits / _queries | Counter | USE.Utilization | 基础 | loggers.py:661,672 | 是 | 前缀缓存命中率 = hits/queries |
| vllm:mm_cache_hits / _queries | Counter | USE.Utilization | 基础 | loggers.py:713,724 | 是 | 多模态缓存 |
| vllm:iteration_tokens_total | Histogram | USE.Saturation | 基础 | loggers.py:887 | 是 | 每 step 处理 token 数，桶 1~16384 |
| vllm:request_prompt_tokens | Histogram | RED.Rate | 基础 | loggers.py:864 | 是 | 请求 prompt 长度分布 |
| vllm:request_params_n / _max_tokens | Histogram | — | 基础 | loggers.py:907,917 | 是 | 请求参数分布 |
| vllm:cache_config_info | Gauge | — | 配置项 | loggers.py:1127 | 是 | 启动期配置快照 |
| vllm:engine_sleep_state | Gauge | 状态 | +`sleep_state` | loggers.py:593 | 是 | 引擎睡眠模式状态 |
| vllm:lora_requests_info | Gauge | — | +lora 名 | loggers.py:1103 | 是 | 条件定义：启用 LoRA 才注册 |

### 3.2 子系统/connector 指标（条件注册，需对应功能启用）

| 族 | 指标 | 源码位置 | 注册条件 |
|---|---|---|---|
| spec decode | vllm:spec_decode_num_accepted_tokens_per_pos 等 | vllm/v1/spec_decode/metrics.py:254 | 启用投机解码 |
| KV 驻留（采样 1%） | vllm:kv_block_lifetime_seconds / idle_before_evict / reuse_gap | vllm/v1/metrics/loggers.py:1041,1054,1067 | `kv_cache_metrics=True` |
| MFU | vllm:estimated_flops_per_gpu_total 等 3 个 | vllm/v1/metrics/perf.py:1763-1787 | `enable_mfu_metrics=True` |
| HTTP 层（Python） | http_request_duration_seconds（instrumentator 默认指标，handler/method/status 标签） | vllm/entrypoints/serve/instrumentator/metrics.py:64-74 | 无条件（随 /metrics 挂载） |
| HTTP 层（Rust） | http_request_duration_seconds + http_request_duration_highr_seconds | rust/src/metrics/src/api_server.rs:88-101 | Rust API server |
| 睡眠/RL | vllm:sleep_mode_* / vllm:rl_weight_update_*（4 个，**均无 documentation**） | vllm/entrypoints/serve/dev/sleep/metrics.py:20-27、vllm/entrypoints/rl/online/metrics.py:18-25 | 对应操作触发 |
| websocket（语音实时） | vllm:websocket_connections_total/active/_duration_seconds | vllm/entrypoints/speech_to_text/realtime/metrics.py:22-33 | 语音实时接口 |
| mooncake | 12 个（传输字节/失败数/时延等） | .../mooncake/stats.py:204-250、store/metrics.py:115-152 | 启用 mooncake connector |
| nixl | 8 个 | .../nixl/stats.py:187-275 | 启用 nixl connector |
| hf3fs | 4 个 | .../hf3fs/hf3fs_connector.py:1135-1164 | 启用 hf3fs connector |
| hisparse | 7 个（3 个条件定义） | .../hisparse/stats.py:102-182 | 启用 hisparse + kv_cache_metrics |
| kv_offload | 3 个定义 + 42 个字面量引用（7.4） | .../offloading/metrics.py:409-429、vllm/v1/kv_offload/cpu/common.py:13-17 | 启用 KV offload |
| 工具解析 | vllm:tool_call_parser_invocations_total（model_name/mode/outcome/request_type） | vllm/parser/metrics.py:37-45 | 惰性注册；**解析异常当前计入 no_tool_call，无失败分量**（parser/metrics.py:74-77） |

### 3.3 指标质量风险

| 风险 | 指标数 | 具体 | 影响 |
|------|--------|------|------|
| 条件定义（可能未启用） | 11 | corrupted_requests / watermark_requests / kv_block_* / hisparse_host_block_* / lora_requests_info / spec_decode_* / num_requests_kv_fetch_by_stage | 默认配置下不注册，告警永不触发；打开前先查 1.0.1 开关表 |
| 无 documentation | 4 | sleep_mode_operation_duration_seconds、sleep_mode_operations_in_flight、rl_weight_update_operation_duration_seconds、rl_weight_update_operations_in_flight | 语义靠猜，告警易设错 |
| 无直接匹配写入点 | 42+8 | kv_offload 常量注册表族 + connector 族（经辅助函数/注册表写入） | 需按 7.4 确认，不能断言为 0 |
| 高基数标签 | 0 | 全部标签为 model_name/engine/reason 等低基数字段 | 无风险（lora_requests_info 的 lora 名与 sleep 的 operation 亦为小集合） |
| 指标名覆盖率 | 98/140 | 42 个字面量为类常量注册表定义（如 `CPUOffloadingMetrics.STORES_SKIPPED` → spec.py:86 注册） | 非缺失，是定义方式未走标准构造器 |

### 3.4 指标暴露方式

| 方式 | 位置 | 说明 |
|------|------|------|
| HTTP 拉取（Python 主路径） | `GET /metrics` | Prometheus 文本；随 API server 无条件挂载（metrics.py:56-81）；多进程经 `PROMETHEUS_MULTIPROC_DIR` 聚合（未设则自动建临时目录，prometheus.py:21-26） |
| HTTP 拉取（Rust 前端） | `GET /metrics` | routes.rs:147；与 Python 侧同名指标并存 |
| HTTP 拉取（离线批处理） | `start_http_server(port, addr)` | 3 处 run_batch 入口，端口由参数指定 |
| OpenMetrics 兼容 | counter 自动补 `_total` | Rust 侧显式约定（rust/src/metrics/src/lib.rs:28-31）；面板/告警统一用暴露名 |

---

## 4. 信号仲裁

需求清单按「在线 LLM 推理服务」形态选取（RED + USE + 推理领域扩展）。

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---------|---------|---------|---------|------|
| 1 | 服务是否活着 | 指标+日志 | `/health`（引擎死亡 503）+ up | health.py:22-34 | 已覆盖 |
| 2 | 启动是否成功 | 日志 | `Available routes are:` | launcher.py:188 | 已覆盖（存在性告警） |
| 3 | 请求延迟分位（e2e/TTFT/TPOT） | 指标 | vllm:e2e_request_latency_seconds、vllm:time_to_first_token_seconds、vllm:request_time_per_output_token_seconds | loggers.py:930,950,960 | 已覆盖（Histogram，`histogram_quantile`） |
| 4 | 错误率 | 指标 | vllm:request_success{finished_reason="error"} | loggers.py:808-815 | 已覆盖（error/总 finished 比值） |
| 5 | 客户端主动断开 | 指标 | request_success{finished_reason="abort"} | loggers.py:1290 | 已覆盖（进白名单，不告警） |
| 6 | 排队/饱和度 | 指标 | num_requests_waiting(_by_reason)、request_queue_time_seconds | loggers.py:537,547,970 | 已覆盖 |
| 7 | KV cache 水位 | 指标 | vllm:kv_cache_usage_perc（0~1） | loggers.py:619 | 已覆盖 |
| 8 | 抢占/驱逐 | 指标 | vllm:num_preemptions、vllm:request_num_preemptions | loggers.py:738,1010 | 已覆盖 |
| 9 | 准入拒绝 | 指标 | vllm:admission_rejections{reason} | loggers.py:847 | 已覆盖 |
| 10 | 吞吐（tokens/s） | 指标 | vllm:prompt_tokens、vllm:generation_tokens | loggers.py:747,798 | 已覆盖（rate()） |
| 11 | 前缀缓存命中 | 指标 | prefix_cache_hits/queries | loggers.py:661,672 | 已覆盖 |
| 12 | 一次请求慢在哪一段 | 链路 + 指标 | 首选指标三段直方图（queue/prefill/decode）；trace `llm_request` 属性含 TTFT/e2e/排队/prefill/decode 全套 | output_processor.py:958-1001 | 已覆盖；trace 默认关闭（1.0.1），指标即可回答分段问题 |
| 13 | 引擎/worker 崩溃 | 日志 | `EngineCore encountered a fatal error.` / `Worker proc %s died unexpectedly` | core.py:1709、multiproc_executor.py:380 | 已覆盖（P0 事件） |
| 14 | HTTP 层延迟/状态码 | 指标 | http_request_duration_seconds（Python+Rust，handler/status 标签） | metrics.py:64-74、api_server.rs:88-101 | 已覆盖 |
| 15 | 工具调用解析失败 | 日志 | `Error in extracting tool call from response.`（ERROR） | vllm/tool_parsers 多处 | 部分覆盖：指标无失败分量（parser/metrics.py:74-77 TODO），只能做 L3 日志频率告警 |
| 16 | 投机解码接受率 | 指标 | vllm:spec_decode_num_accepted_tokens_per_pos | spec_decode/metrics.py:254 | 已覆盖（启用投机解码时） |
| 17 | KV 传输/分层缓存健康 | 指标 | mooncake/nixl/hf3fs/hisparse/umbp 各族 | 3.2 表 | 已覆盖（随 connector 启用） |
| 18 | GPU 硬件层（显存/ECC/温度/利用率） | 指标 | 无 | — | **真缺口**：需外部 DCGM exporter / nvidia-smi 采集，不改源码（检查记录见 4.1） |
| 19 | 崩溃归因（abort/panic） | 日志断言 | TORCH_CHECK/assert!/panic!（不落盘） | csrc、rust/src | 不能做检索关键字；用「日志断档 + 进程重启 + /health 503」组合判定 |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果 |
|------|-----------|------|
| 日志 | 检索 4170 条非断言日志（唯一模板 3731），关键词：gpu util/memory usage/temperature/ecc/nvidia-smi/driver | 仅有 KV cache/GPU 内存分配类业务日志，无硬件层打点 |
| 指标 | 检索 98 个定义名 + 143 个字面量引用（含 42 个未匹配注册表名），关键词：gpu/vram/util/temp/power/ecc/thermal | 无 GPU 硬件层指标（`vllm:kv_cache_usage_perc` 是 KV cache 逻辑水位，非显存物理占用） |
| 链路 | 检索 37 个 span + 请求级 `llm_request`，关键词同上 | 无硬件层 span/属性 |

结论：GPU 硬件层属「源码之外」的资源层，三平面均无信号 → 接外部 exporter。

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配）

| 关键字 | 级别 | 说明 | 来源 |
|--------|------|------|------|
| `EngineCore encountered a fatal error.` | P0 | 引擎核心致命错误 | vllm/v1/engine/core.py:1709 |
| `died unexpectedly`（Worker proc / RayWorkerProc） | P0 | worker 进程崩溃 | vllm/v1/executor/multiproc_executor.py:380、ray_executor_v2.py:522 |
| `Weight cache daemon rank(s) exited during` | P1 | 权重缓存守护进程退出 | vllm/entrypoints/cli/preload.py:399 |
| `coordinator output channel closed` | P1 | 协调器输出通道关闭 | rust/src/engine-core-client/src/coordinator/inproc.rs:268 |
| `engine core exited unexpectedly` | P1 | 引擎核心意外退出触发清理 | vllm/v1/engine/core_client.py:851 |
| `Available routes are:` | 存在性 | 启动成功标志（反向告警：N 秒未出现） | vllm/entrypoints/launchers/launcher.py:188 |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在模板 | 统计方式 | 维度 |
|------|---------|---------|------|
| `%.1f tokens/s` | `Engine %03d: Avg prompt throughput: %.1f tokens/s`（loggers.py:283） | 数值提取趋势；空闲时降为 DEBUG，不能只按 INFO 采样判定空闲 | engine_index |
| `exit code: %s` | `Worker proc %s died unexpectedly (exit code: %s)` | 按退出码分组计数 | engine |
| `%.2fx` | `Maximum concurrency for %s tokens per request: %.2fx`（kv_cache_utils.py:1171） | 启动期容量基线记录 | model |

> 占位符均为 printf 风格（2319 条），变量部分禁止当固定字符串匹配。

### 5.3 降噪白名单

| 关键字/条件 | 原因 | 处理 |
|------------|------|------|
| `finished_reason="abort"`（request_success 分量） | 客户端主动断开 | 不计入错误率；仅异常放大时 P3 观察 |
| `Shutting down.` / `received shutdown signal` / `DPSupervisor received %s, shutting down.` | 计划内关停 | 排除（serve.py:271、main.rs:75、dp_supervisor.py:389） |
| `Shutting down profiler` | 计划内性能分析 | 排除（wrapper.py:194） |
| `Avg prompt throughput` 空闲期 DEBUG | 引擎空闲自动降级（loggers.py:280） | 不做日志告警，吞吐用指标 |
| 重试/降级路径 WARNING（如 flashinfer backend 自动回退、可选依赖缺失 fallback） | 降级路径，服务仍正常 | P3 统计降级次数 |
| 冷启动/首次初始化 INFO（模型加载、warmup、编译缓存） | 启动期必然出现 | 启动后窗口期外排除 |
| once=true 的重复提示（1072 条） | 只打印一次，频率无意义 | 只做存在性判断 |
| conditional 非空的日志（2196 条） | 特性开关之下可能不触发 | 先确认开关再决定监控 |

### 5.4 检索语句示例

```promql
# 请求错误率（error 结束 / 全部结束；不含 abort——客户端断开进白名单）
sum(rate(vllm:request_success_total{finished_reason="error"}[5m]))
/
sum(rate(vllm:request_success_total[5m]))

# TTFT P99（按模型分组）
histogram_quantile(0.99,
  sum(rate(vllm:time_to_first_token_seconds_bucket[5m])) by (le, model_name)
)

# KV cache 水位（0~1）
max by (model_name, engine) (vllm:kv_cache_usage_perc)

# 准入拒绝速率
sum by (reason) (rate(vllm:admission_rejections_total[5m]))
```

```logql
# 引擎致命错误（事件告警）
{job="vllm"} |= "EngineCore encountered a fatal error."

# worker 崩溃（提取退出码字段）
{job="vllm"} |~ "Worker proc .* died unexpectedly" | regexp "exit code: (?P<code>\\d+)"
```

---

## 6. 告警规则草稿（候选阈值，待 SLO/压测/线上数据校准）

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|--------|------|---------|------|------|--------|
| TTFT P99 劣化 | `histogram_quantile(0.99, sum(rate(vllm:time_to_first_token_seconds_bucket[5m])) by (le, model_name)) > T` | T=1s（待按模型/SLO 校准；单位秒，桶密段在亚秒） | P1 | 持续 2 周期；按 model_name 分组；排除冷启动窗口 | 批处理长 prompt 场景单独基线 |
| E2E P99 劣化 | `histogram_quantile(0.99, sum(rate(vllm:e2e_request_latency_seconds_bucket[5m])) by (le, model_name)) > T` | T=30s（待校准；桶上限 7680s，长生成任务需单独基线） | P1 | 同上 | 计划内长文本任务 |
| 请求错误率 | `error 分量 rate / 全部分量 rate > T`（5.4 第 1 条） | T=1%（待历史数据校准） | P1 | 持续 5 分钟；分 model_name | abort/repetition 分量不计入 |
| KV cache 水位过高 | `vllm:kv_cache_usage_perc > 0.95`（量纲 0~1）持续 5 分钟 | 0.95（待压测确认抢占拐点） | P1 | 与 num_requests_waiting 联动判断 | 睡眠模式（engine_sleep_state）期间 |
| 排队持续积压 | `vllm:num_requests_waiting > 0 持续 10 分钟` 且 `num_requests_waiting_by_reason{reason="capacity"} 占比高` | 非零持续（待确认基线） | P2 | 按 reason 细分；先看 by_reason 是否容量型 | 低流量时段 |
| 抢占激增 | `increase(vllm:num_preemptions_total[10m]) > T` | T=10（待校准） | P1 | 与 kv_cache_usage_perc 联动 | 预emption 压测窗口 |
| 准入拒绝激增 | `sum by (reason) (rate(vllm:admission_rejections_total[5m])) > 基线*3` | 3 倍基线（待确定） | P2 | 按 reason 分组 | 客户端主动重试风暴 |
| 输出损坏请求 | `increase(vllm:corrupted_requests_total[10m]) > 0` | 任何非零 | P1 | — | 无（该指标出现即异常；注意需 `VLLM_COMPUTE_NANS_IN_LOGITS` 开启才有数据） |
| HTTP 5xx 率 | `http_request_duration_seconds_count{status=~"5.."}` 速率 / 总速率 > T | T=1%（待校准） | P1 | 分 handler；排除 /health | /metrics /health 已被 instrumentator 排除（metrics.py:65-72） |
| 引擎死亡 | `/health` 探针 503（up==0 或 `EngineDeadError` 路径） | 任何 503 | P0 | 与重启事件关联去重 | 计划内重启 |
| 引擎进入睡眠 | `vllm:engine_sleep_state{sleep_state=...} 变更` | 状态变化即告 | P3 | 仅睡眠部署形态启用 | 非睡眠部署不配置 |

### 6.2 日志事件告警（L2）

| 告警名 | 条件 | 等级 | 降噪 |
|--------|------|------|------|
| 引擎核心致命错误 | 出现 `EngineCore encountered a fatal error.`（core.py:1709） | P0 | 按实例去重；与 /health 503、进程重启关联 |
| Worker 进程崩溃 | 出现 `died unexpectedly` 模板（multiproc_executor.py:380、ray_executor_v2.py:522） | P0 | 按引擎/rank 去重；连续多次升级 |
| 启动超时 | 重启后 N 秒内未出现 `Available routes are:`（launcher.py:188） | P0 | N=600s 候选（待按模型加载实测校准；大模型加载可达数十分钟） |
| 权重缓存守护进程退出 | 出现 `Weight cache daemon rank(s) exited during`（preload.py:399） | P1 | 按退出码分组 |
| 协调器/输出通道断开 | `coordinator output channel closed`（inproc.rs:268）、`engine core exited unexpectedly`（core_client.py:851） | P1 | 按进程去重 |

### 6.3 日志频率告警（L3，谨慎；仅无对应指标者）

| 告警名 | 条件 | 基线来源 | 等级 |
|--------|------|---------|------|
| 工具调用解析错误突增 | `sum(rate({job="vllm"} \|= "Error in extracting tool call from response." [5m])) > 基线*3` | 部署后 7 天历史（parser 指标无失败分量，parser/metrics.py:74-77） | P2 |

> 该异常被降级为「未调用工具」，仅 ERROR 日志可见；必须配白名单（非工具类对话模型天然 0 基线，突增才有意义）与持续 3 周期判定。in_loop 日志（536 条）一律不做频率告警。

### 6.4 链路告警（L4，可选；仅当 1.0.1 两个 trace 开关都打开时）

| 告警名 | span | 条件 | 等级 |
|--------|------|------|------|
| 请求级分段耗时劣化 | `llm_request`（SpanKind.SERVER，属性含 gen_ai.prompt/completion tokens、TTFT、排队、prefill/decode/inference 时间） | span P95 的 `gen_ai.latency.time_in_queue` 环比上升 > 50% | P2 |

> 采样与成本约束：trace 默认关闭；即使指标平面已有三段耗时直方图（queue/prefill/decode），L4 只用于「单请求下钻」，不替代 L1。span 属性完整清单见 output_processor.py:965-1001。

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号，检查记录见 4.1）

| 场景 | 风险 | 建议做法 | 优先级 |
|------|------|---------|--------|
| GPU 硬件层（显存占用、利用率、温度、ECC、功耗） | 无法感知硬件故障与物理显存压力（`kv_cache_usage_perc` 是逻辑水位，非物理显存） | 接入 DCGM exporter / 平台侧采集，不改源码 | P1 |
| 主机层（CPU/内存/磁盘/网络） | 无法感知宿主机瓶颈 | node exporter（平台侧） | P2 |
| 工具调用解析失败无指标分量 | 只能靠 L3 日志告警，阈值不可跨环境复用 | 上游已列 TODO（parser/metrics.py:74-77）：增加 `error` outcome 枚举并透传异常 | P3（跟上游） |

### 7.2 已有但不可用（信号存在但默认拿不到）

| 信号 | 问题 | 建议 |
|------|------|------|
| vllm:corrupted_requests | 条件定义：需 `VLLM_COMPUTE_NANS_IN_LOGITS`（loggers.py:630 证据字段 conditional） | 生产如需监控输出损坏，显式开启并接受开销 |
| vllm:kv_block_lifetime_seconds 等 3 个 | 需 `kv_cache_metrics=True`，采样 1%（observability.py:62-68） | 需要块级驻留分析时开启 |
| vllm:spec_decode_* | 需启用投机解码 | 按部署形态决定 |
| llm_request span 全套分段 | 需 `--otlp-traces-endpoint` + 可选 `--collect-detailed-traces` | 打开后先验证 collector 收数（1.0.1） |
| mooncake/nixl/hf3fs/hisparse/kv_offload 指标族 | 随 connector 启用才注册 | 部署清单与抓取配置同步维护 |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|------|------|------|
| 4 个指标无 documentation | sleep_mode_*（dev/sleep/metrics.py:20,27）、rl_weight_update_*（rl/online/metrics.py:18,25） | 补 documentation 字符串 |
| 敏感字段进日志模板（token/key 字样，含 tokenizer 词表、模型路径） | 分类结果 `sensitive` 标签集 | 采集端脱敏或降采样；确认无凭据明文 |
| 循环内 INFO（536 条 in_loop） | 多在分布式/执行热路径 | 趋势观察，禁频率告警（本方案已遵守） |

### 7.4 需人工确认（脚本未能确定，非「不存在」）

| 项 | 原因 |
|----|------|
| 42 个 `vllm:kv_offload_*` / `vllm:simple_kv_offload_*` 等注册表指标 | 指标名是类常量（如 `CPUOffloadingMetrics.STORES_SKIPPED`，kv_offload/cpu/common.py:13），经元数据注册表动态注册（spec.py:86）；已核实注册路径存在，注册条件（store_threshold 等 extra_config）需按部署确认 |
| hf3fs/mooncake/nixl 写入点未直接匹配（8 个 w=?） | 经辅助函数写入（指标对象作为参数传入）；指标定义真实存在（connector stats.py），运行时是否产生数据取决于 connector 启用 |
| `Avg prompt throughput` 等 stats 日志的多引擎聚合路径（AggregatedLoggingStatLogger） | loggers.py:358-431，聚合语义需按 DP 部署确认 |
| Rust 前端与 Python 引擎并存的抓取拓扑（两套 /metrics 是否合一） | 取决于部署形态（Rust 前端托管 headless engine 或分离），需运行时确认 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本重新提取（三平面，命令同本方案 Step 1）
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json

# 逐面 diff
python3 diff_signals.py loggers_v1.json  loggers_v2.json --output diff_logs.json
python3 diff_signals.py metrics_v1.json  metrics_v2.json --output diff_metrics.json
python3 diff_signals.py traces_v1.json   traces_v2.json  --output diff_traces.json
```

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---------|-------------|------|
| 新增 ERROR 日志 | 新故障模式 | 评估新增 L2/L3 告警 |
| 新增指标 | 新可观测能力 | 评估新增 L1 告警；核对是否条件注册 |
| 删除指标 | 告警静默失效 | **必须下线对应规则** |
| 指标标签变更 | PromQL 静默失效 | 更新查询与看板（重点盯 `finished_reason`/`reason` 枚举） |
| 桶边界变更 | 分位精度变化 | 重新校准 P99 阈值（buckets.py 是单一来源） |
| 开关默认值变更 | 数据流断裂 | 重新核对 1.0.1 表，优先级最高 |
| ObservabilityConfig 新字段 | 新能力默认值 | 同步更新开关表与验证步骤 |

### 8.3 diff 的局限性

- 按「文件名 + 模板」模糊匹配，忽略行号偏移（刻意设计）；同文件内完全相同的模板会被折叠 → 计数是近似值。
- Rust `tests.rs` 内断言（1563 条）不构成生产信号变化，diff 日志平面时应过滤 `is_assertion=true` 且路径含 `/tests` 的记录。

---

## 9. 局限性

### 9.1 阈值与常量

1. 所有阈值均为**候选值**，需结合 SLO、压测基线、线上历史校准。
2. 源码里的超时/重试/容量常量是业务配置，不是告警阈值（如 KV cache 容量日志中的 `Maximum concurrency %.2fx` 是启动基线快照）。
3. `vllm:kv_cache_usage_perc` 量纲 0~1；`http_request_duration_seconds` 单位秒；PromQL 编写时注意。

### 9.2 静态分析边界

1. 提取覆盖率不是 100%：指标名常量注册表（42 个）、辅助函数写入、动态命名均无法完整静态解析；本方案已将此类全部列入 7.4「需人工确认」而非「不存在」。
2. 静态分析看不到运行时：条件注册指标（11 个）默认配置下不出现；trace 默认关闭；采样率（kv_cache_metrics_sample=0.01）与多进程聚合（PROMETHEUS_MULTIPROC_DIR 清理时机，prometheus.py:31-36）需上线验证。
3. prometheus-fastapi-instrumentator 的默认 HTTP 指标标签（handler/method/status）来自中间件，动态路由的 handler 标签值需抓取后核对（metrics.py:17-49 有路由名兼容补丁）。
4. 断言失败（TORCH_CHECK/assert!/panic!，共 2445 处）不落盘：崩溃检测依赖「日志断档 + 进程退出 + /health 503」组合，而非关键字检索。

### 9.3 提取范围与已知污染

1. 脚本默认排除 tests/docs/examples/benchmarks 等目录；本仓库 `rust/src/**/tests.rs`（单测与生产同包）无法按目录排除，已识别 1563 条测试断言并从生产统计中剔除；`rust/src/bench`（125 条）与 `.buildkite`/`tools`（71 条）为基准/CI 代码，分类时已标注，不进告警。
2. 语言覆盖：Python（AST）、Rust（正则）、C/C++（CHECK_* 正则）、JS（4 条，前端静态资源）均已扫；CUDA（.cu，109 文件）无内置日志模式，仅含少量断言，未纳入统计——该层无运行时日志门面，属脚本能力边界（见 SKILL.md 能力矩阵）。
3. 指标暴露名以抓取端为准：Rust counter 暴露为 `*_total`（lib.rs:28-31），源码检索时 `vllm:num_preemptions` 与 `vllm:num_preemptions_total` 需都试。
4. GPU 硬件层、主机层属平台采集范畴，本方案结论为「需外部 exporter」，不构成对 vLLM 源码的埋点缺陷判定。
