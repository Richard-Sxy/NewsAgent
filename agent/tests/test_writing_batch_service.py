"""Offline contract tests: fake runtime only, never load model weights."""
from __future__ import annotations
from contextlib import nullcontext
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "Hunyuan7B/scripts/serve_writing_batch_eval.py"
spec = importlib.util.spec_from_file_location("writing_batch_service_under_test", SCRIPT)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

def request(identifier="r1", text="evidence", cap=1024):
    return {"request_id": identifier, "model": service.MODEL_ID,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": cap, "temperature": 0, "stream": False}

class FakeBackend:
    identity = {"status": "ok", "model": service.MODEL_ID, "revision": service.REVISION}
    def __init__(self, crash=False, finish="stop"):
        self.calls, self.crash, self.finish = [], crash, finish
    def generate_batch(self, requests, attempt, complete):
        self.calls.append([item["request_id"] for item in requests])
        attempt(requests, "initial")
        if self.crash:
            raise KeyboardInterrupt("simulate process loss")
        for item in requests:
            complete(item, service.completion_result(item["request_id"], "draft", self.finish, 5, 7))

class FakeTensor:
    def __init__(self, rows):
        self.rows = rows
        self.shape = (len(rows), len(rows[0]))
    def to(self, device):
        assert device == "cuda:0"
        return self
    def __getitem__(self, key):
        row, part = key
        return SimpleNamespace(tolist=lambda: self.rows[row][part])

class FakeOOM(Exception):
    pass

class FakeTokenizer:
    padding_side = "right"
    pad_token_id, eos_token_id = 9, 9
    eos_token, pad_token = "<eos>", "<eos>"
    def __init__(self):
        self.templates, self.batch_calls = [], []
    def apply_chat_template(self, messages, **kwargs):
        self.templates.append((messages, kwargs))
        return messages[-1]["content"]
    def __call__(self, texts, **kwargs):
        if isinstance(texts, str):
            return {"input_ids": list(range(len(texts)))}
        self.batch_calls.append((texts, kwargs, self.padding_side))
        width = max(map(len, texts))
        rows = [[9] * (width-len(text)) + [1] * len(text) for text in texts]
        masks = [[0] * (width-len(text)) + [1] * len(text) for text in texts]
        return {"input_ids": FakeTensor(rows), "attention_mask": FakeTensor(masks)}
    def decode(self, tokens, **kwargs):
        return "".join({2: "a", 3: "b", 9: "", 10: ""}.get(token, "x") for token in tokens)

class FakeModel:
    generation_config = SimpleNamespace(eos_token_id=[9, 10])
    def __init__(self, oom_calls=()):
        self.calls, self.oom_calls = [], set(oom_calls)
    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) in self.oom_calls:
            raise FakeOOM()
        inputs = kwargs["input_ids"]
        tails = [[2, 9, 9, 9], [2, 3, 10, 9], [2, 3, 2, 9], [2, 3, 2, 3]]
        return FakeTensor([row + tails[index % len(tails)] for index, row in enumerate(inputs.rows)])

def fake_runtime(oom_calls=()):
    cleans = []
    return SimpleNamespace(
        identity=FakeBackend.identity, tokenizer=FakeTokenizer(), model=FakeModel(oom_calls),
        context_limit=16384, output_limit=1024,
        torch=SimpleNamespace(inference_mode=nullcontext,
                              cuda=SimpleNamespace(OutOfMemoryError=FakeOOM, empty_cache=lambda: cleans.append(True))),
        cleans=cleans)

class WritingBatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = service.RequestCache(self.temp.name)
        self.backend = FakeBackend()
        self.app = service.BatchService(self.cache, self.backend)
    def tearDown(self):
        self.temp.cleanup()

    def test_import_does_not_import_gpu(self):
        script = (
            "import builtins,importlib.util;"
            "original=builtins.__import__;"
            "exec('def guarded(name,*a,**k):\\n if name.split(\\\".\\\")[0] in (\\\"torch\\\",\\\"transformers\\\",\\\"assets\\\"): raise AssertionError(name)\\n return original(name,*a,**k)');"
            "builtins.__import__=guarded;"
            f"spec=importlib.util.spec_from_file_location('batch',{str(SCRIPT)!r});"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)"
        )
        subprocess.run([sys.executable, "-B", "-c", script], check=True, capture_output=True)

    def test_completed_idempotency_and_restart(self):
        first = self.app.submit({"requests": [request()]})["results"][0]
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["attempt_count"], 1)
        self.assertFalse(first["cache_hit"])
        second = self.app.submit({"requests": [request()]})["results"][0]
        fresh = FakeBackend()
        restarted = service.BatchService(service.RequestCache(self.temp.name), fresh)
        third = restarted.submit({"requests": [request()]})["results"][0]
        self.assertTrue(second["cache_hit"] and third["cache_hit"])
        self.assertEqual(first["response"], third["response"])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(fresh.calls, [])
        saved = Path(self.temp.name, "r1.json").read_text()
        self.assertNotIn("evidence", saved)
        self.assertNotIn("messages", saved)

    def test_conflict_preflights_entire_batch(self):
        self.app.submit({"requests": [request()]})
        with self.assertRaises(service.Conflict):
            self.app.submit({"requests": [request("new"), request(text="changed")]})
        self.assertIsNone(self.cache.get("new"))
        self.assertEqual(len(self.backend.calls), 1)

    def test_pending_survives_crash_without_replay(self):
        crashed = service.BatchService(self.cache, FakeBackend(crash=True))
        with self.assertRaises(KeyboardInterrupt):
            crashed.submit({"requests": [request()]})
        self.assertEqual(self.cache.get("r1")["status"], "pending")
        result = self.app.submit({"requests": [request()]})["results"][0]
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(result["attempts"]), 1)

    def test_busy_reserves_no_new_id_and_cached_reads_still_work(self):
        self.app.submit({"requests": [request()]})
        self.app.gpu_lock.acquire()
        try:
            self.assertTrue(self.app.submit({"requests": [request()]})["results"][0]["cache_hit"])
            with self.assertRaises(service.Busy):
                self.app.submit({"requests": [request("new")]})
            self.assertIsNone(self.cache.get("new"))
        finally:
            self.app.gpu_lock.release()

    def test_length_is_failure_and_preserves_response(self):
        app = service.BatchService(self.cache, FakeBackend(finish="length"))
        row = app.submit({"requests": [request()]})["results"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"]["type"], "generation_length")
        self.assertEqual(row["response"]["choices"][0]["finish_reason"], "length")
        self.assertEqual(row["response"]["choices"][0]["message"]["content"], "draft")
        self.assertTrue(app.submit({"requests": [request()]})["results"][0]["cache_hit"])

    def test_identity_budget_and_batch_validation(self):
        invalid = [
            {"requests": []}, {"requests": [request(str(i)) for i in range(5)]},
            {"requests": [request(), request()]},
            {"requests": [request("../bad")]},
            {"requests": [request(cap=1025)]}, {"requests": [request(cap=True)]},
            {"requests": [request(), request("r2", cap=512)]},
        ]
        for key, value in [("model", "other"), ("temperature", 0.1), ("temperature", float("nan")),
                           ("stream", True), ("response_format", {"type": "json_object"})]:
            item = request()
            item[key] = value
            invalid.append({"requests": [item]})
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                service.validate_batch(body)
        self.assertEqual(len(service.validate_batch({"requests": [request(str(i)) for i in range(4)]})), 4)
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_group_policy_and_actual_eos(self):
        self.assertEqual(service.batch_groups([3500] * 4), [[0, 1, 2, 3]])
        self.assertEqual(service.batch_groups([3501, 10, 20, 30]), [[0, 1], [2, 3]])
        self.assertEqual(service.trim_at_eos([2, 9, 9, 9], {9, 10}), ([2, 9], True))
        self.assertEqual(service.trim_at_eos([2, 3], {9, 10}), ([2, 3], False))

    def test_true_batch_left_padding_prompt_usage_and_eos(self):
        runtime = fake_runtime()
        app = service.BatchService(self.cache, service.TorchBatchBackend(runtime))
        rows = app.submit({"requests": [request(str(i), text="x" * (i + 1)) for i in range(4)]})["results"]
        self.assertEqual(len(runtime.model.calls), 1)
        self.assertEqual(runtime.model.calls[0]["input_ids"].shape[0], 4)
        self.assertEqual(runtime.tokenizer.batch_calls[0][2], "left")
        self.assertEqual(runtime.tokenizer.batch_calls[0][1]["add_special_tokens"], False)
        self.assertTrue(all(call[1]["tokenize"] is False for call in runtime.tokenizer.templates))
        self.assertTrue(all(call[0][-1]["content"].startswith("/no_think ") for call in runtime.tokenizer.templates))
        self.assertEqual([row["response"]["usage"]["completion_tokens"] for row in rows], [2, 3, 4, 4])
        self.assertEqual([row["response"]["usage"]["prompt_tokens"] for row in rows], [11, 12, 13, 14])
        self.assertEqual(rows[-1]["status"], "failed")
        call = runtime.model.calls[0]
        self.assertFalse(call["do_sample"])
        self.assertEqual((call["max_new_tokens"], call["max_time"]), (1024, 240.0))

    def test_oom_falls_back_once_to_two_and_records_real_attempts(self):
        runtime = fake_runtime(oom_calls={1})
        app = service.BatchService(self.cache, service.TorchBatchBackend(runtime))
        rows = app.submit({"requests": [request(str(i)) for i in range(4)]})["results"]
        self.assertEqual([call["input_ids"].shape[0] for call in runtime.model.calls], [4, 2, 2])
        self.assertEqual(len(runtime.cleans), 1)
        self.assertTrue(all(row["attempt_count"] == 2 for row in rows))
        self.assertTrue(all([attempt["phase"] for attempt in row["attempts"]] == ["initial", "oom_fallback"] for row in rows))

    def test_repeated_oom_stops_and_long_prompt_batch_uses_two(self):
        runtime = fake_runtime(oom_calls={1, 2, 3})
        app = service.BatchService(self.cache, service.TorchBatchBackend(runtime))
        rows = app.submit({"requests": [request(str(i)) for i in range(4)]})["results"]
        self.assertEqual(len(runtime.model.calls), 3)
        self.assertTrue(all(row["error"]["type"] == "cuda_oom" for row in rows))
        runtime2 = fake_runtime()
        app2 = service.BatchService(self.cache, service.TorchBatchBackend(runtime2))
        app2.submit({"requests": [request("long" + str(i), text="x" * 3500) for i in range(4)]})
        self.assertEqual([call["input_ids"].shape[0] for call in runtime2.model.calls], [2, 2])

    def test_context_error_does_not_generate(self):
        runtime = fake_runtime()
        app = service.BatchService(self.cache, service.TorchBatchBackend(runtime))
        row = app.submit({"requests": [request(text="x" * 16000)]})["results"][0]
        self.assertEqual(runtime.model.calls, [])
        self.assertEqual(row["attempt_count"], 0)
        self.assertEqual(row["error"]["type"], "context_budget_exceeded")

    def test_http_cache_health_origin_and_status(self):
        server = service.make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        def call(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path, body=json.dumps(body) if body is not None else None,
                                   headers={"Content-Type": "application/json", **(headers or {})})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            status, health = call("GET", "/health")
            self.assertEqual((status, health["profile"], health["model"]), (200, service.PROFILE, service.MODEL_ID))
            self.assertEqual((health["context_tokens"], health["max_output_tokens"], health["max_batch_size"]), (16384, 1024, 4))
            self.assertEqual((health["thinking_mode"], health["user_prefix"], health["generation_max_time_seconds"]), ("disabled", "/no_think ", 240))
            self.assertEqual(len(health["source_sha256"]), 64)
            self.assertEqual(len(health["runtime_source_sha256"]), 64)
            self.assertEqual(call("GET", "/health", headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(call("POST", "/v1/eval/batch", {"requests": [request()]}, {"Origin": "https://evil.example"})[0], 403)
            self.assertEqual(call("GET", "/v1/eval/requests/r1")[0], 404)
            self.assertEqual(call("POST", "/v1/eval/batch", {"requests": [request()]})[0], 200)
            self.assertEqual(call("GET", "/v1/eval/requests/r1")[1]["status"], "completed")
            self.assertEqual(call("POST", "/v1/eval/batch", {"requests": [request(text="other")]})[0], 409)
            pending = service.validate_batch({"requests": [request("pending")]})[0]
            self.cache.reserve(pending)
            self.assertEqual(call("GET", "/v1/eval/requests/pending")[0], 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == "__main__":
    unittest.main()
