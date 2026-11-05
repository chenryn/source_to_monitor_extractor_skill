# 语义分类体系（日志 / 指标 / 链路）

从源码信号推导监控方案时，先归入统一语义类别，避免混乱。三个平面各有一套分类，但共享同一个判断原则：**基于证据字段，不靠感觉**。

本分类体系与业务领域无关，适用于在线服务、批处理、库/SDK、CLI、基础设施等各类项目。一级分类是固定集合；**子标签、领域词表、span 阶段都是开放集合**，按 Step 0 的项目画像扩展，扩展时不得改变判定顺序。

---

# 一、日志语义分类

## 一级分类（主标签，每条只打一个）

### A. 生命周期类 (lifecycle)
程序启动、停止、初始化、资源加载、崩溃恢复
- 子标签：`startup` / `shutdown` / `init` / `config_load` / `crash` / `signal_received` / `health_check`

### B. 请求/事务类 (request)
外部请求接收、参数校验、处理完成、响应返回
- 子标签：`request_received` / `params_validate` / `request_complete` / `response_sent` / `auth_check`

### C. 下游依赖类 (dependency)
调用 DB / 缓存 / 第三方服务 / RPC / 文件系统 / 外部 API
- 子标签：`db_call` / `cache_call` / `http_call` / `rpc_call` / `external_api` / `file_io` / `job_submit`

### D. 资源指标类 (resource)
队列长度、连接池、内存、磁盘、线程池、缓冲区等有容量上限的资源
- 子标签：`queue_size` / `pool_usage` / `memory_usage` / `disk_usage` / `thread_pool` / `connection_pool` / `buffer_size`

### E. 业务逻辑异常 (business_error)
业务拒绝、权限不足、数据不一致、参数非法、超限
- 子标签：`validation_error` / `permission_denied` / `rate_limited` / `data_mismatch` / `resource_exhausted`

### F. 框架/系统异常 (system_error)
编解码失败、序列化、协议解析、IO 错误、OOM、断言失败
- 子标签：`serialize_error` / `parse_error` / `io_error` / `oom` / `assertion_failed` / `segfault`

### G. 统计打点类 (metric)
专门上报耗时、计数、分布的日志埋点（注意：**这类日志通常已被指标替代**，优先级低）
- 子标签：`latency_timing` / `counter_increment` / `gauge_update`

### 领域扩展子标签（示例，按项目画像选用）

| 领域 | 可追加的子标签 |
|---|---|
| 在线推理服务 | `tokenize` / `batch_schedule` / `kv_cache` / `gpu_alloc` / `model_load` / `preemption` |
| 数据管道 / 批处理 | `checkpoint` / `watermark` / `lag` / `retry` / `backfill` |
| 基础设施组件 | `replica` / `shard` / `compaction` / `connection` / `leader_election` |

这些词只影响分类粒度，**不构成默认规则**；画像确认不属于该领域时不要使用。

## 附加标签（可叠加）

| 标签 | 判定依据（脚本字段） | 对监控的影响 |
|---|---|---|
| `high_volume` | `in_loop == true` | 禁止频率告警，只做趋势或采样 |
| `one_shot` | `once == true` | 只做存在性告警（是否出现过） |
| `feature_gated` | `conditional != ""` | 默认配置下可能不触发，别当必然事件 |
| `sensitive` | 模板含 token/key/password/secret/uid/phone | 输出脱敏建议 |
| `dev_debug` | `level == DEBUG` | 不进告警 |

## 判定顺序（按优先级）

1. **先看 `in_except`**：在异常分支内 → `system_error` 或 `business_error`
2. **再看 `is_assertion`**：断言/abort 类（各语言的 `CHECK_*` / `assert!` / `panic!`）→ 失败即中止，属崩溃信号，但**未必以日志落盘**，不能当可检索关键字
3. **再看级别**：FATAL/ERROR → error 类；INFO → request/resource 类
4. **再看变量语义**：模板含 latency/cost/duration/elapsed → 耗时类
5. **再看模块路径（仅作辅助证据）**：路径语义按本项目画像解释，例如 `handler/` 属于请求路径、`pipeline/` 属于批处理；**不要预设某个目录名一定属于某类**
6. **模棱两可时**：选更靠近「异常/失败」的类别，宁可多分类不要漏告警

---

# 二、指标语义分类

指标不需要「猜语义」——它的类型和名字已经说明了用途。分类的目的是**判断该用什么告警范式**。

## 按指标类型

| 类型 | 语义 | 告警范式 | 注意 |
|---|---|---|---|
| **Counter** | 单调累计计数 | 用 `rate()` 算速率后告警；直接看绝对值无意义 | 进程重启会归零，需 `increase()` 容错 |
| **Gauge** | 瞬时值，可升可降 | 直接比候选阈值；水位类用「相对容量上限的阈值 + 持续 N 分钟」 | 先确认量纲与单位；多进程/多副本采集时注意聚合语义 |
| **Histogram** | 分布，含分桶 | 用 `histogram_quantile()` 算 P95/P99 | **桶边界决定精度**，超出最大桶的值全归入 +Inf |
| **Summary** | 客户端算好的分位 | 直接读分位值 | 分位不可跨实例聚合，多副本场景慎用 |

## 按语义角色（对齐 RED / USE 方法）

### RED（面向请求，适用于服务型组件）
- **R**ate：请求速率 → `*_total` 类 Counter
- **E**rrors：错误率 → `*_errors_total` / `*_rejections` / `*_failures`
- **D**uration：耗时分布 → `*_seconds` / `*_latency_*` 类 Histogram

### USE（面向资源，适用于任何有容量上限的资源：内存 / 连接池 / 队列 / 磁盘 / 线程池 / 加速器）
- **U**tilization：使用率 → `*_usage_perc` / `*_util` / `*_bytes`
- **S**aturation：饱和度 → `*_waiting` / `*_queue_*` / `*_pending` / `*_inflight`
- **E**rrors：资源错误 → `*_oom` / `*_allocation_failures` / `*_rejections`

## 指标分类判定表

| 指标名特征 | 语义类别 | 归入 |
|---|---|---|
| `*_total`, `*_count`, `*_queries`, `*_hits` | 累计计数 | RED.Rate / RED.Errors |
| `*_seconds`, `*_ms`, `*_latency`, `*_time` | 耗时 | RED.Duration |
| `*_usage_perc`, `*_ratio`, `*_util` | 使用率 | USE.Utilization |
| `*_waiting`, `*_queue_*`, `*_running` | 队列/并发 | USE.Saturation |
| `*_rejections`, `*_skipped`, `*_evictions`（调度类还可能有 `*_preemptions`） | 拒绝/降级/驱逐 | USE.Saturation / RED.Errors |
| `*_bytes`, `*_size`, `*_allocation` | 容量 | USE.Utilization |
| `*_state`, `*_status` | 状态枚举 | 单独判断（值语义需查源码） |

## 指标质量检查（必须在方案里体现）

| 检查项 | 判定 | 影响 |
|---|---|---|
| 有 `documentation` 吗 | `documentation == ""` 的比例 | 无文档的指标，语义靠猜，告警容易设错 |
| 是条件定义吗 | `conditional != ""` | 默认配置下可能根本没注册，告警永远不会触发 |
| 标签基数 | `labelnames` 数量 | 标签值是高基数字段（request_id/user_id）会导致指标爆炸 |
| 桶是否够用 | `buckets` | 空桶 = 用默认桶，可能不匹配实际量级 |
| 有写入点吗 | 能否在 `kind=update` 里找到 | 找不到写入点 → 可能是预留指标，永远为 0 |
| 命名是否统一 | 前缀分布 | 前缀混乱 → 查询语句难写，看板难维护 |

---

# 三、链路 span 分类

span 按**所处阶段**分类，因为不同阶段的耗时劣化，排查路径完全不同。下表是与领域无关的基础阶段；批量作业类项目把 `request` 换成「作业/任务」语义即可。

| 阶段 | 语义 | 关注点 |
|---|---|---|
| `startup` | 进程/服务启动、配置加载 | 耗时突增 → 启动超时、就绪探针失败 |
| `init` | 依赖、资源、子组件的初始化 | 失败即起不来；冷启动慢的主因 |
| `request` | 请求 / 事务 / 作业主路径 | 用户可感知延迟、成功率 |
| `external_call` | 跨进程、跨服务、下游调用（client 类 span） | 依赖劣化、调用拓扑断点 |
| `background` | 后台任务、异步作业、周期性维护 | 堆积、滞后、资源争用 |
| `teardown` | 关闭、清理、状态持久化 | 优雅退出失败、数据丢失 |

**领域扩展阶段（示例）**：图编译/算子编译/预热/worker 初始化/GPU 资源分配可归入 `init` 或单列；数据管道的 checkpoint/compaction 归入 `background`。扩展阶段要在方案里注明含义，**不要把示例阶段当成所有项目的默认阶段**。

## span 质量检查

| 检查项 | 判定 | 影响 |
|---|---|---|
| 是否有 `span_name_dynamic` | 名字是变量而非字面量 | 无法按名字聚合，需查调用点确定实际值 |
| 是否带属性 | `attributes` 为空 | 无属性 = 无法按模型/引擎维度下钻 |
| 是否有 server/client 区分 | `span_kind` 全为 `internal` | 无法还原调用拓扑 |
| 是否默认开启 | 查配置项（trace endpoint / exporter 开关） | 未配置 = 生产无 trace 数据 |
| 采样率 | 查 sampler 配置 | 采样过低无法定位偶发问题 |

---

# 四、统一分类 Prompt

```
你是可观测性专家。对以下信号逐条打标签，基于证据字段而非领域经验。
项目画像：{运行形态 / 语言 / 可观测技术栈}（子标签与阶段可按该画像扩展，扩展处需说明理由）

日志（每条含 file/line/level/template/placeholders/in_loop/in_except/once/conditional/is_assertion）：
- main_tag 从 [lifecycle, request, dependency, resource, business_error, system_error, metric] 选一个
- 附加标签按 evidence 字段判定，必须在 reason 中说明引用了哪个字段

指标（每条含 name/metric_type/labelnames/buckets/documentation/conditional）：
- 按 RED / USE 归入语义角色；批处理/基础设施类项目以 USE 为主
- 标记质量风险（无文档 / 条件定义 / 高基数标签 / 空桶 / 无写入点）

span（每条含 span_name/span_kind/style/attributes）：
- 按阶段归入 [startup, init, request, external_call, background, teardown]，可扩展领域阶段
- 标记质量风险（动态命名 / 无属性 / 全 internal / 默认未开启）

输出：JSON 数组，每条含 {type, file, line, name, main_tag, sub_tag, reason, evidence}
```
