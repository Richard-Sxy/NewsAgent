"""Build the opt-in workbench without loading dotenv; repair only missing locked Windows binaries."""
import argparse
import base64
from hashlib import sha512
import io
import json
from pathlib import Path
import subprocess
import tarfile
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node',default='node')
    parser.add_argument('--proxy')
    args=parser.parse_args()
    version=subprocess.check_output([args.node,'--version'],text=True).strip()
    parts=tuple(map(int,version.lstrip('v').split('.')))
    if parts<(20,19,0): raise RuntimeError('Node >=20.19 is required; select an existing compatible runtime with --node')
    frontend=ROOT/'agent'/'frontend'
    lock=json.loads((frontend/'package-lock.json').read_text(encoding='utf-8'))['packages']
    if __import__('os').name=='nt':
        http=build_opener(ProxyHandler({'https':args.proxy} if args.proxy else {}))
        for parent,name in [('rollup','@rollup/rollup-win32-x64-msvc'),('esbuild','@esbuild/win32-x64')]:
            target=frontend/'node_modules'/name
            if target.exists(): continue
            installed=json.loads((frontend/'node_modules'/parent/'package.json').read_text(encoding='utf-8'))['version']
            entry=lock['node_modules/'+name]
            if installed!=entry['version'] or not entry['resolved'].startswith('https://registry.npmjs.org/'):
                raise RuntimeError('Installed bundler differs from locked native dependency')
            with http.open(entry['resolved'],timeout=30) as response: data=response.read(20*1024*1024+1)
            if len(data)>20*1024*1024 or 'sha512-'+base64.b64encode(sha512(data).digest()).decode()!=entry['integrity']:
                raise RuntimeError('Native dependency integrity mismatch')
            with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
                for member in archive.getmembers():
                    parts=Path(member.name).parts
                    if not parts or parts[0]!='package' or member.issym() or member.islnk():
                        raise RuntimeError('Unsafe dependency archive')
                    path=target.joinpath(*parts[1:]).resolve()
                    if not path.is_relative_to(target.resolve()): raise RuntimeError('Archive path escapes package')
                    if member.isdir(): path.mkdir(parents=True,exist_ok=True)
                    elif member.isfile():
                        path.parent.mkdir(parents=True,exist_ok=True)
                        with path.open('xb') as output: output.write(archive.extractfile(member).read())
                    else: raise RuntimeError('Unsupported archive entry')
            print('Installed locked missing binary: '+name,flush=True)
    subprocess.run([args.node,'node_modules/vue-tsc/bin/vue-tsc.js','--noEmit','-p','tsconfig.json'],cwd=frontend,check=True)
    subprocess.run([args.node,'node_modules/vite/bin/vite.js','build','--config','vite.streaming-lab.config.ts'],cwd=frontend,check=True)


if __name__=='__main__': main()
