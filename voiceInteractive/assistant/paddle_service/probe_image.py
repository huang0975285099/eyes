# -*- coding: utf-8 -*-
"""PP-Human 图片模式探针：验证 DET + ATTR 链路与输出字段。

用法（在 voiceInteractive 目录）:
  .venv-paddle\\Scripts\\python.exe assistant\\paddle_service\\probe_image.py [图片路径]
结果图写入 third_party/PaddleDetection/output/
"""
import os
import sys
import types
from pathlib import Path

import numpy as np
import yaml
import paddle

ROOT = Path(__file__).resolve().parents[2] / "third_party" / "PaddleDetection"
# 官方脚本两种导入风格混用：裸导入(datacollector)需要 deploy/pipeline，
# 包导入(pipeline.ppvehicle)需要 deploy
# deploy 必须在前：使 `pipeline` 解析为目录包而非 pipeline.py
sys.path.insert(0, str(ROOT / "deploy" / "pipeline"))
sys.path.insert(0, str(ROOT / "deploy"))
# 最终顺序: deploy, deploy/pipeline, ...
os.chdir(ROOT)  # tracker_config 等相对路径依赖工作目录

paddle.enable_static()

from pipeline.pipeline import PipePredictor  # noqa: E402

CFG_PATH = Path(__file__).resolve().parent / "pphuman_eyes.yml"


def make_args():
    return types.SimpleNamespace(
        output_dir="output",
        draw_center_traj=False,
        secs_interval=2,
        do_entrance_counting=False,
        do_break_in_counting=False,
        region_type="horizontal",
        region_polygon=[],
        illegal_parking_time=-1,
        pushurl="",
        device="CPU",
        run_mode="paddle",
        trt_min_shape=1,
        trt_max_shape=1280,
        trt_opt_shape=640,
        trt_calib_mode=False,
        cpu_threads=8,
        enable_mkldnn=False,
    )


def main():
    img = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "demo" / "road554.png")
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    p = PipePredictor(make_args(), cfg, is_video=False)
    p.run([img])

    res = p.pipeline_res
    print("\n===== pipeline_res keys =====")
    print(list(res.res_dict.keys()))

    det = res.get("det")
    if det is not None:
        boxes = np.array(det["boxes"])
        print(f"\n[det] boxes shape={boxes.shape}")
        print("[det] 列含义: class, score, xmin, ymin, xmax, ymax")
        if boxes.size:
            print(boxes[:5])

    attr = res.get("attr")
    if attr is not None:
        print(f"\n[attr] 人数={len(attr['output'])}")
        for i, a in enumerate(attr["output"][:5]):
            print(f"  person{i}: {a}")


if __name__ == "__main__":
    main()
