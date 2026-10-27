"""Offline BGE encoder process (Python 3.8 compatible).

JSONL on stdin/stdout; model logs go to stderr. No model downloads, remote code,
API keys or relevance labels are used. CLS pooling and normalization match the
local BGE snapshot's SentenceTransformer configuration.
"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--device', default='cpu', choices=['cpu', 'cuda'])
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    if args.device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('requested CUDA device is unavailable')
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True,
                                              trust_remote_code=False)
    model = AutoModel.from_pretrained(args.model_dir, local_files_only=True,
                                     trust_remote_code=False).to(args.device).eval()
    if model.config.model_type != 'bert' or model.config.hidden_size != 1024:
        raise ValueError('expected the local BGE 1024-dimensional BERT model')
    print(json.dumps({'ready': True, 'dimensions': 1024, 'device': args.device,
                      'torch': torch.__version__, 'transformers': transformers.__version__,
                      'pooling': 'cls', 'normalization': 'l2', 'max_tokens': 512}), flush=True)
    for line in sys.stdin:
        try:
            if len(line) > 2_000_000:
                raise ValueError('encoder request exceeds size budget')
            request = json.loads(line)
            if request.get('op') == 'close':
                return
            texts = request.get('texts')
            if not isinstance(texts, list) or not 1 <= len(texts) <= 64:
                raise ValueError('encoder batch must contain 1..64 texts')
            if any(not isinstance(text, str) or not text.strip() or len(text) > 8000 for text in texts):
                raise ValueError('invalid encoder text')
            tokens = tokenizer(texts, padding=True, truncation=False, return_tensors='pt')
            lengths = tokens['attention_mask'].sum(dim=1).tolist()
            if max(lengths) > 512:
                raise ValueError('input exceeds 512 tokens; shorten query or reduce chunk size')
            with torch.inference_mode():
                hidden = model(**{key: value.to(args.device) for key, value in tokens.items()})
                vectors = torch.nn.functional.normalize(hidden.last_hidden_state[:, 0], p=2, dim=1)
            print(json.dumps({'vectors': vectors.cpu().float().tolist(), 'token_counts': lengths,
                              'truncated_inputs': 0}, separators=(',', ':')), flush=True)
        except Exception as exc:
            print(json.dumps({'error': type(exc).__name__, 'message': str(exc)[:240]}), flush=True)


if __name__ == '__main__':
    main()
