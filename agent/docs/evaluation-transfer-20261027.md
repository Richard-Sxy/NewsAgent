# 今日评测：跨电脑拉取与运行

日期：2026-10-27。先阅读仓库根目录[今日TODO](../../今日TODO.md)。

## Git里已有的内容

- 今日任务、评测代码、测试和公开配置示例。
- 年度公开新闻冻结语料、真实BGE正文向量、30条查询及查询向量。
- 原300项单人标注快照、两组检索预测、154项新增候选和独立补标SQLite。
- 写作评测草稿、完整报告试点工具、Hunyuan查询理解开发题及评测器。

模型权重、虚拟环境、GPU驱动、私有接口配置和密钥不随Git迁移。
继续8879补标和对既有结果评分只需要Python应用环境，不需要GPU或BGE/Hunyuan权重。
重新生成查询或训练模型才需要在目标电脑单独准备模型和对应环境。

## 另一台电脑的最短操作

推荐在目标电脑WSL／Linux中执行。以实际克隆目录为准：

```bash
git clone https://github.com/Richard-Sxy/NewsAgent.git
cd NewsAgent
# 已有仓库时，先确认本地标注已备份，再 git pull --ff-only
python3.11 -m venv .venv-eval
.venv-eval/bin/python -m pip install -r deploy/requirements.retrieval-lab.lock.txt

# 检查冻结语料、向量、人工标签和两组排序；不调用模型
.venv-eval/bin/python -B agent/tools/verify_annual_retrieval_transfer.py

# 先运行合同测试
cd agent
../.venv-eval/bin/python -B -m unittest \
  tests.test_retrieval_comparison tests.test_retrieval_lab_store \
  tests.test_retrieval_lab_api -q
cd ..

# 继续新增候选人工标注
.venv-eval/bin/python -B agent/tools/compare_annual_retrieval.py serve --port 8879
```

打开 http://127.0.0.1:8879/ 。此进程保持运行；另一个终端用于后续命令。

补标完成后：

```bash
.venv-eval/bin/python -B agent/tools/compare_annual_retrieval.py score
```

结果为：
- `output/news-events-annual-20261027/comparison-v1/results.json`
- `output/news-events-annual-20261027/comparison-v1/report.md`

Windows原生Python也可继续补标／评分：创建Python3.11环境后，使用
`.venv-eval\\Scripts\\python.exe`替换上述解释器。模型训练和已有WSL控制脚本优先在WSL执行。

## 路径与冻结身份

旧config.json中的 `/home/shi/project/NewsAgent/...` 是执行时证据，保留原字节。
score优先按冻结export的SHA匹配当前工作区的human-review-v1，找不到才检查历史路径。
如果材料另放目录，可显式指定：

```bash
.venv-eval/bin/python -B agent/tools/compare_annual_retrieval.py score \
  --directory output/news-events-annual-20261027/comparison-v1 \
  --review-dir output/news-events-annual-20261027/human-review-v1
```

不能为了绕过错误而重写冻结配置、哈希或人工标签。
迁移验证器会列出相对原执行时发生变化的源码，同时独立用保存的向量复算预测。
旧verify_replay.py使用旧机器绝对路径和零补标前提；迁移后使用新的verify_annual_retrieval_transfer.py。

## 标注进度的搬运

同一实验建议一次只在一台电脑写标注。SQLite不能像文本那样合并两台电脑的冲突。
切换电脑前停止写操作，使用SQLite backup生成一致性备份，检查PRAGMA quick_check为ok，
再提交对应数据库；不要在写入过程中直接复制文件或提交可能依赖WAL的半份数据库。
目标电脑有新的本地标注时先备份，不能用git reset/checkout覆盖。
本次Git提交不包含任何自动生成的相关性标签；剩余数量以目标页面实际显示为准。

## 完整调用链、依赖和验收

上游git拉取代码／冻结语料／标注 → verify_annual_retrieval_transfer校验manifest、正文和向量哈希，
并独立复算旧RRF与新global排序 → ComparisonReviewService加载独立SQLite →
原create_app的HTTP接口和静态页 → PUT save_judgment保存人工修订和审计 →
score解析当前机器的review目录并核对export SHA → calculate_comparison →
results.json/report.md。缺失或无法判断的标签仍阻断总体分数，不能当作负例。

应用依赖由deploy/requirements.retrieval-lab.lock.txt固定，Python3.11+；
补标与评分不依赖外部API、模型权重或生产数据库。
入口参数：serve的--directory/--port，score的--directory/--review-dir，
迁移验证器的--root。不读取.env。
测试入口见上方命令，并包含目录迁移后仍匹配原冻结export、显式路径和错误哈希拒绝测试。

写作及Hunyuan评测需要另核对模型／接口和环境；按今日TODO推进。
没有企业接口时交付本地评测结果与缺口，不将其写成企业正式环境成绩。

## 本轮迁移验收

60项合同测试通过；独立校验30个查询排序、300项原标注与454项共同池。
另在全新临时目录中，仅复制代码与冻结材料，未复制模型权重，完成评分、30查询回放、HTTP保存及重开读回；假标签只写临时副本。
验证记录见 `output/news-events-annual-20261027/transfer-validation-20261027.json`。
使用当前评测Python运行同目录 `transfer-smoke-20261027.py` 可重复这次换目录演练。
