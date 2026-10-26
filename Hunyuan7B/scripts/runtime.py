"""Real local model inference; deliberately independent of NewsAgent business code."""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from assets import MODEL_DIR, REVISION, verify

MODEL_ID = f"hunyuan-7b-instruct-{REVISION[:7]}-nf4"
CONTEXT_TOKENS = 2048


def load_model():
    verify()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; install the CUDA PyTorch wheel")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_DIR,
        local_files_only=True,
        trust_remote_code=False,
        dtype=torch.bfloat16,
        device_map={"": 0},
        attn_implementation="sdpa",
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        ),
    )
    model.eval()
    return tokenizer, model


def fast_messages(messages):
    # The pinned template ignores enable_thinking; use the official control prefix.
    return [{**message, "content": "/no_think " + message["content"]}
            if message["role"] == "user" else dict(message) for message in messages]


def encode(tokenizer, messages):
    encoded = tokenizer.apply_chat_template(
        fast_messages(messages), tokenize=True, add_generation_prompt=True,
        enable_thinking=False, return_tensors="pt", return_dict=True,
    )
    # This tokenizer also emits token_type_ids, which Hunyuan does not accept.
    return {key: value.to("cuda:0") for key, value in encoded.items()
            if key in ("input_ids", "attention_mask")}


def generate(tokenizer, model, messages, max_tokens=256, temperature=0.0):
    inputs = encode(tokenizer, messages)
    prompt_tokens = inputs["input_ids"].shape[-1]
    if prompt_tokens + max_tokens > CONTEXT_TOKENS:
        raise ValueError(f"Input + output exceeds local {CONTEXT_TOKENS}-token budget")
    options = {"do_sample": temperature > 0, "max_new_tokens": max_tokens, "max_time": 90.0}
    if temperature > 0:
        options["temperature"] = temperature
    else:
        options.update(temperature=1.0, top_p=1.0, top_k=50)
    with torch.inference_mode():
        output = model.generate(**inputs, **options)
    generated = output[0, prompt_tokens:]
    text = tokenizer.decode(generated, skip_special_tokens=True).strip()
    # Strip official response wrappers; never return an unfinished thinking block.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    elif "<think>" in text:
        raise RuntimeError("Model returned an unfinished thinking block")
    if text.startswith("<answer>"):
        text = text[len("<answer>"):].strip()
        if "</answer>" in text:
            text = text.split("</answer>", 1)[0].strip()
    eos = model.generation_config.eos_token_id
    eos = eos if isinstance(eos, list) else [eos]
    finished = bool(len(generated) and generated[-1].item() in eos)
    return {
        "content": text,
        "finish_reason": "stop" if finished else "length",
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(generated),
                  "total_tokens": prompt_tokens + len(generated)},
    }
