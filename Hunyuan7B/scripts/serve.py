"""Loopback-only, single-GPU, non-streaming Chat Completions subset."""
import argparse
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime import CONTEXT_TOKENS, MODEL_ID, generate, load_model
from assets import MODEL_DIR, REVISION

logger = logging.getLogger("hunyuan")
lock = threading.Lock()
state = {}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=24000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[Message] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=256, ge=1, le=1024)
    temperature: float = Field(default=0, ge=0, le=2)
    stream: bool = False
    response_format: dict | None = None

    @model_validator(mode="after")
    def bound_text(self):
        if sum(len(m.content) for m in self.messages) > 24000:
            raise ValueError("Total message characters exceed 24000")
        return self


@asynccontextmanager
async def lifespan(app):
    state["tokenizer"], state["model"] = load_model()
    yield
    state.clear()
    torch.cuda.empty_cache()


app = FastAPI(title="NewsAgent local Hunyuan experiment", lifespan=lifespan)


@app.get("/health")
def health():
    if "model" not in state:
        raise HTTPException(503, "Model is not loaded")
    return {"status": "ok", "model": MODEL_ID, "quantization": "nf4-double-quant",
            "context_tokens": CONTEXT_TOKENS, "device": torch.cuda.get_device_name(0),
            "platform": "linux", "model_path": str(MODEL_DIR), "revision": REVISION}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def complete(request: ChatRequest, response: Response):
    if request.model != MODEL_ID:
        raise HTTPException(404, "Unknown model version")
    if request.stream:
        raise HTTPException(400, "This local endpoint supports stream=false only")
    if request.response_format not in (None, {"type": "text"}):
        raise HTTPException(400, "Constrained JSON decoding is not implemented; use response_format=none in NewsAgent")
    if not lock.acquire(blocking=False):
        raise HTTPException(503, "GPU busy; only one generation at a time", headers={"Retry-After": "2"})
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    started = time.monotonic()
    try:
        result = generate(state["tokenizer"], state["model"],
                          [m.model_dump() for m in request.messages], request.max_tokens, request.temperature)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(503, "GPU out of memory; reduce input/output length") from exc
    except Exception as exc:
        logger.error("request_id=%s failure=%s", request_id, type(exc).__name__)
        raise HTTPException(500, "Generation failed; see local service log") from exc
    finally:
        lock.release()
    # Metadata only: prompts, news content and answers never enter service logs.
    logger.warning("request_id=%s seconds=%.3f prompt_tokens=%d completion_tokens=%d finish=%s",
                   request_id, time.monotonic() - started, result["usage"]["prompt_tokens"],
                   result["usage"]["completion_tokens"], result["finish_reason"])
    response.headers["x-request-id"] = request_id
    return {"id": request_id, "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_ID, "choices": [{"index": 0, "message": {"role": "assistant", "content": result["content"]},
                                            "finish_reason": result["finish_reason"]}], "usage": result["usage"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
