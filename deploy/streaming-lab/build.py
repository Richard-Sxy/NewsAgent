"""Verify pinned local artifacts and build Java inside containers; host stdlib only."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--proxy',default='',help='Optional existing proxy for Docker build authentication')
    args=parser.parse_args()
    here=Path(__file__).resolve().parent
    root=here.parents[1]
    deps=root/'var/streaming-lab/lib'
    build=root/'var/streaming-lab/build'
    deps.mkdir(parents=True,exist_ok=True)
    build.mkdir(parents=True,exist_ok=True)
    lock=json.loads((here/'dependencies.lock.json').read_text(encoding='utf-8'))
    for item in lock['artifacts']:
        destination=deps/item['filename']
        if not destination.exists():
            subprocess.run(['curl.exe' if os.name=='nt' else 'curl','--fail','--silent','--show-error',
                '--connect-timeout','10','--max-time','120','--retry','1','--output',str(destination),item['url']],
                check=True,timeout=260)
        if sha256(destination.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Checksum mismatch: '+item['filename'])
    env=os.environ.copy()
    if args.proxy:
        env['HTTP_PROXY']=env['HTTPS_PROXY']=args.proxy
    subprocess.run(['docker','buildx','build','--build-context','deps=./var/streaming-lab/lib',
        '-f','deploy/streaming-lab/Dockerfile.compiler','--output','type=local,dest=./var/streaming-lab/build',
        'deploy/streaming-lab'],cwd=root,env=env,check=True,timeout=300)
    print(json.dumps({'artifact':str(build/'streaming-job.jar'),
        'sha256':sha256((build/'streaming-job.jar').read_bytes()).hexdigest()}))


if __name__=='__main__':main()
