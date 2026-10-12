# -*- coding: utf-8 -*-
"""区域入侵：配置加载与几何判定（worker 内自实现，不依赖 PP-Human 自带区域功能）。

配置文件：assistant/paddle_service/conf/regions.json

```json
{
  "regions": {
    "usb1": [
      {"name": "柜台禁区", "polygon": [[420,180],[1180,180],[1180,700],[420,700]],
       "cooldown": 30, "enabled": true}
    ],
    "*": [ ... ]          // 可选：对所有流生效的兜底区域
  }
}
```

- `polygon`：按顺时针或逆时针顺序的顶点列表，坐标为**原始帧像素**（1280x720 即以该分辨率为准）
- `cooldown`：同一「区域 + track_id」的重复告警冷却秒数，缺省用 DEFAULT_COOLDOWN
- `enabled`：false 时跳过该区域（便于临时停用而不删除坐标）
- `"*"`：对未在表中单独列出的所有流生效；单独列出的流**只**用自己的区域（不叠加 `"*"`）

判定方式：MOT 检测框**中心点**落入多边形即视为进入（见实施方案 4.6）。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_COOLDOWN = 30.0
_REGIONS_FILE = Path(__file__).resolve().parent / "conf" / "regions.json"


class Region:
    """单个禁区：多边形 + 冷却 + 名称。"""

    def __init__(self, name: str, polygon: list, cooldown: float = DEFAULT_COOLDOWN):
        self.name = name
        self.poly = [(float(p[0]), float(p[1])) for p in polygon]
        self.cooldown = float(cooldown)

    @property
    def points_int(self):
        return [(int(round(x)), int(round(y))) for x, y in self.poly]

    def contains(self, x: float, y: float) -> bool:
        """射线法判断点是否在多边形内（顶点数 < 3 恒为 False）。"""
        poly = self.poly
        n = len(poly)
        if n < 3:
            return False
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            # 只在边的 y 区间跨越待判点 y 时求交，避免水平边与除零
            if (yi > y) != (yj > y):
                x_cross = (xj - xi) * (y - yi) / float(yj - yi) + xi
                if x < x_cross:
                    inside = not inside
            j = i
        return inside


def load_regions(stream_name: str, path: Path | None = None) -> list[Region]:
    """读取该路流的禁区配置；文件缺失/格式错误返回空列表（= 不启用区域入侵）。"""
    path = path or _REGIONS_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as error:  # noqa: BLE001
        print(f"[regions] 读取 {path} 失败: {error}", flush=True)
        return []
    table = data.get("regions") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return []
    raw = table.get(stream_name)
    if raw is None:
        raw = table.get("*")
    if not isinstance(raw, list):
        return []
    regions: list[Region] = []
    for item in raw:
        try:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            poly = item.get("polygon")
            if not isinstance(poly, list) or len(poly) < 3:
                print(f"[regions] {stream_name} 区域「{item.get('name')}」多边形无效，已跳过", flush=True)
                continue
            regions.append(Region(str(item.get("name") or "禁区"), poly,
                                  float(item.get("cooldown") or DEFAULT_COOLDOWN)))
        except Exception as error:  # noqa: BLE001
            print(f"[regions] {stream_name} 区域解析失败: {error}", flush=True)
    return regions
