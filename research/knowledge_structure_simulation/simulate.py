"""Offline noisy-screening simulation. No corpus, query text, network or LLM.

All ranking and screening qualities are assumptions, not measured accuracy.
Each method reads the same synthetic world; only the evaluator sees relevance.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import platform
import time

import numpy as np


METHODS = ("flat_ranked", "flat_all_summaries", "hierarchical")


def relevance(rng, n, p, rho, branching, shuffle=False):
    if rho == 0:
        truth = rng.random(n) < p
    else:
        concentration = 1 / rho - 1
        theta = rng.beta(p * concentration, (1 - p) * concentration,
                         math.ceil(n / branching))
        truth = rng.random(n) < np.repeat(theta, branching)[:n]
    if shuffle:
        rng.shuffle(truth)
    return truth


def observations(rng, truth, branching, depth, tpr, fpr, upper_loss,
                 correlated=False):
    """Generate immutable signals; strategy receives these, never node truth."""
    signals = []
    shared = rng.random() if correlated else None
    prefix = np.r_[0, np.cumsum(truth)]
    for level in range(depth):
        width = branching ** (level + 1)
        starts = np.arange(0, len(truth), width)
        stops = np.minimum(starts + width, len(truth))
        has_target = prefix[stops] > prefix[starts]
        sensitivity = max(fpr, tpr - upper_loss * level)
        u = np.full(len(starts), shared) if correlated else rng.random(len(starts))
        signals.append(u < np.where(has_target, sensitivity, fpr))
    return signals


def policy_orders(scores, signals, branching, p, tpr, fpr, upper_loss):
    """Return rankings and visited-summary counts without reading truth.

Flat summary retrieval uses the strongest positive group likelihood ratio
as a ranking bonus. The ratio assumes independent leaves even in clustered
worlds, deliberately exposing model misspecification. Negative signals are
not a permanent exclusion. Hierarchy screens paths, then falls back to the
original flat ranking for all other leaves; it can therefore recover misses.
"""
    n = len(scores)
    flat = np.argsort(-scores, kind="stable")
    bonus = np.zeros(n)
    leaf_ids = np.arange(n)
    for level, signal in enumerate(signals):
        width = branching ** (level + 1)
        sizes = np.minimum(width, n - np.arange(len(signal)) * width)
        s = max(fpr, tpr - upper_loss * level)
        other_positive = -np.expm1((sizes - 1) * np.log1p(-p))
        false_positive = fpr + (s - fpr) * other_positive
        gain = np.log(max(s, 1e-12) / np.maximum(false_positive, 1e-12))
        gain = np.where(signal, np.maximum(gain, 0), 0)
        bonus = np.maximum(bonus, gain[leaf_ids // width])
    flat_summaries = np.argsort(-(scores + bonus), kind="stable")

    active = np.ones(n, dtype=bool)
    visited = [0] * len(signals)
    for level in reversed(range(len(signals))):
        width = branching ** (level + 1)
        starts = np.arange(len(signals[level])) * width
        reached = active[starts]
        visited[level] = int(reached.sum())
        active &= signals[level][leaf_ids // width]
    hierarchical = np.r_[flat[active[flat]], flat[~active[flat]]]
    return (flat, flat_summaries, hierarchical), [
        [0] * len(signals), [len(x) for x in signals], visited]


def recall_curve(truth, order, overhead, budgets):
    # Every summary inspection precedes document inspection in these policies.
    count = np.clip(np.floor(budgets - overhead + 1e-9).astype(int), 0, len(truth))
    cumulative = np.r_[0, np.cumsum(truth[order])]
    if not truth.any():
        return np.full(len(budgets), np.nan), count
    return cumulative[count] / truth.sum(), count


def first_budget(curve, budgets, target):
    eligible = np.flatnonzero(curve >= target)
    return float(budgets[eligible[0]]) if len(eligible) else None


def stderr(values):
    return float(np.std(values, ddof=1) / np.sqrt(len(values))) if len(values) > 1 else None


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(config, output):
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    anchors = config["anchors"]
    b = anchors["branching"]
    repeats = config["repetitions"]
    target = config["target_recall"]
    # Predeclared grid: dense low-budget points and 1%-of-N steps to 2N.
    fractions = np.unique(np.r_[0, np.geomspace(0.0001, 0.1, 80),
                                np.linspace(0.1, 2, 191)])
    summary, trials, curves, arrays = [], [], [], {}
    for profile_index, profile in enumerate(config["profiles"]):
        n = profile["n"]
        p = profile["mean_relevant"] / n
        budgets = fractions * n
        for variant in config["variants"]:
            params = config["assumptions"] | variant
            name = profile["name"] + "__" + variant["name"]
            costs = np.array([
                anchors["summary_length"] / anchors["leaf_length"]
                * params["summary_cost_multiplier"] * params["upper_cost_growth"] ** level
                for level in range(params["depth"])])
            recalls = np.empty((repeats, len(METHODS), len(budgets)), dtype=np.float32)
            counts = []
            for trial in range(repeats):
                # Separate streams preserve identical truths/rank noise across
                # variants with unchanged world parameters.
                seed = [config["seed"], profile_index, trial]
                world_rng = np.random.default_rng(np.random.SeedSequence(seed + [0]))
                rank_rng = np.random.default_rng(np.random.SeedSequence(seed + [1]))
                signal_rng = np.random.default_rng(np.random.SeedSequence(seed + [2]))
                truth = relevance(world_rng, n, p, params["rho"], b, params["shuffle"])
                scores = params["rank_delta"] * (params["rank_delta"] * truth
                          + rank_rng.normal(size=n)) - params["rank_delta"] ** 2 / 2
                signals = observations(signal_rng, truth, b, params["depth"],
                                       params["tpr"], params["fpr"],
                                       params["upper_tpr_loss"], params["correlated_errors"])
                orders, visited = policy_orders(scores, signals, b, p, params["tpr"],
                                                params["fpr"], params["upper_tpr_loss"])
                counts.append(int(truth.sum()))
                for method, order, inspections in zip(METHODS, orders, visited):
                    mi = METHODS.index(method)
                    overhead = float(np.dot(costs, inspections))
                    curve, read_counts = recall_curve(truth, order, overhead, budgets)
                    recalls[trial, mi] = curve
                    row = {"scenario": name, "trial": trial, "method": method,
                           "relevant": int(truth.sum()), "summary_inspections": sum(inspections),
                           "summary_read_cost": overhead, "leaf_scores_available": n,
                           "summary_storage_units": sum(len(s) for s in signals)}
                    for fraction in (0.1, 0.25, 0.5, 1.0):
                        bi = int(np.argmin(abs(fractions - fraction)))
                        row[f"recall_at_{fraction}N"] = float(curve[bi]) if truth.any() else ""
                        row[f"leaves_read_at_{fraction}N"] = int(read_counts[bi])
                    trials.append(row)
            valid = np.array(counts) > 0
            valid_recalls = recalls[valid].astype(float)
            means = valid_recalls.mean(axis=0) if valid.any() else np.full((3, len(budgets)), np.nan)
            flat_cost = first_budget(means[0], budgets, target)
            flat_bi = int(np.searchsorted(budgets, flat_cost)) if flat_cost is not None else None
            for mi, method in enumerate(METHODS):
                cost = first_budget(means[mi], budgets, target)
                crossing = int(np.searchsorted(budgets, cost)) if cost is not None else None
                gain = None if cost is None or flat_cost is None else 1 - cost / flat_cost
                paired = (valid_recalls[:, mi, flat_bi] - valid_recalls[:, 0, flat_bi]
                          if flat_bi is not None else None)
                row = {"scenario": name, "method": method, "n": n, "nominal_p": p,
                       "repetitions": repeats, "answerable_trials": int(valid.sum()),
                       "empty_trials": int((~valid).sum()), "mean_relevant": float(np.mean(counts)),
                       "cost_at_target": cost, "cost_reduction_vs_flat": gain,
                       "recall_mcse_at_target": stderr(valid_recalls[:, mi, crossing]) if crossing is not None else None,
                       "paired_recall_gain_at_flat_budget": float(paired.mean()) if paired is not None else None,
                       "paired_gain_mcse": stderr(paired) if paired is not None else None,
                       "fraction_trials_below_target_at_chosen_budget": float(np.mean(valid_recalls[:, mi, crossing] < target)) if crossing is not None else None}
                summary.append(row)
                for bi, budget in enumerate(budgets):
                    curves.append({"scenario": name, "method": method,
                                   "budget": float(budget), "mean_recall": float(means[mi, bi]),
                                   "mcse": stderr(valid_recalls[:, mi, bi])})
            arrays[name] = recalls
            arrays[name + "__relevant_counts"] = np.array(counts)
    write_csv(output / "summary.csv", summary)
    write_csv(output / "trials.csv", trials)
    write_csv(output / "curves.csv", curves)
    np.savez_compressed(output / "replicates.npz", fractions=fractions, **arrays)
    (output / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    elapsed = time.perf_counter() - started
    manifest = {"elapsed_seconds": elapsed, "python": platform.python_version(),
                "numpy": np.__version__, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "config_sha256": hashlib.sha256((output / "config.json").read_bytes()).hexdigest(),
                "note": "Monte Carlo error excludes model uncertainty. Costs are synthetic reading units; preprocessing and cheap scoring are excluded."}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report(output, config, summary, elapsed)
    print(json.dumps({"scenarios": len(arrays) // 2, "repetitions_each": repeats,
                      "seconds": round(elapsed, 2), "report": str(output / "report.md")}))


def report(output, config, rows, elapsed):
    lines = ["# 概率模拟结果", "",
             f"{len(config['profiles']) * len(config['variants'])} 个预设情景，每情景 {config['repetitions']} 次配对试验；计算与保存耗时 {elapsed:.2f} 秒。",
             "", "这些是合成阅读成本，不是生产延迟、实际 token 用量或问答准确率。",
             "相关率取公开基准标注密度；分组/路由质量与平铺排序质量未经校准。当前部署没有 index。",
             "", f"目标：在非空试验中达到 {config['target_recall']:.0%} **平均**相关文章召回。空试验另列，不将其算为满分。",
             "预算网格固定；不同方法都可回退读取剩余原文。成本包括失败路径摘要读取，不包括离线构建与廉价评分。",
             "", "| 情景 | 非空/总试验 | 平铺成本 | 平铺全部摘要成本 | 分层成本 | 分层相对平铺变化 |",
             "|---|---:|---:|---:|---:|---:|"]
    for offset in range(0, len(rows), 3):
        a, b, c = rows[offset:offset + 3]
        fmt = lambda x: f"{x:.1f}" if x is not None else "未达标"
        gain = c['cost_reduction_vs_flat']
        text_gain = f"减少 {gain:.1%}" if gain is not None and gain >= 0 else (f"增加 {-gain:.1%}" if gain is not None else "未达标")
        lines.append(f"| {a['scenario']} | {a['answerable_trials']}/{a['repetitions']} | {fmt(a['cost_at_target'])} | {fmt(b['cost_at_target'])} | {fmt(c['cost_at_target'])} | {text_gain} |")
    lines += ["", "## 如何解释", "",
              "- reference_assumptions 只是未校准的参考情景，不能称为典型真实世界。perfect_signal_diagnostic 是无噪声对照，并非最优策略的数学上界。",
              "- 同一语料密度在不同信号质量下可能获益或退化；不可只摘取最大的正值。",
              "- summary.csv 保存召回的 Monte Carlo 标准误及同预算配对差；它不覆盖参数假设不确定性。",
              "- 目标是跨非空试验的平均召回，不保证每个查询都达标；逐查询失败比例另存于 summary.csv。",
              "- 平铺全部摘要是一种全量阅读筛查消融，不是 RAPTOR collapsed-tree 或 ANN 检索的复现；主对照是有免费排序信号的 flat_ranked。",
              "- 增加一个摘要的长度成本不能等同于它的建造成本。若另有构建成本 B，每查询净节省 D>0 时，摊销阈值为 Q>B/D；本实验不捏造 B。",
              "- 参数来源见 ../parameter_sources.md，算法与限制见 ../README.md。"]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    # A standalone static plot, useful for exporting the comparison.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(config["profiles"]), figsize=(12, 5), squeeze=False)
    for ax, profile in zip(axes[0], config["profiles"]):
        selected = [r for r in rows if r["method"] == "hierarchical" and r["scenario"].startswith(profile["name"] + "__")]
        labels = [r["scenario"].split("__")[1] for r in selected]
        ratios = [1 - r["cost_reduction_vs_flat"] if r["cost_reduction_vs_flat"] is not None else np.nan for r in selected]
        ax.scatter(ratios, labels, color=["#177e89" if r <= 1 else "#bc4749" for r in ratios])
        ax.axvline(1, color="black", linewidth=0.7)
        ax.set_xscale("log")
        ax.grid(axis="x", alpha=0.2)
        ax.set_title(profile["name"])
        ax.set_xlabel("Read-cost ratio to ranked flat (log scale; lower is better)")
        ax.invert_yaxis()
    fig.suptitle(f"{config['target_recall']:.0%} mean recall; uncalibrated routing/ranking assumptions")
    fig.tight_layout()
    fig.savefig(output / "sensitivity.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("scenarios.json"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("results"))
    parser.add_argument("--repetitions", type=int)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.repetitions is not None:
        config["repetitions"] = args.repetitions
    if config["repetitions"] < 2:
        parser.error("at least two repetitions are required")
    run(config, args.output)
