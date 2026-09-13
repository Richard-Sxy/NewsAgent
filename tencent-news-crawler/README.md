# Tencent News Crawler

本项目用于自动发现腾讯新闻分类内容、抓取正文、使用 SQLite 去重，并将新闻导入 FastGPT 知识库。

> - 企业级知识库容量与架构推演（8000 篇/天 × 10 年）：
>   [`docs/knowledge-base-scale-design.md`](docs/knowledge-base-scale-design.md)
> - 向量入库链路（正文如何进入 FastGPT、metadata、分片、训练状态与检索衔接）：
>   [`docs/vector-ingest/README.md`](docs/vector-ingest/README.md)

## 运行环境

进入项目并激活虚拟环境：

```bash
cd /home/shi/project/NewsAgent/tencent-news-crawler
source .venv/bin/activate
```

首次使用时安装依赖：

```bash
python -m pip install -r requirements.txt
```

运行测试：

```bash
python -m pytest -q
```

## 环境变量

在项目根目录的 `.env` 中配置 FastGPT：

```env
FASTGPT_BASE_URL=http://127.0.0.1:3000
FASTGPT_API_KEY=你的知识库APIKey
FASTGPT_DATASET_ID=你的知识库ID
FASTGPT_TIMEOUT=60

INGEST_DB_PATH=data/news_ingest.db
ARTICLE_CACHE_DIR=data/articles

DAILY_LIMIT_PER_TOPIC=5
DAILY_MAX_PAGES=3
DAILY_TIMEZONE=Asia/Shanghai
DAILY_QA_LIMIT=20
INGEST_MAX_RETRY_COUNT=3
QA_MAX_RETRY_COUNT=3
```

试运行阶段建议每个分类限制为 5 篇。确认连续运行稳定后，再逐步提高数量。

## 本地标题分词与实体抽取

新闻导入链路支持使用哈工大 LTP 在本机完成标题分词、词性标注和实体抽取，当前输出三类实体：

- `person`：人名，对应 LTP 的 `Nh`。
- `organization`：企业或其他机构，对应 `Ni`，并使用企业词典修正简称和别名。
- `proper_noun`：其他专有名词，主要由 `nz` 词性以及英文大写词元补充。

模型只读取新闻标题，不执行标题中的任何指令。抽取结果写入 SQLite 的
`news_title_analyses` 和 `news_title_entities`，同时作为 `title_*` metadata 发送给
FastGPT，便于后续检索、事件聚类和实体热度统计。

首次安装和下载模型：

```bash
python -m pip install -r requirements-nlp.txt
python -m scripts.download_title_nlp_model
```

验证成功后在 `.env` 启用：

```env
TITLE_NLP_ENABLED=true
TITLE_NLP_MODEL=LTP/tiny
TITLE_NLP_CACHE_DIR=data/models/huggingface
TITLE_NLP_LOCAL_FILES_ONLY=true
TITLE_NLP_LEXICON_PATH=intelligence/extraction/title_entity_lexicon.json
TITLE_NLP_REQUIRED=false
```

默认使用约 35MB 的 `LTP/tiny`，CPU 即可运行。生产导入保持
`TITLE_NLP_LOCAL_FILES_ONLY=true`，避免任务运行期间临时联网下载。默认
`TITLE_NLP_REQUIRED=false` 表示 NLP 故障会被单独记录，但不阻断新闻正文和 FastGPT
入库；如果实体是下游必需字段，可改为 `true` 将其提升为数据质量硬门槛。

企业别名和行业专有词维护在
`intelligence/extraction/title_entity_lexicon.json`。通用模型无法可靠区分所有企业和普通
机构，因此生产上线前应从真实标题抽样标注，分别评测人名、企业和专有名词的精确率、
召回率，并持续把高频简称加入词典。

## 启动前检查

每日任务依赖 FastGPT 和 AI Proxy。执行任务前检查服务：

```bash
curl --noproxy "*" -I http://127.0.0.1:3000
curl --noproxy "*" -I http://127.0.0.1:3010/api/status
```

两个地址都应返回 HTTP 200。如果 FastGPT 未运行，入库会出现 `Connection refused`。

## 单篇新闻入库

```bash
python -m scripts.ingest_one \
  "https://news.qq.com/rain/a/文章ID"
```

## URL 文件批量入库

`urls.txt` 每行填写一个腾讯新闻 URL，然后执行：

```bash
python -m scripts.ingest_batch urls.txt
```

## 自动发现分类新闻

只发现新闻、不写数据库、不导入 FastGPT：

```bash
python -m scripts.discover_and_ingest \
  --source "科技=https://news.qq.com/ch/tech/" \
  --source "体育=https://news.qq.com/ch/sports/" \
  --date "2026-08-19" \
  --limit-per-topic 5 \
  --max-pages 3 \
  --dry-run
```

删除 `--dry-run` 即会执行真实入库。

命令换行必须使用反斜杠 `\`。字符 `>` 是 Shell 输出重定向符，不能用来代替换行。

## 每日任务

每日任务默认使用 `Asia/Shanghai` 当天日期，分类来源定义在 `scripts/ingest_daily.py` 的 `DEFAULT_SOURCES` 中。

安全预览：

```bash
python -m scripts.ingest_daily \
  --limit-per-topic 2 \
  --max-pages 2 \
  --dry-run
```

正式运行：

```bash
python -m scripts.ingest_daily \
  --limit-per-topic 2 \
  --max-pages 2
```

使用 `.env` 中的默认数量运行：

```bash
python -m scripts.ingest_daily
```

补跑指定日期：

```bash
python -m scripts.ingest_daily \
  --date "2026-08-19" \
  --limit-per-topic 5 \
  --max-pages 3
```

脚本退出码：

- `0`：所有分类和入库任务成功。
- `1`：存在分类失败、新闻入库失败或程序异常。

检查上一次退出码：

```bash
echo $?
```

实时列表可能在两次运行之间出现新文章。因此第二次运行不一定全部为 `skipped`；相同 URL 应为 `skipped`，新 URL 应为 `success`。

## 稳定性功能

目前已经完成：FastGPT 训练状态检查、失败重试次数限制、新闻正文缓存、
每日统计报告和自动评测。每日脚本执行顺序为：

```text
新闻抓取与入库 → QA 生成 → 自动评测 → 每日报告
```

自动评测默认关闭，需要时在 `.env` 设置：

```env
DAILY_EVALUATION_ENABLED=true
DAILY_EVALUATION_LIMIT=20
```

## 日志包装脚本

项目已经提供：

```text
scripts/run_daily.sh
```

手动执行：

```bash
./scripts/run_daily.sh
echo $?
```

日志按日期写入：

```text
logs/ingest-YYYY-MM-DD.log
```

查看当天日志：

```bash
tail -n 100 "logs/ingest-$(date +%F).log"
```

日志末尾的 `exit=0` 表示任务成功。

每日脚本执行完新闻入库和 QA 提交后，会自动生成两份统计报告：

```text
reports/daily-YYYY-MM-DD.json
reports/daily-YYYY-MM-DD.md
```

查看当天的人工可读报告：

```bash
cat "reports/daily-$(date +%F).md"
```

也可以不执行抓取，单独根据本地数据库重新生成报告：

```bash
python -m scripts.generate_daily_report --date "$(date +%F)"
```

报告统计的是 `publish_time` 属于目标日期的新闻，包括新闻状态、QA 提交状态、达到最大重试次数的记录和正文缓存文件总数。单独生成报告时没有任务退出码，因此报告的任务状态显示为 `unknown`，这是正常现象。

## 每日自动评测

在 `.env` 配置应用信息并开启自动评测：

```env
FASTGPT_APP_API_KEY=你的应用API密钥
FASTGPT_APP_ID=你的应用ID
DAILY_EVALUATION_ENABLED=true
DAILY_EVALUATION_LIMIT=20
EVALUATION_QUESTIONS_PATH=tests/evaluation_questions_v2.json
```

评测结果保存在 `reports/evaluation-YYYY-MM-DD.json`。手动试跑：

```bash
python -m scripts.evaluate_qa --limit 3 --output reports/evaluation-manual.json
```

## 检查 FastGPT 训练状态

分别检查原文知识库和 QA 知识库：

```bash
python -m scripts.check_training_status
echo $?
```

状态和退出码：

- `ready` / `0`：两个知识库均已训练完成。
- `training` / `2`：至少一个知识库仍在训练或重建。
- `error` / `1`：FastGPT 记录了训练错误。
- `failed` / `1`：接口连接、鉴权或响应格式异常。

使用日志包装脚本：

```bash
./scripts/run_training_check.sh
tail -n 100 "logs/training-$(date +%F).log"
```

训练是异步任务，不应在 QA 提交后立即判定失败。需要定时检查时，
可将检查任务安排在每日抓取任务之后，例如每天 21:30：

```cron
30 21 * * * flock -n /tmp/tencent-news-training.lock /home/shi/project/NewsAgent/tencent-news-crawler/scripts/run_training_check.sh
```

## 失败重试限制

原文入库和 QA 生成每次失败都会增加 `retry_count`。达到 `.env`
配置的上限后，自动任务不再请求该新闻：

```env
INGEST_MAX_RETRY_COUNT=3
QA_MAX_RETRY_COUNT=3
```

排查并修复外部问题后，可以人工重置一篇新闻的 QA 重试计数：

```bash
python -m scripts.reset_retry \
  "https://news.qq.com/rain/a/文章ID"
```

重置原文入库计数：

```bash
python -m scripts.reset_retry \
  --target ingest \
  "https://news.qq.com/rain/a/文章ID"
```

## 新闻正文缓存

新闻第一次抓取成功后会保存到：

```text
data/articles/<URL的SHA-256>.json
```

缓存包含 URL、标题、作者、发布时间和正文。原文入库与 QA 生成共用该
缓存，因此一篇新闻通常只请求腾讯一次。缓存文件不存在或JSON损坏时，
程序会重新抓取并原子替换缓存文件。

SQLite仍然负责记录任务状态，正文缓存只负责保存新闻内容；删除缓存不会
删除FastGPT知识库数据，但后续需要正文时会再次请求腾讯新闻。

## 配置 cron

检查 cron 服务：

```bash
systemctl status cron
```

如果未运行：

```bash
sudo systemctl enable --now cron
```

编辑当前用户的定时任务：

```bash
crontab -e
```

例如每天 20:30 执行：

```cron
30 20 * * * /home/shi/project/NewsAgent/tencent-news-crawler/scripts/run_daily.sh
```

确认配置：

```bash
crontab -l
```

首次配置时可以临时改为每 5 分钟执行：

```cron
*/5 * * * * /home/shi/project/NewsAgent/tencent-news-crawler/scripts/run_daily.sh
```

看到日志正常生成后，应立即改回每天一次，避免持续请求腾讯和 FastGPT。

## WSL 注意事项

WSL 内的 cron 只有在以下条件满足时才会执行：

- Windows 处于开机状态且没有休眠。
- WSL 发行版正在运行。
- cron 服务正在运行。
- FastGPT、AI Proxy 和相关数据库服务正在运行。

如果 WSL 经常停止，应改用 Windows 任务计划程序调用 `wsl.exe`，由 Windows 定时启动 WSL 中的 `run_daily.sh`。

## 常见问题

### `Connection refused`

FastGPT 或 AI Proxy 没有启动。先检查 3000 和 3010 端口。

### `unrecognized arguments`

检查是否误用 `>` 代替 `\`。推荐先将命令写成一行执行。

### `status: partial_failed`

查看 `sources` 中是否有失败分类，再查看 `ingest.failed` 和每条新闻的 `error`。

### 重复运行是否会重复入库

不会。SQLite 根据 URL 去重，已成功入库的 URL 返回 `skipped`；实时列表新增 URL 仍会正常入库。

### FastGPT 返回 `insertLen: 0`

集合已经创建，但分片训练通常异步执行。应以 FastGPT 集合状态、训练队列和知识库检索结果为准。
