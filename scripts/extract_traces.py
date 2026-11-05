#!/usr/bin/env python3
"""
链路追踪（Tracing）埋点提取工具
提取源码中的 span 定义，补齐「日志 + 指标」之外的第三个可观测平面。

为什么需要它：
    trace 是排查「这一次请求慢在哪一段」的唯一手段。
    LLM 推理引擎的 span 通常覆盖：模型加载、编译、KV Cache 分配、warmup、引擎初始化等。
    只扫日志和指标，会漏掉「分段耗时」这类只有 trace 才能回答的问题。

支持的写法（已用 vLLM main 实测）：
    - 装饰器: @instrument(span_name="Load model") / @trace_span("x") / @tracer.start_as_current_span("x")
    - 上下文: with tracer.start_as_current_span("x") as span:
    - 手动:   manual_instrument_otel(span_name="x") / tracer.start_span(name="x")
    - NVTX:   nvtx.range_push("x") / with nvtx.range("x"):
    - 属性:   span.set_attribute("k", v) / span.set_attributes({...})

用法:
    python extract_traces.py <源码目录> [--output traces_extracted.json]
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
    "tests", "test", "testing", "docs", "examples", "benchmarks",
}

# 装饰器 / 函数名 → 该写法把 span 名放在哪个关键字参数里
# 注意：不要放裸 "span" —— TensorRT-LLM 里 writer.span(0) 是缓冲区偏移方法，
# 会带来一批 span 名为 "0" 的假 span。
SPAN_DECORATORS = {"instrument", "trace_span", "instrument_otel", "traced"}
SPAN_METHODS = {"start_as_current_span", "start_span", "manual_instrument_otel",
                "range_push", "range_pop", "record_function"}
SPAN_NAME_KWARGS = ("span_name", "name", "span", "description")

# 自研链路 DSL：SGLang 的 TraceReqContext 用这些方法，不走 OTel SDK。
# span 名通常是变量（RequestStage.PREFILL_FORWARD），所以只能记调用点，
# 真正的名字要从「阶段注册表」里取。
SPAN_DSL_METHODS = {"trace_slice", "trace_event", "trace_req_start", "trace_req_finish"}

# 阶段注册表：Class.ATTR = SomeConfig("stage_name", level=N)
STAGE_REGISTRY_HINT = re.compile(r"stage|span|event|phase|step", re.I)

# span 事件（挂在当前 span 上的时间点，不是独立 span）
# 不放 "record_event"：CUDA stream 的 record_event(event) 也是这个名字，
# TensorRT-LLM 的 fmha_bmark 里就有，会误报成 span 事件。
SPAN_EVENT_METHODS = {"add_event", "add_span_event"}


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _span_name_literal(node):
    """返回 (span名, 是否字面量)"""
    for kw in node.keywords:
        if kw.arg in SPAN_NAME_KWARGS:
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value, True
            return _unparse(v), False
    if node.args:
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value, True
        return _unparse(a), False
    return "", True


def _span_name(node) -> str:
    """从 span 构造调用里取出 span 名"""
    for kw in node.keywords:
        if kw.arg in SPAN_NAME_KWARGS:
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value
            return _unparse(v)
    if node.args:
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
        return _unparse(a)
    return ""


def _is_span_ctor(node) -> tuple[bool, str]:
    """判断调用是不是 span 构造，返回 (是否, 调用名)"""
    if not isinstance(node, ast.Call):
        return False, ""
    f = node.func
    if isinstance(f, ast.Attribute):
        if f.attr in SPAN_METHODS:
            return True, f.attr
        # @tracing.trace_span("x") —— 装饰器挂在模块上（TensorRT-LLM 的写法）
        if f.attr in SPAN_DECORATORS:
            return True, f.attr
        # 自研 DSL（SGLang TraceReqContext 等）：span 名多为变量
        if f.attr in SPAN_DSL_METHODS:
            return True, f"dsl:{f.attr}"
        # nvtx.range("x") 只在 nvtx 语境下才是 span，排除内置 range()
        if f.attr == "range" and "nvtx" in _unparse(f.value).lower():
            return True, "nvtx.range"
        return False, ""
    if isinstance(f, ast.Name):
        if f.id in SPAN_METHODS or f.id in SPAN_DECORATORS:
            return True, f.id
    return False, ""


def _plausible_span_name(name: str) -> bool:
    """排除 span(0) / span(2) 这类偏移量调用被当成 span 名"""
    return bool(name) and not re.fullmatch(r"[\d.\s,()]+", name)


class TraceVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, source_lines: list):
        self.rel_path = rel_path.replace(os.sep, "/")
        self.source_lines = source_lines
        self.records = []
        self.func_stack = []
        self.class_stack = []

        rel = self.rel_path
        self.module = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel
        parts = rel.split("/")
        self.package = "/".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "")

    def _visit_func(self, node):
        # 先处理装饰器：装饰器上的 span 属于「整个函数」
        for dec in node.decorator_list:
            ok, ctor = _is_span_ctor(dec)
            if ok:
                name = _span_name(dec)
                if _plausible_span_name(name):
                    rec = self._record(dec.lineno, name, f"decorator:{ctor}", dec)
                    rec["attributes"] = self._kw_attrs(dec)
                    self.records.append(rec)

        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_ClassDef(self, node):
        """span 属性 / 事件名注册表：class SpanAttributes(StrEnum)

        TensorRT-LLM 按 GenAI 语义约定把属性名集中定义在枚举里，
        调用点只写 SpanAttributes.GEN_AI_LATENCY_E2E，扫调用点拿不到字面量。
        """
        bases = {_unparse(b).split(".")[-1] for b in node.bases}
        if bases & {"StrEnum", "Enum"} and re.search(r"attr|event|semconv", node.name, re.I):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                        and isinstance(stmt.targets[0], ast.Name) \
                        and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    rec = self._record(stmt.lineno, stmt.value.value, "attr_schema", stmt)
                    rec["kind"] = "attr_schema"
                    rec["attr"] = stmt.targets[0].id
                    rec["class"] = node.name
                    rec["span_name_dynamic"] = False
                    self.records.append(rec)
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    @staticmethod
    def _kw_attrs(node) -> list:
        """@instrument(attributes={"k": v}) 里声明的 span 属性"""
        out = []
        if not isinstance(node, ast.Call):
            return out
        for kw in node.keywords:
            if kw.arg == "attributes" and isinstance(kw.value, ast.Dict):
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        out.append(k.value)
        return out

    def _record(self, line: int, span_name: str, style: str, node) -> dict:
        kind = "internal"
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "kind":
                    kind = _unparse(kw.value).split(".")[-1].lower()
        return {
            "kind": "span",
            "span_name": span_name,
            "name": span_name,
            "span_kind": kind,
            "style": style,
            "file": self.rel_path,
            "line": line,
            "module": self.module,
            "package": self.package,
            "class": self.class_stack[-1] if self.class_stack else "",
            "function": ".".join(self.class_stack[-1:] + self.func_stack[-1:]) if self.func_stack else "",
            "code_snippet": self.source_lines[line - 1].strip() if 0 < line <= len(self.source_lines) else "",
            "attributes": [],
            "span_name_dynamic": not isinstance(node, ast.Call) or not _span_name_literal(node)[1],
        }

    def visit_Assign(self, node):
        """阶段注册表：Class.ATTR = SomeConfig("stage_name", level=N)

        SGLang 的 RequestStage 就是这种写法——21 个阶段名全在这里，
        而调用点只传变量，扫调用点拿不到名字。
        """
        if self.class_stack and isinstance(node.value, ast.Call):
            call = node.value
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                level = ""
                for kw in call.keywords:
                    if kw.arg in ("level", "stage_level", "depth"):
                        level = _unparse(kw.value)
                has_level = bool(level)
                looks_like_registry = STAGE_REGISTRY_HINT.search(self.class_stack[-1])
                if has_level or looks_like_registry:
                    for t in node.targets:
                        # 注册表的条目名一定是常量风格（TOKENIZE / PREFILL_FORWARD），
                        # 普通数据类属性是小写（name_parts / freqs_cis），用它区分
                        if isinstance(t, ast.Name) and re.match(r"^[A-Z][A-Z0-9_]+$", t.id):
                            rec = self._record(node.lineno, first.value, "stage_registry", call)
                            rec["kind"] = "stage_registry"
                            rec["attr"] = t.id
                            rec["stage_level"] = level
                            rec["span_name_dynamic"] = False
                            rec["class"] = self.class_stack[-1]
                            self.records.append(rec)
        self.generic_visit(node)

    def visit_Call(self, node):
        ok, ctor = _is_span_ctor(node)
        if ok:
            name = _span_name(node)
            if not name and ctor.startswith("dsl:"):
                # trace_req_start() 这类无参调用是「请求根 span」
                name = f"<{ctor[4:]}>"
            if _plausible_span_name(name):
                rec = self._record(node.lineno, name, f"call:{ctor}", node)
                rec["attributes"] = self._kw_attrs(node)
                if ctor.startswith("dsl:"):
                    rec["is_dsl"] = True
                    rec["span_kind"] = "server" if "req" in ctor else "internal"
                self.records.append(rec)

        # span 属性
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("set_attribute", "set_attributes"):
            if node.args:
                key = ""
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    key = a.value
                elif isinstance(a, ast.Dict):
                    key = ",".join(
                        k.value for k in a.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    )
                if key:
                    rec = self._record(node.lineno, key, "attribute", node)
                    rec["kind"] = "attribute"
                    rec["attributes"] = [key]
                    self.records.append(rec)

        # span 事件：add_event("kv_cache_transfer_start")
        # 事件名也常来自枚举：tracing.add_event(SpanEvents.KV_CACHE_TRANSFER_START)
        if isinstance(node.func, ast.Attribute) and node.func.attr in SPAN_EVENT_METHODS:
            if node.args:
                a0 = node.args[0]
                ev_name, literal = "", False
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    ev_name, literal = a0.value, True
                elif isinstance(a0, ast.Attribute):
                    # 枚举成员：SpanEvents.KV_CACHE_TRANSFER_START → 取属性名。
                    # 只认 UPPER_CASE，避免把 self.xxx 这类变量误当事件名。
                    if re.match(r"^[A-Z][A-Z0-9_]+$", a0.attr):
                        ev_name = a0.attr
                if _plausible_span_name(ev_name):
                    rec = self._record(node.lineno, ev_name, "event", node)
                    rec["kind"] = "span_event"
                    rec["span_name_dynamic"] = not literal
                    self.records.append(rec)

        self.generic_visit(node)


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

    lang_cfg = load_config() if _LANG_BACKEND else {}
    records, scanned = [], 0
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
                v = TraceVisitor(rel_path, source.splitlines())
                v.visit(tree)
                if v.records:
                    records.extend(v.records)
            elif _LANG_BACKEND:
                # Java/Go/JS(含 Kotlin/TS)：通用正则后端
                file_lang = language_for_ext(ext)
                if file_lang and lang in ("auto", file_lang) \
                        and not is_test_path(rel_path, file_lang, lang_cfg):
                    scanned += 1
                    recs = lang_extract_file("traces", fpath, rel_path, file_lang, lang_cfg)
                    if recs:
                        records.extend(recs)

    # 阶段注册表去噪：类名带 Stage/Span 但只有一两个属性赋值的，
    # 通常是普通数据类（freqs_cis = load("freqs_cis")），不是阶段注册表。
    reg = [r for r in records if r["kind"] == "stage_registry"]
    if reg:
        by_class = {}
        for r in reg:
            by_class.setdefault((r["file"], r["class"]), []).append(r)
        keep = {k for k, v in by_class.items()
                if any(x.get("stage_level") for x in v) or len(v) >= 3}
        records = [r for r in records
                   if r["kind"] != "stage_registry" or (r["file"], r["class"]) in keep]

    # 装饰器会被同时记为 decorator 和 call，去重保留 decorator
    decorator_lines = {
        (r["file"], r["line"]) for r in records
        if r["kind"] == "span" and r["style"].startswith("decorator:")
    }
    deduped, seen = [], set()
    for r in records:
        key = (r["file"], r["line"], r["span_name"], r["kind"])
        if key in seen:
            continue
        if (r["file"], r["line"]) in decorator_lines and r["style"].startswith("call:"):
            continue
        seen.add(key)
        deduped.append(r)

    reassign_packages(deduped)
    return deduped, scanned


def print_summary(records: list, scanned: int):
    spans = [r for r in records if r["kind"] == "span"]
    attrs = [r for r in records if r["kind"] == "attribute"]
    stages = [r for r in records if r["kind"] == "stage_registry"]
    events = [r for r in records if r["kind"] == "span_event"]
    schema = [r for r in records if r["kind"] == "attr_schema"]

    print(f"\n{'=' * 64}")
    print("链路追踪埋点提取完成")
    print(f"{'=' * 64}")
    print(f"扫描源码文件数: {scanned}")
    print(f"span 定义: {len(spans)}    属性设置: {len(attrs)}    阶段注册表: {len(stages)}")
    if events or schema:
        print(f"span 事件: {len(events)}    属性名注册表: {len(schema)}")

    lang_spans = _Counter(r.get("language", "python") for r in spans)
    if len(lang_spans) > 1:
        print("\nspan 按语言分布:")
        for l, c in lang_spans.most_common():
            print(f"  {l:8s}: {c}")

    if schema:
        by_cls = {}
        for r in schema:
            by_cls.setdefault(r["class"], []).append(r)
        print("\nspan 属性名注册表（GenAI 语义约定）:")
        for cls, items in by_cls.items():
            print(f"  {cls} ({len(items)} 个属性)")
            for r in items[:20]:
                print(f"      {r['attr']:34s} → {r['span_name']}")

    if stages:
        print("\n阶段注册表（自定义 trace DSL 的真实 span 名在这里）:")
        by_class = {}
        for r in stages:
            by_class.setdefault(r["class"], []).append(r)
        for cls, items in by_class.items():
            print(f"  {cls} ({len(items)} 个阶段)")
            for r in sorted(items, key=lambda x: (x.get("stage_level") or "", x["span_name"]))[:40]:
                lvl = f" level={r['stage_level']}" if r.get("stage_level") else ""
                print(f"      {r['attr']:28s} → {r['span_name']}{lvl}")

    dsl = [r for r in spans if r.get("is_dsl")]
    if dsl:
        print(f"\n自研 DSL 调用点: {len(dsl)} 个"
              f"（span 名多由变量传入，需配合阶段注册表解读）")

    if not spans:
        print("\n未发现 trace span。该项目可能未接入 OTel/NVTX 链路追踪，")
        print("或其链路写法不在 signal_patterns.json 覆盖范围内（先全文检索确认，再补规则或走人工降级）。")
        print(f"{'=' * 64}\n")
        return

    styles = _Counter(r["style"] for r in spans)
    print("\n按写法分布:")
    for st, c in styles.most_common():
        print(f"  {st:30s}: {c}")

    pkgs = _Counter(r["package"] for r in spans)
    print("\n按包分布 (Top 8):")
    for pkg, c in pkgs.most_common(8):
        print(f"  {(pkg or '<root>'):40s}: {c}")

    names = [r["span_name"] for r in spans]
    dup = _Counter(names)
    print("\n高频 span 名 (Top 10):")
    for n, c in dup.most_common(10):
        print(f"  {n[:50]:52s}: {c}")

    with_attrs = sum(1 for r in spans if r.get("attributes"))
    print(f"\n带 span 属性的 span: {with_attrs} / {len(spans)}")

    kinds = _Counter(r["span_kind"] for r in spans)
    print("\nspan 类型:")
    for k, c in kinds.most_common():
        print(f"  {k:12s}: {c}")
    print(f"{'=' * 64}\n")


def main():
    parser = argparse.ArgumentParser(description="链路追踪埋点提取工具")
    parser.add_argument("src_dir")
    parser.add_argument("--lang", choices=["auto", "python", "java", "go", "js"], default="auto")
    parser.add_argument("--output", "-o", default="traces_extracted.json")
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
