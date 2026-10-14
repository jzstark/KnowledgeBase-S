# 概率模拟结果

20 个预设情景，每情景 1000 次配对试验；计算与保存耗时 27.46 秒。

这些是合成阅读成本，不是生产延迟、实际 token 用量或问答准确率。
相关率取公开基准标注密度；分组/路由质量与平铺排序质量未经校准。当前部署没有 index。

目标：在非空试验中达到 90% **平均**相关文章召回。空试验另列，不将其算为满分。
预算网格固定；不同方法都可回退读取剩余原文。成本包括失败路径摘要读取，不包括离线构建与廉价评分。

| 情景 | 非空/总试验 | 平铺成本 | 平铺全部摘要成本 | 分层成本 | 分层相对平铺变化 |
|---|---:|---:|---:|---:|---:|
| density_01_scifact__reference_assumptions | 853/1000 | 2400.0 | 2900.0 | 542.2 | 减少 77.4% |
| density_01_scifact__moderate_summaries | 853/1000 | 2400.0 | 3300.0 | 916.3 | 减少 61.8% |
| density_01_scifact__noisy_summaries | 853/1000 | 2400.0 | 3900.0 | 2000.0 | 减少 16.7% |
| density_01_scifact__strong_flat_ranker | 853/1000 | 36.1 | 2600.0 | 542.2 | 增加 1403.9% |
| density_02_interpolated__reference_assumptions | 998/1000 | 2400.0 | 3000.0 | 645.8 | 减少 73.1% |
| density_02_interpolated__moderate_summaries | 998/1000 | 2400.0 | 3300.0 | 1100.0 | 减少 54.2% |
| density_02_interpolated__noisy_summaries | 998/1000 | 2400.0 | 4000.0 | 2100.0 | 减少 12.5% |
| density_02_interpolated__strong_flat_ranker | 998/1000 | 42.9 | 2600.0 | 591.8 | 增加 1278.0% |
| density_03_trec_covid__reference_assumptions | 1000/1000 | 2500.0 | 3100.0 | 916.3 | 减少 63.3% |
| density_03_trec_covid__moderate_summaries | 1000/1000 | 2500.0 | 3400.0 | 1300.0 | 减少 48.0% |
| density_03_trec_covid__noisy_summaries | 1000/1000 | 2500.0 | 4100.0 | 2300.0 | 减少 8.0% |
| density_03_trec_covid__strong_flat_ranker | 1000/1000 | 60.9 | 2600.0 | 839.6 | 增加 1278.0% |
| density_04_interpolated__reference_assumptions | 1000/1000 | 2500.0 | 3200.0 | 1200.0 | 减少 52.0% |
| density_04_interpolated__moderate_summaries | 1000/1000 | 2500.0 | 3600.0 | 1700.0 | 减少 32.0% |
| density_04_interpolated__noisy_summaries | 1000/1000 | 2500.0 | 4200.0 | 2600.0 | 增加 4.0% |
| density_04_interpolated__strong_flat_ranker | 1000/1000 | 86.4 | 2600.0 | 1100.0 | 增加 1172.6% |
| density_05_nfcorpus__reference_assumptions | 1000/1000 | 2500.0 | 3400.0 | 1700.0 | 减少 32.0% |
| density_05_nfcorpus__moderate_summaries | 1000/1000 | 2500.0 | 3700.0 | 2300.0 | 减少 8.0% |
| density_05_nfcorpus__noisy_summaries | 1000/1000 | 2500.0 | 4300.0 | 3200.0 | 增加 28.0% |
| density_05_nfcorpus__strong_flat_ranker | 1000/1000 | 133.8 | 2700.0 | 1500.0 | 增加 1020.8% |

## 如何解释

- reference_assumptions 只是未校准的参考情景，不能称为典型真实世界。perfect_signal_diagnostic 是无噪声对照，并非最优策略的数学上界。
- 同一语料密度在不同信号质量下可能获益或退化；不可只摘取最大的正值。
- summary.csv 保存召回的 Monte Carlo 标准误及同预算配对差；它不覆盖参数假设不确定性。
- 目标是跨非空试验的平均召回，不保证每个查询都达标；逐查询失败比例另存于 summary.csv。
- 平铺全部摘要是一种全量阅读筛查消融，不是 RAPTOR collapsed-tree 或 ANN 检索的复现；主对照是有免费排序信号的 flat_ranked。
- 增加一个摘要的长度成本不能等同于它的建造成本。若另有构建成本 B，每查询净节省 D>0 时，摊销阈值为 Q>B/D；本实验不捏造 B。
- 参数来源见 ../parameter_sources.md，算法与限制见 ../README.md。
