#!/usr/bin/env python3
"""
Java / Go / JS(含 Kotlin / TS) 三平面信号的通用正则提取后端。

为什么要单独一层：
    C++/Rust 的日志提取是「逐行正则」，但 Java/Go/JS 的指标与 span 定义几乎都跨行，
    且参数里混着对象字面量、结构化字段、链式调用。逐行正则会把
    `prometheus.NewCounterVec(prometheus.CounterOpts{Name: "x", ...}, []string{"a"})`
    拆碎。这里用一个轻量词法层把注释和字符串内容屏蔽成等长占位符，再在「掩码全文」
    上做正则 + 括号平衡的参数解析，因此跨行调用、嵌套对象、字符串里的逗号/括号都能正确处理。

配置：scripts/signal_patterns.json。规则通用字段：
    pattern          正则（re.I | re.M），建议以 "\\s*\\(" 结尾；匹配位置附近的 '(' 即调用起点
    name             规则名（写进 logger_type / constructor / style）
    level            "group:level"（取命名组）或 "fixed:INFO"
    name_from        arg0 / arg1 / last / message / none / key:Name / chain:name / all_args
    doc_from         key:Help | chain:setDescription（可用 | 分隔多个候选）
    labels_from      arg1 | key:labelNames
    prefix_from      key:Namespace（能求值时拼进指标名，求不出时记 namespace_expr）
    receiver_hint    命名组 recv 必须匹配的正则，用于挡掉同名 API（如 http.Error / list.add）
    print            默认排除，--include-print 才计入
    assertion        失败即中止/抛异常，is_assertion=true
    kind             definition / span / attribute / span_event / exposure / registration
    all_args         每个参数各取第一个字符串（span.SetAttributes(attribute.String("k", v), ...)）
    literal_pattern  对字符串字面量值做匹配（暴露路径等；掩码文本里看不到字符串内容）
    require_key      必须存在该 key 才算命中（挡住 `new Counter()` 这类同名业务类）

对外接口：
    load_config()                         → dict
    language_for_ext(ext)                 → "java" / "go" / "js" / None
    is_test_path(rel_path, lang, cfg)     → bool
    extract_file(plane, filepath, rel_path, lang, cfg, include_print=False, var_index=None) → list[dict]

    plane ∈ {"loggers", "metrics", "traces"}
输出记录与三个 Python 提取脚本的 JSON 同构，额外带 language 字段。
"""

import bisect
import json
import re
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "signal_patterns.json"

EXT2LANG = {
    ".java": "java", ".kt": "java",
    ".go": "go",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js",
}

LEVEL_MAP = {
    "trace": "DEBUG", "debug": "DEBUG", "verbose": "DEBUG", "log": "INFO",
    "info": "INFO", "config": "INFO", "notice": "INFO",
    "print": "INFO", "printf": "INFO", "println": "INFO",
    "warn": "WARNING", "warning": "WARNING",
    "error": "ERROR", "severe": "ERROR",
    "fatal": "FATAL", "critical": "FATAL", "panic": "FATAL", "dpanic": "FATAL",
    "fatalf": "FATAL", "fatalln": "FATAL", "panicf": "FATAL", "panicln": "FATAL",
    "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG",
}

METRIC_WORD_RE = re.compile(r"counter|gauge|histogram|summary|metric|stat|timer|latency|request", re.I)
PRINTF_RE = re.compile(r"%(?:\((?P<name>[^)]+)\))?[-+#0 ]*\d*(?:\.\d+)?[hlL]?(?P<conv>[sdifgxXeEorv%])")
BRACE_RE = re.compile(r"\{([^{}]*)\}")
TEMPLATE_RE = re.compile(r"\$\{([^}]*)\}")


def load_config(path=None) -> dict:
    with open(path or CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def language_for_ext(ext: str):
    return EXT2LANG.get(ext.lower())


def is_test_path(rel_path: str, lang: str, cfg: dict) -> bool:
    """单测文件名排除：Go 的 _test.go 与生产代码同目录，按目录排除不掉。"""
    rel = rel_path.replace("\\", "/")
    pat = cfg.get(lang, {}).get("test_path_re")
    if pat and re.search(pat, rel, re.I):
        return True
    low = rel.lower()
    return any(seg in low for seg in ("/__tests__/", "/testdata/", "/vendor/", "/node_modules/"))


def _compile_rules(cfg: dict, lang: str, plane: str) -> list:
    out = []
    for r in cfg.get(lang, {}).get(plane, []):
        item = dict(r)
        pattern = item.get("pattern")
        item["_re"] = re.compile(pattern, re.I | re.M) if pattern else None
        if item.get("literal_pattern"):
            item["_lit_re"] = re.compile(item["literal_pattern"], re.I)
        hint = item.get("receiver_hint")
        item["_hint_re"] = re.compile(hint, re.I) if hint else None
        out.append(item)
    return out


class Literal:
    __slots__ = ("start", "end", "value", "quote")

    def __init__(self, start, end, value, quote):
        self.start, self.end, self.value, self.quote = start, end, value, quote

    def __repr__(self):
        return f"Literal({self.start},{self.value!r})"


class Call:
    __slots__ = ("open_idx", "close_idx", "args")

    def __init__(self, open_idx, close_idx, args):
        self.open_idx, self.close_idx, self.args = open_idx, close_idx, args
        # args: list of (text, start_offset, end_offset)


class Source:
    """掩码源 + 行号索引 + 近似作用域上下文。"""

    def __init__(self, text: str, lang: str):
        self.raw = text
        self.lang = lang
        self.masked, self.literals = self._mask(text)
        self.lit_starts = [l.start for l in self.literals]
        self.line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        self.raw_lines = text.splitlines()
        self.masked_lines = self.masked.splitlines()
        self.contexts = self._build_contexts()

    # ── 词法屏蔽 ──────────────────────────────────────────────
    def _mask(self, text: str):
        out = list(text)
        literals = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                for k in range(i, j):
                    if text[k] != "\n":
                        out[k] = " "
                i = j
                continue
            # 字符串 / 模板串 / 字符字面量：内容屏蔽为等长占位符，保留引号与换行
            if c in ('"', "'", "`"):
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == c:
                        break
                    j += 1
                j = min(j, n - 1)
                literals.append(Literal(i, j + 1, text[i + 1:j], c))
                for k in range(i + 1, j):
                    out[k] = "\n" if text[k] == "\n" else "~"
                i = j + 1
                continue
            i += 1
        return "".join(out), literals

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def literals_in(self, start: int, end: int) -> list:
        lo = bisect.bisect_left(self.lit_starts, start)
        out = []
        for k in range(lo, len(self.literals)):
            lit = self.literals[k]
            if lit.start >= end:
                break
            if lit.start >= start and lit.end <= end + 1:
                out.append(lit)
        return out

    # ── 作用域（近似，用于 in_loop / in_except / conditional / function / class） ──
    def _build_contexts(self):
        lines = self.masked_lines
        stack = []   # (depth_at_open, kind, name)
        depth = 0
        contexts = []
        if self.lang == "java":
            cls_re = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$]\w*)")
            fn_re = re.compile(r"(?:^|[\w\s><\[\],.?]+?)\b([A-Za-z_$]\w*)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\{?\s*$")
        elif self.lang == "go":
            cls_re = None
            fn_re = re.compile(r"\bfunc\s+(?:\(([^)]*)\)\s*)?([A-Za-z_]\w*)\s*\(")
        else:
            cls_re = re.compile(r"\bclass\s+([A-Za-z_$]\w*)")
            fn_re = re.compile(r"(?:\b(?:async\s+)?function\s+([A-Za-z_$]\w*)|(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$]\w*)\s*=>|^\s*(?:async\s+)?([A-Za-z_$]\w*)\s*\([^;{}]*\)\s*(?::[^{]+)?\{?\s*$)")
        ctrl_re = re.compile(r"\b(if|for|while|switch|catch|else\s+if)\b", re.I)

        for idx, line in enumerate(lines, 1):
            func = cls = ""
            loop = exc = False
            cond = ""
            for d, kind, name in stack:
                if kind == "func" and name:
                    func = name
                elif kind == "class" and name:
                    cls = name
                elif kind == "loop":
                    loop = True
                elif kind == "except":
                    exc = True
                elif kind == "cond" and name:
                    cond = name
            contexts.append({
                "function": f"{cls}.{func}" if cls and func else (func or ""),
                "class": cls, "in_loop": loop, "in_except": exc, "conditional": cond[:160],
            })

            pending = []
            cm = cls_re.search(line) if cls_re else None
            if cm:
                pending.append(("class", cm.group(1)))
            fm = fn_re.search(line)
            # 赋值语句（`Counter c = Counter.builder()`）不是方法声明
            has_assign = re.search(r"(?<![=!<>])=(?!=)", line) is not None
            if fm and (self.lang == "go" or not has_assign):
                if self.lang == "go":
                    recv = fm.group(1) or ""
                    rm = re.search(r"\*?([A-Za-z_]\w*)", recv)
                    if rm:
                        pending.append(("class", rm.group(1)))
                    pending.append(("func", fm.group(2)))
                elif self.lang == "java":
                    name = fm.group(1)
                    # 排除控制流关键字被误认成方法定义
                    if name not in ("if", "for", "while", "switch", "catch", "return", "new"):
                        pending.append(("func", name))
                else:
                    name = next((g for g in fm.groups() if g), "")
                    if name and name not in ("if", "for", "while", "switch", "catch"):
                        pending.append(("func", name))
            controls = ctrl_re.findall(line)
            for kw in controls:
                kw = kw.strip().lower()
                if kw in ("for", "while", "switch"):
                    pending.append(("loop", ""))
                elif kw == "catch":
                    pending.append(("except", ""))
                else:
                    pm = re.search(r"\bif\s*\(([^)]*)\)", line)
                    pending.append(("cond", pm.group(1).strip() if pm else kw))

            # 逐字符更新大括号栈；同一行的多个声明按 pending 顺序挂到前几个 '{'
            p = 0
            for ch in line:
                if ch == "{":
                    depth += 1
                    kind, name = pending.pop(0) if pending else ("block", "")
                    stack.append((depth, kind, name))
                elif ch == "}":
                    if depth > 0:
                        stack = [s for s in stack if s[0] < depth]
                        depth -= 1
                p += 1
        return contexts

    def ctx_at(self, line_no: int) -> dict:
        if 1 <= line_no <= len(self.contexts):
            return self.contexts[line_no - 1]
        return {"function": "", "class": "", "in_loop": False, "in_except": False, "conditional": ""}

    # ── 调用参数 ──────────────────────────────────────────────
    def call_at(self, m) -> Call | None:
        """从正则命中处找到最近的 '('，按括号平衡切出顶层参数。"""
        open_idx = self.masked.rfind("(", m.start(), m.end())
        if open_idx < 0:
            open_idx = self.masked.find("(", m.end() - 1, m.end() + 8)
        if open_idx < 0:
            return None
        depth, seg, args, i = 0, open_idx + 1, [], open_idx
        n = len(self.masked)
        while i < n and i - open_idx < 6000:
            ch = self.masked[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth <= 0:
                    args.append((self.masked[seg:i], seg, i))
                    return Call(open_idx, i, args)
            elif ch == "," and depth == 1:
                args.append((self.masked[seg:i], seg, i))
                seg = i + 1
            i += 1
        return Call(open_idx, i, args)

    def snippet(self, line_no: int, limit: int = 240) -> str:
        if not (1 <= line_no <= len(self.raw_lines)):
            return ""
        out = self.raw_lines[line_no - 1].strip()
        for nxt in range(line_no, min(line_no + 2, len(self.raw_lines))):
            if out and self.raw_lines[nxt].strip().startswith((".", ")", "};", "]")):
                out += " " + self.raw_lines[nxt].strip()
        return out[:limit]


# ── 值解析 ────────────────────────────────────────────────────

def _literals_of_arg(src: Source, arg) -> list:
    if not arg:
        return []
    return src.literals_in(arg[1], arg[2])


def _first_string(src: Source, call: Call, idx: int) -> tuple:
    if not call or idx >= len(call.args):
        return None, None
    lits = _literals_of_arg(src, call.args[idx])
    return (lits[0], call.args[idx]) if lits else (None, call.args[idx])


def _key_string(src: Source, call: Call, key: str) -> tuple:
    """在参数里找 `Name: "x"` / `name: 'x'`：返回 (字面量, 表达式文本)。"""
    if not call:
        return None, ""
    rx = re.compile(r"\b" + re.escape(key) + r"\s*[:=]\s*", re.I)
    for arg in call.args:
        text = arg[0]
        m = rx.search(text)
        if not m:
            continue
        abs_pos = arg[1] + m.end()
        lits = src.literals_in(abs_pos, arg[2])
        if lits and lits[0].value:
            return lits[0], text[m.end():m.end() + 80].strip()
        # 常量/变量：交给调用方标动态
        expr = text[m.end():].split(",")[0].split("}")[0].strip()
        return None, expr
    return None, ""


def _chain_string(src: Source, call: Call, method: str) -> tuple:
    """链式调用取字面量：Counter.builder().name("x") / .setDescription("...")。"""
    if not call:
        return None, ""
    tail = src.masked[call.close_idx:call.close_idx + 600]
    m = re.search(r"\." + re.escape(method) + r"\s*\(", tail, re.I)
    if not m:
        return None, ""
    abs_open = call.close_idx + m.end() - 1
    # 直接复用括号平衡逻辑：构造一个假的 match 对象
    class _M:
        def __init__(self, s, e):
            self._s, self._e = s, e
        def start(self):
            return self._s
        def end(self):
            return self._e
    sub = src.call_at(_M(abs_open, abs_open + 1))
    if not sub:
        return None, ""
    lit, _ = _first_string(src, sub, 0)
    return lit, ""


def _value_from(src: Source, call: Call, spec: str):
    """返回 (值, 字面量或 None, 辅助文本)。spec 可用 | 分隔多个候选。"""
    if not spec:
        return "", None, ""
    for candidate in spec.split("|"):
        candidate = candidate.strip()
        if candidate == "none":
            return "", None, ""
        if candidate.startswith("arg"):
            try:
                idx = int(candidate[3:])
            except ValueError:
                continue
            lit, _ = _first_string(src, call, idx)
            if lit:
                return lit.value, lit, ""
        elif candidate == "last":
            if call:
                for arg in reversed(call.args):
                    lits = _literals_of_arg(src, arg)
                    if lits:
                        return lits[-1].value, lits[-1], ""
        elif candidate == "message":
            if not call or not call.args:
                return "", None, ""
            first_text = call.args[0][0].strip()
            structured = first_text.startswith("{") or first_text.startswith("[")
            if structured:
                for arg in reversed(call.args):
                    lits = _literals_of_arg(src, arg)
                    if lits:
                        return lits[-1].value, lits[-1], "structured"
                return "", None, "structured"
            lits = _literals_of_arg(src, call.args[0])
            if lits:
                return lits[0].value, lits[0], ""
        elif candidate == "all_args":
            if call:
                vals = []
                for arg in call.args:
                    lits = _literals_of_arg(src, arg)
                    if lits and lits[0].value:
                        vals.append(lits[0].value)
                if vals:
                    return vals[0], None, "\x1f".join(vals)
        elif candidate.startswith("key:"):
            lit, expr = _key_string(src, call, candidate[4:])
            if lit:
                return lit.value, lit, ""
            if expr:
                return "", None, expr
        elif candidate.startswith("chain:"):
            lit, _ = _chain_string(src, call, candidate[6:])
            if lit:
                return lit.value, lit, ""
    return "", None, ""


def infer_metric_type(ctor: str) -> str:
    c = (ctor or "").lower()
    if "histogram" in c or "distribution" in c:
        return "histogram"
    if "summary" in c:
        return "summary"
    if "timer" in c:
        return "timer"
    if "gauge" in c:
        return "gauge"
    if "counter" in c:
        return "counter"
    return "unknown"


def _norm_level(rule: dict, m) -> str:
    spec = rule.get("level", "fixed:INFO")
    raw = ""
    if spec.startswith("group:"):
        try:
            raw = m.group(spec[6:]) or ""
        except (IndexError, KeyError):
            raw = ""
    elif spec.startswith("fixed:"):
        raw = spec[6:]
    raw = raw.strip().lower()
    return LEVEL_MAP.get(raw, "INFO")


def parse_template(lit: Literal, arg) -> tuple:
    """返回 (template, placeholders, style)。"""
    if lit is None:
        return "", [], "none"
    text = lit.value
    style, placeholders = "none", []
    if lit.quote == "`":
        placeholders = [p.strip() for p in TEMPLATE_RE.findall(text)]
        text = TEMPLATE_RE.sub("{}", text)
        style = "template" if placeholders else "none"
    else:
        braces = [b.strip() for b in BRACE_RE.findall(text)]
        printf = [mm.group("name") or mm.group("conv") for mm in PRINTF_RE.finditer(text)
                  if mm.group("conv") != "%"]
        if braces:
            style, placeholders = "brace", braces
        elif printf:
            style, placeholders = "printf", printf
    # "msg " + var / `msg ${var}`：拼接变量补一个 {}，避免把变量当固定关键字
    if arg and lit.quote != "`":
        tail = arg[0][max(0, lit.end - arg[1]):]
        plus = re.search(r"\+\s*([A-Za-z_$][\w$.]*)\s*(?:\(|\)|,|;|$)", tail)
        if plus and not re.search(r"^\s*['\"]~", tail):
            text = text.rstrip() + " {}"
            placeholders.append(plus.group(1))
            if style == "none":
                style = "brace"
    return text, placeholders, style


# ── 主提取 ────────────────────────────────────────────────────

def _module_of(rel_path: str, lang_cfg: dict) -> str:
    rel = rel_path.replace("\\", "/")
    for prefix in lang_cfg.get("module_strip", []):
        i = rel.find(prefix)
        if i >= 0:
            rel = rel[i + len(prefix):]
            break
    rel = re.sub(r"\.(java|kt|go|js|jsx|mjs|cjs|ts|tsx)$", "", rel)
    return rel.replace("/", ".")


def _assigned_var(src: Source, offset: int) -> str:
    prefix = src.masked[max(0, offset - 180):offset]
    m = re.search(r"([A-Za-z_$][\w$]*)\s*(?::=|=|:)\s*$", prefix)
    if m:
        return m.group(1)
    return ""


def _receiver_var(src: Source, offset: int, known: set) -> str:
    prefix = src.masked[max(0, offset - 160):offset]
    best, best_pos = "", -1
    for name in known:
        pos = prefix.rfind(name)
        if pos > best_pos and re.search(r"\b" + re.escape(name) + r"\b", prefix[pos:]):
            best, best_pos = name, pos
    if best:
        return best
    m = re.search(r"([A-Za-z_$][\w$]*)\s*\.\s*(?:WithLabelValues\s*\([^)]*\)|labels\s*\([^)]*\)\s*\.\s*)?$", prefix)
    return m.group(1) if m else ""


def extract_file(plane: str, filepath: str, rel_path: str, lang: str, cfg: dict,
                 include_print: bool = False, var_index: dict | None = None) -> list:
    """提取单文件某一平面的信号。var_index 由调用方跨文件共享，用于写入点溯源。"""
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    src = Source(text, lang)
    lang_cfg = cfg[lang]
    rel = rel_path.replace("\\", "/")
    module = _module_of(rel, lang_cfg)
    parts = rel.split("/")
    dir_parts = parts[:-1] if len(parts) > 1 else parts
    package = "/".join(dir_parts[:2]) if dir_parts else (parts[0] if parts else "")
    var_index = var_index if var_index is not None else {}
    local_vars = {}
    records = []

    def base(line: int, offset: int) -> dict:
        ctx = src.ctx_at(line)
        return {
            "file": rel, "line": line, "module": module, "package": package,
            "class": ctx["class"], "function": ctx["function"],
            "code_snippet": src.snippet(line), "conditional": ctx["conditional"],
            "language": lang,
            "_offset": offset,
        }

    rule_key = {"loggers": "loggers", "metrics": "metrics", "traces": "spans"}[plane]
    rules = _compile_rules(cfg, lang, rule_key)
    updates = _compile_rules(cfg, lang, "updates") if plane == "metrics" else []
    exposures = _compile_rules(cfg, lang, "exposures") if plane == "metrics" else []
    claimed = set()

    for rule in rules:
        rx = rule.get("_re")
        if rx is None:
            continue
        for m in rx.finditer(src.masked):
            off = m.start()
            if off in claimed:
                continue
            hint = rule.get("_hint_re")
            if hint:
                try:
                    recv = m.group("recv") or ""
                except (IndexError, KeyError):
                    recv = ""
                if not hint.search(recv):
                    continue
            line = src.line_of(off)
            rec = base(line, off)
            call = src.call_at(m) if rule.get("name_from") or rule.get("doc_from") else None

            # ── 日志 ──
            if plane == "loggers":
                if rule.get("print") and not include_print:
                    continue
                val, lit, aux = _value_from(src, call, rule.get("name_from", "arg0"))
                arg = None
                if lit and call:
                    for a in call.args:
                        if a[1] <= lit.start < a[2]:
                            arg = a
                            break
                template, placeholders, style = parse_template(lit, arg)
                level = _norm_level(rule, m)
                rec.update({
                    "level": level,
                    "logger_type": rule["name"],
                    "template": template,
                    "placeholders": placeholders,
                    "placeholder_style": style,
                    "args_count": max(0, len(call.args) - 1) if call else 0,
                    "in_loop": src.ctx_at(line)["in_loop"],
                    "in_except": src.ctx_at(line)["in_except"],
                    "once": False,
                    "is_assertion": bool(rule.get("assertion")),
                    "structured": aux == "structured",
                })
                claimed.add(off)

            # ── 指标 ──
            elif plane == "metrics":
                if rule.get("require_key") and not _key_string_has_lit(src, call, rule["require_key"]):
                    # 没有 name 字段的 new Counter() 多半是业务类，不是 prom-client 指标
                    continue
                ctor = ""
                try:
                    ctor = m.group("ctor") or ""
                except (IndexError, KeyError):
                    ctor = ""
                mtype = rule.get("metric_type", "auto")
                if mtype == "auto":
                    mtype = infer_metric_type(ctor)
                name, lit, aux = _value_from(src, call, rule.get("name_from", "arg0"))
                if not name and rule.get("chain_name"):
                    chain_lit, _ = _chain_string(src, call, rule["chain_name"])
                    if chain_lit:
                        name, lit = chain_lit.value, chain_lit
                if not name and rule.get("kind") == "definition":
                    # 名字由参数/常量构造 → wrapper，不能断言「没有指标」
                    expr = aux or (call.args[0][0].strip()[:80] if call and call.args else "")
                    rec.update({
                        "kind": "wrapper", "name": "", "metric_type": mtype,
                        "constructor": f"{rule['name']}:{ctor or rule['name']}",
                        "name_source": expr, "labelnames": [], "buckets": "",
                        "documentation": "", "name_dynamic": True,
                    })
                    claimed.add(off)
                    records.append(rec)
                    continue
                doc, _, _ = _value_from(src, call, rule.get("doc_from", ""))
                labels = []
                lspec = rule.get("labels_from", "")
                if lspec.startswith("arg"):
                    try:
                        idx = int(lspec[3:])
                        if call and idx < len(call.args):
                            labels = [l.value for l in _literals_of_arg(src, call.args[idx])]
                    except ValueError:
                        pass
                elif lspec.startswith("key:"):
                    key = lspec[4:]
                    if call:
                        for arg in call.args:
                            km = re.search(r"\b" + re.escape(key) + r"\s*[:=]\s*", arg[0], re.I)
                            if km:
                                labels = [l.value for l in src.literals_in(arg[1] + km.end(), arg[2])]
                                break
                var = _assigned_var(src, off)
                rec.update({
                    "kind": rule.get("kind", "definition"),
                    "name": name.strip(),
                    "metric_type": mtype,
                    "documentation": doc,
                    "labelnames": labels,
                    "buckets": "",
                    "unit": "",
                    "constructor": f"{rule['name']}:{ctor or rule['name']}",
                    "metric_var": var,
                    "annotation": bool(rule.get("annotation")),
                })
                pspec = rule.get("prefix_from", "")
                if pspec.startswith("key:"):
                    key = pspec[4:]
                    plit, pexpr = _key_string(src, call, key)
                    if plit and plit.value:
                        rec["name"] = f"{plit.value}_{rec['name']}"
                        rec["namespace"] = plit.value
                    elif pexpr:
                        rec["namespace_expr"] = pexpr[:80]
                        rec["name_incomplete"] = True
                if var and name:
                    local_vars[var] = rec["name"]
                    var_index.setdefault(var, rec["name"])
                claimed.add(off)

            # ── 链路 ──
            else:
                kind = rule.get("kind", "span")
                all_vals = []
                name, lit, aux = _value_from(src, call, rule.get("name_from", "arg0"))
                if rule.get("all_args") and aux:
                    all_vals = [v for v in aux.split("\x1f") if v]
                if kind == "span" and not name:
                    continue
                rec.update({
                    "kind": kind,
                    "name": name,
                    "span_name": name,
                    "span_kind": "internal",
                    "style": rule.get("style", f"call:{rule['name']}") if kind == "span" else kind,
                    "attributes": all_vals if kind == "attribute" else [],
                    "span_name_dynamic": lit is None,
                })
                if kind == "attribute":
                    rec["attributes"] = all_vals or ([name] if name else [])
                claimed.add(off)

            records.append(rec)

    # ── 指标写入点 ──
    if plane == "metrics":
        known = set(local_vars) | set(var_index)
        for rule in updates:
            for m in rule["_re"].finditer(src.masked):
                off = m.start()
                if off in claimed:
                    continue
                name = _receiver_var(src, off, known)
                if not name or not (name in known or METRIC_WORD_RE.search(name)):
                    continue
                line = src.line_of(off)
                rec = base(line, off)
                call = src.call_at(m)
                try:
                    method = m.group("method")
                except (IndexError, KeyError):
                    method = ""
                rec.update({
                    "kind": "update",
                    "metric_var": name,
                    "metric_name": local_vars.get(name) or var_index.get(name, ""),
                    "method": method,
                    "name": local_vars.get(name) or var_index.get(name, ""),
                    "metric_type": "",
                    "value_expr": (call.args[0][0].strip()[:200] if call and call.args else ""),
                })
                records.append(rec)
                claimed.add(off)

        # ── 暴露点（代码调用 + 路径字面量） ──
        for rule in exposures:
            rx = rule.get("_re")
            if rx:
                need_lit = rule.get("require_literal")
                lit_re = re.compile(need_lit, re.I) if need_lit else None
                for m in rx.finditer(src.masked):
                    if lit_re:
                        call = src.call_at(m)
                        vals = [l.value for a in (call.args if call else []) for l in _literals_of_arg(src, a)]
                        if not any(lit_re.match(v.strip()) for v in vals):
                            continue
                    line = src.line_of(m.start())
                    rec = base(line, m.start())
                    rec.update({
                        "kind": "exposure", "name": rule["name"],
                        "exposure_type": rule.get("exposure_type", ""),
                        "metric_type": "",
                    })
                    records.append(rec)
            lit_re = rule.get("_lit_re")
            if lit_re:
                for lit in src.literals:
                    if lit_re.match(lit.value.strip()):
                        line = src.line_of(lit.start)
                        rec = base(line, lit.start)
                        rec.update({
                            "kind": "exposure", "name": lit.value.strip(),
                            "exposure_type": rule.get("exposure_type", ""),
                            "metric_type": "", "evidence": "literal",
                        })
                        records.append(rec)

    for r in records:
        r.pop("_offset", None)
    return records


def _key_string_has_lit(src: Source, call: Call, key: str) -> bool:
    lit, _ = _key_string(src, call, key)
    return lit is not None
