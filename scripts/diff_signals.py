#!/usr/bin/env python3
"""
可观测信号增量对比工具（日志 / 指标 / 链路通用）
项目升级后，用它对比两个版本的提取结果，只分析变化部分。

用法:
    python diff_signals.py old.json new.json [--kind auto|log|metric|trace] [-o diff.json]

匹配键（故意忽略行号，避免代码插入导致大面积误报）:
    日志: 文件名 + 日志模板
    指标: 指标名
    链路: 文件名 + span 名

输出三类:
    added     新增信号 —— 重点分析，新增的 ERROR/WARN 或新指标要补告警
    removed   删除信号 —— 检查对应告警规则是否该下线
    modified  字段变更 —— 级别变了要改告警等级，标签/桶变了要改查询口径
"""

import argparse
import json
import os
import sys

# 各类型需要重点比对的字段
WATCH_FIELDS = {
    "log": ("level", "template", "once", "is_assertion"),
    "metric": ("metric_type", "labelnames", "buckets", "documentation", "conditional"),
    "trace": ("span_name", "span_kind", "attributes"),
}


def detect_kind(records: list) -> str:
    if not records:
        return "unknown"
    sample = records[0]
    if "template" in sample:
        return "log"
    if "span_name" in sample:
        return "trace"
    if "metric_type" in sample or "name" in sample:
        return "metric"
    return "unknown"


def make_key(rec: dict, kind: str, fuzzy: bool = True) -> str:
    if kind == "log":
        tpl = rec.get("template") or rec.get("code_snippet", "")
        f = os.path.basename(rec.get("file", "")) if fuzzy else rec.get("file", "")
        return f"{f}::{tpl}"
    if kind == "metric":
        # 指标按名字匹配，同名不同类型的（极少）加类型区分
        return f"{rec.get('name', '')}::{rec.get('kind', 'definition')}"
    if kind == "trace":
        f = os.path.basename(rec.get("file", "")) if fuzzy else rec.get("file", "")
        return f"{f}::{rec.get('span_name', '')}"
    return json.dumps(rec, sort_keys=True)


def _norm(v):
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return v


def diff_records(old: list, new: list, kind: str, fuzzy: bool = True) -> dict:
    old_map, new_map = {}, {}
    for r in old:
        old_map.setdefault(make_key(r, kind, fuzzy), r)
    for r in new:
        new_map.setdefault(make_key(r, kind, fuzzy), r)

    old_keys, new_keys = set(old_map), set(new_map)
    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    common = old_keys & new_keys

    fields = WATCH_FIELDS.get(kind, ())
    modified = []
    for k in common:
        o, n = old_map[k], new_map[k]
        changes = {}
        for f in fields:
            if _norm(o.get(f)) != _norm(n.get(f)):
                changes[f] = {"old": o.get(f), "new": n.get(f)}
        if changes:
            modified.append({"key": k, "old": o, "new": n, "changes": changes})

    added = sorted((new_map[k] for k in added_keys), key=lambda x: (x.get("file", ""), x.get("line", 0)))
    removed = sorted((old_map[k] for k in removed_keys), key=lambda x: (x.get("file", ""), x.get("line", 0)))
    modified.sort(key=lambda x: (x["new"].get("file", ""), x["new"].get("line", 0)))

    return {
        "kind": kind,
        "summary": {
            "old_total": len(old),
            "new_total": len(new),
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
            "net_change": len(added) - len(removed),
        },
        "added": added,
        "removed": removed,
        "modified": modified,
    }


def print_summary(result: dict):
    s = result["summary"]
    kind = result["kind"]
    label = {"log": "日志埋点", "metric": "指标", "trace": "span"}.get(kind, "信号")

    print(f"\n{'=' * 64}")
    print(f"{label}增量对比结果")
    print(f"{'=' * 64}")
    print(f"旧版本: {s['old_total']}    新版本: {s['new_total']}    净变化: {s['net_change']:+d}")
    print(f"  新增: +{s['added_count']}")
    print(f"  删除: -{s['removed_count']}")
    print(f"  变更:  {s['modified_count']}")

    def level_breakdown(items, sign):
        from collections import Counter
        c = Counter(x.get("level") or x.get("metric_type") or x.get("span_kind", "") for x in items)
        for lvl, n in c.most_common():
            print(f"    {lvl or 'unknown':12s}: {sign}{n}")

    if result["added"]:
        print("\n新增（按级别/类型）:")
        level_breakdown(result["added"], "+")
        high = [x for x in result["added"] if x.get("level") in ("ERROR", "WARNING", "FATAL")]
        if high:
            print(f"  ⚠ 其中 ERROR/WARNING/FATAL {len(high)} 条，需要评估是否新增告警规则：")
            for x in high[:5]:
                print(f"      {x.get('file')}:{x.get('line')}  {x.get('template', '')[:60]}")

    if result["removed"]:
        print("\n删除（按级别/类型）:")
        level_breakdown(result["removed"], "-")
        print("  ⚠ 检查这些埋点对应的告警规则是否应下线，避免告警永不触发却仍挂着")

    if result["modified"]:
        print("\n变更明细（前 8 条）:")
        for m in result["modified"][:8]:
            for f, ch in m["changes"].items():
                print(f"  {m['new'].get('file')}:{m['new'].get('line')} [{f}] "
                      f"{str(ch['old'])[:40]} → {str(ch['new'])[:40]}")

    print(f"{'=' * 64}\n")


def main():
    parser = argparse.ArgumentParser(description="可观测信号增量对比工具")
    parser.add_argument("old_json")
    parser.add_argument("new_json")
    parser.add_argument("--kind", choices=["auto", "log", "metric", "trace"], default="auto")
    parser.add_argument("--output", "-o", default="diff_result.json")
    parser.add_argument("--exact", action="store_true", help="用完整路径匹配（默认按文件名）")
    parser.add_argument("--all-records", action="store_true",
                        help="指标对比时包含写入点/封装/数据源字段（默认只比定义）")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args()

    with open(args.old_json, encoding="utf-8") as f:
        old = json.load(f)
    with open(args.new_json, encoding="utf-8") as f:
        new = json.load(f)

    kind = args.kind if args.kind != "auto" else detect_kind(new or old)
    if kind == "unknown":
        print("错误: 无法识别输入类型，请用 --kind 指定", file=sys.stderr)
        sys.exit(1)

    # 指标文件里混有 definition / update / wrapper / stats_field / name_literal，
    # 默认只对比「指标定义」，避免写入点变动制造大量噪声
    if kind == "metric" and not args.all_records:
        before = (len(old), len(new))
        old = [r for r in old if r.get("kind") == "definition"]
        new = [r for r in new if r.get("kind") == "definition"]
        if before[0] != len(old) or before[1] != len(new):
            print(f"提示: 仅对比指标定义（{before[0]}→{len(old)} / {before[1]}→{len(new)} 条），"
                  f"用 --all-records 对比全部记录")

    result = diff_records(old, new, kind, fuzzy=not args.exact)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    if not args.no_summary:
        print_summary(result)
    print(f"已写入: {args.output}")


if __name__ == "__main__":
    main()
