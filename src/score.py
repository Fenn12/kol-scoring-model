#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
达人评估打分脚本

用法：
    python score.py --input data/sample_kols.csv
    python score.py --input data/sample_kols.csv --config model/scoring-config.json
    python score.py --input data/sample_kols.csv --out output/scored.csv

无第三方依赖。
"""

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CONFIG = os.path.join(ROOT, "model", "scoring-config.json")


# ---------- 工具函数 ----------

def pct_rank(value, sorted_values):
    """分位数归一化：把同批次内的值映射到 0-100。

    用分位数而非绝对值，是因为不同品类的 CPM、互动率不是一个量级，
    绝对阈值没有可比性。代价是分数只在批次内可比。
    """
    n = len(sorted_values)
    if n <= 1:
        return 50.0
    # 用中位排名处理并列，避免同值不同分
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    mid = below + (equal - 1) / 2.0
    return round(mid / (n - 1) * 100, 2)


def div(a, b, default=0.0):
    """安全除法。"""
    try:
        return a / b if b else default
    except ZeroDivisionError:
        return default


def tier_of(score, tiers):
    if score >= tiers["S"]:
        return "S"
    if score >= tiers["A"]:
        return "A"
    if score >= tiers["B"]:
        return "B"
    return "C"


def advice(d1, d2, d3, d4, tier):
    """按四维组合给差异化动作，而不是只看分层。"""
    if tier in ("S", "A"):
        if d1 >= 60 and d3 < 50:
            return "人对但内容没打中：换内容角度重投，不换人"
        if d1 < 50 and d3 >= 60:
            return "人不对但带货强：仅用于拓新客，不做主力"
        if d3 >= 60 and d4 < 50:
            return "有效但贵：优先谈价，其次谈置换"
        if d2 >= 60 and d3 < 50:
            return "内容好看不带货：只做曝光，不背转化"
        return "正常排期，连续 2 轮达标后升级"
    if tier == "B":
        if d4 < 40:
            return "成本偏低，可小额测试 1-2 条验证转化"
        return "小额测试，先验证进店 UV 再谈放量"
    return "不投；若 CPM 显著低于均值，可做铺量不背 KPI"


# ---------- 主流程 ----------

def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rows(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def build_metrics(rows, cfg):
    """把原始字段加工成各维度子指标。"""
    out = []
    for r in rows:
        exposure = float(r["exposure"])
        uv = float(r["store_uv"])
        orders = float(r["orders"])
        aov = float(r["aov"])
        quote = float(r["quote"])
        gmv = orders * aov

        m = {
            "kol_id": r["kol_id"],
            "handle": r["handle"],
            "followers": float(r["followers"]),
            "category": r["category"],
            "quote": quote,
            "gmv": gmv,
            # D1
            "audience_overlap": float(r["audience_overlap"]),
            "category_purity": float(r["category_purity"]),
            "scene_fit": float(r["scene_fit"]) / 5.0,
            # D2
            "viral_rate": float(r["viral_rate"]),
            "engagement_rate": float(r["engagement_rate"]),
            "comment_intent": float(r["comment_intent"]),
            "post_stability": float(r["post_stability"]),
            # D3
            "cvr": div(orders, uv),
            "uv_rate": div(uv, exposure),
            "aov_match": 1.0 - min(abs(aov - cfg["target_aov"]) / cfg["target_aov"], 1.0),
            "repurchase_rate": float(r["repurchase_rate"]),
            # D4（后两项越小越好）
            "cpm": div(quote, exposure) * 1000,
            "quote_to_gmv": div(quote, gmv),
            "fulfill_rate": float(r["fulfill_rate"]),
        }
        out.append(m)
    return out


def score_all(metrics, cfg):
    """先算分位数，再按子权重合成四个维度分，最后加权。"""
    fields = ["audience_overlap", "category_purity", "scene_fit",
              "viral_rate", "engagement_rate", "comment_intent", "post_stability",
              "cvr", "uv_rate", "aov_match", "repurchase_rate",
              "cpm", "quote_to_gmv", "fulfill_rate"]

    sorted_map = {f: sorted(m[f] for m in metrics) for f in fields}
    reversed_set = set(cfg.get("reversed_sub_metrics", []))

    for m in metrics:
        norm = {}
        for f in fields:
            v = pct_rank(m[f], sorted_map[f])
            # 反向指标：越低成本越好
            norm[f] = 100 - v if f in reversed_set else v

        sw = cfg["sub_weights"]
        m["D1"] = round(sum(norm[k] * w for k, w in sw["D1"].items()), 2)
        m["D2"] = round(sum(norm[k] * w for k, w in sw["D2"].items()), 2)
        m["D3"] = round(sum(norm[k] * w for k, w in sw["D3"].items()), 2)
        m["D4"] = round(sum(norm[k] * w for k, w in sw["D4"].items()), 2)

        w = cfg["weights"]
        m["total"] = round(
            m["D1"] * w["D1_match"] + m["D2"] * w["D2_content"] +
            m["D3"] * w["D3_conversion"] + m["D4"] * w["D4_cost"], 2)

        m["tier"] = tier_of(m["total"], cfg["tiers"])
        m["advice"] = advice(m["D1"], m["D2"], m["D3"], m["D4"], m["tier"])
    return metrics


def apply_gates(metrics, cfg):
    """硬闸门。只标记不删除，方便复盘看谁被挡在门外。"""
    g = cfg["gates"]
    for m in metrics:
        reasons = []
        if m["followers"] < g["followers_min"]:
            reasons.append("粉丝量低于下限")
        if m["followers"] > g["followers_max"]:
            reasons.append("粉丝量高于上限")
        if m["quote"] > g["max_quote"]:
            reasons.append("报价超预算")
        m["gate_pass"] = "N" if reasons else "Y"
        m["gate_reason"] = "; ".join(reasons)
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "scored_kols.csv"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    rows = load_rows(args.input)

    metrics = build_metrics(rows, cfg)
    metrics = apply_gates(metrics, cfg)
    metrics = score_all(metrics, cfg)

    # 闸门未过的置底，但保留在结果里
    metrics.sort(key=lambda m: (m["gate_pass"] == "N", -m["total"]))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cols = ["kol_id", "handle", "followers", "category", "gate_pass", "gate_reason",
            "D1", "D2", "D3", "D4", "total", "tier", "advice", "gmv", "quote"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(metrics)

    # ---- 控制台汇总 ----
    n = len(metrics)
    print(f"批次人数：{n} ｜ 配置：{os.path.basename(args.config)}")
    if n < cfg.get("min_batch_size", 15):
        print(f"[warn] 样本量 {n} < {cfg['min_batch_size']}，排序不稳定，结论仅参考\n")

    passed = [m for m in metrics if m["gate_pass"] == "Y"]
    blocked = n - len(passed)
    print(f"通过闸门：{len(passed)} ｜ 被挡：{blocked}\n")

    for t in ["S", "A", "B", "C"]:
        group = [m for m in passed if m["tier"] == t]
        if not group:
            continue
        print(f"  {t} 层 {len(group)} 人")
        for m in group[:3]:
            print(f"    {m['handle']:<12} 总分 {m['total']:>5}  "
                  f"(D1 {m['D1']:.0f} / D2 {m['D2']:.0f} / D3 {m['D3']:.0f} / D4 {m['D4']:.0f})")
            print(f"      → {m['advice']}")
        if len(group) > 3:
            print(f"    ... 另 {len(group) - 3} 人")
        print()

    print(f"明细：{args.out}")


if __name__ == "__main__":
    main()
