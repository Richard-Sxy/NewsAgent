"""Build a portable report from existing frozen evidence; no model/DB calls."""

from __future__ import annotations

import base64
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell(value):
    return str(value).replace("|", " / ").replace("\n", " ")


def casebook_tables(casebook):
    words = {"allow": "允许", "block": "拒绝", "error": "异常"}
    sql = ["| 案例ID | 变异与检查意图 | 严格预期与实际 | 通用预期与实际 |", "| --- | --- | --- | --- |"]
    service = ["| 案例ID | 操作与保护机制 | 预期与实际 | 替身模型次数 | 攻击后数仓替身次数 |", "| --- | --- | --- | --- | --- |"]
    service_errors = ["| 案例ID | 真实结束阶段 | 错误类型与主要原因 |", "| --- | --- | --- |"]
    for case in casebook:
        if case["kind"] == "sql":
            by_suite = {row["suite"]: row for row in case["results"]}
            statuses = []
            for suite in ("assistant_sql_guard", "generic_sql_guard"):
                result = by_suite[suite]
                statuses.append(f"{words[result['expected']]} → {words[result['observed']]}，{'符合' if result['passed'] else '**失败**'}")
            sql.append(f"| `{case['case_id']}` | {cell(case['description_zh'])} {cell(case['mechanism_zh'])} | {statuses[0]} | {statuses[1]} |")
        else:
            result = case["results"][0]
            after = result["warehouse_stub_calls_after_attack"] if result["expected"] == "block" else "1（合法正例）"
            service.append(f"| `{case['case_id']}` | {cell(case['description_zh'])} {cell(case['mechanism_zh'])} | {words[result['expected']]} → {words[result['observed']]}，{'符合' if result['passed'] else '**失败**'} | {result['model_stub_calls']} | {after} |")
            end = next((stage for stage in reversed(result["stages"]) if stage["name"] in {"preview", "execute"}), {})
            error = result["error"]
            message = "正常完成；RecordingWarehouse与MemorySnapshots均为替身"
            if error:
                # Keep the actual Pydantic error and original diagnostic data in
                # casebook JSON; the reader table omits repetitive URL boilerplate.
                description = error["message"].split("For further information")[0].strip()
                message = error["type"] + "：" + description
            service_errors.append(f"| `{case['case_id']}` | {end.get('name','')} {end.get('status','')} | {cell(message)} |")
    return "\n".join(sql), "\n".join(service) + "\n\n### Service真实阻断阶段与错误\n\n" + "\n".join(service_errors)


def inline(value):
    tokens = []

    def protect(markup):
        tokens.append(markup)
        return f"@@REPORTTOKEN{len(tokens)-1}@@"

    def image_replace(match):
        label, target = match.groups()
        path = ROOT / target
        if path.is_file():
            uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        else:
            uri = target
        return protect(f'<img src="{html.escape(uri, quote=True)}" alt="{html.escape(label, quote=True)}" loading="lazy">')

    value = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_replace, value)
    value = re.sub(r"`([^`]+)`", lambda m: protect("<code>" + html.escape(m[1]) + "</code>"), value)
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: protect(f'<a href="{html.escape(m[2], quote=True)}">{html.escape(m[1])}</a>'), value)
    value = html.escape(value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    for index, token in enumerate(tokens):
        value = value.replace(f"@@REPORTTOKEN{index}@@", token)
    return value


def markdown_to_html(markdown):
    lines = markdown.splitlines()
    output, toc = [], []
    index = 0
    heading_index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            output.append(f'<pre aria-label="{html.escape(language)}"><code>' + html.escape("\n".join(code)) + "</code></pre>")
            index += 1
            continue
        if line.startswith("#"):
            match = re.match(r"^(#{1,6}) (.*)$", line)
            level, title = len(match[1]), match[2]
            heading_index += 1
            anchor = f"section-{heading_index}"
            output.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>')
            if level == 2:
                toc.append((anchor, title))
            index += 1
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                row = [part.strip() for part in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", value.replace(" ", "")) for value in row):
                    rows.append(row)
                index += 1
            result = ['<div class="table-wrap"><table><thead><tr>']
            result += ["<th>" + inline(value) + "</th>" for value in rows[0]]
            result.append("</tr></thead><tbody>")
            for row in rows[1:]:
                result.append("<tr>" + "".join("<td>" + inline(value) + "</td>" for value in row) + "</tr>")
            result.append("</tbody></table></div>")
            output.append("".join(result))
            continue
        if line.startswith("- ") or re.match(r"^\d+\. ", line):
            ordered = not line.startswith("- ")
            tag = "ol" if ordered else "ul"
            items = []
            while index < len(lines) and (re.match(r"^\d+\. ", lines[index]) if ordered else lines[index].startswith("- ")):
                value = re.sub(r"^\d+\. ", "", lines[index]) if ordered else lines[index][2:]
                items.append("<li>" + inline(value) + "</li>")
                index += 1
            output.append("<" + tag + ">" + "".join(items) + "</" + tag + ">")
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(r"^(#|\||```|- |\d+\. )", lines[index]):
            paragraph.append(lines[index])
            index += 1
        output.append("<p>" + inline(" ".join(paragraph)) + "</p>")
    nav = "".join(f'<a href="#{anchor}">{html.escape(title)}</a>' for anchor, title in toc)
    return nav, "\n".join(output)


CSS = """
:root{color-scheme:light;--ink:#162238;--muted:#5a697c;--line:#d9e1e9;--accent:#165c93}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:#f5f7fa;color:var(--ink);font:16px/1.9 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}
.layout{display:grid;grid-template-columns:255px minmax(0,1060px);gap:42px;max-width:1450px;padding:42px 40px 100px;margin:auto}
nav{position:sticky;top:24px;align-self:start;max-height:calc(100vh - 48px);overflow:auto;font-size:13px;line-height:1.55;padding:10px 12px 20px 0}
nav strong{display:block;color:#000;font-size:15px;margin-bottom:14px}nav a{display:block;text-decoration:none;color:#536376;padding:7px 0}nav a:hover{color:var(--accent)}
main{background:white;border:1px solid #e3e8ef;border-radius:12px;padding:45px 50px;min-width:0}
h1,h2,h3{color:#000;line-height:1.4;overflow-wrap:anywhere}h1{font-size:34px;margin:0 0 24px;font-weight:700}h2{font-size:25px;margin:46px 0 20px;padding-top:7px}h3{font-size:19px;margin:28px 0 12px}
p{margin:0 0 18px}a{color:var(--accent);text-underline-offset:3px}strong{font-weight:650}img{display:block;width:100%;height:auto;margin:28px 0 16px;border-radius:8px}
.table-wrap{max-width:100%;overflow-x:auto;margin:22px 0 26px}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.7;table-layout:auto}th,td{border:1px solid var(--line);padding:11px 12px;vertical-align:middle;overflow-wrap:anywhere}th{background:#e9f0f7;color:#000;font-weight:650;text-align:left}tbody tr:nth-child(even){background:#f8fafc}th:first-child,td:first-child{min-width:112px}td code{font-size:12px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.86em;overflow-wrap:anywhere;background:#f0f3f7;border-radius:3px;padding:1px 4px}pre{background:#f3f5f8;border:1px solid #e2e7ed;border-radius:7px;padding:18px 20px;line-height:1.7;overflow:auto;margin:20px 0 24px;font-size:13px}pre code{background:none;padding:0;white-space:pre;overflow-wrap:normal;font-size:inherit}
ul,ol{padding-left:24px;margin:14px 0 24px}li{margin:8px 0}.endnote{margin-top:42px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:18px}
@media(max-width:1100px){.layout{display:block;padding:20px 18px 60px}nav{position:static;max-height:none;margin-bottom:20px;columns:2}nav strong{column-span:all}main{padding:30px 24px}}
@media(max-width:600px){body{font-size:15px}nav{columns:1}h1{font-size:28px}h2{font-size:22px}main{padding:24px 18px}th,td{padding:9px;font-size:13px}}
@media print{body{background:white;font-size:10.5pt}.layout{display:block;padding:0}nav{display:none}main{border:0;border-radius:0;padding:0}h1{font-size:24pt}h2{break-after:avoid;font-size:17pt}h3{break-after:avoid}pre{white-space:pre-wrap;font-size:8pt}pre code{white-space:pre-wrap}.table-wrap{overflow:visible}thead{display:table-header-group}tr,img{break-inside:avoid}a{color:inherit}table{font-size:8.5pt}}
"""


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            if values["id"] in self.ids:
                raise ValueError("duplicate heading ID")
            self.ids.add(values["id"])
        if tag == "a":
            self.links.append(values.get("href", ""))
        elif tag == "img":
            self.images.append(values.get("src", ""))


def main():
    report = ROOT / "report.md"
    source_text = report.read_text(encoding="utf-8")
    prepared = ROOT / "evidence/detailed-casebook.json"
    if not prepared.exists():
        shutil.copyfile("/private/tmp/newsagent-text2sql-casebook.json", prepared)
    casebook = json.loads(prepared.read_text(encoding="utf-8"))
    assert len(casebook) == 37
    sql_table, service_table = casebook_tables(casebook)
    if "<!-- SQL_CASEBOOK -->" in source_text:
        source_text = source_text.replace("<!-- SQL_CASEBOOK -->", sql_table)
        source_text = source_text.replace("<!-- SERVICE_CASEBOOK -->", service_table)
        source_text += "\n\n## 附录D 可打开的原始证据文件\n\n"
        source_text += "以下链接在解压后的报告目录中可打开。HTML正文和结果图无需联网；查阅证据文件需保持evidence子目录。\n\n"
        source_text += "\n".join(f"- [{p.relative_to(ROOT)}]({p.relative_to(ROOT)})" for p in sorted((ROOT / "evidence").rglob("*")) if p.is_file()) + "\n"
        report.write_text(source_text, encoding="utf-8")
    assert "<!-- SQL_CASEBOOK -->" not in source_text and "<!-- SERVICE_CASEBOOK -->" not in source_text
    nav, body = markdown_to_html(source_text)
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="NewsAgent Text2SQL安全测试的范围、数据、指标、结果、失败案例、全部用例和复现说明">
<title>NewsAgent Text2SQL 安全测试与结果报告</title><style>{CSS}</style></head>
<body><div class="layout"><nav aria-label="目录"><strong>阅读目录</strong>{nav}</nav><main>{body}
<p class="endnote">报告基于2026年10月19日冻结实验。没有重新调用模型或数据库，没有生产发布批准。</p></main></div></body></html>'''
    (ROOT / "report.html").write_text(document, encoding="utf-8")
    parser = LinkParser()
    parser.feed(document)
    for link in parser.links:
        if link.startswith("#"):
            assert link[1:] in parser.ids, link
        elif not link.startswith(("http:", "https:")):
            assert (ROOT / link).is_file(), link
    assert len(parser.images) == 1 and parser.images[0].startswith("data:image/png;base64,")
    assert "<script" not in document.lower()
    assert len(re.findall(r"^\| `sql_|^\| `semantic_", source_text, re.M)) == 22
    # Service IDs appear once in the contract table and once in its error table.
    assert len(re.findall(r"^\| `service_", source_text, re.M)) == 30
    with (ROOT / "evidence/run-02/results.json").open() as stream:
        summary = json.load(stream)
    assert summary["completed_records"] == 1859
    checks = {"casebook_sql_cases": 22, "casebook_service_cases": 15, "original_measurement_rows": 1859,
              "html_internal_links_checked": True, "html_image_embedded": True, "html_scripts": 0,
              "heading_count": len(parser.ids), "external_and_internal_links": len(parser.links),
              "source_results_sha256": sha256(ROOT / "evidence/run-02/results.json"),
              "source_cases_sha256": sha256(ROOT / "evidence/run-02/cases.jsonl"),
              "markdown_chars": len(source_text), "no_new_model_or_database_calls": True}
    (ROOT / "report-validation.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"purpose": "Portable report and frozen evidence; no raw public dataset or enterprise data",
                "files": [{"path": str(p.relative_to(ROOT)), "bytes": p.stat().st_size, "sha256": sha256(p)}
                          for p in sorted(ROOT.rglob("*")) if p.is_file() and p.name != "package-manifest.json"]}
    (ROOT / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = ROOT.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(Path(ROOT.name) / path.relative_to(ROOT)))
    print(json.dumps({"html": str(ROOT / "report.html"), "markdown": str(report), "zip": str(archive), **checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
