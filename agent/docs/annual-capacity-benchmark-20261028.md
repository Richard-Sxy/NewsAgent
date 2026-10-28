# 年度容量测试：检索延迟、近邻召回与物理清理

日期：2026-10-28。输入是明确标识的合成容量数据。
结果见output/annual-capacity-benchmark-20261028/report.md和resume-ready.md。

## 本次完成了什么

- 18.25万篇有效文档、91.25万条1024维FP32向量全部建成三层Faiss IVFFlat索引。
- 100组预编码合成查询：50个独立随机向量、50个库内向量加噪；不是真实新闻问题或用户流量。
- 对完整库按每篇最佳分片计算精确Top10新闻邻居，保存独立oracle。
- 单并发全探针P95=217.68ms；nprobe16/64较快，但邻居Recall仅0.170/0.458，不能只报最快耗时。
- 10并发持续60.327秒，665次请求全部完成且Top10集合与精确oracle一致，索引阶段P95=1135.08ms。
- 在隔离索引实际注入、过滤、删除5000个旧版本切片和17500个过期切片，读回残留0，原有效索引量恢复。
- 28项数据／统计合同测试通过；从2265条实际请求样本独立重算P95和近邻Recall通过。

## 结果范围

全探针nprobe=256在本例热64／温128／冷256分桶上全部扫描，属于精确邻域基线；
不能称为已经优化出低成本近似搜索方案。
小nprobe在这组高维随机数据上漏召回明显，所以推荐以218ms作为保守的本地索引阶段成绩。

ANN邻域Recall衡量找回精确向量近邻的程度，不等于新闻相关性、事实理解或nDCG。
100组查询也不能改名为100条人工独立测试查询。
单并发100请求；部分探针500请求是同100查询重复5次，不能增加独立样本量。

本轮不含查询Embedding、HTTP、租户鉴权、PG台账、Worker、网络和生产ANN服务。
665/665是“返回10篇并与精确Top10向量集合一致”的本地完成率，不是企业业务回答100%正确。
物理删除探针使用构造的越界ID和静态有效成员gate，未验证真实语义替代、生产TTL调度及跨副本竞态。

## 环境和配置

- WSL/Linux、Python3.11；本次CPU为Ryzen 5 7600，进程RSS峰值约7.46GiB。
- numpy==2.2.0、faiss-cpu==1.12.0、packaging==26.3，锁定文件deploy/requirements.capacity-benchmark.txt。
- 训练/添加4线程；每个查询worker一个Faiss线程，三层顺序搜索、新闻去重融合。
- 每层分桶64/128/256；每簇64训练样本，训练10轮，种子20261028。
- 当前参数在观察结果前写入run-v1/config.json；没有按期望成绩选择“最佳结果”。
- 原数据约4.82GiB，新增索引约3.49GiB；复制模型或另一套全量索引会额外占用资源。
- 本工具依赖Linux resource统计，Windows电脑请在WSL执行。标注页面的Windows原生支持不等于本基准也原生支持。

Faiss参数和索引方法依据：
[索引类型](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)、
[索引IO与参数](https://github.com/facebookresearch/faiss/wiki/Index-IO%2C-cloning-and-hyper-parameter-tuning)。
官方说明只支持方法解释，不是对本项目企业成绩的背书。

## 跨电脑复现

先按annual-capacity-20261027.md重建被Git忽略的年度数据。使用独立Python环境：

```bash
python -m pip install -r deploy/requirements.capacity-benchmark.txt

# 输出和索引目录必须全新，不能覆盖已冻结成绩
python -B agent/tools/benchmark_annual_capacity.py \
  --index-dir var/news-capacity-20261027/ivf-local-replay \
  --output-dir output/annual-capacity-benchmark-20261028/local-replay

python -B agent/tools/soak_annual_capacity.py \
  --run-dir output/annual-capacity-benchmark-20261028/local-replay \
  --index-dir var/news-capacity-20261027/ivf-local-replay \
  --output-dir output/annual-capacity-benchmark-20261028/local-soak \
  --concurrency 10 --duration 60
```

若Faiss以pip --target安装在独立目录，使用--faiss-path指定；正常venv安装可直接import。
基准额外支持--dataset、--queries、--requests、--threads；
持续测试额外支持--faiss-path，限定并发1～16和持续10～600秒。
若要改变nlist/nprobe等参数，另建代码／配置版本；不能覆盖原配置或只挑最快结果。

## 完整调用链与产物

上游：annual容量manifest、3个真实落盘的合成向量数组。
benchmark_annual_capacity CLI冻结配置 → exact_top_news分块计算新闻级精确oracle →
Faiss IndexIVFFlat train/add_with_ids/write_index →
AnnualANN.search三层检索 → fuse_candidates有效成员过滤和新闻去重 →
measure_case预热与闭环请求 → R7百分位、邻居Recall →
lifecycle_probe注入旧/过期ID、gate、remove_ids和倒排表读回。
soak_annual_capacity重新加载并验证索引SHA → 60秒闭环worker →
逐请求结果 → NumPy quantile独立复核 → 汇总报告和简历可用表述。

大索引保存在var/news-capacity-20261027/，被Git忽略；测试参数和结果、查询向量、oracle和请求日志体积较小，可随Git迁移。
runtime依赖单独安装，无企业密钥或接口，不读取.env，不写原300人工标签与补标库。

产物：
- run-v1/config.json、report.json、requests.jsonl、query-vectors.npy、exact-news-top10.npy。
- soak-v1/config.json、report.json、requests.jsonl。
- validation.json、report.md、resume-ready.md。

测试：

```bash
cd agent
python -B -m unittest tests.test_annual_capacity tests.test_annual_capacity_benchmark -q
```

覆盖7/30/365边界、合成数据生成、过期/替代过滤、去重、稳定排序、非有限值拒绝、
分块精确oracle与逐文档朴素参考一致、真实Recall分母及R7分位数。

## 仍未完成

新闻nDCG前后A/B仍需要新增154项人工标签及合适的独立业务测试集；
本次不能补造这些数字。
生产365天TTL Worker、真实PG门禁和企业向量库性能仍需独立接线及验收。
写作错误率、人工修订耗时、Hunyuan LoRA提升不在本轮测试范围。
