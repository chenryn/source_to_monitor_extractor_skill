#!/usr/bin/env python3
"""
Prometheus / OpenTelemetry 指标定义提取工具
从源码中批量提取「已存在的指标定义」，与日志埋点组成完整的信号清单。

为什么需要它：
    日志只反映「开发者打印了什么」，而生产监控的主体是指标（Prometheus 拉取 /metrics）。
    只扫日志会得出错误结论，例如「没有请求级延迟监控」——实际上 TTFT/ITL/e2e 都在指标里。

支持的写法（已用 vLLM main 实测）：
    - prometheus_client: Counter / Gauge / Histogram / Summary / Info / Enum
    - 工厂别名: self._counter_cls(name=...) / _gauge_cls / _histogram_cls（vLLM 风格）
    - 模块级别名: C = Counter; C(name=...)
    - OpenTelemetry: meter.create_counter / create_histogram / create_up_down_counter / create_gauge
    - 名称来自常量: NAME = "vllm:xxx"; Gauge(name=NAME)
    - 指标写入点: .labels(...).observe() / .inc() / .set()
    - 暴露点: /metrics 路由、make_asgi_app、generate_latest
    - 指标数据源: 名为 *Stats 的 dataclass 字段（vLLM 的 IterationStats 等）

用法:
    python extract_metrics.py <源码目录> [--output metrics_extracted.json]
                              [--include-all] [--exclude GLOB ...]

输出为记录列表，每条含 kind 字段：
    definition  指标定义（最重要）
    update      指标写入点
    exposure    指标暴露端点
    stats_field 指标数据源字段
"""

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
from collections import Counter as _Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from lang_extract import (EXT2LANG, extract_file as lang_extract_file,
                              is_test_path, language_for_ext, load_config)
    _LANG_BACKEND = True
except ImportError:
    _LANG_BACKEND = False

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
    "third_party", "thirdparty", ".mypy_cache", ".pytest_cache",
    "tests", "test", "docs", "examples", "benchmarks",
}

# prometheus_client 构造器
PROM_TYPES = {    "Counter": "counter",
    "Gauge": "gauge",
    "Histogram": "histogram",
    "Summary": "summary",
    "Info": "info",
    "Enum": "enum",
}

# vLLM 式工厂别名后缀 → 类型
FACTORY_SUFFIX = {
    "_counter_cls": "counter",
    "_gauge_cls": "gauge",
    "_histogram_cls": "histogram",
    "_summary_cls": "summary",
}

# OpenTelemetry 仪表构造器
OTEL_TYPES = {
    "create_counter": "counter",
    "create_histogram": "histogram",
    "create_up_down_counter": "gauge",
    "create_gauge": "gauge",
    "create_observable_gauge": "gauge",
    "create_observable_counter": "counter",
}

# 指标写入方法
UPDATE_METHODS = {"inc", "dec", "observe", "set", "inc_by", "add"}

# 统一写入助手：指标对象作为参数传进去（TensorRT-LLM 的 _log_counter / _log_histogram）
HELPER_METRIC_RE = re.compile(r"counter|histogram|gauge|summary|metric|stat", re.I)

EXPOSURE_HINTS = ("make_asgi_app", "generate_latest", "start_http_server", "prometheus_client")


# 形如 vllm:xxx / sglang:xxx 的指标名字面量（用于兜底发现自定义指标 DSL）
#
# 这里只做「宽松收集」，真正的判定在 scan() 末尾按前缀做：
# 指标命名空间是从已解析出的指标定义里学出来的，不靠后缀白名单。
# 后缀白名单的教训：vLLM 上用着还行，到 SGLang 直接漏掉 64/187
# （sglang:num_running_reqs、sglang:cache_hit_rate 都不在名单里）。
METRIC_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*:[a-zA-Z][a-zA-Z0-9_]*$")

# 直方图/摘要的派生样本后缀
DERIVED_SUFFIXES = ("_count", "_sum", "_bucket", "_created")

CONST_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*(?::\s*[\w\[\], \"']+)?=\s*[\"']([^\"']+)[\"']", re.M)


def build_global_constants(src_path) -> dict:
    """跨模块字符串常量索引：NAME -> 值。
    指标名常写成 NAME = "vllm:xxx"，再在别的模块用 name=NAME 引用。"""
    index = {}
    for p in Path(src_path).rglob("*.py"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, value in CONST_RE.findall(text):
            index.setdefault(name, value)
    return index


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _literal_list(node):
    """把字面量 list/tuple 转成字符串列表，否则返回 None"""
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant):
                out.append(str(elt.value))
            else:
                return None
        return out
    return None


def collect_constants(tree) -> dict:
    """模块级字符串常量 + 列表常量，用于解析 name=CONST / labelnames=CONST"""
    consts = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    lit = _literal_list(value)
                    if lit is not None:
                        consts[t.id] = lit
                    elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                        consts[t.id] = value.value
                    elif isinstance(value, (ast.List, ast.Tuple)) and not lit:
                        # 元素是变量的列表，保留原始文本
                        consts[t.id] = _unparse(value)
    return consts


def resolve_metric_type(func_node, aliases: dict) -> str:
    """判断这是不是指标构造器，返回指标类型或空串"""
    if isinstance(func_node, ast.Attribute):
        attr = func_node.attr
        if attr in PROM_TYPES:
            return PROM_TYPES[attr]
        if attr in FACTORY_SUFFIX:
            return FACTORY_SUFFIX[attr]
        if attr in OTEL_TYPES:
            return OTEL_TYPES[attr]
        # 自定义封装：xxx_metric_cls / xxx_histogram
        low = attr.lower()
        for key, typ in (
            ("histogram", "histogram"), ("summary", "summary"),
            ("counter", "counter"), ("gauge", "gauge"),
        ):
            if key in low and (low.endswith("_cls") or low.endswith("_metric")):
                return typ
        return ""
    if isinstance(func_node, ast.Name):
        name = func_node.id
        if name in PROM_TYPES:
            return PROM_TYPES[name]
        if name in OTEL_TYPES:
            return OTEL_TYPES[name]
        if name in aliases:
            return aliases[name]
    return ""


class MetricVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, consts: dict, aliases: dict, source_lines: list,
                 global_consts: dict | None = None, enum_values: dict | None = None):
        self.rel_path = rel_path.replace(os.sep, "/")
        self.consts = consts
        self.global_consts = global_consts or {}
        self.enum_values = enum_values or {}
        self.attr_consts = {}  # self.attr = "..." 形式的实例属性常量
        self.aliases = aliases
        self.source_lines = source_lines
        self.records = []
        self.func_stack = []
        self.class_stack = []
        self.cond_stack = []
        self.metric_vars = {}  # 变量名 → 指标名，用于定位写入点

        rel = self.rel_path
        self.module = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
        parts = rel.split("/")
        self.package = "/".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "")

    # —— 作用域 ——
    def _visit_func(self, node):
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_If(self, node):
        self.cond_stack.append(_unparse(node.test)[:160])
        self.generic_visit(node)
        self.cond_stack.pop()

    def _base_record(self, kind: str, line: int) -> dict:
        snippet = ""
        if 0 < line <= len(self.source_lines):
            snippet = self.source_lines[line - 1].strip()
        return {
            "kind": kind,
            "file": self.rel_path,
            "line": line,
            "module": self.module,
            "package": self.package,
            "class": self.class_stack[-1] if self.class_stack else "",
            "function": ".".join(self.class_stack[-1:] + self.func_stack[-1:]) if self.func_stack else "",
            "code_snippet": snippet,
            "conditional": self.cond_stack[-1] if self.cond_stack else "",
        }

    def _kw(self, node, *names):
        for kw in node.keywords:
            if kw.arg in names:
                return kw.value
        return None

    def _resolve_str(self, node):
        v = self._try_resolve(node)
        if v is not None:
            return v
        return _unparse(node) if node is not None else ""

    def _try_resolve(self, node):
        """能静态求出字符串就返回，求不出返回 None。

        覆盖 TensorRT-LLM 这类写法：`name=self.metric_prefix + "e2e_latency"`，
        其中 `self.metric_prefix = "trtllm_"` 在 __init__ 里赋值。
        """
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            for table in (self.consts, self.global_consts):
                v = table.get(node.id)
                if isinstance(v, str):
                    return v
            return None
        if isinstance(node, ast.Attribute):
            # self.attr = "..." （实例属性常量）
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                v = self.attr_consts.get(node.attr)
                if isinstance(v, str):
                    return v
            for table in (self.consts, self.global_consts):
                v = table.get(node.attr)
                if isinstance(v, str):
                    return v
            # 枚举成员：MetricNames.TTFT / MetricNames.TTFT.value
            key = f"{_unparse(node.value)}.{node.attr}"
            if key in self.enum_values:
                return self.enum_values[key]
            if node.attr == "value" and key[: -len(".value")] in self.enum_values:
                return self.enum_values[key[: -len(".value")]]
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._try_resolve(node.left)
            right = self._try_resolve(node.right)
            if left is not None and right is not None:
                return left + right
            return None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    sub = self._try_resolve(v.value)
                    if sub is None:
                        return None
                    parts.append(sub)
            return "".join(parts)
        return None

    def _clean_receiver(self, recv: str) -> str:
        """去掉下标与 .labels(...)，用于把写入点对回指标变量"""
        recv = re.split(r"\.labels\(", recv)[0]
        recv = re.sub(r"\[[^\]]*\]", "", recv)
        return recv.strip()

    def visit_Call(self, node):
        mtype = resolve_metric_type(node.func, self.aliases)

        # —— 指标定义 ——
        if mtype:
            # prometheus_client.Enum 必须带 states=；
            # 标准库 enum.Enum 的函数式写法 Enum("AuxStreamType", [...]) 不是指标
            if mtype == "enum" and self._kw(node, "states") is None:
                self.generic_visit(node)
                return

            name_node = self._kw(node, "name")
            explicit = name_node is not None
            name = self._resolve_str(name_node) if explicit else ""

            if not explicit and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value

            # 无 name 参数且位置参数不是字符串 → 不是 Prometheus 指标
            # （典型误报：collections.Counter(generator) 的计数用法）
            if not name:
                self.generic_visit(node)
                return

            # 指标名来自函数参数 / 对象属性（如 name=name、name=metric.name）→ 动态封装
            # 判据统一交给 _try_resolve：能静态求值就是定义，求不出才是封装。
            dynamic_name = name_node is not None and self._try_resolve(name_node) is None

            clean_name = name.strip()
            is_definition = (not dynamic_name) and bool(
                re.match(r"^[A-Za-z_][A-Za-z0-9_:.\-]*$", clean_name)
            ) and not any(ch in clean_name for ch in " ()[]{},'\"")

            if is_definition:
                doc = self._resolve_str(self._kw(node, "documentation", "description", "help"))

                labels_node = self._kw(node, "labelnames", "labels")
                labels = []
                if labels_node is not None:
                    lit = _literal_list(labels_node)
                    labels = lit if lit is not None else [_unparse(labels_node)]

                buckets_node = self._kw(node, "buckets")
                buckets = ""
                if buckets_node is not None:
                    lit = _literal_list(buckets_node)
                    buckets = ",".join(lit) if lit else _unparse(buckets_node)

                rec = self._base_record("definition", node.lineno)
                rec.update({
                    "name": clean_name,
                    "metric_type": mtype,
                    "documentation": doc,
                    "labelnames": labels,
                    "buckets": buckets,
                    "unit": self._resolve_str(self._kw(node, "unit")),
                    "multiprocess_mode": self._resolve_str(self._kw(node, "multiprocess_mode")),
                    "constructor": _unparse(node.func),
                })
                self.records.append(rec)

                parent_var = getattr(node, "_assigned_to", None)
                if parent_var:
                    self.metric_vars[parent_var] = clean_name
            else:
                # 指标名由参数/属性传入的封装函数，记录为 wrapper
                rec = self._base_record("wrapper", node.lineno)
                rec.update({
                    "name": clean_name[:80],
                    "metric_type": mtype,
                    "constructor": _unparse(node.func),
                    "name_source": _unparse(name_node) if explicit else "",
                    "labelnames": [],
                    "buckets": "",
                    "documentation": "",
                })
                self.records.append(rec)

        # —— 指标写入点 ——
        if isinstance(node.func, ast.Attribute) and node.func.attr in UPDATE_METHODS:
            recv = _unparse(node.func.value)
            cleaned = self._clean_receiver(recv)
            metric_name = self.metric_vars.get(cleaned) or self.metric_vars.get(cleaned.split(".")[-1], "")
            looks_like_metric = bool(re.search(r"(counter|gauge|histogram|summary|metric)", recv, re.I))
            if metric_name or looks_like_metric:
                rec = self._base_record("update", node.lineno)
                rec.update({
                    "metric_var": cleaned,
                    "metric_name": metric_name,
                    "method": node.func.attr,
                    "value_expr": _unparse(node.args[0])[:200] if node.args else "",
                })
                self.records.append(rec)

        # —— 指标作为参数传给统一写入助手 ——
        # TensorRT-LLM 的写法：self._log_counter(self.counter_request_success, labels, 1)
        # 指标对象不出现在 .inc() 的接收者位置，而是作为辅助函数参数传入。
        # 只认「辅助函数名里带 counter/histogram/gauge/metric/stat」的调用，
        # 否则 logger.info(metric) 之类的也会被算进来。
        if isinstance(node.func, ast.Attribute) and HELPER_METRIC_RE.search(node.func.attr) \
                and not node.func.attr.startswith("logger"):
            for a in node.args:
                key = _unparse(a)
                name = self.metric_vars.get(key) or self.metric_vars.get(key.split(".")[-1], "")
                if name:
                    rec = self._base_record("update", node.lineno)
                    rec.update({
                        "metric_var": key,
                        "metric_name": name,
                        "method": node.func.attr,
                        "via_helper": True,
                        "value_expr": _unparse(node.args[1])[:200] if len(node.args) > 1 else "",
                    })
                    self.records.append(rec)
                    break

        # —— 暴露点 ——
        fname = ""
        if isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        elif isinstance(node.func, ast.Name):
            fname = node.func.id
        if fname in ("make_asgi_app", "generate_latest", "start_http_server"):
            rec = self._base_record("exposure", node.lineno)
            rec.update({"exposure_type": fname, "name": fname, "metric_type": ""})
            self.records.append(rec)

        self.generic_visit(node)

    def visit_Assign(self, node):
        # 标记「指标构造结果赋给了哪个变量」
        for t in node.targets:
            if isinstance(t, (ast.Name, ast.Attribute)) and isinstance(node.value, ast.Call):
                node.value._assigned_to = _unparse(t)
            # self.metric_prefix = "trtllm_" 之类的实例属性常量，供 name= 拼接解析
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                self.attr_consts[t.attr] = node.value.value
        self.generic_visit(node)


def collect_aliases(tree) -> dict:
    """模块级别名：C = Counter / _gauge_cls = Gauge"""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Name) and node.value.id in PROM_TYPES:
                aliases[node.targets[0].id] = PROM_TYPES[node.value.id]
    return aliases


def collect_enum_values(tree) -> dict:
    """枚举里的字符串常量：`class MetricNames(Enum): TTFT = "trtllm_ttft_ms"`

    返回 {"MetricNames.TTFT": "trtllm_ttft_ms"}。用于解析
    `name=MetricNames.TTFT.value` 这类写法。
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {_unparse(b).split(".")[-1] for b in node.bases}
        if not (bases & {"Enum", "StrEnum", "IntEnum", "Flag"}):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                tgt = stmt.targets[0]
                if isinstance(tgt, ast.Name) and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    out[f"{node.name}.{tgt.id}"] = stmt.value.value
    return out


def collect_stats_fields(tree, rel_path: str, source_lines: list) -> list:
    """提取 *Stats 数据类字段——它们是指标的数据来源（vLLM 的 IterationStats 等）"""
    out = []
    rel = rel_path.replace(os.sep, "/")
    parts = rel.split("/")
    package = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith(("Stats", "Metrics")):
            continue
        if not any(isinstance(b, ast.Name) and b.id == "dataclass" for b in node.decorator_list):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                comment = ""
                line = stmt.lineno
                if 0 < line <= len(source_lines) and "#" in source_lines[line - 1]:
                    comment = source_lines[line - 1].split("#", 1)[1].strip()
                out.append({
                    "kind": "stats_field",
                    "file": rel,
                    "line": line,
                    "module": rel[:-3].replace("/", ".") if rel.endswith(".py") else rel,
                    "package": package,
                    "class": node.name,
                    "function": "",
                    "code_snippet": source_lines[line - 1].strip() if 0 < line <= len(source_lines) else "",
                    "name": f"{node.name}.{stmt.target.id}",
                    "metric_type": "",
                    "field_type": _unparse(stmt.annotation),
                    "documentation": comment,
                    "conditional": "",
                })
    return out


LAYOUT_PREFIXES = ("python", "src", "lib", "source")


def _strip_layout(parts: list) -> list:
    """剥掉 src-layout 前缀：python/sglang/srt/x.py → sglang/srt/x.py"""
    if len(parts) > 2 and parts[0] in LAYOUT_PREFIXES:
        return parts[1:]
    return parts


def reassign_packages(records: list) -> int:
    """按分布自适应决定包粒度（固定取两段在 src-layout 仓库上会全落一个桶）"""
    if not records:
        return 2
    paths = {r["file"]: _strip_layout(r["file"].split("/")[:-1]) or r["file"].split("/") for r in records}
    depth = 2
    for d in (2, 3, 4):
        depth = d
        buckets = _Counter("/".join(p[:d]) for p in paths.values())
        if max(buckets.values()) / len(records) <= 0.60:
            break
    for r in records:
        p = paths[r["file"]]
        r["package"] = "/".join(p[:depth]) if len(p) > 1 else (p[0] if p else "")
    return depth


def scan(src_dir: str, exclude_globs: list, include_all: bool, lang: str = "auto"):
    src_path = Path(src_dir).resolve()
    if not src_path.exists():
        print(f"错误: 目录不存在 - {src_dir}", file=sys.stderr)
        sys.exit(1)

    global_consts = build_global_constants(src_path)
    lang_cfg = load_config() if _LANG_BACKEND else {}
    var_index = {}  # 指标变量名 → 指标名，跨文件共享（Java/Go/JS 写入点溯源）
    records, scanned = [], 0
    literal_candidates = []
    for root, dirs, files in os.walk(src_path):
        dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDE_DIRS or include_all]
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, src_path)
            if any(fnmatch.fnmatch(rel_path, g) for g in exclude_globs):
                continue

            ext = Path(fname).suffix.lower()

            if ext == ".py" and lang in ("auto", "python"):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        source = f.read()
                    tree = ast.parse(source, filename=fpath)
                except (OSError, SyntaxError, ValueError):
                    continue

                scanned += 1
                lines = source.splitlines()
                consts = collect_constants(tree)
                aliases = collect_aliases(tree)
                enums = collect_enum_values(tree)
                visitor = MetricVisitor(rel_path, consts, aliases, lines, global_consts, enums)
                visitor.visit(tree)
                recs = visitor.records + collect_stats_fields(tree, rel_path, lines)

                # 兜底：收集形如 ns:name 的字符串字面量，
                # 用于发现不走 prometheus_client 的自定义指标 DSL。
                # 这里先不判定，等扫完拿到指标前缀集合再筛。
                seen_names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        v = node.value.strip()
                        if METRIC_NAME_RE.match(v) and v not in seen_names:
                            seen_names.add(v)
                            literal_candidates.append({
                                "kind": "name_literal",
                                "file": rel_path.replace(os.sep, "/"),
                                "line": getattr(node, "lineno", 0),
                                "module": "", "package": "", "class": "", "function": "",
                                "code_snippet": "", "conditional": "",
                                "name": v, "metric_type": "", "documentation": "",
                            })
                if recs:
                    records.extend(recs)
            elif _LANG_BACKEND:
                # Java/Go/JS(含 Kotlin/TS)：通用正则后端
                file_lang = language_for_ext(ext)
                if file_lang and lang in ("auto", file_lang) \
                        and not is_test_path(rel_path, file_lang, lang_cfg):
                    scanned += 1
                    recs = lang_extract_file(
                        "metrics", fpath, rel_path, file_lang, lang_cfg,
                        var_index=var_index,
                    )
                    if recs:
                        records.extend(recs)

    # 用「已解析出的指标定义」学出命名空间前缀与词表，再筛字面量。
    # 只看前缀不够：auto_round:auto_awq 这类字符串会出现 4 次，前缀也算「常见」。
    # 再看词表：指标名里的词（token、latency、usage、size…）在定义里出现过才认。
    known_prefixes = _Counter(
        r["name"].split(":")[0] for r in records if r["kind"] == "definition"
    )
    vocab = set()
    for r in records:
        if r["kind"] == "definition":
            vocab.update(t for t in r["name"].split(":")[-1].split("_") if t)
    cand_prefixes = _Counter(r["name"].split(":")[0] for r in literal_candidates)
    defined = {r["name"] for r in records if r["kind"] == "definition"}

    for r in literal_candidates:
        name = r["name"]
        prefix, _, body = name.partition(":")
        # 排除 sglang:diffusion_ 这种拼接残片
        if not body or body in ("_",) or name.endswith("_"):
            continue
        if prefix not in known_prefixes:
            # 未知命名空间：要求它至少被引用两次，且名字里含有指标词表里的词
            if cand_prefixes[prefix] < 2:
                continue
            if not (set(body.split("_")) & vocab):
                continue
            r["foreign_namespace"] = True
        base = name
        for suf in DERIVED_SUFFIXES:
            if name.endswith(suf) and name[: -len(suf)] in defined:
                base = name[: -len(suf)]
                break
        if base != name:
            r["derived_from"] = base
        records.append(r)

    reassign_packages(records)
    return records, scanned


def print_summary(records: list, scanned: int):
    defs = [r for r in records if r["kind"] == "definition"]
    ups = [r for r in records if r["kind"] == "update"]
    exp = [r for r in records if r["kind"] == "exposure"]
    stf = [r for r in records if r["kind"] == "stats_field"]
    wrap = [r for r in records if r["kind"] == "wrapper"]

    print(f"\n{'=' * 64}")
    print("指标定义提取完成")
    print(f"{'=' * 64}")
    print(f"扫描源码文件数: {scanned}")
    print(f"指标定义: {len(defs)}    写入点: {len(ups)}    暴露点: {len(exp)}")
    print(f"数据源字段: {len(stf)}   动态封装: {len(wrap)}（指标名由参数传入，需查调用点）")

    def _lang(r):
        return r.get("language", "python")

    lang_defs = _Counter(_lang(r) for r in defs)
    if len(lang_defs) > 1:
        print("\n定义按语言分布:")
        for l, c in lang_defs.most_common():
            print(f"  {l:8s}: {c}")

    if not defs:
        print("\n未发现指标定义。若项目确实使用 Prometheus/OTel，请检查写法是否在")
        print("signal_patterns.json 覆盖范围内（先全文检索确认，再补规则或走人工降级）。")
        print(f"{'=' * 64}\n")
        return

    types = _Counter(r["metric_type"] for r in defs)
    print("\n按类型分布:")
    for t, c in types.most_common():
        print(f"  {t:10s}: {c:4d}")

    pkgs = _Counter(r["package"] for r in defs)
    print("\n按包分布 (Top 8):")
    for pkg, c in pkgs.most_common(8):
        print(f"  {(pkg or '<root>'):40s}: {c}")

    # 命名前缀：优先看 Prometheus 官方的 ns:name，退而看 prefix_name（TensorRT-LLM 风格）
    prefixes = _Counter()
    style = "ns:name"
    for r in defs:
        m = re.match(r"^([a-zA-Z0-9_]+):", r["name"])
        if m:
            prefixes[m.group(1) + ":"] += 1
    if not prefixes:
        style = "prefix_name"
        for r in defs:
            m = re.match(r"^([a-z][a-z0-9]*)_", r["name"])
            prefixes[m.group(1) + "_" if m else "<no-prefix>"] += 1
    print(f"\n命名前缀分布（判断是否统一约定，风格 {style}）:")
    for p, c in prefixes.most_common(6):
        print(f"  {p:20s}: {c}")

    literals = {r["name"] for r in records if r["kind"] == "name_literal"
                and not r.get("foreign_namespace")}
    defined = {r["name"] for r in defs}
    derived = {r["name"]: r.get("derived_from", "") for r in records
               if r["kind"] == "name_literal" and r.get("derived_from")}
    # 直方图的 _count/_sum/_bucket 是自动派生样本，不算「未覆盖」
    uncovered = sorted(literals - defined - set(derived))

    cond = sum(1 for r in defs if r["conditional"])
    no_doc = sum(1 for r in defs if not r["documentation"])
    print("\n质量检查:")
    print(f"  条件定义（特性开关，可能默认不生效）: {cond}")
    print(f"  缺少 documentation 的指标        : {no_doc}")
    print(f"  有写入点可溯源的指标             : {len({u['metric_name'] for u in ups if u['metric_name']})}")
    if literals:
        print(f"  指标名覆盖率                     : {len(defined & literals)}/{len(literals)}"
              f"（{len(defined & literals) / len(literals):.0%}）")
        if derived:
            print(f"  直方图派生样本(_count/_sum/_bucket): {len(derived)} 个（自动生成，非缺口）")
        if uncovered:
            foreign = [n for n in uncovered if ":" in n and n.split(":")[0] not in
                       {d.split(":")[0] for d in defined}]
            print(f"  仅有名字引用、未匹配到构造器     : {len(uncovered)} 个"
                  f"（自定义指标 DSL 或跨模块定义，需人工确认）")
            if foreign:
                print(f"      其中来自其他命名空间（可能是外部包指标）: {len(foreign)} 个")
            for n in uncovered[:6]:
                print(f"      - {n}")
    else:
        print("  指标名覆盖率                     : N/A（本项目未使用 ns:name 风格的自定义 DSL）")
    skipped_foreign = sum(1 for r in records
                          if r["kind"] == "name_literal" and r.get("foreign_namespace"))
    if skipped_foreign:
        print(f"  已排除的异命名空间字面量         : {skipped_foreign} 个（非本项目指标，如样例数据标签）")
    print(f"{'=' * 64}\n")


def main():
    parser = argparse.ArgumentParser(description="Prometheus / OTel 指标定义提取工具")
    parser.add_argument("src_dir")
    parser.add_argument("--lang", choices=["auto", "python", "java", "go", "js"], default="auto")
    parser.add_argument("--output", "-o", default="metrics_extracted.json")
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args()

    records, scanned = scan(args.src_dir, args.exclude, args.include_all, args.lang)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    if not args.no_summary:
        print_summary(records, scanned)
    print(f"已写入: {args.output} ({len(records)} 条记录)")


if __name__ == "__main__":
    main()
