"""Durable loopback batch evaluation; importing this module never loads GPU weights."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import gc
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from serve_writing_eval import MODEL_ID, REVISION, ROOT, Runtime, checked_request, unwrap

PROFILE = "batch-writing-v1"
CONTEXT_TOKENS, OUTPUT_TOKENS, MAX_BATCH = 16384, 1024, 4
MAX_BODY = 8 * 1024 * 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\Z")

class Conflict(ValueError):
    pass

class Busy(RuntimeError):
    pass

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def profile_identity(identity):
    return {**identity, "profile": PROFILE, "max_batch_size": MAX_BATCH,
            "context_tokens": CONTEXT_TOKENS, "max_output_tokens": OUTPUT_TOKENS,
            "thinking_mode": "disabled", "user_prefix": "/no_think ",
            "generation_max_time_seconds": 240,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "runtime_source_sha256": hashlib.sha256((SCRIPT_DIR / "serve_writing_eval.py").read_bytes()).hexdigest()}

def validate_batch(body):
    if not isinstance(body, dict) or set(body) != {"requests"}:
        raise ValueError("requests object required")
    requests = body["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH:
        raise ValueError("batch must contain one to four requests")
    seen, caps, checked = set(), set(), []
    allowed = {"request_id", "model", "messages", "max_tokens", "temperature", "stream", "response_format"}
    for request in requests:
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("unsupported request fields")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or request_id in seen:
            raise ValueError("invalid or duplicate request_id")
        messages, cap = checked_request(request, OUTPUT_TOKENS)
        seen.add(request_id)
        caps.add(cap)
        checked.append({"request_id": request_id, "messages": messages, "cap": cap,
                        "payload_sha256": hashlib.sha256(canonical(request)).hexdigest()})
    if len(caps) != 1:
        raise ValueError("all requests must use the same output cap")
    return checked

class RequestCache:
    """Durable ID-bound records; pending is never automatically replayed."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("invalid request_id")
        return self.directory / (request_id + ".json")

    def get(self, request_id, payload_sha256=None):
        with self.lock:
            try:
                value = json.loads(self.path(request_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            if payload_sha256 is not None and value["payload_sha256"] != payload_sha256:
                raise Conflict("request_id is bound to another payload")
            return value

    def _write(self, request_id, record):
        target = self.path(request_id)
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(canonical(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            if hasattr(os, "O_DIRECTORY"):
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def reserve(self, request):
        with self.lock:
            if self.get(request["request_id"], request["payload_sha256"]) is not None:
                return False
            self._write(request["request_id"], {
                "request_id": request["request_id"], "payload_sha256": request["payload_sha256"],
                "status": "pending", "started_at": timestamp(), "attempts": [],
                "response": None, "error": {"type": "pending_unknown", "message": "Do not replay this ID automatically."},
            })
            return True

    def attempt(self, requests, phase):
        with self.lock:
            at, identifiers = timestamp(), [request["request_id"] for request in requests]
            for request in requests:
                record = self.get(request["request_id"], request["payload_sha256"])
                if record["status"] != "pending":
                    raise RuntimeError("attempt requires pending request")
                record["attempts"].append({"at": at, "phase": phase, "batch_size": len(requests), "request_ids": identifiers})
                self._write(request["request_id"], record)

    def complete(self, request, result):
        with self.lock:
            record = self.get(request["request_id"], request["payload_sha256"])
            if record["status"] != "pending":
                raise RuntimeError("result already finalized")
            if result.get("status") not in ("completed", "failed"):
                raise ValueError("invalid terminal result")
            record.update(result)
            record["completed_at"] = timestamp()
            record["attempt_count"] = len(record["attempts"])
            self._write(request["request_id"], record)

def public_result(record, cache_hit):
    return {key: record.get(key) for key in (
        "request_id", "status", "response", "error", "attempts", "attempt_count", "started_at", "completed_at"
    )} | {"cache_hit": cache_hit}

def batch_groups(lengths):
    width = MAX_BATCH if max(lengths, default=0) <= 3500 else 2
    return [list(range(start, min(start + width, len(lengths)))) for start in range(0, len(lengths), width)]

def trim_at_eos(tokens, eos_ids):
    """Count the first actual EOS, excluding batch padding after it."""
    for position, token in enumerate(tokens):
        if token in eos_ids:
            return tokens[:position + 1], True
    return tokens, False

def completion_result(request_id, content, finish_reason, prompt_tokens, completion_tokens, error=None):
    response = {
        "id": "chatcmpl-" + request_id, "object": "chat.completion", "created": int(time.time()), "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }
    if error is None and finish_reason != "stop":
        error = {"type": "generation_length", "message": "Generation did not reach EOS within the cap/time limit."}
    if error is None and not content.strip():
        error = {"type": "empty_completion"}
    return {"status": "failed" if error else "completed", "response": response, "error": error}

class TorchBatchBackend:
    def __init__(self, runtime):
        self.runtime = runtime
        if runtime.identity.get("model") != MODEL_ID or runtime.identity.get("revision") != REVISION:
            raise ValueError("unapproved runtime model identity")
        if runtime.context_limit != CONTEXT_TOKENS or runtime.output_limit != OUTPUT_TOKENS:
            raise ValueError("runtime budget differs from frozen profile")
        self.identity = profile_identity(runtime.identity)
        runtime.tokenizer.padding_side = "left"
        if runtime.tokenizer.pad_token_id is None:
            runtime.tokenizer.pad_token = runtime.tokenizer.eos_token
        if type(runtime.tokenizer.pad_token_id) is not int or runtime.tokenizer.pad_token_id < 0:
            raise ValueError("tokenizer requires a valid pad token")
        eos = runtime.model.generation_config.eos_token_id
        eos = eos if isinstance(eos, (list, tuple)) else [eos]
        if not eos or any(type(token) is not int or token < 0 for token in eos):
            raise ValueError("model requires valid EOS token IDs")
        self.eos_ids = set(eos)

    def _generate(self, requests, texts, lengths, attempt, phase):
        runtime = self.runtime
        inputs = runtime.tokenizer(texts, padding=True, return_tensors="pt", add_special_tokens=False)
        inputs = {key: value.to("cuda:0") for key, value in inputs.items() if key in ("input_ids", "attention_mask")}
        padded_width = inputs["input_ids"].shape[-1]
        attempt(requests, phase)
        with runtime.torch.inference_mode():
            output = runtime.model.generate(
                **inputs, do_sample=False, temperature=1.0, top_p=1.0, top_k=50,
                max_new_tokens=requests[0]["cap"], max_time=240.0, pad_token_id=runtime.tokenizer.pad_token_id)
        results = []
        for index, request in enumerate(requests):
            tokens, stopped = trim_at_eos(output[index, padded_width:].tolist(), self.eos_ids)
            raw = runtime.tokenizer.decode(tokens, skip_special_tokens=True).strip()
            error = None
            try:
                content = unwrap(raw)
            except ValueError:
                content, error = raw, {"type": "unfinished_thinking"}
            results.append(completion_result(request["request_id"], content, "stop" if stopped else "length",
                                             lengths[index], len(tokens), error))
        return results

    def _is_oom(self, error):
        return isinstance(error, self.runtime.torch.cuda.OutOfMemoryError)

    def _clean_oom(self):
        gc.collect()
        self.runtime.torch.cuda.empty_cache()

    def generate_batch(self, requests, attempt, complete):
        tokenizer, prepared = self.runtime.tokenizer, []
        for request in requests:
            messages = [{**message, "content": "/no_think " + message["content"]}
                        if message["role"] == "user" else dict(message) for message in request["messages"]]
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            length = len(tokenizer(text, add_special_tokens=False)["input_ids"])
            if length + request["cap"] > CONTEXT_TOKENS:
                complete(request, {"status": "failed", "response": None, "error": {"type": "context_budget_exceeded"}})
            else:
                prepared.append((request, text, length))
        for indices in batch_groups([row[2] for row in prepared]):
            group, oom = [prepared[index] for index in indices], False
            try:
                results = self._generate([row[0] for row in group], [row[1] for row in group], [row[2] for row in group], attempt, "initial")
            except Exception as error:
                if not self._is_oom(error):
                    raise
                oom = True
            # Leave the exception scope before clearing cached CUDA allocations.
            if not oom:
                for row, result in zip(group, results):
                    complete(row[0], result)
                continue
            self._clean_oom()
            if len(group) <= 2:
                for row in group:
                    complete(row[0], {"status": "failed", "response": None, "error": {"type": "cuda_oom"}})
                continue
            for start in range(0, len(group), 2):
                smaller, second_oom = group[start:start + 2], False
                try:
                    results = self._generate([row[0] for row in smaller], [row[1] for row in smaller], [row[2] for row in smaller], attempt, "oom_fallback")
                except Exception as error:
                    if not self._is_oom(error):
                        raise
                    second_oom = True
                if second_oom:
                    self._clean_oom()
                    for row in smaller:
                        complete(row[0], {"status": "failed", "response": None, "error": {"type": "cuda_oom"}})
                else:
                    for row, result in zip(smaller, results):
                        complete(row[0], result)

class BatchService:
    def __init__(self, cache, backend):
        self.cache, self.backend = cache, backend
        self.gpu_lock = threading.Lock()

    def submit(self, body):
        requests = validate_batch(body)
        existing = [self.cache.get(request["request_id"], request["payload_sha256"]) for request in requests]
        if all(record is not None for record in existing):
            return {"profile": PROFILE, "results": [public_result(record, True) for record in existing]}
        if not self.gpu_lock.acquire(blocking=False):
            raise Busy("GPU generation is already running")
        newly_reserved = set()
        try:
            # Check every conflict before creating any new pending records.
            for request in requests:
                self.cache.get(request["request_id"], request["payload_sha256"])
            new = []
            for request in requests:
                if self.cache.reserve(request):
                    new.append(request)
                    newly_reserved.add(request["request_id"])
            if new:
                try:
                    self.backend.generate_batch(new, self.cache.attempt, self.cache.complete)
                    for request in new:
                        if self.cache.get(request["request_id"])["status"] == "pending":
                            self.cache.complete(request, {"status": "failed", "response": None, "error": {"type": "backend_result_missing"}})
                except Exception as error:
                    for request in new:
                        if self.cache.get(request["request_id"])["status"] == "pending":
                            self.cache.complete(request, {"status": "failed", "response": None, "error": {"type": type(error).__name__}})
        finally:
            self.gpu_lock.release()
        return {"profile": PROFILE, "results": [public_result(self.cache.get(request["request_id"]), request["request_id"] not in newly_reserved) for request in requests]}

def make_server(service, port=8019):
    health = profile_identity(service.backend.identity)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def local_request(self):
            host, actual_port = self.headers.get("Host", ""), self.server.server_address[1]
            if host not in (f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"):
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == "http://" + host

        def send(self, status, value):
            data = canonical(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.local_request():
                self.send(403, {"error": {"type": "loopback_origin_required"}})
            elif self.path == "/health":
                self.send(200, health)
            elif self.path.startswith("/v1/eval/requests/"):
                request_id = self.path.removeprefix("/v1/eval/requests/")
                if not REQUEST_ID.fullmatch(request_id):
                    self.send(400, {"error": {"type": "invalid_request_id"}})
                    return
                record = service.cache.get(request_id)
                if record is None:
                    self.send(404, {"request_id": request_id, "error": {"type": "not_started"}})
                else:
                    self.send(409 if record["status"] == "pending" else 200, public_result(record, True))
            else:
                self.send(404, {"error": {"type": "not_found"}})

        def do_POST(self):
            if not self.local_request():
                self.send(403, {"error": {"type": "loopback_origin_required"}})
                return
            if self.path != "/v1/eval/batch":
                self.send(404, {"error": {"type": "not_found"}})
                return
            try:
                if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                    raise ValueError("JSON body required")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("invalid body size")
                result = service.submit(json.loads(self.rfile.read(size)))
            except Conflict:
                self.send(409, {"error": {"type": "request_payload_conflict"}})
            except Busy:
                self.send(503, {"error": {"type": "model_busy", "message": "No new request was started."}})
            except (ValueError, TypeError, OverflowError):
                self.send(400, {"error": {"type": "invalid_request"}})
            else:
                self.send(200, result)
                print(json.dumps({"profile": PROFILE, "results": [{"request_id": row["request_id"], "status": row["status"],
                    "cache_hit": row["cache_hit"], "attempt_count": row["attempt_count"]} for row in result["results"]]}), flush=True)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "Hunyuan7B/models/Hunyuan-7B-Instruct")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8019)
    args = parser.parse_args()
    if args.port != 8019:
        parser.error("this profile uses loopback port8019")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    # A Linux process lease prevents separate server processes from sharing a cache.
    # Hold the descriptor until process exit; pending from previous processes stays pending.
    import fcntl
    cache = RequestCache(args.cache_dir)
    lease = (cache.directory / ".service.lock").open("a+b")
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("this cache is already owned by another service process")
    runtime = Runtime(args.model_dir.resolve(), CONTEXT_TOKENS, OUTPUT_TOKENS)
    service = BatchService(cache, TorchBatchBackend(runtime))
    server = make_server(service, args.port)
    print(json.dumps({"status": "ready", **service.backend.identity, "port": args.port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
