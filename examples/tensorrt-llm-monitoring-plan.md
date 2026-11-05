# TensorRT-LLM 监控方案（源码 → 三平面监控）

- 分析对象：NVIDIA TensorRT-LLM `1.4.0rc3`（`tensorrt_llm/version.py:15`）
- 运行形态：**在线推理服务**（`trtllm-serve`，`setup.py:723` → `tensorrt_llm/commands/serve.py`），FastAPI/uvicorn HTTP（OpenAI / Anthropic 协议）为主，可选 gRPC（`smg` / `openengine` 协议）；同时兼有 Python 库（`LLM` API）与 C++ 运行时库形态，服务形态结论以 `trtllm-serve` 为准
- 源码语言（实测，非估算）：

| 语言 | 文件数 | 代码行数 | 行数占比 |
|---|---|---|---|
| Python (`.py`) | 2701 | 1,442,322 | 64.6% |
| C++/CUDA (`.cpp/.h/.hpp/.cu/.cuh/.cc/.c`) | 1771 | 790,086 | 35.4% |

  注：C++ 代码除 `cpp/` 顶层目录外，还以 `tensorrt_llm/_torch/**/csrc/*.cpp`（Torch 自定义算子）与 `thop/` 形式嵌在 Python 包内，扫描范围必须包含。
- 扫描文件数：8971 条日志埋点、220 条指标记录、313 条链路记录对应的全仓库源码（未排除生产目录；`examples/`、`bench/`、`tests/` 中的信号已按包归位并在分类时降权）
- 提取方式：脚本提取（Python AST + C++ 正则）+ 规则化语义分类；C++ 指标构造与 C++ 内 NVTX 之外的 span 未内置脚本支持，相关部分走人工降级，见第 9 章

---

## 1. 项目画像与三平面总览

### 1.0 项目画像

| 项 | 结论 | 证据 |
|---|---|---|
| 日志门面（Python） | 自研 logger：`logging` 标准库封装，`[I]/[W]/[E]/[F]` 标签解析、按 module 前缀提取，全局单例 `logger` | `tensorrt_llm/logger.py:27-56`；调用点如 `tensorrt_llm/serve/openai_server.py:797` |
| 日志门面（C++） | 自研 `Logger` 类 + `TLLM_LOG_TRACE/DEBUG/INFO/WARNING/ERROR/EXCEPTION` 宏（printf 风格）；断言宏 `TLLM_CHECK` / `TLLM_CHECK_WITH_INFO` / `TORCH_CHECK` 失败即抛异常/abort | `cpp/include/tensorrt_llm/common/logger.h:397-413`；`cpp/include/tensorrt_llm/common/assert.h:40-66` |
| 指标 SDK | `prometheus_client`（`MetricsCollector` 类改自 vLLM，指标名 = `self.metric_prefix + 字面量`，前缀统一 `trtllm_`，脚本已正确求值前缀） | `tensorrt_llm/metrics/collector.py:37-52, 209` |
| 链路 SDK | OpenTelemetry OTLP（`tracing.py`，GenAI 语义约定属性）；另有 NVTX range（`py_executor`）与生成的 MoE kernel DSL 内 `iket.range_push`（profiler range，非分布式 trace） | `tensorrt_llm/llmapi/tracing.py:63-90,137-167`；`_torch/pyexecutor/py_executor.py:3893` |
| HTTP 暴露 | `GET /metrics` = **JSON 迭代统计**（非 Prometheus 文本）；`GET /prometheus/metrics` = Prometheus 文本（仅 `return_perf_metrics=true` 时注册）；`/health`、`/health_generate`、`/version` 常驻；`/energy_metrics`、`POST /kv_cache_events`、`/start_profile`、`/stop_profile` | 路由注册 `tensorrt_llm/serve/openai_server.py:1226-1252,1236,1334-1351,1370`；handler 实现 `openai_server.py:1793-1833` |
| gRPC 暴露（openengine） | 独立 metrics HTTP 端口（默认 8001），路径 `/prometheus/metrics`，仅 `--grpc --grpc-protocol openengine` 时可用 | `tensorrt_llm/grpc/openengine/metrics.py:208`；`grpc/openengine/server.py:337-340,490-491` |
| 端点语义辨析 | 同名路径两种协议：`/metrics` 返回 JSON（`get_iteration_stats`），`/prometheus/metrics` 返回 Prometheus 文本（`make_asgi_app`）；`prometheus_fastapi_instrumentator` 虽被挂载但 `excluded_handlers=[".*"]`，HTTP 层请求指标实际全部排除，RED 指标完全由 `MetricsCollector` 承担 | `openai_server.py:1236-1238 vs 1353-1373`（`excluded_handlers` 在 1366 行） |
| `/metrics` 消费语义 | **消费式端点**：每次抓取会清空自上次抓取以来积累的迭代统计（tee buffer / queue drain）；多个 scraper 并发抓取会互相抢空 | `openai_server.py:1812-1831`（源码注释明确记录 nvbug 6102381） |
| 健康自愈 | `/health` 探测到引擎 fatal error 时主动向自身发 SIGINT 关停，避免僵尸 pod | `openai_server.py:1710-1722` |

### 1.0.1 可观测开关与默认值（本方案第一优先级）

| 能力 | 开关（LlmArgs 字段） | 源码默认 | CLI 专用参数 | 实际启用方式 | 证据 |
|---|---|---|---|---|---|
| Prometheus 指标采集 + `/prometheus/metrics` 端点 | `return_perf_metrics` | `false` | **无** | `--extra_llm_api_options` YAML 文件或 `--set return_perf_metrics=true`；openengine gRPC 模式下 server 自动置 true | `llmapi/llm_args.py:6245-6251`；`serve.py:1224-1233`（无专用 flag）；`grpc/openengine/server.py:369-374` |
| 请求级指标 JSONL 落盘 | `perf_metrics_output_dir` | `None` | **无** | 同上；设置该目录即开启采集（即使 `return_perf_metrics=false`） | `llmapi/llm_args.py:6263-6269`；`serve/openai_server.py:717-738` |
| 迭代性能统计（`/metrics` 数据源） | `enable_iter_perf_stats` | `false` | **无** | 同上；未开启时 `GET /metrics` 恒返回 `[]` | `llmapi/llm_args.py:6986-6990`；`serve/openai_server.py:1807-1810` |
| 每请求迭代统计 | `enable_iter_req_stats` | `false` | **无** | 同上，且必须同时开 `enable_iter_perf_stats` | `llmapi/llm_args.py:6991-6997` |
| 迭代统计采样间隔 | `iter_perf_stats_interval` | `1`（每迭代） | 无 | 同上 | `llmapi/llm_args.py:6998` |
| OTLP 分布式 trace | `otlp_traces_endpoint` | `None`（关闭） | **有**：`--otlp_traces_endpoint` | 传 collector URL；导出协议由环境变量 `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` 控制，默认 `grpc` | `serve.py:1269-1273`；`llmapi/llm_args.py:6231`；`llmapi/llm.py:532-535`；`llmapi/tracing.py:79-90` |
| 每请求性能指标返回 | 请求头 `X-TRTLLM-return-metrics: 1` | 关（需逐请求携带） | 请求头 | 服务端还需 `return_perf_metrics=true` | `serve/perf_metrics.py:203`；`llmapi/llm_args.py:6249` |
| 分段耗时响应头 | `Server-Timing` / `X-TRTLLM-Start-End-Time` / `X-TRTLLM-Step-Metrics` / `X-TRTLLM-Ctx-Chunk-Metrics` | 随 `return_perf_metrics` + 请求头 | — | 流式响应则以 SSE 事件 `trtllm.perf_metrics` 返回 | `serve/perf_metrics.py:17-47` |
| GPU 能耗监控 | `enable_energy_metrics` | `false` | **无** | 同 YAML / `--set`；失败时仅 WARNING 降级 | `serve/openai_server.py:790-799` |
| per-request spec-decode 统计 | `per_request_spec_decode_stats` | `false` | **无** | 同 YAML / `--set`；与 `return_perf_metrics` 刻意独立 | `llmapi/llm_args.py:6253-6261`；`serve/openai_server.py:724-728` |
| 延迟直方图自定义桶 | `prometheus_metrics_config.*` | `None`（用内置桶） | **无** | 同 YAML / `--set` | `llmapi/llm_args.py:4022-4056` |
| 匿名使用统计（NVIDIA 遥测，**非监控信号**） | `--telemetry/--no-telemetry` | `true` | 有 | 与监控告警无关，仅需在画像中区分 | `serve.py:1274-1278` |

**多进程采集前提**：`PROMETHEUS_MULTIPROC_DIR` 由 launcher 自动创建并在多前端间共享，仅当 `return_perf_metrics` 或 `perf_metrics_output_dir` 生效时设置（`serve.py:572-584`）。

**打开并验证数据流（默认全关部分的标准操作）**：

1. 写 YAML：`{return_perf_metrics: true, enable_iter_perf_stats: true, enable_iter_req_stats: true}`，以 `--extra_llm_api_options <file>` 启动（或等价 `--set` 逐项）。
2. 验证指标面：`curl http://<host>:<port>/prometheus/metrics | grep trtllm_` 应出现 `trtllm_` 前缀文本序列；`curl http://<host>:<port>/metrics` 应返回**非空** JSON 数组（空数组 = `enable_iter_perf_stats` 未生效）。
3. 验证链路面：`--otlp_traces_endpoint <collector>:4317`，启动日志出现 `Initialized OTLP tracer successfully`（`llmapi/llm.py:535`）；发送带 `traceparent` 头的请求，检查 `llm_request` span（`executor/result.py:935`）。
4. 验证每请求分段：请求携带 `X-TRTLLM-return-metrics: 1`，检查响应头 `Server-Timing`。
5. 抓取配置注意：`/metrics` 是消费式 JSON 端点，**Prometheus 抓取必须只配 `/prometheus/metrics`**；`/metrics` 若要接入需单实例独占抓取（见 1.0 端点语义辨析）。

### 三平面总览

| 平面 | 数量 | 分布特征 | 可观测成熟度 |
|---|---|---|---|
| 日志埋点 | 8971 条（Python 3311 / C++ 5660；去重模板 6441） | Python 集中在 `_torch`、`serve`、`executor`、`llmapi`；C++ 集中在 `batch_manager`、`executor`；**断言占 49.6%（4451 条），真实可检索埋点 4520 条** | 高 |
| 指标定义 | 68 个（全部 `trtllm_` 前缀；90 个写入点、3 个暴露点、56 个 stats 字段） | 64 个定义在 `metrics/collector.py`，3 个在 `grpc/openengine/metrics.py`，1 个在 `kv_hints/metrics.py`；定义与写入点一一对应 | 高（但**默认全部不启用**） |
| 链路 span | 313 条记录 | 分布式 trace 仅 4 处（`disaggregated_request`、`llm_request`、tracing.py 装饰器、result.py span）；279 条是生成 MoE kernel DSL 内的 `iket.range_push` profiler range + 4 条 NVTX；24 条是时序属性枚举注册表（`metrics/enums.py`） | 中（span 是指标的投影，见 4 章 #8） |

### 1.1 日志按级别分布（分语言）

| 级别 | Python | C++ | 合计 | 占比 | 是否纳入告警 |
|---|---|---|---|---|---|
| FATAL | 7 | 4451（全部为 `TLLM_CHECK*`/`TORCH_CHECK` 断言） | 4458 | 49.7% | **否——断言失败即 abort，不落可检索日志**；崩溃信号另行表述（见第 6 章 L2-2） |
| ERROR | 323 | 96 | 419 | 4.7% | 是（P1/P2，需白名单） |
| WARNING | 1127 | 387 | 1514 | 16.9% | 部分（需白名单） |
| INFO | 1355 | 96 | 1451 | 16.2% | 否（生命周期事件类除外） |
| DEBUG | 499 | 630 | 1129 | 12.6% | 否（dev_debug） |

Python 侧 0 条断言；C++ 侧 4451 条断言（占 C++ 记录 78.6%）。**级别分布被断言严重扭曲**，任何按级别占比设计的告警都不可用。

### 1.2 日志按包分布（Top 10）

| 包 | 埋点数 | 其中断言 | 主要职责 |
|---|---|---|---|
| `cpp/tensorrt_llm`（含 batch_manager/executor） | 5156 | ~4100 | C++ 批调度器、KV 缓存管理、执行器 |
| `tensorrt_llm/_torch` | 2516 | ~330（含 csrc/*.cpp） | PyTorch 后端引擎、disaggregation、MoE |
| `tensorrt_llm/serve` | 367 | 0 | OpenAI/Anthropic HTTP 服务 |
| `tensorrt_llm/executor` | 166 | 0 | 执行器 API（fatal error、worker 生命周期） |
| `tensorrt_llm/llmapi` | 140 | 0 | LLM API、参数校验、tracing |
| `cpp/kernels` | 122 | 少量 | kernel 调试日志 |
| `tensorrt_llm/bench` | 95 | 0 | 基准工具（非生产路径） |
| `tensorrt_llm/evaluate` | 79 | 0 | 评测（非生产路径） |
| `cpp/include` | 64 | 少量 | C++ 公共头 |
| `tensorrt_llm/grpc` | 57 | 0 | gRPC servicer |

### 1.3 指标按类型分布

| 类型 | 数量 | 说明 |
|---|---|---|
| Gauge | 31 | 水位/并发/配置 info 类 |
| Counter | 26 | 请求/Token/缓存块累计 |
| Histogram | 11 | 请求延迟与 token 分布 |

### 1.4 链路 span 按阶段分布

| 阶段 | 数量 | 典型 span |
|---|---|---|
| request（分布式 SERVER span） | 3 | `disaggregated_request`（`openai_disagg_server.py:427`，`trace_span` 装饰器）、`llm_request`（`executor/result.py:935-940`）、`tracing.py:212` 通用装饰器 |
| background（NVTX/引擎内部分段） | 4 | `_forward_step_inter_pp`、`_forward_step_last_pp`、`_handle_executed_batch_pp`（`py_executor.py:3893,3900,4472`）、`record_function` 动态名（`py_executor.py:6071`） |
| profiler range（kernel DSL，非 trace） | 279 | `scheduler.wait_sizes_ready`、`router.histogram` 等（生成 MoE kernel 源码内 `iket.range_push`） |
| attr_schema（时序属性枚举注册表） | 24 | `RequestEventTiming`（arrival_time/first_token_time/first_scheduled_time/last_token_time/kv_cache_transfer_start/end/kv_cache_size）、`MetricNames`（ttft/tpot/e2e/request_queue_time/prefill_time/decode_time/inference_time 等），真实名称已从 `tensorrt_llm/metrics/enums.py:23-44` 还原 |
| span_event | 2 | `kv_cache_transfer_start/end`（`tracing.py:162-167`，经 `add_event` 写入） |

### 1.5 上下文证据摘要

| 证据 | 数量 | 占比 | 对方案的影响 |
|---|---|---|---|
| in_loop（循环内） | 430 | 4.8% | 高频候选，禁止做频率告警 |
| in_except（异常分支） | 675 | 7.5% | error 类主力 |
| once（仅一次） | 223 | 2.5% | 只做存在性告警 |
| conditional（条件分支） | 1932 | 21.5% | 特性开关相关，可能默认不启用 |
| is_assertion（断言/abort） | 4451 | 49.6% | **不是可检索日志**；崩溃表现为「日志断档 + 进程退出 + `/health` 200 消失」 |
| 占位符风格 | py: f-string 2254 / printf 176 / none 1197；cpp: printf 704 / none 4623 | — | py none 中 52 条 ERROR 为动态消息变量（`logger.error(msg)`），无法提取模板 |

---

## 2. 日志埋点明细（按语义分类，按模板聚合）

分类基于证据字段（`in_except`/`is_assertion`/级别/模板语义）按模板去重聚合，判定顺序遵循分类体系（先 `in_except`，再断言，再级别，再变量语义，模块路径仅作辅助）；领域子标签按「在线推理服务」画像扩展（`kv_cache`/`batch_schedule`/`model_load` 等）。

### 2.0 分类汇总

| 语言 | 主标签 | 子标签 | 埋点数 |
|---|---|---|---|
| C++ | system_error | assertion_failed（`TLLM_CHECK*`/`TORCH_CHECK`） | 4451 |
| Python | request | —（INFO 默认归请求路径） | 674 |
| C++ | dev_debug | `TLLM_LOG_DEBUG` | 630 |
| Python | system_error | exception_path（in_except） | 530 |
| Python | dev_debug | — | 413 |
| Python | system_error | — | 371 |
| Python | lifecycle | startup（模型加载/权重加载/服务启动） | 257 |
| Python | business_error | auth/校验语义 | 202 |
| Python | resource | kv_cache | 145 |
| Python | request | request_path | 141 |
| Python | resource | memory_usage | 136 |
| Python | dependency | connection（NCCL/NIXL/UCX/ZMQ/握手） | 113 |
| C++ | dependency | connection | 96 |
| C++ | business_error | validation_error | 95 |
| C++ | system_error | —（`TLLM_LOG_ERROR/WARNING`） | 168 |
| Python | resource | batch_schedule / memory_usage 等 | 215 |
| Python | business_error | exception_path / validation_error | 93 |
| Python | metric | latency_timing（总耗时打印，如 `Total pipeline time: {}s`） | 64 |
| Python | system_error | assertion_failed（py FATAL） | 7 |
| C++ | resource/kv_cache/request/lifecycle | — | 185 |
| C++ | metric | latency_timing | 2 |

### 2.1 生命周期类 (lifecycle)——监控相关代表

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| `tensorrt_llm/executor/executor.py:337` | ERROR | `Fatal engine error recorded: {}` | **引擎致命错误的权威记录点**，P0 候选 |
| `tensorrt_llm/executor/proxy.py:1072` | ERROR | `Executor worker initialization error: {}` | worker 初始化失败 |
| `tensorrt_llm/_torch/pyexecutor/py_executor_creator.py:1429` | ERROR | `Failed to roll back DWDP after PyExecutor construction error{}` | 构造失败回滚失败，雪上加霜 |
| `tensorrt_llm/_torch/pyexecutor/kv_cache_events.py:233` | INFO | `Started streaming KV event publisher rank={} endpoint={} topic={}` | KV 事件流就绪 |
| `tensorrt_llm/serve/openai_server.py:837` | INFO | `Started background iteration stats collector task` | 指标后台循环已启动（数据流验证点） |
| `tensorrt_llm/_torch/disaggregation/transceiver.py:92` | FATAL | `UNPROVEN_FATAL: hard-stopping through MPI/launcher; ...` | 主动硬停（disagg），P1 |

### 2.2 系统异常 (system_error) 代表

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| `tensorrt_llm/_torch/pyexecutor/py_executor.py:10105` | ERROR | `Encountered an error in sampling: {}` | 采样阶段错误 |
| `tensorrt_llm/serve/tool_parser/base_tool_parser.py:415` | ERROR | `Error in parse_streaming_increment: {}` | 流式工具解析失败 |
| `tensorrt_llm/_torch/attention/backends/utils.py:362` | ERROR | `Attention kernel failed ({type(exc).__name__}): ...` | attention kernel 失败（动态消息） |
| `tensorrt_llm/_torch/disaggregation/native/transfer.py`（1082/1178/1258 等多处） | ERROR | 动态消息变量（`logger.error(msg)`） | **模板为空，无法全文匹配**，见 7.4 |
| `cpp/tensorrt_llm/batch_manager/contextTransferCoordinator.cpp:212` | ERROR | `Timed out shutting down asynchronous context-transfer coordinator; rank=%d peer_closes=%zu` | C++ 关停超时 |
| `tensorrt_llm/_torch/attention/backends/sparse/dsa/metadata.py:651,682` | WARNING | `... warmup ran out of memory; ... will JIT-compile lazily` | OOM 降级（预期内，白名单） |

### 2.3 请求/事务类 (request) 代表

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| `tensorrt_llm/serve/openai_server.py:1128` | INFO | `{} is disconnected, abort {}` | 客户端断开（**白名单**） |
| `tensorrt_llm/grpc/smg/servicer.py:156` | INFO | `Client disconnected for {}` | gRPC 客户端断开（**白名单**） |
| `tensorrt_llm/serve/tool_parser/base_tool_parser.py:172` | WARNING | `Model attempted to call undefined function: {}` | 模型行为异常 |
| `tensorrt_llm/llmapi/tracing.py:187` | WARNING | `Received a request with trace context but tracing is disabled` | once=true；trace 未开启的提示（白名单/配置核查信号） |

### 2.4 下游依赖类 (dependency) 代表

| 文件:行 | 级别 | 模板 | 说明 |
|---|---|---|---|
| `cpp/tensorrt_llm/common/ncclUtils.cpp:1169` | WARNING | `[NCCLUtil] ncclMemAlloc failed on at least one other rank; freeing local allocation (size=...)` | NCCL 内存分配失败 |
| `tensorrt_llm/_torch/disaggregation/transceiver.py:146` | WARNING | `KV transfer lifecycle enforcement ENABLED. KV retirement fail-stop may terminate the entire ...` | disagg fail-stop 风险提示 |
| `tensorrt_llm/_torch/modules/dwdp/transport.py:540` | ERROR | `[DWDPTransport] Failed to close local export FD {}: {}` | 传输资源清理失败 |

### 2.5 资源类 (resource) 代表

KV 缓存块分配/复用（`cpp/tensorrt_llm/batch_manager/kvCacheManager.cpp:1918` 等，多为 DEBUG）、GPU/主机内存使用（`_torch/memory/gpu_memory_backend.py`）、权重加载预算（`rank_striped_read_ahead.py:360`、`weight_loader.py:229`——主机内存不足降级，WARNING，预期内）。

### 2.6 调试日志（不纳入告警）

Python DEBUG 499 条 + C++ `TLLM_LOG_DEBUG` 630 条（KV 块级明细、传输明细），默认不采集或仅采样采集。

---

## 3. 指标清单

### 3.1 已定义的指标（68 个，全部 `trtllm_` 前缀）

命名约定：源码构造名 = `self.metric_prefix + 字面量`（`metrics/collector.py`）；**暴露名规则 = prometheus_client 对 Counter 自动补 `_total` 后缀**（如源码 `trtllm_kv_cache_reused_blocks` → 抓取名 `trtllm_kv_cache_reused_blocks_total`；源码名已带 `_total` 的保持不变）。下表「暴露名」列为最终抓取名。

**RED 组（请求级，Per-request）**——定义于 `tensorrt_llm/metrics/collector.py`，标签含 `finished_reason`/HTTP code/`model_name` 等低基数维度：

| 暴露名 | 类型 | 语义角色 | 源码位置 | 写入点 |
|---|---|---|---|---|
| `trtllm_request_success_total` | Counter | RED.Rate(成功) | collector.py:209 | 有 |
| `trtllm_request_error_total` | Counter | RED.Errors（按 HTTP 码） | collector.py:578 | 有 |
| `trtllm_e2e_request_latency_seconds` | Histogram | RED.Duration | collector.py:214 | 有 |
| `trtllm_time_to_first_token_seconds` | Histogram | RED.Duration(TTFT) | collector.py:223 | 有 |
| `trtllm_time_per_output_token_seconds` | Histogram | RED.Duration(ITL/TPOT) | collector.py:233 | 有 |
| `trtllm_request_queue_time_seconds` | Histogram | RED.Duration(排队) | collector.py:242 | 有 |
| `trtllm_request_prefill_time_seconds` | Histogram | RED.Duration(预填充) | collector.py:252 | 有 |
| `trtllm_request_decode_time_seconds` | Histogram | RED.Duration(解码) | collector.py:263 | 有 |
| `trtllm_request_inference_time_seconds` | Histogram | RED.Duration(推理) | collector.py:274 | 有 |
| `trtllm_prompt_tokens_total` | Counter | 吞吐 | collector.py:284 | 有 |
| `trtllm_generation_tokens_total` | Counter | 吞吐 | collector.py:290 | 有 |
| `trtllm_prompt_cached_tokens_total` | Counter | 前缀缓存命中 token | collector.py:493 | 有 |
| `trtllm_prompt_cache_hit_tokens_total` | Counter | 缓存命中（按 cache_level） | collector.py:507 | 有 |
| `trtllm_prompt_cached_tokens_per_request` | Histogram | 每请求缓存命中分布 | collector.py:527 | 有 |
| `trtllm_spec_decode_drafted_tokens_total` | Counter | spec-decode 草稿（按 token_position，上限 16） | collector.py:538 | 有 |
| `trtllm_spec_decode_accepted_tokens_total` | Counter | spec-decode 接受 | collector.py:543 | 有 |
| `trtllm_prefill_perplexity` / `trtllm_generation_perplexity` | Histogram | 质量参考 | collector.py:550,555 | 有 |

**USE 组（引擎迭代级，Iteration-level）**——标签含 `dp_rank`（低基数）：

| 暴露名 | 类型 | 语义角色 | 源码位置 |
|---|---|---|---|
| `trtllm_num_requests_running` | Gauge | USE.Saturation(并发) | collector.py:378 |
| `trtllm_num_requests_waiting` | Gauge | USE.Saturation(排队) | collector.py:382 |
| `trtllm_num_requests_completed_total` | Counter | 吞吐 | collector.py:386 |
| `trtllm_max_num_active_requests` | Gauge | 容量 | collector.py:390 |
| `trtllm_iteration_latency_seconds` | Gauge | 引擎迭代耗时 | collector.py:396 |
| `trtllm_num_paused_requests` | Gauge | **抢占/暂停**（preemption 信号） | collector.py:456 |
| `trtllm_num_scheduled_requests` / `trtllm_num_context_requests` / `trtllm_num_generation_requests` | Gauge | 调度状态 | collector.py:460,448,452 |
| `trtllm_total_context_tokens` / `trtllm_avg_decoded_tokens_per_iter` | Gauge | 负载形态 | collector.py:464,468 |
| `trtllm_prefill_batch_occupancy` / `trtllm_prefill_batch_tokens` | Gauge/Histogram | 预填充批次水位 | collector.py:562,567 |
| `trtllm_gpu_memory_usage_bytes` / `trtllm_cpu_memory_usage_bytes` / `trtllm_pinned_memory_usage_bytes` | Gauge | USE.Utilization(内存) | collector.py:402,406,410 |
| `trtllm_max_batch_size_static` / `trtllm_max_batch_size_runtime` / `trtllm_max_num_tokens_runtime` | Gauge | 容量上限 | collector.py:416,420,424 |
| `trtllm_kv_cache_utilization` | Gauge | **USE.Utilization(KV，量纲 0~1，待确认)** | collector.py:310 |
| `trtllm_kv_cache_host_utilization` | Gauge | 主机侧 KV 水位 | collector.py:318 |
| `trtllm_kv_cache_hit_rate` | Gauge | 缓存命中率 | collector.py:296 |
| `trtllm_kv_cache_iter_reuse_rate` | Gauge | 迭代内复用率 | collector.py:322 |
| `trtllm_kv_cache_max_blocks` / `trtllm_kv_cache_free_blocks` / `trtllm_kv_cache_used_blocks` | Gauge | KV 容量 | collector.py:430,434,438 |
| `trtllm_kv_cache_tokens_per_block` | Gauge | 配置 | collector.py:442 |
| `trtllm_kv_cache_reused_blocks_total` / `trtllm_kv_cache_missed_blocks_total`（源码名无 `_total`） | Counter | 缓存命中/未命中块 | collector.py:300,305 |
| `trtllm_kv_cache_iter_reused_blocks_total` / `..._full_...` / `..._partial_...` / `..._missed_...` / `..._connector_...` | Counter | 迭代级复用明细 | collector.py:328-344 |
| `trtllm_kv_cache_gen_alloc_blocks_total` | Counter | 生成期分配 | collector.py:352 |
| `trtllm_kv_cache_onboard_bytes_total` / `trtllm_kv_cache_offload_bytes_total` / `trtllm_kv_cache_disk_prefetch_blocks_total` / `trtllm_kv_cache_intra_device_copy_bytes_total` | Counter | 分层缓存搬运量 | collector.py:356-371 |
| `trtllm_spec_decode_num_draft_tokens_total` / `trtllm_spec_decode_num_accepted_tokens_total` / `trtllm_spec_decode_acceptance_length` / `trtllm_spec_decode_draft_overhead` | Counter/Gauge | spec-decode 效果 | collector.py:474-487 |

**配置 info 组**（启动时经 `log_config_from_args` 打一次，`openai_server.py:1040-1041`）：`trtllm_model_config_info`、`trtllm_parallel_config_info`、`trtllm_speculative_config_info`、`trtllm_kv_cache_config_info`（collector.py:696-718，条件定义）。

**gRPC openengine / kv_hints 组**：`trtllm_openengine_rpc_outcomes_total`（`grpc/openengine/metrics.py:184`）、`trtllm_openengine_metrics_errors_total`（同文件:191）、`trtllm_kv_hints_ignored_total`（`kv_hints/metrics.py:29`，按 `reason` 标签，惰性单例条件定义）。

### 3.2 指标质量风险

| 风险 | 数量 | 具体指标 | 影响 |
|---|---|---|---|
| 条件定义（默认不注册） | 5 | 4 个 `*_config_info`（受对应 config 是否存在控制）+ `trtllm_kv_hints_ignored_total`（惰性单例） | 默认配置下不出现该序列，告警永不触发 |
| 缺少 documentation | 2（另 1 需人工确认） | `trtllm_openengine_rpc_outcomes_total`、`trtllm_openengine_metrics_errors_total`（脚本另标记 kv_hints 1 条，但源码实际带 documentation 字符串） | 语义靠猜，建议补文档 |
| 高基数标签 | 0 | `token_position` 已封顶 16（`collector.py:46`）；其余标签为 finished_reason/http_code/dp_rank/cache_level 等低基数 | 无指标爆炸风险 |
| 无写入点 | 0 | 68 个定义均有对应写入点（90 条 update 记录） | 无预留死指标 |
| 暴露名变形 | 10 | Counter 源码名与抓取名差 `_total` 后缀（见 3.1 注） | 面板/告警必须用抓取名；源码检索时两种写法都试 |
| 直方图桶 | — | TTFT/TPOT/E2E/queue 等桶可用 `prometheus_metrics_config` 自定义（`llm_args.py:4022`），默认内置值未在字段默认中给出 | 分位精度需抓取后核对 bucket 边界 |

### 3.3 指标暴露方式

| 方式 | 位置 | 说明 |
|---|---|---|
| HTTP 拉取（Prometheus 文本） | `GET /prometheus/metrics`（OpenAI 服务） | 仅 `return_perf_metrics=true` 时注册（`openai_server.py:1349-1351,1370`）；多进程模式要求 `PROMETHEUS_MULTIPROC_DIR`（`serve.py:572-584`） |
| HTTP 拉取（独立端口） | `:8001/prometheus/metrics`（openengine gRPC） | `--grpc --grpc-protocol openengine` 下由 server 自动开启采集并起独立 HTTP（`grpc/openengine/server.py:369-374,490-491`；端口可用 `--openengine-metrics-port` 改） |
| HTTP 拉取（disagg server） | `/prometheus/metrics` mount | `openai_disagg_server.py:365` |
| JSON 迭代统计（消费式） | `GET /metrics` | 需 `enable_iter_perf_stats=true`；每次抓取清空缓冲（`openai_server.py:1793-1833`） |
| 请求级指标 | `Server-Timing` 等响应头 / SSE 事件 `trtllm.perf_metrics` / JSONL 文件 | 逐请求头 + 服务端开关（`perf_metrics.py:17-47,203`；`perf_metrics_output_dir` 落盘） |
| OTLP | trace → collector | `--otlp_traces_endpoint`（默认关闭） |
| 能耗 | `GET /energy_metrics`（JSON） | `enable_energy_metrics=true`（`openai_server.py:790-794,1239`） |

---

## 4. 信号仲裁

| # | 监控需求 | 归属平面 | 现有信号 | 源码位置 | 结论 |
|---|---|---|---|---|---|
| 1 | 服务是否活着 | 指标+日志 | `up`（抓取探活）；`/health`（fatal 时自关停）；`/health_generate` | `openai_server.py:1710,1226-1227` | 已覆盖（L1 up + L2 存在性） |
| 2 | 启动成功判定 | 日志 | `Started background iteration stats collector task`、KV publisher 就绪日志、模型加载完成日志 | `openai_server.py:837`；`kv_cache_events.py:233` | 已覆盖（存在性告警；once 类只判出现） |
| 3 | 请求错误率 | 指标 | `trtllm_request_error_total`（按 http code）/ `trtllm_request_success_total` | `collector.py:578,209` | 已覆盖（L1 主力） |
| 4 | TTFT/ITL/E2E 延迟分位 | 指标 | 3 个 Histogram + queue/prefill/decode/inference 4 个分段 Histogram | `collector.py:214-274` | 已覆盖，且**分段延迟在指标平面**（不在子 span） |
| 5 | 排队与并发压力 | 指标 | `trtllm_num_requests_waiting/running`、`trtllm_request_queue_time_seconds` | `collector.py:382,378,242` | 已覆盖 |
| 6 | KV 缓存水位与命中率 | 指标 | `trtllm_kv_cache_utilization/hit_rate/free/used_blocks` 等 20+ | `collector.py:296-442` | 已覆盖 |
| 7 | 抢占/暂停 | 指标 | `trtllm_num_paused_requests` | `collector.py:456` | 已覆盖（LLM 服务最易被忽视的体验劣化先行指标） |
| 8 | 单请求慢在哪一段 | 链路+响应头+span 属性 | **无子 span**；分段耗时在：① span 属性 `gen_ai.latency.time_to_first_token/e2e/time_in_queue/kv_cache_transfer_time`；② span 事件 `kv_cache_transfer_start/end`；③ `Server-Timing`/`X-TRTLLM-Step-Metrics` 响应头（step-0-forward/sample、ctx-chunk-forward）；④ 指标直方图 | `executor/result.py:969-1007`；`perf_metrics.py:17-47`；`llmapi/tracing.py:137-167` | 部分覆盖——**链路是指标的投影**：`llm_request` span 生成依赖 `metrics_dict`/`req_perf_metrics_dict`，不足时直接放弃（`result.py:920-924` 打 `Insufficient request metrics available` 警告）。开 trace 必须同时开 `return_perf_metrics` |
| 9 | 崩溃/abort 归因 | 日志+进程信号 | 断言 4451 处失败即 abort **不落日志**；落日志的权威事件为 `Fatal engine error recorded: {}` 与 `/health` 自关停 | `assert.h:40-66`；`executor/executor.py:337`；`openai_server.py:1710-1722` | 部分覆盖——告警用「日志断档 + up=0 + 重启」组合，断言仅用于崩溃后归因（启动 smoke test） |
| 10 | 吞吐（token/s） | 指标 | `trtllm_prompt/generation_tokens_total`、`trtllm_num_requests_completed_total` | `collector.py:284,290,386` | 已覆盖 |
| 11 | spec-decode 收益 | 指标 | acceptance_length / draft_overhead / drafted/accepted tokens | `collector.py:474-543` | 已覆盖（默认关闭，见 1.0.1） |
| 12 | GPU 硬件层（SM 占用、温度、功耗、ECC） | 指标 | 无（`trtllm_gpu_memory_usage_bytes` 仅为引擎视角内存） | 三平面检查见 4.1 | **真缺口**：接 DCGM exporter（dcgm-exporter），不改源码 |
| 13 | 能耗 | HTTP JSON | `GET /energy_metrics` | `openai_server.py:790-794,1239` | 已覆盖（默认关，非 Prometheus 格式，需自行转换或用 json exporter） |
| 14 | KV 事件流（disagg 联动） | HTTP POST/流 | `POST /kv_cache_events`、`Started streaming KV event publisher` | `openai_server.py:1250`；`kv_cache_events.py:233` | 已覆盖（面向 disagg 调度器，非常规抓取） |

### 4.1 三平面检查记录（下「缺口」结论的必要条件）

| 平面 | 已检查内容 | 结果（对应需求 #12 GPU 硬件层） |
|---|---|---|
| 日志 | 8971 条埋点、3898 个非断言去重模板，检索 `sm util|temperature|power draw|ecc|throttle|clock` 等词根 | 仅 `enable_energy_metrics` 能耗路径与降级 WARNING；无硬件利用率/温度埋点 |
| 指标 | 68 个定义名 + 90 个写入点，检索 `gpu_|device_|power|temp|util` | 仅 `trtllm_gpu_memory_usage_bytes`（引擎已用显存，非利用率/温度） |
| 链路 | 313 条 span/属性记录 | 无硬件层 span/属性 |

结论：GPU 硬件层属真缺口，正确做法是接外部 DCGM exporter 而非新增埋点。

---

## 5. 关键字库

### 5.1 告警关键字（事件类，可全文匹配；模板含 `{}`/`%d` 部分按前缀匹配）

| 关键字 | 级别 | 说明 | 来源 |
|---|---|---|---|
| `Fatal engine error recorded` | P0 | 引擎致命错误 | `executor/executor.py:337` |
| `Executor worker initialization error` | P0 | worker 起不来 | `executor/proxy.py:1072` |
| `UNPROVEN_FATAL: hard-stopping` | P1 | disagg 主动硬停 | `_torch/disaggregation/transceiver.py:92` |
| `Failed to roll back DWDP after PyExecutor construction error` | P0 | 构造失败且回滚失败 | `_torch/pyexecutor/py_executor_creator.py:1429` |
| `Timed out shutting down asynchronous context-transfer coordinator` | P1 | C++ 关停卡死 | `cpp/.../contextTransferCoordinator.cpp:212` |
| `ncclMemAlloc failed on at least one other rank` | P1 | NCCL 分配失败（多 rank 联动故障） | `cpp/.../ncclUtils.cpp:1169` |
| `Encountered an error in sampling` | P2 | 采样错误（有对应引擎统计，降级为频率观察） | `_torch/pyexecutor/py_executor.py:10105` |

### 5.2 统计字段（不可全文匹配，需按字段提取）

| 字段 | 所在位置 | 统计方式 | 维度 |
|---|---|---|---|
| `server_queue/server_ttft/server_e2e` 等 `dur=` | `Server-Timing` 响应头 | 数值提取 → 分位 | 请求级 |
| `step-0-forward/step-0-sample` | `X-TRTLLM-Step-Metrics` 头 | 数值提取 → 分步耗时 | step 序号 |
| 迭代统计 JSON 字段（numActiveRequests/numQueuedRequests 等） | `GET /metrics` 数组元素 | 按字段统计 | iter |
| `http_code` | `trtllm_request_error_total` 标签 | 按值分组计数 | code |
| `finished_reason` | `trtllm_request_success_total` 标签 | 按值分组 | reason |

### 5.3 降噪白名单

| 关键字/条件 | 原因 | 处理 |
|---|---|---|
| `is disconnected, abort`（`openai_server.py:1128`）、`Client disconnected for` | 客户端主动断开，正常现象 | 忽略，不计错误率 |
| `warmup ran out of memory`（`dsa/metadata.py:651,682`） | 启动期 OOM 降级为惰性 JIT，服务正常 | P3 趋势观察 |
| `checkpoint bytes ({}) exceed available host`（`weight_loader.py:229`） | 加载降级路径 | 忽略 |
| `Received a request with trace context but tracing is disabled`（`tracing.py:187`，once） | 配置提示非故障 | 配置核查信号，P3 |
| `Resource governor is disabled`（`openai_server.py:1274`） | 显式关闭的功能 | 忽略 |
| 错误计数中来自客户端参数错误的 400 | `log_request_error(http_code=400)` 属请求方问题 | 错误率按 code 分组，400 单独基线 |
| 冷启动加载类 INFO（`Loading ...`） | 启动期必然出现 | 排除启动窗口 |

### 5.4 检索语句示例

```promql
# TTFT P99（抓取名；桶边界需抓取后核对）
histogram_quantile(0.99,
  sum(rate(trtllm_time_to_first_token_seconds_bucket[5m])) by (le)
)

# 错误率（排除客户端 400）
sum(rate(trtllm_request_error_total{http_code!="400"}[5m]))
/ sum(rate(trtllm_request_success_total[5m]))

# KV 水位（量纲待确认，先看实际值域）
max(trtllm_kv_cache_utilization)
```

```logql
# 引擎致命错误存在性
{job="trtllm-serve"} |= "Fatal engine error recorded"
```

---

## 6. 告警规则草稿（候选阈值，待 SLO/压测/线上数据校准）

**层级取舍说明**：本项目指标平面信号密度高（68 个定义全带写入点），L1 为主力；但所有指标**默认关闭**，第一优先级是第 1 章 1.0.1 的「打开并验证数据流」，否则以下规则全部空转。

### 6.1 指标告警（L1，主力）

| 告警名 | 条件 | 候选阈值 | 等级 | 降噪 | 白名单 |
|---|---|---|---|---|---|
| 服务失联 | `up{job="trtllm-serve"} == 0` 持续 1m | 1m | P0 | 重启窗口抑制 | 计划内重启 |
| 引擎错误率 | `sum(rate(trtllm_request_error_total{http_code!="400"}[5m])) / sum(rate(trtllm_request_success_total[5m])) > 0.05` | 5%（比率 0~1）持续 10m | P1 | 按 `http_code` 分组；排除 400 | 压测窗口 |
| TTFT P99 劣化 | `histogram_quantile(0.99, sum(rate(trtllm_time_to_first_token_seconds_bucket[5m])) by (le)) > 2` | 2s（单位秒；按业务 SLO 校准） | P1 | 持续 2 个周期；排除冷启动 | 批量大 prefill 场景 |
| E2E P99 劣化 | 同上，`trtllm_e2e_request_latency_seconds_bucket` > 30 | 30s | P1 | 同上 | 长输出任务 |
| 排队积压 | `max(trtllm_num_requests_waiting) > 32` 持续 5m | 32（待容量基线校准） | P2 | 与 `num_requests_running` 联动 | 预期高峰时段 |
| KV 缓存水位 | `max(trtllm_kv_cache_utilization) > 0.95` 持续 5m | 0.95（量纲 0~1 待抓取确认） | P1 | 与 free_blocks 联动 | — |
| 抢占发生 | `max(trtllm_num_paused_requests) > 0` 持续 5m | 任何非零 | P2 | preemption 是体验劣化先行指标 | — |
| 迭代耗时突增 | `max(trtllm_iteration_latency_seconds) > 基线*3` 持续 5m | 3 倍 7 日基线 | P2 | 相对基线 | — |
| KV 容量告急 | `min(trtllm_kv_cache_free_blocks) < 8` 持续 5m | 8 blocks（待校准） | P2 | 与 kv_cache_utilization 联动 | — |
| spec-decode 收益塌陷 | `avg(trtllm_spec_decode_acceptance_length) < 1.2` 持续 30m | 1.2（仅开启 spec-decode 时） | P3 | 对比发布前后 | 未启用 spec-decode 的实例 |

### 6.2 日志事件告警（L2，存在性）

| 告警名 | 条件 | 等级 | 降噪 | 来源 |
|---|---|---|---|---|
| 引擎致命错误 | 出现 `Fatal engine error recorded` | P0 | 按实例去重；与 up=0 联动 | `executor/executor.py:337` |
| worker 初始化失败 | 出现 `Executor worker initialization error` | P0 | 重启后窗口判定 | `executor/proxy.py:1072` |
| 启动超时 | 重启后 300s 内未出现指标后台循环就绪日志 `Started background iteration stats collector task` | P1 | N 按实测启动耗时校准 | `openai_server.py:837` |
| disagg 硬停 | 出现 `UNPROVEN_FATAL: hard-stopping` | P1 | 按集群去重 | `transceiver.py:92` |
| NCCL 分配失败 | 出现 `ncclMemAlloc failed on at least one other rank` | P1 | 按 rank 聚合 | `ncclUtils.cpp:1169` |
| 进程异常终止（断言崩溃） | 日志断档 + `up == 0` + 进程重启三条件同时满足 | P0 | **断言失败不落日志，不能做日志关键字**；按实例去重 | 4451 处断言，`assert.h:40-66` |

### 6.3 日志频率告警（L3，谨慎）

| 告警名 | 条件 | 基线来源 | 等级 | 说明 |
|---|---|---|---|---|
| Python ERROR 速率突增 | `sum(rate({job="trtllm-serve"} \| level="ERROR" [5m])) > 基线*5` 持续 3 周期 | 7 日相对基线 | P2 | 52 条 ERROR 模板是动态消息变量无法按模板聚合，只能按级别统计；必须配 5.3 白名单 |
| 客户端断开占比 | 断开类 INFO 速率 / 请求速率 > 基线*3 | 历史基线 | P3 | 预期行为，仅观察 |

### 6.4 链路告警（L4，可选，仅 trace 已开启时）

| 告警名 | span | 条件 | 等级 |
|---|---|---|---|
| 请求 span 错误率 | `disaggregated_request`（`openai_disagg_server.py:427`，装饰器记录异常并置 ERROR status，`tracing.py:219-223`） | ERROR status 占比 > 5% | P2 |
| KV 传输段劣化 | `llm_request` span 属性 `gen_ai.latency.kv_cache_transfer_time` + 事件 `kv_cache_transfer_start/end`（`result.py:983-1007`） | P95 环比上升 > 50% | P2 |

注意：`llm_request` 的分段值全部来自属性而非子 span（见 4 章 #8），L4 告警实际是对 span 属性的统计，且依赖 `return_perf_metrics`。

---

## 7. 缺口与建议

### 7.1 真缺口（三平面均无信号）

| 场景 | 风险 | 建议做法 | 优先级 |
|---|---|---|---|
| GPU 硬件层（SM 利用率、温度、功耗、ECC、NVLink） | 硬件故障/降频不可见 | 接 DCGM exporter（外部组件，不改源码）；引擎内 `trtllm_gpu_memory_usage_bytes` 只覆盖显存占用 | P1 |
| HTTP 层请求量/连接错误 | `prometheus_fastapi_instrumentator` 被 `excluded_handlers=[".*"]` 全排除（`openai_server.py:1363-1368`），无 HTTP QPS/连接层指标 | 依赖 `trtllm_request_success/error_total` 重建请求速率；如需 L7 全量观测可另接网关层指标 | P2 |
| 硬件级能耗仅 JSON 端点 | 无法进 Prometheus | `/energy_metrics` 需 JSON→Prometheus 转换或用 DCGM 功耗读数替代 | P3 |

### 7.2 已有但不可用（信号存在质量问题）

| 信号 | 问题 | 建议 |
|---|---|---|
| 全部 68 个指标 | 默认不注册（`return_perf_metrics=false`） | 按 1.0.1 开启并验证；抓取配置只配 `/prometheus/metrics` |
| `GET /metrics` | 消费式 JSON 端点，多 scraper 抢空（源码注释明确） | 单实例独占抓取或改为消费 tee buffer 的采集器 |
| 4 个 `trtllm_*_config_info` | 条件定义，对应 config 缺失时不出现 | 确认部署配置；缺失时告警用「序列不存在」 |
| Counter 的 `_total` 变形 | 源码名与抓取名不一致（10 个指标） | 面板/告警统一用抓取名（3.1 表已列） |
| 直方图桶 | 默认桶未随字段给出，可能不匹配 LLM 延迟量级 | 用 `prometheus_metrics_config` 自定义桶并压测校准（`llm_args.py:4022`） |

### 7.3 现有埋点问题

| 问题 | 位置 | 建议 |
|---|---|---|
| 断言密度极高（4451 处 FATAL 级） | C++ 全库（`TLLM_CHECK*`/`TORCH_CHECK`） | 非缺陷；但监控侧必须按「日志断档+重启」建模崩溃，勿按关键字 |
| 动态消息 ERROR 无法聚合 | `_torch/disaggregation/native/transfer.py:1082,1178,1258` 等 52 处 | 改为固定前缀 + 结构化字段 |
| KV 块级 DEBUG 日志高频 | `kvCacheManager.cpp:1918` 等 | 保持 DEBUG 级别，勿在生产开启 |
| 引擎内部异常被 WARNING 降级（如 `[M3 dispatch] engine failed to build`） | `_torch/_experimental/modeling_v2/models/minimax_m3/*/modeling.py:1783,2049` | 视业务关键度评估升为 ERROR |

### 7.4 需人工确认（脚本未能确定）

| 项 | 原因 |
|---|---|
| C++ 平面无指标构造（`extract_metrics.py` 不支持 C++ SDK） | 已人工检索 `cpp/`：无 prometheus/OTel meter 调用，C++ 侧统计经 `IterationStats` 结构序列化传回 Python 由 `MetricsCollector` 转换（`py_executor.py:3470`），结论可信但覆盖率按人工检索计 |
| 生成 MoE kernel 源码中 `iket.range_push` 279 处 | 属 kernel DSL profiler range，非分布式 trace；是否接入 Nsight 需运行时确认 |
| `trtllm_kv_cache_utilization` 量纲 | 文档未标 0~1 或 0~100，需抓取实际值确认 |
| `prometheus_metrics_config` 默认桶边界 | 字段默认 None，内置值需运行时抓取 `_bucket` 序列确认 |
| 52 条空模板 ERROR 的真实消息 | 首参为变量（`logger.error(msg)`），需运行时日志确认格式 |

---

## 8. 版本迭代

### 8.1 标准流程

```bash
# 新版本重新提取（三平面都要）
python3 extract_loggers.py <新源码> --output loggers_v2.json
python3 extract_metrics.py <新源码> --output metrics_v2.json
python3 extract_traces.py  <新源码> --output traces_v2.json

# 逐个对比
python3 diff_signals.py loggers.json  loggers_v2.json  --output diff_logs.json
python3 diff_signals.py metrics.json  metrics_v2.json  --output diff_metrics.json
python3 diff_signals.py traces.json   traces_v2.json   --output diff_traces.json
```

人工提取清单（C++ 指标路径、端点开关表）整理成同构 JSON 后走同一套 diff。

### 8.2 变化处理矩阵

| 变化类型 | 对监控的影响 | 动作 |
|---|---|---|
| 新增 ERROR/WARN 日志 | 新故障模式 | 评估新增 L2 告警 |
| 新增 `trtllm_*` 指标 | 新可观测能力 | 评估新增 L1 告警 |
| 删除指标 | 告警静默失效 | **必须下线对应规则** |
| `trtllm_` 指标标签变更 | PromQL 静默失效 | 更新查询与看板 |
| `prometheus_metrics_config` 桶变更 | 分位精度变化 | 重新校准阈值 |
| **开关默认值变更**（如 `return_perf_metrics` 默认翻正） | 整个方案的数据流前提 | 重核 1.0.1 表，优先级最高 |
| 端点路径/handler 变更 | 抓取配置失效 | 重查路由注册代码（1.0 端点语义辨析） |

### 8.3 diff 的局限性

按「文件名 + 模板」模糊匹配、忽略行号偏移；同文件同模板多条被折叠，计数是近似值。行号漂移频繁的仓库（本仓库单文件可达万行）中尤甚。

---

## 9. 局限性

1. **阈值为候选值**：所有 P0~P3 阈值需结合 SLO、压测基线与线上历史校准；源码常量（如 `/metrics` 的 2s 等待、`iter_stats_max_iterations=1000`）是业务配置，不是告警阈值。
2. **静态分析看不到运行时**：指标注册与否依赖 `return_perf_metrics` 等开关的实际配置；OTel 采样率未在源码中固定（`BatchSpanProcessor` 默认策略，`tracing.py:72`）；多进程模式下 `PROMETHEUS_MULTIPROC_DIR` 行为需在目标部署形态验证。
3. **提取覆盖率不是 100%**：
   - C++ 指标构造未内置脚本支持，本章相关结论来自人工检索（覆盖率按检索词表计）；
   - 52 条 Python ERROR 为动态消息变量，模板为空；
   - 生成 kernel DSL 源码（`kernel_src/`）中的 `iket.range_push` 属于 DSL 调用，语义按 profiler range 解读，运行时行为未验证；
   - `cpp/tests/`、`tensorrt_llm/bench/`、`evaluate/` 中的信号未纳入告警设计。
4. **断言 ≠ 日志**：4451 处 `TLLM_CHECK*`/`TORCH_CHECK` 失败即中止，多以异常/abort 结束，崩溃信号的正确建模是「日志断档 + up=0 + 进程重启」，断言清单仅用于崩溃后归因与启动 smoke test。
5. **端点语义以源码为准但端口/路径可被部署覆盖**：`--port`、`--openengine-metrics-port`、反向代理都会改变实际抓取地址；上线前按 1.0.1 第 2 步实测。
6. **脚本能力边界**：C++/CUDA 仅支持日志与 NVTX/profiler range 识别；指标平面仅 Python（AST）覆盖。凡「无 X」结论均已按第 4.1 节格式附三平面检索记录；未附记录处一律表述为「需人工确认」而非「不存在」。
