"""生成小型示例 GeoTIFF（EPSG:4326、北向上、单波段、含 NoData）。

用法：
    python scripts/generate_sample.py [输出路径] [宽] [高]

默认生成 256x256、覆盖东经 100-110 / 北纬 20-30 的降水样式栅格，
像元值为合成的降水毫米数，四角为 NoData(-9999)。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

# 与示例范围保持一致，方便文档中的调用示例直接使用
SAMPLE_BBOX = (103.0, 23.0, 107.0, 27.0)


def generate(path: str | Path, width: int = 256, height: int = 256) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    west, south, east, north = 100.0, 20.0, 110.0, 30.0
    transform = from_bounds(west, south, east, north, width, height)

    # 合成降水场：中心高、四周低的平滑场 + 轻微波动
    xs = np.linspace(west, east, width, dtype=np.float64)
    ys = np.linspace(north, south, height, dtype=np.float64)
    xx, yy = np.meshgrid(xs, ys)
    cx, cy = 105.0, 25.0
    dist2 = (xx - cx) ** 2 + (yy - cy) ** 2
    values = 120.0 * np.exp(-dist2 / 12.0)
    values += 8.0 * np.sin(xx * 2.0) * np.cos(yy * 2.0)
    values = np.clip(np.round(values, 2), 0.0, None).astype(np.float32)

    nodata = np.float32(-9999.0)
    values[0, 0] = nodata
    values[0, -1] = nodata
    values[-1, 0] = nodata
    values[-1, -1] = nodata

    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": float(nodata),
        "tiled": True,
        "blockxsize": 64,
        "blockysize": 64,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        dst.set_band_description(1, "synthetic precipitation (mm)")
    return path


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/sample/precip_sample.tif")
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 256
    path = generate(out, width, height)
    with rasterio.open(path) as ds:
        print(
            f"created {path} | {ds.width}x{ds.height} | crs={ds.crs} | "
            f"bands={ds.count} | nodata={ds.nodata}"
        )
        print(f"sample clip bbox (minx,miny,maxx,maxy) = {SAMPLE_BBOX}")


if __name__ == "__main__":
    main()
