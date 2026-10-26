# Hunyuan 7B Instruct：WSL 独立部署

工作目录为 Ubuntu-20.04 /home/shi/project/NewsAgent。本部署直接运行在 WSL Linux，
使用 RTX 4070 12GB；权重、Python 环境和运行日志均位于 WSL 文件系统。
它是独立的本地真实模型实验服务，不自动切换现有 NewsAgent API/Worker 的模型路由。

## 资产位置

| 资产 | WSL 路径 |
| --- | --- |
| 原始模型权重 | /home/shi/project/NewsAgent/small_model/models/Hunyuan-7B-Instruct |
| Python 虚拟环境 | /home/shi/project/NewsAgent/small_model/environments/hunyuan |
| 部署与运行代码 | /home/shi/project/NewsAgent/small_model/scripts |
| 日志、运行回执和验收报告 | /home/shi/project/NewsAgent/small_model/runtime/hunyuan |
| 私有 Python 3.11 解释器及安装缓存 | /home/shi/project/NewsAgent/small_model/runtime/hunyuan/python、uv-cache |

模型为官方 [tencent/Hunyuan-7B-Instruct](https://huggingface.co/tencent/Hunyuan-7B-Instruct)，
固定 revision：6fd6ecb05e76589bc43b79f49e3619445c6b4593。
从 Windows E:\NewsAgent\var\models\Hunyuan-7B-Instruct 复制到 WSL，保留原始 BF16
权重及许可证；不移动或删除 Windows 原件。16 个文件使用既有固定下载清单逐项复核，
包括约 15.01 GB 的四个 safetensors 权重文件。原始权重不会被 NF4 加载或 LoRA 覆写。

本地接口模型名为 hunyuan-7b-instruct-6fd6ecb-nf4。原 Windows 副本的 `.venv-hunyuan` 不能作为
Linux 虚拟环境使用，本部署另建 Python 3.11.14、PyTorch 2.9.1+cu128、
Transformers 4.57.6、bitsandbytes 0.49.2、PEFT 0.18.1、Accelerate 1.12.0 的独立环境。
顶层依赖固定在 requirements.txt，官方 Linux PyTorch wheel 及 SHA-256 固定在
torch-cu128-linux.txt，实际传递依赖记录在 small_model/runtime/hunyuan/installed-packages.txt。

## 日常启动、检查和停止

在 WSL 终端中执行：

~~~bash
cd /home/shi/project/NewsAgent

# 启动后台服务；输出 ready 才表示权重已加载。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py start

# 查询进程和模型健康状态。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py status

# 停止服务，释放模型占用的显存。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py stop
~~~

状态 starting 表示仍在加载，不是部署成功；not_running 表示进程不存在或回执已失效。
日志路径由命令输出及 small_model/runtime/hunyuan/wsl-service.json 给出。进程管理检查 UID、PID、
进程启动时钟和完整命令行，旧 PID 不会用于终止其他进程。
服务没有配置开机自动启动；WSL 关闭或系统重启后，重新执行 start。

默认端口为 8008，可用 start --port 8009 指定其他空闲端口。已有同一服务运行时不启动
第二份；端口被其他进程占用则失败，不关闭其他服务。

接口：

- GET http://127.0.0.1:8008/health
- GET http://127.0.0.1:8008/v1/models
- POST http://127.0.0.1:8008/v1/chat/completions

健康接口同时返回 platform、model_path、revision，便于确认请求实际到达 WSL 模型。
这是仅绑定 WSL 回环地址的本地服务，不开放局域网，不提供生产鉴权。
Windows 是否能通过同一 localhost 地址访问，以本次实际验证报告为准；
Docker 中的 127.0.0.1 指向容器自身，不能直接作为业务容器的接入地址。

请求示例：

~~~bash
curl --noproxy '*' http://127.0.0.1:8008/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hunyuan-7b-instruct-6fd6ecb-nf4","messages":[{"role":"user","content":"用一句话解释为什么新闻分析需要引用证据。"}],"max_tokens":128,"temperature":0,"stream":false}'
~~~

## 可重复安装与权重导入

已有环境无需每次重新安装。setup.sh 不修改系统 Python，不在 Agent 的依赖环境中安装
训练库，不读取 .env，也不安装或替换 GPU 驱动。

~~~bash
cd /home/shi/project/NewsAgent
bash small_model/scripts/setup.sh

# 仅在目标 WSL 权重尚未齐备时导入；已存在且哈希正确的文件会复用。
python3 small_model/scripts/assets.py \
  --import-from /mnt/e/NewsAgent/var/models/Hunyuan-7B-Instruct

# 独立复核所有权重与配置文件的 SHA-256。
python3 small_model/scripts/assets.py
~~~

setup.sh → bootstrap_uv.py 从 PyPI 下载 uv 0.9.2 并校验官方元数据中的 wheel SHA-256
→ 安装私有 Python 3.11.14 → 创建 small_model/environments/hunyuan → 从官方固定 URL 安装 CUDA PyTorch
→ 从 PyPI 安装其余锁定的顶层依赖 → pip check → 保存实际版本。
PyTorch 的依赖从 PyPI 解析，避免把所有依赖都限制到 PyTorch 索引及其 CDN 重定向链。

依赖下载可使用当前进程的 HTTPS_PROXY/HTTP_PROXY；本次使用用户机器已有代理，
没有修改系统代理或 WSL 网络配置。网关地址可能在 WSL 重启后变化，不写死进脚本。
模型运行时设置离线模式，权重加载使用 local_files_only=True、trust_remote_code=False。

assets.import_assets() 绑定固定清单哈希 → 核对源文件尺寸 → 分块复制并计算 SHA-256
→ 发布文件及原始清单 → assets.verify() 回读核验。目标文件不匹配时拒绝覆盖；
中断留下的 .importing 文件应先人工检查，脚本不会自动删除来源或未知文件。

## 推理调用链与配置边界

manage.py start → 新建独立 Linux 进程 → serve.py / FastAPI lifespan →
runtime.load_model() → assets.verify() 逐文件哈希校验 → AutoTokenizer、
AutoModelForCausalLM → NF4 + double quantization、BF16 compute、CUDA:0、SDPA。

HTTP 请求 → ChatRequest 校验 → 版本校验和单并发锁 → runtime.fast_messages()
添加官方 /no_think 控制前缀 → 官方 chat template → 仅保留 input_ids/attention_mask
→ 输入/输出 token 预算校验 → model.generate() → 截取新增 token、处理官方包装
→ Chat Completions 响应、真实 usage、finish_reason、request ID。

输入加输出上限暂为 2048 tokens，默认输出 256、最大 1024，单并发。
这是本机实验预算，不是模型原生上下文长度。生成按步骤检查 max_time=90 的软期限，
不提供生产硬取消或任务队列。服务不支持 streaming、Embedding、工具执行、
强约束 JSON 或加载已训练 adapter；自然语言要求输出 JSON 不等于结构化解码保证。

模型未知返回 404；stream/强约束 JSON 或 token 预算不支持返回 400；
字段越界返回 422；GPU 忙或 OOM 返回 503。日志只记录请求编号、耗时、token 数、
终止原因或错误类型，不记录新闻正文、完整 Prompt 或回答。

接入 NewsAgent 时应保持现有 InferencePort → OpenAICompatibleInferenceClient →
JSON/Pydantic/业务 Validator 调用链，并在隔离配置中同时绑定 URL、模型版本、
Prompt 和 Bundle；本部署不修改业务路由。指标、热度和权威数字继续由 Python/SQL 计算。

## 验证入口与微调边界

~~~bash
cd /home/shi/project/NewsAgent

# 控制逻辑和资产校验测试，不加载模型。
small_model/environments/hunyuan/bin/python -B small_model/scripts/test_control.py

# 服务运行时，验证真实 HTTP 推理和错误边界。
small_model/environments/hunyuan/bin/python small_model/scripts/verify_api.py
# 自定义端口：上述命令追加 --port 8009。

# 先停止推理服务，再做单次 GPU 推理及短样本 QLoRA 反向传播检查。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py stop
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  small_model/environments/hunyuan/bin/python small_model/scripts/smoke.py --qlora-backward
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py start
~~~

smoke.py 使用合成问题 → 同一原始底模 NF4 加载 → 中文生成 → 可选 PEFT
prepare_model_for_kbit_training() / LoraConfig(r=8, alpha=16, q_proj/v_proj)
→ 冻结底模、仅 assistant token 计算 loss → backward → 检查有限且非零的梯度。
不执行 optimizer step，不保存 adapter，不使用待审核新闻集训练。

输出分别为 small_model/runtime/hunyuan/smoke-report.json、api-report.json；
本轮综合验收在完成实际检查后保存为 deployment-report.json。
这些合成检查只证明部署与短序列反向传播可用，不是新闻领域质量或长上下文训练验收。

TODO：完成库存样本审核与事件隔离、冻结训练/验证/测试版本、实现正式训练及 adapter
版本管理/加载、比较基座与 LoRA、业务隔离接线、长文本显存/吞吐与 OTel 验证。
正式训练前先停止推理服务，避免两份模型同时占用同一张显卡。
