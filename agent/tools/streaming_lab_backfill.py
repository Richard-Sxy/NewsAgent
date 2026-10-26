"""Submit one bounded synthetic full-recount package; never retry automatically."""
import argparse
import json
from pathlib import Path
import subprocess


def submit(package):
    raw=json.dumps(package,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(raw)>8*1024*1024: raise ValueError('Package exceeds 8 MiB')
    command=['docker','exec','-i','newsagent-streaming-lab-agent-1','python','-m','simulation.streaming_lab.backfill']
    result=subprocess.run(command,input=raw,capture_output=True,timeout=40)
    if result.returncode:
        # Surface validation class/message, never the raw package or DSN.
        last=result.stderr.decode('utf-8',errors='replace').strip().splitlines()[-1:]
        raise RuntimeError(last[0] if last and last[0].startswith('ValueError:') else 'Backfill command failed; inspect isolated agent health')
    return json.loads(result.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    args=parser.parse_args()
    if args.package.stat().st_size>8*1024*1024: raise ValueError('Package exceeds 8 MiB')
    print(json.dumps(submit(json.loads(args.package.read_text(encoding='utf-8')))),flush=True)


if __name__=='__main__': main()
