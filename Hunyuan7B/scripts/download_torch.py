"""Fetch the official Windows CPython 3.11 CUDA wheel, verifying its index hash."""
import re
import urllib.request

from download import ROOT
from ranged_download import download


def main():
    index = "https://download.pytorch.org/whl/cu128/torch/"
    with urllib.request.urlopen(index, timeout=30) as response:
        html = response.read().decode()
    match = re.search(r'href="([^"]*torch-2\.9\.1%2Bcu128-cp311-cp311-win_amd64\.whl)#sha256=([a-f0-9]+)"', html)
    if not match:
        raise RuntimeError("Official CUDA wheel link missing")
    url, sha256 = match.groups()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers={"Range": "bytes=0-0"}), timeout=30) as response:
        size = int(response.headers["Content-Range"].split("/")[-1])
    target = ROOT / "small_model/runtime/hunyuan/wheels/torch-2.9.1+cu128-cp311-cp311-win_amd64.whl"
    print(f"Official CUDA wheel: {size} bytes", flush=True)
    download(url, target, size, sha256, workers=8, direct=True)


if __name__ == "__main__":
    main()
