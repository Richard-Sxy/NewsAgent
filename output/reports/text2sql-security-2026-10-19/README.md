# NewsAgent Text2SQL 测试报告阅读说明

建议先用浏览器打开 `report.html`。正文和结果图都在该HTML内，无需联网或安装依赖。

- `report.md` 是可编辑源稿，内容与HTML一致。
- `assets/overview.png` 是结果图独立文件。
- `evidence/` 包含原实验的逐例结果、运行规格、命令、测试日志、runner和合成用例副本。
- `report-facts.json` 记录报告整理范围及当前代码相对测量快照的变化。
- `report-validation.json` 记录计数、链接与图片嵌入校验。
- `package-manifest.json` 记录交付文件的SHA-256和字节长度，不包含它自身。

转发时可只发送HTML供阅读；需要审计或复现说明时，请发送完整ZIP并保持解压目录结构。
原始SQLShield CSV/JSONL与整份业务源码没有包含在包内，独立复现实验需另行准备；报告第12章提供说明。

报告基于2026年10月19日已冻结的离线实验，不是新一次模型或数据库测试，也不是生产发布批准。
真实模型ASR、PostgreSQL权限、正常查询结果正确率与生产性能仍未测量。

若需校验完整性，在该目录运行：

```python
import hashlib, json
from pathlib import Path
manifest = json.loads(Path('package-manifest.json').read_text())
for row in manifest['files']:
    path = Path(row['path'])
    assert path.stat().st_size == row['bytes']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
print('全部文件摘要匹配')
```

`build_report.py` 只重新构建报告和ZIP，不调用模型或数据库。它复用已保存的证据文件与源稿，不会修复生产SQL护栏。
