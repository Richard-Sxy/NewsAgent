"""Synthetic deployment checks; these do not measure news-domain model quality."""
import argparse
import json
import sys
import time

import torch

from assets import ROOT, REVISION, MODEL_DIR
from runtime import MODEL_ID, fast_messages, generate, load_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qlora-backward", action="store_true")
    args = parser.parse_args()
    tokenizer, model = load_model()
    started = time.monotonic()
    result = generate(tokenizer, model, [
        {"role": "system", "content": "你是新闻运营助手，只依据提供的事实回答，不编造数据。"},
        {"role": "user", "content": "某新闻的阅读量上升，但没有推送实验数据。能否据此断言推送导致阅读量增长？请用一句话回答。"},
    ], max_tokens=128)
    if not result["content"] or result["finish_reason"] != "stop":
        raise RuntimeError("Smoke response empty or truncated")
    report = {"model": MODEL_ID, "revision": REVISION, "device": torch.cuda.get_device_name(0),
              "platform": sys.platform, "python": sys.version, "model_dir": str(MODEL_DIR),
              "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
              "inference_seconds": round(time.monotonic() - started, 3), "synthetic_inference": result,
              "scope": "local synthetic deployment smoke only; no business quality evaluation"}
    if args.qlora_backward:
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True,
                                                gradient_checkpointing_kwargs={"use_reentrant": False})
        model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
                                               target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"))
        model.train()
        model.config.use_cache = False
        prefix = tokenizer.apply_chat_template(fast_messages([{"role": "user", "content": "仅有阅读量增长能否证明推送效果？"}]),
                                                tokenize=False, add_generation_prompt=True, enable_thinking=False)
        prefix_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
        answer_ids = tokenizer("<think>\n\n</think>\n<answer>\n不能，需要对照实验。\n</answer>", add_special_tokens=False)["input_ids"] + [tokenizer.eos_token_id]
        ids = torch.tensor([prefix_ids + answer_ids], device="cuda")
        labels = ids.clone()
        labels[:, :len(prefix_ids)] = -100
        loss = model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=labels).loss
        if not torch.isfinite(loss):
            raise RuntimeError("QLoRA loss is not finite")
        loss.backward()
        grads = [p.grad for p in model.parameters() if p.requires_grad and p.grad is not None]
        if not grads or not all(torch.isfinite(g).all().item() for g in grads) or not any(torch.count_nonzero(g).item() for g in grads):
            raise RuntimeError("QLoRA gradients missing, zero or non-finite")
        if any(p.requires_grad for name, p in model.named_parameters() if "lora_" not in name):
            raise RuntimeError("Base parameters unexpectedly trainable")
        report["qlora_backward"] = {"loss": loss.item(), "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
                                     "target_modules": ["q_proj", "v_proj"], "rank": 8, "tokens": ids.shape[-1],
                                     "optimizer_steps": 0, "adapter_saved": False}
    report["cuda_peak_allocated_gib"] = round(torch.cuda.max_memory_allocated() / 1024 ** 3, 3)
    output = ROOT / "small_model/runtime/hunyuan/smoke-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
