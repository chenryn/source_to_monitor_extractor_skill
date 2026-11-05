#!/usr/bin/env python3
"""
源码日志埋点提取工具（v2）
从 Python / C++ 源码中批量提取所有 logger 调用，输出结构化 JSON。

v2 相对 v1 的修复（均已用 vLLM main 实测验证）：
  1. 支持 vLLM 的 *_once 日志方法（info_once / warning_once / error_once / debug_once）
  2. 支持 printf 风格占位符（%s / %d / %.2fs / %(name)s）——vLLM 约 68% 的模板是这种
  3. 修复 async def 内日志丢失函数名（vLLM 请求路径大量为 async）
  4. module 改为相对源码根的 dotted path，修复「按模块分布」统计为空
  5. 新增 in_loop / in_except / conditional 三个上下文证据字段
  6. 默认排除 tests/docs/examples/benchmarks，避免非生产埋点稀释统计
  7. C++ 级别映射修复（v1 把所有 C++ 日志都标成 ERROR）

用法:
    python extract_loggers.py <源码目录> [--lang auto] [--output loggers.json]
                              [--include-all] [--include-print] [--exclude GLOB ...]

输出 JSON 结构:
[
  {
    "file": "vllm/v1/core/scheduler.py",   # 相对源码根的路径
    "line": 420,
    "level": "WARNING",
    "logger_type": "ast_logger_call",
    "code_snippet": "logger.warning_once(\"No available memory for the cache blocks\")",
    "template": "No available memory for the cache blocks",
    "placeholders": [],
    "placeholder_style": "none",           # none / brace / printf / fstring
    "args_count": 0,
    "function": "Scheduler.schedule",
    "class": "Scheduler",
    "module": "vllm.v1.core.scheduler",    # dotted，相对源码根
    "package": "vllm/v1",                  # 一级/二级目录，用于汇总
    "in_loop": false,
    "in_except": false,
    "once": true,                          # *_once 方法，只在启动/首次触发时打印
    "conditional": ""                      # 若在 if 分支内，记录条件表达式
  }
]
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
except ImportError:  # 独立拷贝脚本时降级为仅 Python/C++/Rust
    _LANG_BACKEND = False

SCRIPT_DIR = Path(__file__).parent
PATTERNS_FILE = SCRIPT_DIR / "logger_patterns.json"

# 基础日志方法 → 标准级别
LEVEL_METHODS = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "warn": "WARNING",
    "error": "ERROR",
    "critical": "FATAL",
    "fatal": "FATAL",
    "exception": "ERROR",
    "msg": "INFO",  # structlog
}

# vLLM / SGLang 等项目的 *_once 变体：只打印一次，属于「存在性信号」
ONCE_SUFFIX = "_once"

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
    "third_party", "thirdparty", ".mypy_cache", ".pytest_cache",
    # 非生产路径，默认排除（用 --include-all 关闭）
    "tests", "test", "docs", "examples", "benchmarks",
}

# 判定「这是不是 logger 对象」的标识
LOGGER_HINTS = ("logger", "log", "logging", "LOGGER", "LOG")

# printf 风格占位符
PRINTF_RE = re.compile(r"%(?:\((?P<name>[^)]+)\))?[-+#0 ]*\d*(?:\.\d+)?[hlL]?(?P<conv>[sdifgxXeEor%])")
BRACE_RE = re.compile(r"\{([^{}]*)\}")


# ─── 占位符解析 ────────────────────────────────────────────────

def parse_placeholders(template: str) -> tuple[list[str], str]:
    """返回 (占位符列表, 风格)。风格: none / brace / printf / fstring"""
    if not template:
        return [], "none"

    printf = [
        m.group("name") or m.group("conv")
        for m in PRINTF_RE.finditer(template)
        if m.group("conv") != "%"
    ]
    brace = [b.strip() for b in BRACE_RE.findall(template)]

    if brace:
        return brace, "brace"
    if printf:
        return printf, "printf"
    return [], "none"


# ─── Python AST 精确解析 ───────────────────────────────────────

class PythonLoggerExtractor(ast.NodeVisitor):
    """用 Python AST 精确提取 logger 调用，并保留触发上下文证据。"""

    def __init__(self, filepath: str, source_lines: list, rel_path: str, src_root: str):
        self.filepath = filepath
        self.source_lines = source_lines
        self.rel_path = rel_path
        self.results = []
        self.func_stack = []
        self.class_stack = []
        self.loop_depth = 0
        self.except_depth = 0
        self.cond_stack = []  # if 分支的条件表达式
        self.logger_vars = set()
        self.str_constants = {}  # 模块级字符串常量，用于解析 logger.xxx(_CONST) 形式

    # —— 作用域追踪 ——
    def _visit_func(self, node):
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        # v1 缺失这个分支，导致 async 函数内日志全部丢失函数名
        self._visit_func(node)

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_For(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_AsyncFor(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_While(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_ExceptHandler(self, node):
        self.except_depth += 1
        self.generic_visit(node)
        self.except_depth -= 1

    def visit_If(self, node):
        try:
            cond = ast.unparse(node.test)
        except Exception:
            cond = ""
        self.cond_stack.append(cond[:160])
        self.generic_visit(node)
        self.cond_stack.pop()

    # —— 核心：识别日志调用 ——
    def _resolve_level(self, method: str, node) -> tuple[str, bool]:
        """返回 (级别, 是否 once)"""
        once = method.endswith(ONCE_SUFFIX)
        base = method[: -len(ONCE_SUFFIX)] if once else method
        if base in LEVEL_METHODS:
            return LEVEL_METHODS[base], once
        return "", once

    def _is_logger_receiver(self, receiver: str) -> bool:
        low = receiver.lower()
        if any(h in low for h in ("logger", "logging")):
            return True
        # log / _log / LOG / LOGGER 等短名
        last = receiver.split(".")[-1]
        return last in self.logger_vars or last.lower() in ("log", "_log", "log_")

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            try:
                receiver = ast.unparse(node.func.value)
            except Exception:
                receiver = ""

            level, once = self._resolve_level(method, node)

            # 兼容 logger.log(logging.INFO, "msg")
            if method == "log" and node.args and self._is_logger_receiver(receiver):
                lvl_arg = node.args[0]
                if isinstance(lvl_arg, ast.Attribute) and lvl_arg.attr.isupper():
                    level = lvl_arg.attr
                elif isinstance(lvl_arg, ast.Constant) and isinstance(lvl_arg.value, int):
                    level = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "FATAL"}.get(
                        lvl_arg.value, "INFO"
                    )
                msg_index = 1
            else:
                msg_index = 0

            if level and self._is_logger_receiver(receiver):
                template, style, args_count = self._extract_template(node, msg_index)
                line_no = node.lineno
                line_text = (
                    self.source_lines[line_no - 1].strip()
                    if 0 < line_no <= len(self.source_lines)
                    else ""
                )
                placeholders, detected_style = parse_placeholders(template)
                if style == "fstring":
                    detected_style = "fstring"

                self.results.append(self._build_record(
                    line_no=line_no,
                    level=level,
                    logger_type="ast_logger_call",
                    code_snippet=line_text,
                    template=template,
                    placeholders=placeholders,
                    placeholder_style=detected_style,
                    args_count=args_count,
                    once=once,
                ))

        self.generic_visit(node)

    def _extract_template(self, node, msg_index: int):
        """提取模板字符串，返回 (template, style, args_count)"""
        args = node.args[msg_index:]
        if not args:
            return "", "none", 0
        first = args[0]
        extra = len(args) - 1

        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value, "none", extra

        if isinstance(first, ast.JoinedStr):
            parts, n = [], 0
            for val in first.values:
                if isinstance(val, ast.Constant):
                    parts.append(str(val.value))
                elif isinstance(val, ast.FormattedValue):
                    parts.append("{}")
                    n += 1
            return "".join(parts), "fstring", n

        # logger.xxx(_SOME_CONST)：回填模块级字符串常量
        if isinstance(first, ast.Name) and first.id in self.str_constants:
            return self.str_constants[first.id], "none", extra

        # "..." .format(...)：取被 format 的字面量
        if (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Attribute)
            and first.func.attr == "format"
            and isinstance(first.func.value, ast.Constant)
            and isinstance(first.func.value.value, str)
        ):
            return first.func.value.value, "brace", extra

        # 变量拼接 / 其他动态构造，无法静态求值
        return "", "none", extra

    def _build_record(self, **kw) -> dict:
        rel = self.rel_path.replace(os.sep, "/")
        module = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
        parts = rel.split("/")
        package = "/".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "")

        return {
            "file": rel,
            "line": kw["line_no"],
            "level": kw["level"],
            "logger_type": kw["logger_type"],
            "code_snippet": kw["code_snippet"],
            "template": kw["template"],
            "placeholders": kw["placeholders"],
            "placeholder_style": kw["placeholder_style"],
            "args_count": kw["args_count"],
            "function": ".".join(self.class_stack + self.func_stack[-1:]) if self.func_stack else "",
            "class": self.class_stack[-1] if self.class_stack else "",
            "module": module,
            "package": package,
            "in_loop": self.loop_depth > 0,
            "in_except": self.except_depth > 0,
            "once": kw.get("once", False),
            "is_assertion": False,
            "conditional": self.cond_stack[-1] if self.cond_stack else "",
        }


def collect_module_context(tree) -> tuple[dict, set]:
    """预扫描：收集模块级字符串常量与 logger 变量名，供模板回填使用。"""
    constants, logger_vars = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for t in targets:
                if not isinstance(t, ast.Name):
                    continue
                name = t.id
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    constants[name] = value.value
                # logger = init_logger(__name__) / LOGGER = logging.getLogger(...)
                if isinstance(value, ast.Call):
                    fn = value.func
                    fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    if fname in ("init_logger", "getLogger", "Logger", "get_logger"):
                        logger_vars.add(name)
    return constants, logger_vars


def extract_python(filepath: str, rel_path: str, src_root: str, patterns: dict) -> list:
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except OSError:
        return []

    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, ValueError):
        return extract_python_regex(filepath, lines, rel_path, patterns)

    constants, logger_vars = collect_module_context(tree)
    extractor = PythonLoggerExtractor(filepath, lines, rel_path, src_root)
    extractor.str_constants = constants
    extractor.logger_vars = logger_vars
    try:
        extractor.visit(tree)
    except RecursionError:
        return extract_python_regex(filepath, lines, rel_path, patterns)
    return extractor.results


def extract_python_regex(filepath: str, lines: list, rel_path: str, patterns: dict) -> list:
    """正则回退方案（AST 解析失败时）"""
    results = []
    py_patterns = patterns.get("python", {}).get("loggers", [])
    rel = rel_path.replace(os.sep, "/")
    module = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel
    parts = rel.split("/")
    package = "/".join(parts[:2]) if len(parts) > 1 else parts[0]

    for i, line in enumerate(lines, 1):
        for logger_def in py_patterns:
            m = re.search(logger_def["pattern"], line)
            if not m:
                continue
            level = (m.group(logger_def["level_group"]) or "INFO").upper() if logger_def.get("level_group") else "INFO"
            if level == "WARN":
                level = "WARNING"
            str_m = re.search(r'["\']([^"\']+)["\']', line)
            template = str_m.group(1) if str_m else ""
            placeholders, style = parse_placeholders(template)
            results.append({
                "file": rel,
                "line": i,
                "level": level,
                "logger_type": logger_def["name"],
                "code_snippet": line.strip(),
                "template": template,
                "placeholders": placeholders,
                "placeholder_style": style,
                "args_count": 0,
                "function": "",
                "class": "",
                "module": module,
                "package": package,
                "in_loop": False,
                "in_except": False,
                "once": "_once(" in line,
                "is_assertion": False,
                "conditional": "",
            })
            break
    return results


# ─── C++ 正则提取 ─────────────────────────────────────────────

CPP_LEVEL_MAP = {
    "INFO": "INFO", "WARNING": "WARNING", "WARN": "WARNING", "ERROR": "ERROR",
    "FATAL": "FATAL", "DEBUG": "DEBUG", "TRACE": "DEBUG", "CRITICAL": "FATAL",
}


def _join_cpp_statement(lines: list, start: int) -> str:
    """把跨行的日志语句拼成一行（括号未闭合时继续拼接）"""
    buf = lines[start].rstrip("\n")
    depth = buf.count("(") - buf.count(")")
    i = start
    while depth > 0 and i + 1 < len(lines) and i - start < 15:
        i += 1
        nxt = lines[i].rstrip("\n")
        buf += " " + nxt.strip()
        depth += nxt.count("(") - nxt.count(")")
    return buf


def extract_cpp(filepath: str, rel_path: str, patterns: dict) -> list:
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return []

    results = []
    cpp_patterns = patterns.get("cpp", {}).get("loggers", [])
    rel = rel_path.replace(os.sep, "/")
    module = re.sub(r"\.(cc|cpp|cxx|h|hpp)$", "", rel).replace("/", ".")
    parts = rel.split("/")
    package = "/".join(parts[:2]) if len(parts) > 1 else parts[0]

    current_function = ""
    in_macro_def = False  # 跳过 #define 多行宏体，否则宏定义会被当成日志调用

    for i, line in enumerate(lines):
        stripped = line.strip()

        # —— 宏定义过滤 ——
        if in_macro_def:
            in_macro_def = stripped.endswith("\\")
            continue
        if stripped.startswith("#define"):
            in_macro_def = stripped.endswith("\\")
            continue
        if not stripped or stripped.startswith(("//", "/*", "*", "#include", "#pragma")):
            continue

        # 函数定义追踪：只认形如 `ret name(` 或 `ns::name(` 且以 { 结尾/下一行 {
        func_match = re.search(
            r"(?:^|\s)(?:[\w:<>,\*&\s]+?\s+)?([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$",
            stripped,
        )
        if func_match and "LOG" not in stripped and "(" in stripped:
            cand = func_match.group(1)
            if cand not in ("if", "for", "while", "switch", "return", "catch"):
                current_function = cand.split("::")[-1]

        for logger_def in cpp_patterns:
            m = re.search(logger_def["pattern"], line)
            if not m:
                continue

            # 级别解析：优先命名组 level，其次固定值，最后按宏名推断
            level = ""
            if "level" in (m.groupdict() or {}):
                level = (m.group("level") or "").upper()
            if not level and logger_def.get("level_group") not in (None, "ERROR"):
                raw = m.group(logger_def["level_group"]) or ""
                level = raw.upper()
                if level.startswith("LOG_"):
                    level = level[4:]
            if not level:
                level = logger_def.get("fixed_level") or "INFO"
            level = CPP_LEVEL_MAP.get(level, "INFO")

            stmt = _join_cpp_statement(lines, i)
            strings = re.findall(r'"([^"]*)"', stmt)
            template = " ".join(s for s in strings if s)

            # cout/cerr 的字符串在 << 之后
            if logger_def["name"] in ("cout", "cerr") and not template:
                template = ""

            placeholders, style = parse_placeholders(template)

            results.append({
                "file": rel,
                "line": i + 1,
                "level": level,
                "logger_type": logger_def["name"],
                "code_snippet": stmt.strip(),
                "template": template,
                "placeholders": placeholders,
                "placeholder_style": style,
                "args_count": 0,
                "function": current_function,
                "class": "",
                "module": module,
                "package": package,
                "in_loop": False,
                "in_except": False,
                "once": False,
                "is_assertion": logger_def["name"] in ("glog_check", "prefixed_check"),
                "conditional": "",
            })
            break

    return results


# ─── Rust 正则提取 ────────────────────────────────────────────

RUST_LEVEL_MAP = {
    "INFO": "INFO", "WARN": "WARNING", "WARNING": "WARNING", "ERROR": "ERROR",
    "DEBUG": "DEBUG", "TRACE": "DEBUG", "FATAL": "FATAL",
}

RUST_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _join_rust_statement(lines: list, start: int) -> str:
    """Rust 宏调用常跨多行，括号未闭合时继续拼接"""
    buf = lines[start].rstrip("\n")
    depth = buf.count("(") - buf.count(")")
    i = start
    while depth > 0 and i + 1 < len(lines) and i - start < 20:
        i += 1
        nxt = lines[i].rstrip("\n")
        buf += " " + nxt.strip()
        depth += nxt.count("(") - nxt.count(")")
    return buf


def _rust_template(stmt: str) -> str:
    """取宏里的第一个「消息」字符串字面量。

    tracing 的结构化字段写法是 `field = "value"` / `target: "x"`，
    这些是元数据不是消息，要跳过。否则 error!(function = "Stage::execute", "真消息")
    会把 "Stage::execute" 当成模板。
    """
    for m in RUST_STRING_RE.finditer(stmt):
        head = stmt[: m.start()]
        if re.search(r"[A-Za-z_]\w*\s*[=:]\s*$", head):
            continue
        return m.group(1)
    return ""


def extract_rust(filepath: str, rel_path: str, patterns: dict) -> list:
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return []

    results = []
    rust_patterns = patterns.get("rust", {}).get("loggers", [])
    rel = rel_path.replace(os.sep, "/")
    module = re.sub(r"\.rs$", "", rel).replace("/", "::")
    parts = rel.split("/")
    package = "/".join(parts[:2]) if len(parts) > 1 else parts[0]

    current_function = ""
    in_block_comment = False
    test_block_depth = 0      # #[cfg(test)] mod tests { ... } 内部
    pending_test_mod = False  # 已见 #[cfg(test)]，等下一个 { 开始计数

    for i, line in enumerate(lines):
        stripped = line.strip()

        # —— 单元测试模块过滤 ——
        # Rust 惯例是把测试写在同文件 #[cfg(test)] mod tests 里，
        # 不像 Python 能按目录排除。不过滤的话断言会把 FATAL 占比顶到 50%+。
        if re.match(r"#\[cfg\(test\)\]", stripped):
            pending_test_mod = True
            continue
        if pending_test_mod:
            if "{" in stripped:
                test_block_depth = stripped.count("{") - stripped.count("}")
                pending_test_mod = False
            continue
        if test_block_depth > 0:
            test_block_depth += stripped.count("{") - stripped.count("}")
            continue

        # 块注释过滤（Rust 文档注释 /// 是行注释，单独处理）
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        if not stripped or stripped.startswith(("//", "#[")):
            # 属性宏 #[instrument] 单独统计，不当日志
            continue

        # 函数定义追踪：fn name( / async fn name( / pub fn name(
        fn_match = re.search(r"\b(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)", stripped)
        if fn_match:
            current_function = fn_match.group(1)

        for logger_def in rust_patterns:
            m = re.search(logger_def["pattern"], line)
            if not m:
                continue
            level = ""
            if "level" in (m.groupdict() or {}):
                level = (m.group("level") or "").upper()
            if not level:
                level = logger_def.get("fixed_level") or "INFO"
            level = RUST_LEVEL_MAP.get(level, "INFO")

            stmt = _join_rust_statement(lines, i)
            template = _rust_template(stmt)
            placeholders, style = parse_placeholders(template)

            results.append({
                "file": rel,
                "line": i + 1,
                "level": level,
                "logger_type": logger_def["name"],
                "code_snippet": stmt.strip(),
                "template": template,
                "placeholders": placeholders,
                "placeholder_style": style,
                "args_count": 0,
                "function": current_function,
                "class": "",
                "module": module,
                "package": package,
                "in_loop": False,
                "in_except": False,
                "once": False,
                "is_assertion": logger_def["name"] == "rust_assert",
                "conditional": "",
            })
            break

    return results


# ─── 主流程 ────────────────────────────────────────────────────

def load_patterns() -> dict:
    with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


LAYOUT_PREFIXES = ("python", "src", "lib", "source")


def _strip_layout(parts: list) -> list:
    """剥掉 src-layout 前缀：python/sglang/srt/x.py → sglang/srt/x.py"""
    if len(parts) > 2 and parts[0] in LAYOUT_PREFIXES:
        return parts[1:]
    return parts


def reassign_packages(records: list) -> int:
    """按分布自适应决定包粒度。

    固定取前两段在 src-layout 仓库上会把 95% 的埋点塞进同一个桶
    （SGLang 的 python/sglang）。这里从 2 级起，若最大桶占比 > 60%
    就加深一级，最多到 4 级，保证「按包分布」这张表有信息量。
    """
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


def scan_directory(src_dir: str, lang: str, exclude_globs: list, include_all: bool,
                   include_print: bool) -> tuple[list, int]:
    patterns = load_patterns()
    lang_cfg = load_config() if _LANG_BACKEND else {}
    all_results = []
    scanned_files = 0

    src_path = Path(src_dir).resolve()
    if not src_path.exists():
        print(f"错误: 目录不存在 - {src_dir}", file=sys.stderr)
        sys.exit(1)

    for root, dirs, files in os.walk(src_path):
        dirs[:] = [
            d for d in dirs
            if d not in DEFAULT_EXCLUDE_DIRS or include_all
        ]
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]

        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, src_path)

            if any(fnmatch.fnmatch(rel_path, g) for g in exclude_globs):
                continue

            ext = Path(fname).suffix.lower()
            results = []

            if lang in ("auto", "python") and ext == ".py":
                results = extract_python(fpath, rel_path, str(src_path), patterns)
            elif lang in ("auto", "cpp") and ext in (".cc", ".cpp", ".cxx", ".h", ".hpp"):
                results = extract_cpp(fpath, rel_path, patterns)
            elif lang in ("auto", "rust") and ext == ".rs":
                results = extract_rust(fpath, rel_path, patterns)
            elif _LANG_BACKEND:
                # Java/Go/JS(含 Kotlin/TS)：通用正则后端（lang_extract.py + signal_patterns.json）
                file_lang = language_for_ext(ext)
                if file_lang and lang in ("auto", file_lang) and not is_test_path(rel_path, file_lang, lang_cfg):
                    results = lang_extract_file(
                        "loggers", fpath, rel_path, file_lang, lang_cfg,
                        include_print=include_print,
                    )

            if results:
                scanned_files += 1
                all_results.extend(results)

    if not include_print:
        all_results = [r for r in all_results
                       if r["logger_type"] not in ("print", "cout", "cerr", "println", "eprintln",
                                                   "system_out", "system_err", "fmt_print",
                                                   "console", "process_stdout")]

    # 去重（同一文件同一行同一级别可能被多条正则命中）
    seen, deduped = set(), []
    for r in all_results:
        key = (r["file"], r["line"], r["level"], r["template"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    deduped.sort(key=lambda x: (x["file"], x["line"]))
    reassign_packages(deduped)
    return deduped, scanned_files


def print_summary(results: list, scanned_files: int):
    total = len(results)
    print(f"\n{'=' * 64}")
    print("日志埋点提取完成")
    print(f"{'=' * 64}")
    print(f"扫描文件数（含埋点）: {scanned_files}")
    _asserts = sum(1 for r in results if r.get("is_assertion"))
    if _asserts:
        print(f"埋点总数: {total}（其中断言 {_asserts}，实际日志调用 {total - _asserts}）")
    else:
        print(f"埋点总数: {total}")

    if not total:
        print(f"{'=' * 64}\n")
        return

    levels = _Counter(r["level"] for r in results)
    assertions = sum(1 for r in results if r.get("is_assertion"))
    print("\n按级别分布:")
    peak = max(levels.values())
    for lvl, c in sorted(levels.items(), key=lambda x: -x[1]):
        bar = "█" * max(1, int(c / peak * 20))
        note = ""
        if lvl == "FATAL" and assertions:
            note = f"  ← 其中断言 {assertions} 条"
        print(f"  {lvl:8s}: {c:5d} ({c / total:5.1%})  {bar}{note}")
    if assertions:
        print(f"  断言类合计 (is_assertion): {assertions}"
              f" → 失败即 abort/panic，不以日志落盘，不能当可检索关键字")

    langs = _Counter(r.get("language", "python") for r in results)
    if len(langs) > 1:
        print("\n按语言分布:")
        for l, c in langs.most_common():
            print(f"  {l:8s}: {c}")

    pkgs = _Counter(r.get("package", "") for r in results)
    print("\n按包分布 (Top 10):")
    for pkg, c in pkgs.most_common(10):
        print(f"  {(pkg or '<root>'):40s}: {c}")

    print("\n上下文证据:")
    loop = sum(1 for r in results if r.get("in_loop"))
    exc = sum(1 for r in results if r.get("in_except"))
    once = sum(1 for r in results if r.get("once"))
    cond = sum(1 for r in results if r.get("conditional"))
    printf = sum(1 for r in results if r.get("placeholder_style") == "printf")
    brace = sum(1 for r in results if r.get("placeholder_style") == "brace")
    templ = sum(1 for r in results if r.get("placeholder_style") == "template")
    print(f"  循环内 (in_loop)     : {loop} ({loop / total:.1%}) → 高频候选，禁止做频率告警")
    print(f"  异常分支 (in_except) : {exc} ({exc / total:.1%}) → error 类优先")
    print(f"  仅打印一次 (once)    : {once} → 存在性告警候选")
    print(f"  条件分支 (conditional): {cond} ({cond / total:.1%}) → 特性开关相关，可能未启用")
    print(f"  占位符风格           : printf={printf} brace={brace} template={templ} "
          f"none={total - printf - brace - templ}")

    print(f"\n{'=' * 64}\n")


def main():
    parser = argparse.ArgumentParser(description="源码日志埋点提取工具 v2")
    parser.add_argument("src_dir", help="源码根目录")
    parser.add_argument("--lang", choices=["auto", "python", "cpp", "rust", "java", "go", "js"],
                        default="auto")
    parser.add_argument("--output", "-o", default="loggers_extracted.json")
    parser.add_argument("--include-all", action="store_true",
                        help="不排除 tests/docs/examples/benchmarks")
    parser.add_argument("--include-print", action="store_true",
                        help="把 print / std::cout / std::cerr 也算作日志埋点")
    parser.add_argument("--exclude", action="append", default=[],
                        help="额外排除的 glob，可重复，如 --exclude '*/legacy/*'")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args()

    results, scanned = scan_directory(
        args.src_dir, args.lang, args.exclude, args.include_all, args.include_print
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    if not args.no_summary:
        print_summary(results, scanned)

    print(f"已写入: {args.output} ({len(results)} 条记录)")


if __name__ == "__main__":
    main()
