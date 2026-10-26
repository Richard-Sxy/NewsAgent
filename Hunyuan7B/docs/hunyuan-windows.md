# Hunyuan 7B Instruct 本地部署与 QLoRA 准备

本目录部署真实腾讯开源模型，独立于 NewsAgent 业务服务。目标是建立可复现的推理和
微调实验底座；不自动切换现有模型路由、Prompt、Production Bundle 或线上服务。

## 固定资产与资源

- 官方模型：[`tencent/Hunyuan-7B-Instruct`](https://huggingface.co/tencent/Hunyuan-7B-Instruct)。
- 固定 revision：`6fd6ecb05e76589bc43b79f49e3619445c6b4593`。
- 官方参考：<https://github.com/Tencent-Hunyuan/Hunyuan-7B>。
- 本机实测硬件：Windows、RTX 4070 12GB、32GB RAM。原始 BF16 权重约 15.01 GB，
  保留在 `small_model/models/Hunyuan-7B-Instruct/`，加载时使用 NF4 + double quantization。
- 独立环境：仓库根 `small_model/environments/hunyuan/`，Python 3.11；不向 Agent 的依赖中添加训练库。
- 依赖入口：`small_model/scripts/requirements.txt`；PyTorch 2.9.1 CUDA 12.8、Transformers
  4.57.6、bitsandbytes 0.49.2、PEFT 0.18.1。使用 Transformers 内置 Hunyuan 实现，
  `trust_remote_code=False`、本地权重加载，不运行模型仓库附带的任意 Python。
- 官方模型许可证和 Notice 随权重完整保留。量化不是改写原始 checkpoint。
- 本地接口模型名：`hunyuan-7b-instruct-6fd6ecb-nf4`。上下文实验预算为输入加输出 2048
  tokens，默认输出 256、最大输出 1024，单并发。它不代表模型原生上下文极限。

## 安装、启动、验证

在仓库根 PowerShell 执行：

```powershell
# 建立独立环境、安装依赖并下载/校验；可再次运行以恢复中断下载。
powershell -ExecutionPolicy Bypass -File .\small_model\scripts\setup.ps1
# 如需显式使用本机已有代理（端口需与自己的配置一致）：
# powershell -ExecutionPolicy Bypass -File .\small_model\scripts\setup.ps1 -Proxy http://127.0.0.1:7897

# 在服务没有占用显卡时进行真实中文推理 + 合成样本 LoRA backward 检查。
.\small_model\environments\hunyuan\Scripts\python.exe .\small_model\scripts\smoke.py --qlora-backward

# 前台启动，关闭此进程即释放模型；不自动开机启动。
powershell -ExecutionPolicy Bypass -File .\small_model\scripts\start.ps1

# 可选：后台运行；用对应脚本关闭并释放显存。
# powershell -ExecutionPolicy Bypass -File .\small_model\scripts\start.ps1 -Background
# powershell -ExecutionPolicy Bypass -File .\small_model\scripts\stop.ps1

# 另一个终端中检查真实 HTTP 接口和错误边界。
.\small_model\environments\hunyuan\Scripts\python.exe .\small_model\scripts\verify_api.py
```

接口：`http://127.0.0.1:8008/health`、`/v1/models`、`/v1/chat/completions`。
后台启动回执只表示进程已创建；必须等`/health`成功才表示模型已加载。后台日志和PID
位于`small_model/runtime/hunyuan/`，停止脚本核对脚本路径与PID以防误杀其他进程；没有注册开机任务。
`start.ps1 -Port 8009` 可调整端口；`verify_api.py` 当前固定验证默认端口 8008。
本地回环服务不设鉴权，不对局域网开放；不提供 Embedding、工具执行或流式生成。

示例请求：

```powershell
$body = @{
    model = 'hunyuan-7b-instruct-6fd6ecb-nf4'
    messages = @(@{ role = 'user'; content = '用一句话说明新闻运营分析应如何使用证据。' })
    max_tokens = 128
    temperature = 0
    stream = $false
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri 'http://127.0.0.1:8008/v1/chat/completions' -Method Post `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

## 完整调用链

### 下载与版本绑定

`setup.ps1` → Python 3.11 独立 venv → `download_torch.main` 从官方索引解析wheel地址与
SHA-256，直连分段下载并校验 → CUDA PyTorch 与锁定顶层依赖 → `download.main`
→ Hugging Face 官方固定 revision 的文件元数据 → 小文件固定版本下载 →
`ranged_download.download` 调用系统curl，从腾讯官方ModelScope镜像以8MiB网络请求读取，
每64MiB组成一个续传分片，最多同时下载三个文件、每文件八个分片
→ Git blob SHA-1 / LFS SHA-256 与长度校验 → 原子生成 `download-manifest.json`。

ModelScope 的 master 只作为传输源，接受条件是每个文件与固定 HF revision 哈希完全
相同；不同版本直接失败。用 `download.py --source huggingface` 可改为固定 HF 源。
curl沿用其环境代理配置；`--proxy http://127.0.0.1:7897` 可显式指定已有本机代理，
`--direct` 显式禁用curl代理，不修改系统网络设置；不要把凭证写进代理URL。
Windows系统代理与curl环境代理可能不同；本机的Python下载连接曾停滞，curl分段已验证可用。
只读HTTP分段每次请求至多重试三次；中断保留已完成分片，复跑继续，校验成功后才清理自己的
分片文件。校验失败保留文件诊断，不记录下载成功。权重和运行产物都被 Git 忽略。

### 推理服务

`start.ps1` / `serve.py --port 8008` → FastAPI lifespan → `runtime.load_model`
→ 校验下载清单 revision 与文件长度 → `AutoTokenizer` / `AutoModelForCausalLM`
→ bitsandbytes NF4、BF16 compute、CUDA:0、PyTorch SDPA → 模型驻留显存。

`POST /v1/chat/completions` → `ChatRequest` 字段/角色/字符预算校验 → 固定模型版本校验
→ 单并发锁 → `runtime.fast_messages` 为用户内容添加官方 `/no_think` 前缀 →
`runtime.encode` 的官方chat template（同时传`enable_thinking=False`；本revision模板
实际忽略此参数，所以不能只依赖它；只向模型传input_ids和attention_mask，过滤该
tokenizer额外产生但模型不接受的token_type_ids）→ 输入+输出token预算 → `runtime.generate`
→ 截取新增tokens、移除完整thinking段与answer包装
→ Chat Completions `choices/message/content`、真实 usage、finish_reason 和 request ID。
输出未遇 EOS 则标记 length，不能当作完整报告。未闭合 thinking 块失败关闭。

输入过长返回400，字段越界422，未知模型404，不支持的stream/强约束JSON返回400，
GPU忙或OOM返回503。服务日志仅记录request ID、耗时、token数、终止原因/错误类型，
不记录Prompt、新闻正文、回答或企业用户明细。生成 `max_time=90` 是按生成步检查的
软期限，不能代替生产进程隔离、硬取消或请求队列。

### 接入 NewsAgent 的现有边界

未来经评测后可沿用：会话/热点Workflow → `StructuredInferenceService` / `InferencePort`
→ `OpenAICompatibleInferenceClient.complete` → 本服务 → 原有 JSON解析、Pydantic
Schema、业务 Validator → 原有数据库/报告/界面。取数、SQL、热度和权威数字仍由
Python/SQL完成；模型生成不是数据访问或发布授权。

下列只是现有 YAML 的 **inference 配置片段**，不能单独作为完整运行配置：

```yaml
inference:
  provider: openai_compatible
  url: http://127.0.0.1:8008/v1/chat/completions
  api_key_env: HUNYUAN_LOCAL_API_KEY
  model_routes: [hunyuan-7b-instruct-6fd6ecb-nf4]
  timeout_seconds: 120
  response_format: none
  extra_body:
    max_tokens: 512
```

现有 Adapter 要求非空 api_key；本地可用进程变量 `HUNYUAN_LOCAL_API_KEY=local-only`，
这是兼容字段的占位值，本服务不将其视为鉴权。现有 prompts、scene profiles、Bundle
模型版本必须一并在隔离实验配置中绑定，不能只改 URL。Docker 容器到 Windows 回环
接口尚未验证，不能直接照抄127.0.0.1；生产认证、反向代理和网络部署另行配置。

这里没有实现强约束 JSON 解码，所以 `response_format` 必须为 `none`；JSON提示只是
模型行为，不提供结构保证。NewsAgent 原有 Schema/Validator 不得跳过。

### QLoRA 验证与数据路径

`smoke.py --qlora-backward` → 同一原始模型4-bit加载 → 合成中文推理 →
`prepare_model_for_kbit_training` 冻结底模并开启梯度检查点 →
`get_peft_model(LoraConfig(r=8, alpha=16, target_modules=[q_proj,v_proj]))`
→ 一个自建短合成问答按官方快思考模板编码 → 用户/系统部分 labels=-100，仅监督回答
→ loss/backward → 检查有限非零LoRA梯度和底模冻结 → `small_model/runtime/hunyuan/smoke-report.json`。

没有执行optimizer step，没有保存可用于业务的adapter，也没有开展正式训练。这项验证
只证明当前依赖、架构、显卡可以完成短序列QLoRA反向传播。不能据此承诺长上下文、
大batch、全部线性层或真实业务数据下不会OOM。后续从batch=1、短序列、梯度累积起测。

未来训练集入口应为已脱敏、经过审核的反馈/标签与版本化新闻证据，按事件主线隔离
train/validation/test，避免同事件泄漏。候选adapter需绑定底模revision、数据清单、
Prompt、超参和评测版本，再做当前生产/上一实验双基线比较及人工晋升。

## 测试与待完成项

- `smoke.py`：真实GPU中文推理；可选QLoRA前向/反向、有限梯度、冻结底模与显存峰值。
- `verify_api.py`：真实HTTP健康/模型列表/中文JSON生成，模型版本、request ID、usage、
  EOS终止与异常边界；输出 `small_model/runtime/hunyuan/api-report.json`。这是合成样本，不是质量评测。
- `download-manifest.json`：完整资产校验清单；依赖实装清单可用 `pip freeze` 保存到
  `small_model/runtime/hunyuan/installed-packages.txt`。每次升级依赖或底模均需重新验证。
- TODO：选定首个微调任务、审核训练集、训练脚本/训练评测切分、adapter版本台账与
  加载、业务离线评测、NewsAgent隔离集成和逐问题OTel贯通、长文本显存/吞吐验证。
- 本服务单独运行；没有启动或替换NewsAgent API/Worker，没有伪造业务Workflow或
  OTel Trace。请求链证据是本地API request ID及报告，不能冒充企业模型质量验收。

## 2026-10-22 本机验收结果

- 16个官方仓库文件的长度及Git blob/LFS哈希全部通过；四个权重文件合计
  15,009,178,056字节。清单位于模型目录的`download-manifest.json`。
- `pip check`通过；CUDA 12.8运行时识别RTX 4070，真实NF4算子可执行。
- `smoke.py --qlora-backward`通过：中文回答35个输出tokens、2.969秒；34-token
  合成训练样本loss=2.300493，3,407,872个可训练LoRA参数，梯度有限且非零、底模冻结。
  没有执行optimizer step或保存adapter。PyTorch CUDA allocator峰值6.297GiB，
  不包括桌面、驱动和非allocator显存占用，也不代表长序列训练需求。
- `verify_api.py`通过：健康、模型列表、中文JSON生成与五个失败边界；JSON生成
  45个输出tokens、3.422秒。请求ID为`chatcmpl-9fdc974d79c942899dfd6a0c0947f717`。
  完整合成测试证据见`small_model/runtime/hunyuan/smoke-report.json`、`api-report.json`。
- 服务使用`start.ps1 -Background`在`127.0.0.1:8008`启动；前述结果只覆盖本机
  部署与短样本验证，NewsAgent业务集成、正式微调和模型质量评测仍未完成。
- 后台停止→端口释放→重启→健康检查通过。当前交付状态见
  `small_model/runtime/hunyuan/deployment-report.json`；PID只用于本次进程，重启后会改变。
