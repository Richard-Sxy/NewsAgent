"""Isolated loopback Hunyuan writing evaluation; real NF4, no training.

No secrets, dotenv, production service changes, remote code or remote downloads.
Model loading happens only in main(). Existing deployment scripts are untouched.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import uuid

MODEL_ID = "local-hunyuan-7b-6fd6ecb-nf4-writing"
REVISION = "6fd6ecb05e76589bc43b79f49e3619445c6b4593"
ROOT = Path(__file__).resolve().parents[2]
MAX_BODY = 2 * 1024 * 1024


def checked_request(body, output_limit):
    if not isinstance(body, dict) or body.get("model") != MODEL_ID:
        raise ValueError("model identity mismatch")
    if body.get("stream", False) is not False:
        raise ValueError("streaming is not supported")
    if body.get("response_format") not in (None, {"type": "text"}):
        raise ValueError("constrained JSON decoding is not available")
    temperature = body.get("temperature", 0)
    if type(temperature) not in (int, float) or temperature != 0:
        raise ValueError("this frozen profile requires temperature=0")
    cap = body.get("max_tokens", output_limit)
    if type(cap) is not int or not 1 <= cap <= output_limit:
        raise ValueError("invalid output token budget")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise ValueError("messages must be a bounded list")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("unsupported message fields")
        if message["role"] not in ("system", "user", "assistant"):
            raise ValueError("unsupported role")
        if not isinstance(message["content"], str) or not message["content"]:
            raise ValueError("message content required")
    if sum(len(m["content"]) for m in messages) > 200_000:
        raise ValueError("input character limit exceeded")
    return messages, cap


def unwrap(text):
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    elif "<think>" in text:
        raise ValueError("unfinished thinking block")
    if text.startswith("<answer>"):
        text = text[len("<answer>"):].strip()
        if "</answer>" in text:
            text = text.split("</answer>", 1)[0].strip()
    return text.strip()


class Runtime:
    def __init__(self, model_dir, context_limit, output_limit):
        # Import GPU dependencies only in this explicit runtime constructor.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from assets import verify
        manifest = verify(model_dir)
        if manifest["revision"] != REVISION:
            raise ValueError("unapproved model revision")
        if not torch.cuda.is_available():
            raise ValueError("CUDA required")
        self.torch = torch
        self.context_limit = context_limit
        self.output_limit = output_limit
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_dir, local_files_only=True, trust_remote_code=False,
            dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="sdpa",
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16))
        self.model.eval()
        if context_limit > self.model.config.max_position_embeddings:
            raise ValueError("requested context exceeds model config")
        torch.manual_seed(20261028)
        self.identity = {
            "status": "ok", "model": MODEL_ID, "revision": REVISION,
            "quantization": "nf4-double-quant", "compute_dtype": "bfloat16",
            "context_tokens": context_limit, "max_output_tokens": output_limit,
            "model_path": str(model_dir), "device": torch.cuda.get_device_name(0),
            "torch": torch.__version__, "transformers": __import__("transformers").__version__,
            "asset_manifest_sha256": hashlib.sha256((model_dir / "download-manifest.json").read_bytes()).hexdigest(),
            "scope": "isolated local real-model writing pilot; no LoRA or enterprise acceptance",
        }

    def generate(self, messages, cap):
        messages = [{**m, "content": "/no_think " + m["content"]} if m["role"] == "user" else dict(m) for m in messages]
        inputs = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, enable_thinking=False,
            return_tensors="pt", return_dict=True)
        inputs = {k: v.to("cuda:0") for k, v in inputs.items() if k in ("input_ids", "attention_mask")}
        prompt_tokens = inputs["input_ids"].shape[-1]
        if prompt_tokens + cap > self.context_limit:
            raise ValueError("input plus output exceeds frozen context budget")
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, temperature=1.0,
                top_p=1.0, top_k=50, max_new_tokens=cap, max_time=240.0)
        generated = output[0, prompt_tokens:]
        content = unwrap(self.tokenizer.decode(generated, skip_special_tokens=True).strip())
        eos = self.model.generation_config.eos_token_id
        eos = eos if isinstance(eos, list) else [eos]
        stopped = bool(len(generated) and generated[-1].item() in eos)
        return {"content": content, "finish_reason": "stop" if stopped else "length",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(generated),
                      "total_tokens": prompt_tokens + len(generated)}}


def serve(runtime, port):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send(200, runtime.identity)
            elif self.path == "/v1/models":
                self.send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
            else:
                self.send(404, {"error": {"type": "not_found"}})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send(404, {"error": {"type": "not_found"}})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("invalid body size")
                body = json.loads(self.rfile.read(size))
                messages, cap = checked_request(body, runtime.output_limit)
            except (ValueError, TypeError, OverflowError):
                self.send(400, {"error": {"type": "invalid_request"}})
                return
            if not lock.acquire(blocking=False):
                self.send(503, {"error": {"type": "model_busy"}})
                return
            started = time.monotonic()
            try:
                result = runtime.generate(messages, cap)
            except ValueError as exc:
                self.send(400, {"error": {"type": type(exc).__name__}})
                return
            except Exception as exc:
                runtime.torch.cuda.empty_cache()
                self.send(503, {"error": {"type": type(exc).__name__}})
                return
            finally:
                lock.release()
            request_id = "chatcmpl-" + uuid.uuid4().hex
            self.send(200, {"id": request_id, "object": "chat.completion",
                "created": int(time.time()), "model": MODEL_ID,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]},
                             "finish_reason": result["finish_reason"]}], "usage": result["usage"]})
            print(json.dumps({"request_id": request_id, "seconds": round(time.monotonic()-started, 3),
                "usage": result["usage"], "finish_reason": result["finish_reason"]}), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(json.dumps({"status": "ready", **runtime.identity, "port": port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "Hunyuan7B/models/Hunyuan-7B-Instruct")
    parser.add_argument("--port", type=int, default=8018)
    parser.add_argument("--context-tokens", type=int, default=16384)
    parser.add_argument("--max-output-tokens", type=int, default=3072)
    args = parser.parse_args()
    if args.port != 8018 or not 4096 <= args.context_tokens <= 32768 or not 64 <= args.max_output_tokens <= 4096:
        parser.error("use loopback writing port8018 and bounded context/output")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    runtime = Runtime(args.model_dir.resolve(), args.context_tokens, args.max_output_tokens)
    serve(runtime, args.port)


if __name__ == "__main__":
    main()
