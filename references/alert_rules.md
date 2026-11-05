# 分层告警规则模板库

所有阈值均为**候选参考值**，必须结合 SLO、压测基线、线上历史校准。源码里的超时常量 ≠ 告警阈值。

模板结构、等级定义与降噪原则与业务领域无关；文中的**指标名、维度、阈值都是占位符或示例**，必须替换为本项目提取结果里的真实名称。领域词表只在项目画像确认后使用。

## 一、告警等级定义

| 等级 | 含义 | 通知方式 | 响应时效 |
|---|---|---|---|
| P0 | 服务完全不可用 / 数据丢失 / 安全事件 | 电话 + IM + 邮件 | 5 分钟内 |
| P1 | 核心功能受损 / 错误率飙升 / 性能严重下降 | IM + 邮件 | 30 分钟内 |
| P2 | 非核心功能异常 / 资源预警 / 趋势性劣化 | IM | 4 小时内 |
| P3 | 信息类 / 趋势观察 / 容量规划参考 | 日报汇总 | 每日 |

## 二、分层原则（最重要的一节）

| 层级 | 信号平面 | 承载内容 | 典型等级 | 选取原则 |
|---|---|---|---|---|
| L1 | **指标** | 延迟分位、错误率、资源水位、趋势 | P0/P1 | 有对应指标时优先，能覆盖大部分度量类需求 |
| L2 | **日志（事件）** | 崩溃、启动失败、OOM、连接断开、作业失败 | P0/P1 | 一次性事件、需要失败上下文时用 |
| L3 | 日志（频率） | 某类错误计数突增 | P2/P3 | 仅当该异常没有对应指标时使用，必须配白名单 |
| L4 | 链路 | 分段耗时劣化 | P2（可选） | 项目确实开启 trace 时才用 |

**为什么 L1 优先**：指标可聚合、可算分位、跨实例可比、成本恒定；日志量随流量线性增长，阈值无法跨环境复用。

**各层占比按项目实际信号密度决定**，不要套用固定比例。指标平面贫乏的项目（小型服务、CLI、批处理）允许以 L2 为主，但要在方案里写明这一取舍的代价：阈值不可跨环境复用、噪声更高、无法算分位。

**什么时候才用 L3（日志频率告警）**：仅当某个异常**没有对应指标**时才用，且必须配白名单 + 持续周期判定 + 相对基线。

## 三、通用基线信号清单（按运行形态选）

**先查本项目有没有下面这些语义角色，再谈缺什么。** 表里描述的是角色，不是指标名；实际名字一律取自 `metrics_extracted.json` / 人工提取清单。

| 语义角色 | 在线服务 / API | 批处理 / 数据管道 | 库 / SDK | 基础设施组件 |
|---|---|---|---|---|
| 流量 / 速率 | 请求数、QPS | 作业数、处理条数 | 由宿主采集的调用次数 | 连接数、消息数 |
| 错误 / 拒绝 | 错误率、超时、拒绝数 | 失败作业、重试、死信 | 公开错误类型与返回码 | 失败操作、副本错误 |
| 延迟 / 时长 | 请求耗时分布 | 作业时长、阶段耗时、lag | 关键方法耗时（可选） | 操作耗时分布 |
| 饱和度 | 排队数、并发数 | 积压、待处理量 | — | 队列深度、连接池占用 |
| 资源水位 | 内存、连接池 | 内存、磁盘、临时空间 | — | 磁盘、内存、句柄 |
| 健康 / 存活 | 就绪、存活探针 | 上次成功运行时间、心跳 | — | 副本状态、leader 状态 |

在线服务常用 RED + USE 组合；批处理与基础设施以 USE 为主；库/SDK 的重点是「暴露什么信号给宿主」，不必硬凑 RED 告警。

### 3.1 领域基线示例：LLM 推理服务（仅说明领域词表怎么用）

在这类项目里，上表角色常映射为 TTFT / ITL / 端到端延迟 / 排队时间 / 缓存命中 / 抢占 / KV 缓存水位 / 准入拒绝，类型多为 Histogram 或 Gauge，且**往往默认关闭采集**。**这些名字只在该领域成立**；真实指标名、别名与阈值示例见 `../examples/vllm-monitoring-plan.md`，其他项目一律从自己的提取结果重新推导。

### 3.2 基线信号缺失时的动作

不要自己编指标名。标注「本项目无此信号」，并说明：

- 等效信号是否在别的平面（例如延迟可能藏在日志的 `cost=%sms` 字段、span 属性、响应头里）；
- 三平面都没有时，是「需新增埋点」还是「需外部 exporter / 平台侧采集」；
- 该功能是否受开关控制，默认是否开启（见 `../SKILL.md` Step 0.4）。

---

## 四、各层告警模板

> `<...>` 是占位符，必须用本项目提取结果里的真实指标名、日志模板、span 名与源码位置替换。维度必须是低基数字段；高基数字段（请求 ID、用户 ID）不能进 `by`。

### L1：指标告警（主力）

```yaml
- alert_name: "<处理> 延迟 P99 劣化"
  level: P1
  signal_plane: metric
  condition: |
    histogram_quantile(0.99,
      sum(rate(<duration_histogram>_bucket[5m])) by (le, <低基数维度>)
    ) > <候选阈值>
  candidate_threshold: "P99 > <值>（待按业务 SLO 校准；写明单位）"
  source_ref: "<定义该指标的源码文件:行号>"
  impact: "<用户/业务可感知的后果>"
  noise_reduction: |
    按 <低基数维度> 分组，避免局部问题触发全局告警；
    持续 2 个周期才触发；排除冷启动与压测窗口
  whitelist: "<计划内窗口、已知低流量时段>"

- alert_name: "错误率超阈值"
  level: P1
  signal_plane: metric
  condition: |
    sum(rate(<error_counter>[5m])) / sum(rate(<request_counter>[5m])) > <候选阈值>
  candidate_threshold: "<值>（待用历史数据校准）"
  source_ref: "<源码文件:行号>"
  impact: "<失败比例上升的业务影响>"

- alert_name: "资源水位过高"
  level: P1
  signal_plane: metric
  condition: "<usage_gauge> > <候选阈值>"
  candidate_threshold: "<值>（待压测确认拐点；注明量纲 0~1 还是 0~100）"
  source_ref: "<源码文件:行号>"
  impact: "<吞吐下降 / 拒绝服务 / 排队加剧>"
  noise_reduction: "持续 N 分钟；与饱和度指标联动判断"

- alert_name: "饱和度持续不为零"
  level: P2
  signal_plane: metric
  condition: "<queue_or_waiting_gauge> > 0 持续 5 分钟"
  candidate_threshold: "任何非零值（待确认基线）"
  source_ref: "<源码文件:行号>"
  impact: "<容量不足的领先指标，比总延迟更早反映问题>"
```

### L2：日志事件告警（存在性）

```yaml
- alert_name: "启动 / 就绪失败"
  level: P0
  signal_plane: log_event
  condition: |
    启动后 <N>s 内未出现 "<项目就绪日志模板>"
    或出现 "<启动失败类 ERROR 模板>"
  candidate_threshold: "<N>s（待按本项目启动耗时校准）"
  source_ref: "<从 loggers_extracted.json 检索 lifecycle 类 ERROR>"
  impact: "服务完全不可用"
  noise_reduction: "只在重启事件后判定；按实例去重"
  whitelist: "计划内重启"

- alert_name: "进程异常终止"
  level: P0
  signal_plane: log_event
  condition: "出现 <崩溃/abort/致命错误 类模板>"
  impact: "服务中断"
  noise_reduction: "按实例去重；连续多次升级；注意断言类失败可能不打日志，需配合日志断档 + 进程重启判定"

- alert_name: "资源耗尽"
  level: P1
  signal_plane: log_event
  condition: "出现 <OOM / 配额 / 句柄耗尽 类模板>"
  impact: "<请求失败或进程退出>"
  noise_reduction: "按资源维度去重；区分可降级与致命场景"

- alert_name: "关键依赖不可用"
  level: P1
  signal_plane: log_event
  condition: "出现 <依赖连接失败/超时 类 ERROR 模板>"
  impact: "<核心链路降级或不可用>"
  noise_reduction: "按依赖类型聚合；排除计划内维护窗口"
```

### L3：日志频率告警（谨慎使用）

```yaml
- alert_name: "<某类错误> 计数突增"
  level: P2
  signal_plane: log_rate
  condition: |
    sum(rate({job="<service>"} |= "<错误模板>" [5m])) > 基线 * <倍数>
  candidate_threshold: "<倍数> 倍基线（待用历史数据确定基线）"
  source_ref: "<日志模板所在源码文件:行号>"
  impact: "<该功能退化的影响>"
  noise_reduction: |
    必须用「相对基线」而非绝对阈值；
    排除已知预期内的异常请求；持续 3 个周期
  whitelist: "<已知正常重试、客户端断开、降级路径>"
```

### L4：链路告警（可选，仅在确实开启 trace 时）

```yaml
- alert_name: "<关键阶段> 耗时劣化"
  level: P2
  signal_plane: trace
  condition: "span <阶段名> 的 P95 时长环比上升 > <候选阈值>"
  candidate_threshold: "<值>（待校准）"
  source_ref: "<span 定义处源码文件:行号>"
  impact: "<启动/发布变慢或用户可感知延迟上升>"
  noise_reduction: "只在发布后观察；区分冷/热缓存；确认采样率"
```

---

## 五、降噪白名单（必须显式列出）

| 关键字 / 情况 | 为什么不该告警 | 处理方式 |
|---|---|---|
| `client disconnected` / `connection reset` | 客户端主动断开，正常现象 | 忽略，不计入错误率 |
| `retry attempt N` | 正常重试逻辑 | 只在重试耗尽后告警 |
| 可选特性不可用（`xxx not found, falling back`） | 降级路径，服务仍正常 | 忽略；若要监控，用 P3 统计降级次数 |
| 冷启动 / 首次初始化日志 | 启动期必然出现 | 排除启动后设定的窗口期 |
| `feature_gated`（`conditional` 非空）的日志 | 该特性可能未启用 | 先确认配置，再决定是否监控 |
| `once=true` 的重复提示 | 只打印一次，频率无意义 | 只做存在性判断 |

**白名单不是可选项**。没有白名单的日志告警，上线一周内必然被静音。

---

## 六、告警生成 Prompt

```
基于指标清单 + 已分类日志 + span 清单，生成候选告警规则。

输出 JSON 数组，每条含：
{
  "alert_name": "简短描述",
  "level": "P0/P1/P2/P3",
  "signal_plane": "metric / log_event / log_rate / trace",
  "condition": "PromQL 或日志检索条件",
  "candidate_threshold": "候选阈值（标注：待校准）",
  "source_ref": "源码文件:行号",
  "impact": "故障影响",
  "noise_reduction": "降噪策略",
  "whitelist": "需要排除的已知预期情况"
}

约束（严格遵守）：
1. 能用指标表达的必须用指标，不要用日志频率替代指标
2. 阈值只给候选值，禁止说「确定值」「应设为」
3. 每条必须溯源到具体源码位置，指标名/字段名必须来自提取结果
4. 源码超时常量 ≠ 告警阈值，必须分别说明
5. in_loop=true 的日志禁止频率告警，只能趋势
6. once=true 的日志只做存在性告警
7. 每条规则必须配 whitelist，空的要说明为什么不需要
8. 指标阈值要写明该指标在本项目的量纲与单位
9. 需求清单必须匹配运行形态；库/SDK 类项目优先输出「暴露给宿主什么信号」
10. 禁止套用样例或领域基线里的指标名、阈值、维度；一律从本项目推导
```
