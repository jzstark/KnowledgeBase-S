"""Report fixed-size density sweep in simulated token units, with paired CIs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np


def target_costs(means, budgets, target):
    reached = means >= target
    costs = budgets[np.argmax(reached, axis=-1)].astype(float)
    return np.where(reached.any(axis=-1), costs, np.nan)


def paired_bootstrap(recalls, budgets, target, repeats=400, seed=20261015):
    """Resample the same query indices for flat and hierarchical curves.

    Input is answerable queries x methods x budgets. Percentile intervals
    quantify Monte Carlo query sampling only; the budget grid is unchanged.
    """
    rng = np.random.default_rng(seed)
    n = len(recalls)
    weights = rng.multinomial(n, np.full(n, 1 / n), size=repeats) / n
    flat = target_costs(weights @ recalls[:, 0, :], budgets, target)
    hierarchy = target_costs(weights @ recalls[:, 2, :], budgets, target)
    reduction = 1 - hierarchy / flat
    return flat, hierarchy, reduction


def summarize(root):
    config = json.loads((root / "config.json").read_text())
    with (root / "summary.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    length = config["anchors"]["leaf_length"]
    target = config["target_recall"]
    references = []
    with np.load(root / "replicates.npz") as arrays:
        for index, profile in enumerate(config["profiles"]):
            name = profile["name"] + "__reference_assumptions"
            selected = {r["method"]: r for r in rows if r["scenario"] == name}
            counts = arrays[name + "__relevant_counts"]
            recalls = arrays[name][counts > 0].astype(float)
            budgets = arrays["fractions"] * profile["n"]
            flat, hierarchy, reduction = paired_bootstrap(recalls, budgets, target,
                                                          seed=20261015 + index)
            finite = np.isfinite(reduction)
            if not finite.all():
                raise ValueError("A bootstrap replicate did not reach the target; do not silently drop it.")
            ci = np.percentile(reduction, [2.5, 97.5])
            r = selected["hierarchical"]
            references.append({
                "profile": profile["name"],
                "density_percent": 100 * profile["mean_relevant"] / profile["n"],
                "density_provenance": profile["density_provenance"],
                "expected_relevant": profile["mean_relevant"],
                "answerable_trials": int((counts > 0).sum()),
                "total_trials": len(counts),
                "flat_token_budget": float(selected["flat_ranked"]["cost_at_target"]) * length,
                "hierarchical_token_budget": float(r["cost_at_target"]) * length,
                "reduction": float(r["cost_reduction_vs_flat"]),
                "reduction_ci_low": float(ci[0]), "reduction_ci_high": float(ci[1]),
                "flat_token_ci_low": float(np.percentile(flat, 2.5) * length),
                "flat_token_ci_high": float(np.percentile(flat, 97.5) * length),
                "hierarchical_token_ci_low": float(np.percentile(hierarchy, 2.5) * length),
                "hierarchical_token_ci_high": float(np.percentile(hierarchy, 97.5) * length),
            })
    with (root / "density_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(references[0]))
        writer.writeheader()
        writer.writerows(references)
    gains = [r["reduction"] for r in references]
    params = config["assumptions"]
    lines = ["# 固定库规模的密度扫描", "",
             "目的：区分相关密度变化与原先不同库规模的影响，补充中间点；不为了缩小区间而挑选结果。",
             "", f"所有情景 N=10,000；5 档相关密度 × 4 档策略参数 × {config['repetitions']} 次试验。",
             f"主比较保持 depth={params['depth']}、rho={params['rho']}、排序 delta={params['rank_delta']}、摘要 TPR={params['tpr']}、FPR={params['fpr']} 不变。",
             "密度的三个锚点来自 BEIR；两个中点是相邻锚点的几何插值。不是五个真实数据集的测评。",
             "", f"主指标：达到 {target:.0%} **平均**召回所需的模拟查询 token 预算。归一化成本 ×85.6 换算为 token；不包括模型输出、提示词、索引构建和检索计算。",
             "", "| 名义相关比例 | 参数来源 | 平铺 token 预算 | 分层 token 预算 | 节省 | 95% bootstrap 区间 |",
             "|---|---|---:|---:|---:|---:|"]
    for r in references:
        label = "几何插值" if "interpolated" in r["profile"] else r["density_provenance"].split(":")[0]
        lines.append(f"| {r['density_percent']:.4f}% | {label} | {r['flat_token_budget']:,.0f} | {r['hierarchical_token_budget']:,.0f} | {r['reduction']:.1%} | {r['reduction_ci_low']:.1%}–{r['reduction_ci_high']:.1%} |")
    lines += ["", f"这五个参考情景的点估计范围为 **{min(gains):.1%}–{max(gains):.1%}**。这是固定参数下跨五档密度的最小/最大值，不是置信区间、全参数空间范围或所有真实查询的保证。",
              "", "区间采用 400 次配对 bootstrap，在非空试验上同时重采样三种策略的同一查询。它只反映模拟抽样误差，不反映摘要质量等假设是否符合现实，也不消除预算网格离散误差。",
              "", "## 同密度范围内改变策略参数", "",
              "| 参数情景 | 五档密度的节省范围（负值表示增加） |",
              "|---|---:|"]
    all_gains = []
    for variant in config["variants"]:
        selected = [float(r["cost_reduction_vs_flat"]) for r in rows
                    if r["method"] == "hierarchical" and r["scenario"].endswith("__" + variant["name"])]
        all_gains.extend(selected)
        lines.append(f"| {variant['name']} | {min(selected):.1%}–{max(selected):.1%} |")
    lines += ["", "参考范围不能替代这张压力测试表；若摘要较差或平铺排序非常强，部分或全部密度档可能没有收益。",
              "", "## 可解释的机制", "",
              "在这个模型中，稀疏目标使分组检测可以排除更多无关分支。密度提高时，更多分支含目标，需要支付更多摘要与原文读取开销；分层的相对收益因而可能收窄。这是模型机制解释，不是任意检索器的单调性定理。",
              "", "## CV 口径", "",
              f"“在固定规模、五档相关密度的概率仿真中，以相同 {target:.0%} 平均召回，相比平铺检索降低约 {min(gains):.0%}–{max(gains):.0%} 的模拟查询 token 预算。”",
              "", "该表述仅指上述参考路由/排序参数。面试追问时应提供参数、原始输出和负结果；不能称为生产实测或普适区间。",
              "", "参数出处：[parameter_sources.md](../parameter_sources.md)。复跑配置：[density_sweep.json](../density_sweep.json)。"]
    (root / "density_report.md").write_text("\n".join(lines) + "\n")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = np.array([r["density_percent"] for r in references])
    y = 100 * np.array(gains)
    low = 100 * np.array([r["reduction_ci_low"] for r in references])
    high = 100 * np.array([r["reduction_ci_high"] for r in references])
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for method, label in (("flat", "Ranked flat"), ("hierarchical", "Hierarchical")):
        axes[0].plot(x, [r[method + "_token_budget"] for r in references], "o-", label=label)
    axes[0].set_ylabel("Simulated query token budget")
    axes[0].legend()
    axes[1].plot(x, y, "o-", color="#177e89")
    axes[1].fill_between(x, low, high, alpha=0.2, color="#177e89", label="95% paired bootstrap interval")
    axes[1].set_ylabel("Token budget reduction (%)")
    axes[1].legend(fontsize=8)
    for ax in axes:
        ax.set_xscale("log")
        ax.set_xlabel("Nominal relevance density (%)")
        ax.grid(alpha=0.25)
    fig.suptitle("N=10,000; 90% mean recall; assumed TPR/FPR=0.95/0.05, rank delta=2")
    fig.tight_layout()
    fig.savefig(root / "density_trend.png", dpi=160)
    plt.close(fig)
    manifest = {"bootstrap_repetitions": 400, "bootstrap_seed_base": 20261015,
                "numpy": np.__version__,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "inputs_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                  for name in ("config.json", "summary.csv", "replicates.npz")}}
    (root / "density_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"reference_range": [min(gains), max(gains)],
                      "all_stress_range": [min(all_gains), max(all_gains)],
                      "report": str(root / "density_report.md")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path(__file__).with_name("density_results"))
    summarize(parser.parse_args().results)
