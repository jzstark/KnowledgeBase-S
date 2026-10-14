# 概率模拟结果

28 个预设情景，每情景 1000 次配对试验；计算与保存耗时 18.85 秒。

这些是合成阅读成本，不是生产延迟、实际 token 用量或问答准确率。
相关率取公开基准标注密度；分组/路由质量与平铺排序质量未经校准。当前部署没有 index。

目标：在非空试验中达到 90% **平均**相关文章召回。空试验另列，不将其算为满分。
预算网格固定；不同方法都可回退读取剩余原文。成本包括失败路径摘要读取，不包括离线构建与廉价评分。

| 情景 | 非空/总试验 | 平铺成本 | 平铺全部摘要成本 | 分层成本 | 分层相对平铺变化 |
|---|---:|---:|---:|---:|---:|
| scifact_density__reference_assumptions | 610/1000 | 1243.9 | 1503.1 | 306.7 | 减少 75.3% |
| scifact_density__independent | 655/1000 | 1243.9 | 1503.1 | 306.7 | 减少 75.3% |
| scifact_density__strong_clustering | 479/1000 | 1192.1 | 1503.1 | 306.7 | 减少 74.3% |
| scifact_density__shuffled_groups | 610/1000 | 1243.9 | 1554.9 | 306.7 | 减少 75.3% |
| scifact_density__strong_flat_ranker | 610/1000 | 17.1 | 1347.6 | 281.0 | 增加 1541.3% |
| scifact_density__weak_flat_ranker | 610/1000 | 3109.8 | 1658.6 | 365.3 | 减少 88.3% |
| scifact_density__noisy_summaries | 610/1000 | 1243.9 | 2021.4 | 984.8 | 减少 20.8% |
| scifact_density__uninformative_summaries | 610/1000 | 1243.9 | 2539.7 | 2798.8 | 增加 125.0% |
| scifact_density__one_level | 610/1000 | 1243.9 | 1347.6 | 1295.8 | 增加 4.2% |
| scifact_density__three_levels | 610/1000 | 1243.9 | 1606.7 | 139.6 | 减少 88.8% |
| scifact_density__degrading_upper_levels | 610/1000 | 1243.9 | 1814.0 | 398.7 | 减少 67.9% |
| scifact_density__correlated_errors | 610/1000 | 1243.9 | 1503.1 | 257.5 | 减少 79.3% |
| scifact_density__double_summary_cost | 610/1000 | 1243.9 | 2798.8 | 570.1 | 减少 54.2% |
| scifact_density__perfect_signal_diagnostic | 610/1000 | 1243.9 | 1347.6 | 198.1 | 减少 84.1% |
| nfcorpus_density__reference_assumptions | 1000/1000 | 871.9 | 1235.2 | 653.9 | 减少 25.0% |
| nfcorpus_density__independent | 1000/1000 | 908.2 | 1235.2 | 690.3 | 减少 24.0% |
| nfcorpus_density__strong_clustering | 1000/1000 | 908.2 | 1198.9 | 545.0 | 减少 40.0% |
| nfcorpus_density__shuffled_groups | 1000/1000 | 908.2 | 1235.2 | 690.3 | 减少 24.0% |
| nfcorpus_density__strong_flat_ranker | 1000/1000 | 48.6 | 980.9 | 581.3 | 增加 1095.5% |
| nfcorpus_density__weak_flat_ranker | 1000/1000 | 2216.1 | 1453.2 | 726.6 | 减少 67.2% |
| nfcorpus_density__noisy_summaries | 1000/1000 | 871.9 | 1525.9 | 1162.6 | 增加 33.3% |
| nfcorpus_density__uninformative_summaries | 1000/1000 | 871.9 | 1780.2 | 1998.2 | 增加 129.2% |
| nfcorpus_density__one_level | 1000/1000 | 871.9 | 1053.6 | 980.9 | 增加 12.5% |
| nfcorpus_density__three_levels | 1000/1000 | 871.9 | 1235.2 | 690.3 | 减少 20.8% |
| nfcorpus_density__degrading_upper_levels | 1000/1000 | 871.9 | 1416.9 | 908.2 | 增加 4.2% |
| nfcorpus_density__correlated_errors | 1000/1000 | 871.9 | 1235.2 | 545.0 | 减少 37.5% |
| nfcorpus_density__double_summary_cost | 1000/1000 | 871.9 | 2143.5 | 1089.9 | 增加 25.0% |
| nfcorpus_density__perfect_signal_diagnostic | 1000/1000 | 871.9 | 1089.9 | 545.0 | 减少 37.5% |

## 如何解释

- reference_assumptions 只是未校准的参考情景，不能称为典型真实世界。perfect_signal_diagnostic 是无噪声对照，并非最优策略的数学上界。
- 同一语料密度在不同信号质量下可能获益或退化；不可只摘取最大的正值。
- summary.csv 保存召回的 Monte Carlo 标准误及同预算配对差；它不覆盖参数假设不确定性。
- 目标是跨非空试验的平均召回，不保证每个查询都达标；逐查询失败比例另存于 summary.csv。
- 平铺全部摘要是一种全量阅读筛查消融，不是 RAPTOR collapsed-tree 或 ANN 检索的复现；主对照是有免费排序信号的 flat_ranked。
- 增加一个摘要的长度成本不能等同于它的建造成本。若另有构建成本 B，每查询净节省 D>0 时，摊销阈值为 Q>B/D；本实验不捏造 B。
- 参数来源见 ../parameter_sources.md，算法与限制见 ../README.md。
