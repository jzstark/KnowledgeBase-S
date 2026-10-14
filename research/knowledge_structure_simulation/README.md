# 内容无关的知识结构概率模拟

用合成相关性标签与带噪声的筛查信号，比较达到相同平均召回所需的阅读成本。没有真实文章或自然语言查询，不调用网络、数据库、embedding 或 LLM，不访问运营 Wiki。

本实验有文献锚点，但尚未校准真实摘要/检索器的能力。结果是模型条件下的设计评估，不是生产性能证明。当前系统没有 index；本实验中的 index 是拟议设计。

## 运行

在仓库根目录执行（环境已有 NumPy、Matplotlib）：

```bash
python -m unittest discover -s research/knowledge_structure_simulation -p 'test_*.py' -v
MPLCONFIGDIR=/tmp/kb-simulation-mpl python research/knowledge_structure_simulation/simulate.py
```

快速重跑或保存到另一个目录：

```bash
MPLCONFIGDIR=/tmp/kb-simulation-mpl python research/knowledge_structure_simulation/simulate.py --repetitions 100 --output /tmp/kb-simulation-quick
```

不需要下载模型。运行依赖为 `numpy` 和 `matplotlib`；版本记录在结果 manifest。默认 28 个情景，各 1,000 次试验，同一试验的三个方法共享隐藏真值、原文分数和摘要信号。空试验单独保留，平均召回仅针对非空试验。

## 哪些参数有来源

详细出处和定义见 [parameter_sources.md](parameter_sources.md)。

| 参数 | 使用值 | 性质 |
|---|---|---|
| 稀疏情景 N / 平均相关数 | 5,183 / 1.1 | BEIR SciFact 的规模与已标注相关数 |
| 较密情景 N / 平均相关数 | 3,633 / 38.2 | BEIR NFCorpus 的规模与已标注相关数 |
| 名义相关率 p | 1.1/5183、38.2/3633 | 上述统计的比值，不是真实普适相关率 |
| 分支数 | 7 | RAPTOR 聚类消融使用过的窗口大小；本实验采用严格分组 |
| 摘要/叶节点阅读成本 | 131/85.6 ≈ 1.53 | RAPTOR 平均文本长度之比；不是 0.28 |
| 路由 TPR/FPR、聚集 rho、排序 delta、深度 | 见 scenarios.json | 未校准的显式假设与压力测试 |

**单位边界：** 每个叶节点是等长的合成证据单元。相关密度参考文档级基准，长度比参考短文本块。将二者组合是我们新建的情景，不是对 BEIR/RAPTOR 的复现，也不是“真实整篇文章只长 85.6 token”的假设。没有模型依据将短块摘要比直接当作系统中整篇文章的摘要比。

基准标注可能不完整，因此 p 只锚定一个可追溯的合成密度。没有查到可直接迁移的真实路由 TPR/FPR；不会把 RAPTOR 的幻觉率换算成召回率。

## 模型与三个策略

树在查询之前固定，连续七个叶节点构成一个组，逐层向上分组。相关性为独立 Bernoulli 或最底层组内 Beta-Bernoulli；树不会根据当前查询的答案临时生成。shuffle 情景把生成的标签打乱，用作错误分组对照。

原文便宜的排序信号为 `x = delta*R + Normal(0,1)`，以其对数似然比分数 `delta*x - delta²/2` 排序。所有方法都有同样的原文分数，分数不等于原文已被阅读。delta 控制基线区分能力，不表示任何真实 BM25/向量模型的测量值。

摘要筛查是一个有误报与漏报的二元观测，隐藏目标是“后代至少含一个相关单元”。模拟器生成信号，策略只能读取信号；原文访问后才验证真实证据。摘要命中不直接算召回。

1. **flat_ranked**：直接按原文分数排序阅读。这是主要基线，不是随机扫描。
2. **flat_all_summaries**：先阅读全部层级摘要，以每篇原文获得的最强正向组证据作为排序加成，再阅读原文。组证据使用独立叶模型的正向似然比，不叠加相关证据、不永久排除阴性结果。该似然比在聚集世界里可能失配，这是固定策略的一部分。
3. **hierarchical**：先读根级摘要，仅在阳性分支继续读取下一级摘要。完成筛查后，先按原文分数读取通过整条路径的叶节点，再按原文分数回退读取其余叶节点。漏检不会永久删掉资料。

第 2 组是“全量阅读摘要”的筛查消融，**不是**能免费对所有摘要 embedding 排序的现代全局向量检索，也不是 RAPTOR collapsed-tree 的复现。不能据它较差的结果宣称分层一定胜过平铺向量检索。三个策略都不是全面优化后的最优策略；首版评估的是这些明确的访问规则。

共同的原文廉价评分成本不计入阅读指标，但评分次数另记。摘要路由信号只有支付读取成本后策略才可使用。所有策略的摘要检查阶段在原文读取之前结束；小预算下筛查开销可能耗尽预算。这是本版策略的真实限制。

## 参数扫描与成本

参考假设：深度 2、rho=0.05、delta=2、TPR=0.95、FPR=0.05。它只是扫描的起点，不被称为文献标定的典型场景。扫描包括：独立/强聚集、错误分组、强/弱平铺排序、低质量/无信息摘要、1/3 层、上层信号退化和成本增长、跨层相关误差、双倍摘要成本、完美信号诊断。完美信号诊断不是最优成本的数学界。

相关误差情景使用同一查询级均匀随机数驱动所有组检测，表示极端共同失效压力测试，不是现实误差相关性的估计。上层退化情景每升一层降低 TPR 0.05，并将摘要成本乘 2；这些同样是压力参数。

每个原文单元成本为 1；每条摘要成本为 131/85.6，再乘相应压力参数。记录被检查后丢弃的摘要、空结果查询、以及无关原文的成本。存储节点数另列。构建成本没有可靠数值，故不混入访问成本；若构建成本为 B，每查询净节省为 D>0，则至少需查询 B/D 次才能摊销。

## 指标、误差与验证

- 固定预算网格由低预算对数点及 0.1N 到 2N 的 0.01N 步长组成，运行前固定；没有为每题使用隐藏标签来停止检索。
- 在相同的非空查询集合上取平均召回，报告第一个达到目标平均召回的网格预算。它是离线评估阈值，不是线上可观察的停止条件；预算结果带网格离散误差。
- 目标为平均召回 90%，并非每题 90%。逐题未达标比例单列；超过最大预算未达标就保留为空，不能只报告成功试验。
- summary.csv 中的召回 Monte Carlo 标准误只覆盖有限重复次数的随机误差，不覆盖参数失真或跨领域迁移。收益百分比没有被包装成精确无误差的真实常数。
- 六项测试覆盖两阶段解析成本、Beta-Bernoulli 边际/相关性、零深度退化、摘要成本、空查询和剪枝回退。验证不要求“新方法一定更好”。

## 产物

- [results/report.md](results/report.md)：全部情景的结果与解释。
- [results/sensitivity.png](results/sensitivity.png)：成本比图，低于 1 表示减少成本。
- results/summary.csv：目标预算、同预算配对召回差、标准误和查询失败比例。
- results/trials.csv：逐次的相关数、摘要访问、多个预算的原文访问数与召回。
- results/curves.csv、results/replicates.npz：完整平均/逐次召回曲线。
- results/config.json、results/manifest.json：运行配置、种子、版本、代码指纹和计算耗时（不含绘图）。

### CV 使用口径

可以写“构建由公开检索基准统计启发的概率模拟，在明确的路由与排序假设下，以相同平均召回降低模拟访问成本”。若引用百分比，必须指出情景以及对照方法；不可隐去负结果，不能写成线上效果、问答准确率或实际 token 节省。若要求一个不带这些条件的产品提升百分比，本实验尚不能提供。

## 五档密度扩展：报告模拟查询 token 预算

`density_sweep.json` 固定 N=10,000，使用三个有来源的密度锚点和两个几何插值点；每档重复参考参数、两档较差摘要和强平铺排序，共 20 个情景。该配置在看到新结果前固定，保留原先实验输出在 results/。

```bash
MPLCONFIGDIR=/tmp/kb-simulation-mpl python research/knowledge_structure_simulation/simulate.py --config research/knowledge_structure_simulation/density_sweep.json --output research/knowledge_structure_simulation/density_results
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/kb-simulation-mpl python research/knowledge_structure_simulation/summarize_density.py
```

读取 [density_results/density_report.md](density_results/density_report.md) 和 [趋势图](density_results/density_trend.png)。报告把原有成本单位乘 85.6，表达为**达到目标平均召回所需的模拟查询 token 预算**；比例不受单位转换影响。它不含生成输出、系统提示、构建或检索计算，不是 API 计费 token。

五档参考参数结果的最小/最大值表示跨情景范围。400 次配对 bootstrap 则表示各档的模拟抽样不确定性，不代表参数/模型不确定性。八项校验包含同曲线 bootstrap 配对一致和未达标不能被判成零成本。
