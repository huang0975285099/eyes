# -*- coding: utf-8 -*-
"""PP-Human 模型快速下载器。

单连接到 bcebos 很慢（~0.05MB/s），服务端支持 Range，用 8 线程分块下载
可聚合到 ~1MB/s。下载后按 deploy/pipeline/download.py 的规则解压到
~/.cache/paddle/infer_weights/，并对有 MD5 记录的包做校验。

用法: .venv-paddle\\Scripts\\python.exe -m assistant.paddle_service.download_models
"""
import hashlib
import os
import os.path as osp
import sys
import threading
import time
import urllib.request
import zipfile

WEIGHTS_HOME = osp.expanduser("~/.cache/paddle/infer_weights")
THREADS = 8

# url -> md5（MOT pipeline 包官方未给 md5，填 None）
MODELS = {
    "https://bj.bcebos.com/v1/paddledet/models/pipeline/mot_ppyoloe_l_36e_pipeline.zip": None,
    "https://bj.bcebos.com/v1/paddledet/models/pipeline/dark_hrnet_w32_256x192.zip":
        "a20d5f6ca087bff0e9f2b18df45a36f2",
    "https://bj.bcebos.com/v1/paddledet/models/pipeline/STGCN.zip":
        "cf1c3c4bae90b975accb954d13129ea4",
    "https://bj.bcebos.com/v1/paddledet/models/pipeline/PPLCNet_x1_0_person_attribute_945_infer.zip":
        "1dfb161bf12bbc1365b2ed6866674483",
    "https://videotag.bj.bcebos.com/PaddleVideo-release2.3/ppTSM_fight.zip":
        "5d4609142501258608bf0a1445eedaba",
}

# 解压后目录重命名（与官方 download.py decompress_name_map 一致）
RENAMES = {"ppTSM_fight": "ppTSM"}


def get_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as r:
        return int(r.headers["Content-Length"])


def md5file(path):
    m = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            m.update(chunk)
    return m.hexdigest()


def download_one(url, dst):
    """THREADS 线程分块下载 url 到 dst（已存在且大小正确则跳过）。"""
    total = get_size(url)
    if osp.exists(dst) and osp.getsize(dst) == total:
        print(f"[skip] {osp.basename(dst)} 已存在")
        return
    tmp = dst + "_tmp"
    part_dir = dst + "_parts"
    os.makedirs(part_dir, exist_ok=True)
    chunk = (total + THREADS - 1) // THREADS
    progress = [0] * THREADS
    stop = threading.Event()

    def worker(i):
        start = i * chunk
        end = min(start + chunk, total) - 1
        part = osp.join(part_dir, f"part{i}")
        have = osp.getsize(part) if osp.exists(part) else 0
        if have > 0:
            progress[i] = have
        if start + have > end:
            return
        for attempt in range(5):
            try:
                headers = {"Range": f"bytes={start + have}-{end}"}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r, open(part, "ab") as f:
                    while not stop.is_set():
                        b = r.read(1 << 16)
                        if not b:
                            break
                        f.write(b)
                        progress[i] += len(b)
                return
            except Exception as e:
                have = osp.getsize(part) if osp.exists(part) else 0
                progress[i] = have
                time.sleep(1 + attempt)

    def meter():
        t0 = time.time()
        last = 0
        while not stop.wait(1):
            got = sum(progress)
            speed = (got - last) / 1024 / 1024
            pct = got * 100 / total
            print(f"\r  {got/1024/1024:7.1f}/{total/1024/1024:.1f} MB {pct:5.1f}% "
                  f"{speed:5.2f} MB/s   ", end="", flush=True)
            last = got

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(THREADS)]
    mt = threading.Thread(target=meter, daemon=True)
    t0 = time.time()
    [t.start() for t in ths]
    mt.start()
    [t.join() for t in ths]
    stop.set()
    print()

    # 合并分片
    with open(tmp, "wb") as out:
        for i in range(THREADS):
            p = osp.join(part_dir, f"part{i}")
            if osp.exists(p):
                with open(p, "rb") as f:
                    while True:
                        b = f.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
    size_ok = osp.getsize(tmp) == total
    if not size_ok:
        raise RuntimeError(f"{osp.basename(dst)} 合并后大小不符: "
                           f"{osp.getsize(tmp)} != {total} ({time.time()-t0:.0f}s)")
    os.replace(tmp, dst)
    for i in range(THREADS):
        p = osp.join(part_dir, f"part{i}")
        if osp.exists(p):
            os.remove(p)
    os.rmdir(part_dir)
    print(f"[done] {osp.basename(dst)} 用时 {time.time()-t0:.0f}s")


def extract(path):
    with zipfile.ZipFile(path) as zf:
        zf.extractall(WEIGHTS_HOME)
    for old, new in RENAMES.items():
        old_p, new_p = osp.join(WEIGHTS_HOME, old), osp.join(WEIGHTS_HOME, new)
        if osp.isdir(old_p) and not osp.isdir(new_p):
            os.rename(old_p, new_p)
            print(f"  重命名 {old} -> {new}")


def main():
    os.makedirs(WEIGHTS_HOME, exist_ok=True)
    for url, md5 in MODELS.items():
        fname = osp.split(url)[-1]
        dst = osp.join(WEIGHTS_HOME, fname)
        print(f"=== {fname} ===")
        download_one(url, dst)
        if md5:
            got = md5file(dst)
            if got != md5:
                print(f"  [警告] MD5 不符: {got} != {md5}，删除请重跑")
                sys.exit(1)
            print("  MD5 校验通过")
        if fname.endswith(".zip"):
            extract(dst)
            os.remove(dst)
    print("全部模型就绪:", WEIGHTS_HOME)


if __name__ == "__main__":
    main()
