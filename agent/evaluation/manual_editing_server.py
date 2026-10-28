"""Local human editing stopwatch with durable events; no model or automatic approval.

Task package: schema_version=1, experiment_id, gold_sha256, criterion_version,
criterion_text, data_origin=human_recorded|synthetic_test, tasks=[{blind_id,
case_id,event_group_id,arm,source_run_id,title,draft_content,
evidence:[{title,content,url}]}].
From agent/: python -m evaluation.manual_editing_server --package FILE
--state-dir NEW_OR_MATCHING_DIR --port 8881. --export NEW.json exports and exits.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import uuid
from urllib.parse import urlsplit

from evaluation.editing_time import make_template

MAX_BODY = 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def text(value, name, maximum=500000):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            name + ": nonempty bounded text required")
    return value


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    require(value.tzinfo is not None, "clock timezone required")
    return value.astimezone(timezone.utc).isoformat()


def parsed(value):
    return datetime.fromisoformat(value)


def sha(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_new(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class EditingStore:
    """Fixed package, append-only task snapshots, and human-pending exports."""

    def __init__(self, package_file, state_dir, clock=now, recover_running=True):
        self.clock, self.directory, self.lock = clock, Path(state_dir), threading.RLock()
        raw = Path(package_file).read_bytes()
        self.package = json.loads(raw)
        self.package_hash = hashlib.sha256(raw).hexdigest()
        p = self.package
        require(p.get("schema_version") == 1, "unsupported task package")
        require(p.get("data_origin") in ("human_recorded", "synthetic_test"), "data_origin required")
        require(isinstance(p.get("tasks"), list) and p["tasks"], "tasks required")
        self.tasks, cases, arms = {}, {}, set()
        for task in p["tasks"]:
            blind = text(task.get("blind_id"), "blind_id", 80)
            require(ID.fullmatch(blind) is not None and blind not in self.tasks, "invalid/duplicate blind_id")
            case = text(task.get("case_id"), "case_id", 120)
            group = text(task.get("event_group_id"), "event_group_id", 120)
            require(case not in cases or cases[case] == group, "case crosses event groups")
            cases[case] = group
            arm = task.get("arm")
            require(arm in ("baseline", "candidate") and (case, arm) not in arms, "invalid/duplicate arm")
            arms.add((case, arm))
            for key in ("source_run_id", "title", "draft_content"):
                text(task.get(key), key)
            require(isinstance(task.get("evidence"), list) and task["evidence"], "evidence required")
            for item in task["evidence"]:
                for key in ("title", "content"):
                    text(item.get(key), "evidence." + key)
                require(isinstance(item.get("url", ""), str), "evidence.url must be text")
            self.tasks[blind] = task
        require(arms == {(case, arm) for case in cases for arm in ("baseline", "candidate")},
                "each case requires both arms")
        self.form = make_template(
            {"status": p.get("gold_status"), "cases": [
                {"case_id": case, "event_group_id": group} for case, group in cases.items()]},
            experiment_id=p.get("experiment_id"), criterion_version=p.get("criterion_version"),
            criterion_text=p.get("criterion_text"), gold_sha256=p.get("gold_sha256"))
        self.states = {blind: {"status": "not_started", "revision": 0, "editor": None,
            "order_index": None, "started_at": None, "finished_at": None, "pauses": [],
            "content": task["draft_content"], "notes": "", "updated_at": None}
            for blind, task in self.tasks.items()}
        manifest = self.directory / "manifest.json"
        if self.directory.exists():
            require(manifest.is_file(), "existing directory is not an editing session")
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            require(saved["package_sha256"] == self.package_hash, "package changed; use new state directory")
        else:
            self.directory.mkdir(parents=True)
            (self.directory / "events").mkdir()
            (self.directory / "drafts").mkdir()
            write_new(manifest, {"schema_version": 1, "package_sha256": self.package_hash,
                "created_at": stamp(clock()), "data_origin": p["data_origin"]})
            with (self.directory / "package.json").open("xb") as stream:
                stream.write(raw)
        events = sorted((self.directory / "events").glob("*.json"))
        self.sequence = 0
        for path in events:
            self.sequence += 1
            require(path.name == f"{self.sequence:08d}.json", "event sequence is incomplete")
            event = json.loads(path.read_text(encoding="utf-8"))
            require(event["blind_id"] in self.states and event["sequence"] == self.sequence,
                    "event identity mismatch")
            self.states[event["blind_id"]] = event["state"]
        for blind, state in list(self.states.items()):
            if state["status"] == "running" and recover_running:
                # Exclude unobserved downtime from the last saved event onward.
                state = deepcopy(state)
                state["pauses"].append({"started_at": state["updated_at"], "finished_at": None,
                                        "reason": "server_restart_gap"})
                state["status"] = "paused"
                self._record(blind, "recovery_pause", state, clock(),
                             "Timer paused from last saved event; user must explicitly resume.")
            if state["status"] == "finished" and recover_running:
                self._write_final(blind, state)

    def _record(self, blind, action, state, at, note=""):
        next_number = self.sequence + 1
        state["revision"] += 1
        state["updated_at"] = stamp(at)
        event = {"sequence": next_number, "blind_id": blind, "action": action,
                 "at": stamp(at), "note": note, "state": state}
        target = self.directory / "events" / f"{next_number:08d}.json"
        temporary = target.with_suffix(".tmp")
        if temporary.exists():
            temporary.rename(temporary.with_name(temporary.name + ".orphan-" + uuid.uuid4().hex))
        write_new(temporary, event)
        require(not target.exists(), "event already exists")
        os.replace(temporary, target)
        self.sequence, self.states[blind] = next_number, state

    def _write_final(self, blind, state):
        target = self.directory / "drafts" / (blind + ".md")
        raw = state["content"].encode("utf-8")
        if target.exists():
            require(target.read_bytes() == raw, "finished artifact differs from event")
        else:
            with target.open("xb") as stream:
                stream.write(raw)

    def public(self, blind):
        require(blind in self.tasks, "unknown task")
        task, state = self.tasks[blind], self.states[blind]
        view = {"blind_id": blind, "status": state["status"], "revision": state["revision"],
                "editor": state["editor"], "order_index": state["order_index"],
                "notes": state["notes"], "criterion_text": self.package["criterion_text"]}
        if state["started_at"] is not None:
            finish = parsed(state["finished_at"]) if state["finished_at"] else self.clock()
            elapsed = max(0, (finish - parsed(state["started_at"])).total_seconds())
            paused = sum(max(0, ((parsed(item["finished_at"]) if item["finished_at"] else finish)
                                 - parsed(item["started_at"])).total_seconds()) for item in state["pauses"])
            view.update(title=task["title"], content=state["content"], evidence=task["evidence"],
                        active_seconds=max(0, elapsed - paused), started_at=state["started_at"],
                        finished_at=state["finished_at"])
        return view

    def listing(self):
        with self.lock:
            return {"experiment_id": self.package["experiment_id"],
                    "criterion_text": self.package["criterion_text"],
                    "data_origin": self.package["data_origin"],
                    "tasks": [{"blind_id": b, "status": s["status"]} for b, s in self.states.items()]}

    def _require_editor_available(self, blind, editor):
        require(not any(other != blind and state["status"] == "running"
                        and (state["editor"] or "").strip() == editor.strip()
                        for other, state in self.states.items()),
                "pause or finish the editor's other running task first")

    def action(self, blind, action, body):
        with self.lock:
            require(blind in self.states, "unknown task")
            require(isinstance(body, dict) and set(body) <=
                    {"expected_revision", "editor", "content", "notes"}, "unsupported fields")
            state = deepcopy(self.states[blind])
            require(type(body.get("expected_revision")) is int and
                    body["expected_revision"] == state["revision"], "revision conflict; reload this task")
            at = self.clock()
            if state["updated_at"]:
                require(at >= parsed(state["updated_at"]), "clock moved backwards")
            status = state["status"]
            if action == "start":
                require(status == "not_started", "task already started")
                editor = text(body.get("editor"), "editor", 100).strip()
                self._require_editor_available(blind, editor)
                order = max([s["order_index"] or 0 for s in self.states.values()
                             if s["editor"] == editor] + [0]) + 1
                state.update(status="running", editor=editor, order_index=order, started_at=stamp(at))
            elif action in ("save", "pause", "finish"):
                require(status == "running", "resume the timer before editing or finishing")
                state["content"] = text(body.get("content"), "content")
                note = body.get("notes", "")
                require(isinstance(note, str) and len(note) <= 5000, "notes too long")
                state["notes"] = note
                if action == "pause":
                    state["pauses"].append({"started_at": stamp(at), "finished_at": None, "reason": "user"})
                    state["status"] = "paused"
                elif action == "finish":
                    state.update(status="finished", finished_at=stamp(at))
            elif action == "resume":
                require(status == "paused", "task is not paused")
                self._require_editor_available(blind, state["editor"])
                require(state["pauses"] and state["pauses"][-1]["finished_at"] is None, "pause missing")
                state["pauses"][-1]["finished_at"] = stamp(at)
                state["status"] = "running"
            else:
                raise ValueError("unknown action")
            self._record(blind, action, state, at)
            if action == "finish":
                self._write_final(blind, state)
            return self.public(blind)

    def export(self):
        with self.lock:
            pack = deepcopy(self.form)
            pack["data_origin"] = self.package["data_origin"]
            pack["package_sha256"] = self.package_hash
            lookup = {(r["case_id"], r["arm"]): r for r in pack["records"]}
            for blind, task in self.tasks.items():
                state = self.states[blind]
                row = lookup[task["case_id"], task["arm"]]
                row.update(blind_id=blind, source_run_id=task["source_run_id"],
                    draft_sha256=sha(task["draft_content"]), editor=state["editor"],
                    order_index=state["order_index"], started_at=state["started_at"],
                    finished_at=state["finished_at"], notes=state["notes"])
                if state["status"] == "finished":
                    row["edited_sha256"] = sha(state["content"])
                    row["paused_seconds"] = sum((parsed(p["finished_at"]) - parsed(p["started_at"])).total_seconds()
                                                for p in state["pauses"])
            pack["timing_note"] = ("Server timestamps and saved events; no automatic quality approval. "
                                   "This does not independently prove actual human attention or editing.")
            return pack


HTML = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>稿件修订与真实计时</title>
<style>body{font:16px system-ui;margin:24px;max-width:1200px;background:#f5f7fb;color:#182333}
button,select,input{font:inherit;padding:8px;margin:4px}textarea{width:98%;min-height:350px;font:16px/1.65 system-ui}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px;border-radius:8px}
section{margin:20px 0}#status{font-weight:bold}#error{color:#b21c28}small{display:block}</style>
<h1>稿件修订与真实计时</h1><p>计时包括读稿、核对材料和编辑。开始后展示稿件；离开任务请暂停，刷新前保存。切换任务前先暂停或完成原任务，同一编辑者不能同时计时两篇。
完成只记录时长和终稿，质量仍需人工审核。</p>
<select id="tasks"></select><label>编辑者 <input id="editor" placeholder="填写你的标注名" maxlength="100"></label>
<button id="start">开始计时</button><button id="pause">保存并暂停</button><button id="resume">继续计时</button>
<button id="save">保存草稿</button><button id="finish">结束计时并保存终稿</button>
<a href="/api/export" download="editing-records.pending.json">下载待审核记录</a>
<p id="status"></p><p id="error"></p><small id="criterion"></small>
<section><h2 id="title">开始后展示稿件</h2><textarea id="content" disabled></textarea>
<label>备注<textarea id="notes" style="min-height:70px" disabled></textarea></label></section>
<section><h2>给定材料</h2><div id="evidence"></div></section><script src="/app.js"></script></html>"""

JS = r"""'use strict';
let state=null,dirty=false,busy=false;
const el=id=>document.getElementById(id);
function render(s){state=s;el('editor').value=s.editor||el('editor').value;
el('status').textContent=s.blind_id+' · '+s.status+' · 有效编辑 '+((s.active_seconds||0)/60).toFixed(2)+' 分钟';
el('criterion').textContent=s.criterion_text;el('title').textContent=s.title||'开始后展示稿件';
el('content').value=s.content||'';el('notes').value=s.notes||'';el('evidence').replaceChildren();
for(const e of s.evidence||[]){const heading=document.createElement('h3');heading.textContent=e.title;
const body=document.createElement('pre');body.textContent=e.content;
const url=document.createElement('small');url.textContent=e.url||'';el('evidence').append(heading,body,url);}
controls();}
function controls(){const running=state&&state.status==='running';
for(const id of ['pause','save','finish'])el(id).disabled=busy||!running;
el('start').disabled=busy||!state||state.status!=='not_started';
el('resume').disabled=busy||!state||state.status!=='paused';
el('content').disabled=busy||!running;el('notes').disabled=busy||!running;
el('editor').disabled=busy||!state||state.status!=='not_started';el('tasks').disabled=busy;}
async function request(url,options){const r=await fetch(url,options);const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
async function load(){busy=true;controls();try{dirty=false;render(await request('/api/tasks/'+el('tasks').value));el('error').textContent='';}
catch(e){el('error').textContent=e.message;}finally{busy=false;controls();}}
async function act(action){if(busy||!state)return;const payload={expected_revision:state.revision};
if(action==='start')payload.editor=el('editor').value;
if(['save','pause','finish'].includes(action)){payload.content=el('content').value;payload.notes=el('notes').value;}
busy=true;controls();try{render(await request('/api/tasks/'+state.blind_id+'/'+action,{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));dirty=false;el('error').textContent='';
const old=el('tasks').value;const list=await request('/api/tasks');el('tasks').replaceChildren();
for(const t of list.tasks){const o=document.createElement('option');o.value=t.blind_id;o.textContent=t.blind_id+' · '+t.status;el('tasks').append(o);}
el('tasks').value=old;}catch(e){el('error').textContent=e.message+'；如版本冲突请先复制未保存正文，再重新选择任务。';}
finally{busy=false;controls();}}
for(const a of ['start','pause','resume','save','finish'])el(a).onclick=()=>act(a);
el('content').oninput=el('notes').oninput=()=>{dirty=true;el('error').textContent='有未保存修改；暂停/完成也会保存。';};
el('tasks').onchange=()=>{if(dirty&&!confirm('未保存修改将丢失，确定切换？')){el('tasks').value=state.blind_id;return;}load();};
addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
(async()=>{try{const list=await request('/api/tasks');for(const t of list.tasks){const o=document.createElement('option');
o.value=t.blind_id;o.textContent=t.blind_id+' · '+t.status;el('tasks').append(o);}await load();}
catch(e){el('error').textContent=e.message;}})();"""


def make_server(store, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def host_ok(self):
            host = self.headers.get("Host")
            return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def reply(self, code, payload, kind="application/json; charset=utf-8"):
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if kind.startswith("application/json") else payload.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {"error": "invalid host"})
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    return self.reply(200, HTML, "text/html; charset=utf-8")
                if path == "/app.js":
                    return self.reply(200, JS, "application/javascript; charset=utf-8")
                if path == "/api/tasks":
                    return self.reply(200, store.listing())
                if path == "/api/export":
                    return self.reply(200, store.export())
                if path.startswith("/api/tasks/") and ID.fullmatch(path[len("/api/tasks/"):]):
                    with store.lock:
                        return self.reply(200, store.public(path[len("/api/tasks/"):]))
                self.reply(404, {"error": "not found"})
            except (ValueError, KeyError):
                self.reply(400, {"error": "invalid task"})

        def do_POST(self):
            if not self.host_ok() or self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
                return self.reply(403, {"error": "same-origin request required"})
            try:
                require(self.headers.get("Content-Type", "").split(";")[0] == "application/json", "JSON required")
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= MAX_BODY, "body exceeds limit or empty")
                pieces = urlsplit(self.path).path.split("/")
                require(len(pieces) == 5 and pieces[1:3] == ["api", "tasks"] and ID.fullmatch(pieces[3]), "invalid route")
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                self.reply(200, store.action(pieces[3], pieces[4], body))
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.reply(409, {"error": "操作或版本无效；刷新任务后重试。时间只由服务器记录。"})
            except OSError:
                self.reply(500, {"error": "保存失败；请保留页面正文并检查磁盘，不要反复提交。"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8881)
    parser.add_argument("--export", type=Path, help="Write a new pending records file and exit.")
    args = parser.parse_args(argv)
    require(1024 <= args.port <= 65535, "port must be in 1024..65535")
    store = EditingStore(args.package, args.state_dir, recover_running=not bool(args.export))
    if args.export:
        write_new(args.export, store.export())
    else:
        server = make_server(store, args.port)
        print(f"Manual editing: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
