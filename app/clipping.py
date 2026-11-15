"""栅格校验与可中断裁切。

裁切语义：
- 仅接受 EPSG:4326、北向上（north-up）的单波段 GeoTIFF；
- 按与矩形范围相交的像元窗口裁切，保留原始像元数值、NoData 与地理定位；
- 与范围无交集抛出 :class:`NoIntersectionError`；
- 分块读写，每块前检查取消信号，使长任务可被及时中断；
- 先写 ``.part`` 临时文件并 fsync，再由调用方原子改名，保证半成品永不发布。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import rasterio
from rasterio.errors import WindowError
from rasterio.windows import Window, from_bounds, intersection

from . import config


class ValidationError(ValueError):
    """输入栅格或范围不满足接口约束（HTTP 422）。"""


class NoIntersectionError(ValidationError):
    """裁切范围与栅格像元无交集（HTTP 422）。"""


class CancelledError(Exception):
    """裁切被取消信号中断（公开异常，供 worker 捕获）。"""


# 允许的上传后缀
GEOTIFF_SUFFIXES = {".tif", ".tiff", ".geotiff"}


# ---------------------------------------------------------------- 校验
def validate_raster(path: str | Path) -> tuple[rasterio.DatasetReaderBase, int, int]:
    """打开并校验栅格，返回 (dataset, width, height)。

    调用方负责关闭 dataset。
    """
    path = Path(path)
    if path.suffix.lower() not in GEOTIFF_SUFFIXES:
        raise ValidationError(f"unsupported file extension: {path.suffix} (expect .tif/.tiff)")

    try:
        dataset = rasterio.open(path)
    except rasterio.RasterioIOError as exc:
        raise ValidationError(f"cannot open raster with rasterio: {exc}") from exc

    try:
        if dataset.count != 1:
            raise ValidationError(
                f"single-band raster required, got {dataset.count} bands"
            )
        crs = dataset.crs
        if crs is None or crs.to_epsg() != 4326:
            raise ValidationError(
                f"raster CRS must be EPSG:4326, got {crs if crs is not None else 'undefined'}"
            )
        # 北向上：transform 无旋转（b == d == 0）且像元高度为负（行向下、北向上）
        transform = dataset.transform
        if transform.b != 0.0 or transform.d != 0.0:
            raise ValidationError("raster must be north-up (no rotation allowed)")
        if transform.e >= 0.0:
            raise ValidationError("raster must be north-up (negative pixel height required)")
        if dataset.width <= 0 or dataset.height <= 0:
            raise ValidationError("raster has empty dimensions")
        return dataset, dataset.width, dataset.height
    except Exception:
        dataset.close()
        raise


def parse_bbox(minx: float, miny: float, maxx: float, maxy: float) -> tuple[float, float, float, float]:
    """校验矩形范围参数。"""
    values = (minx, miny, maxx, maxy)
    if any(v is None for v in values):
        raise ValidationError("all bbox coordinates are required")
    if not all(-180.0 <= v <= 180.0 if i in (0, 2) else -90.0 <= v <= 90.0
               for i, v in enumerate(values)):
        raise ValidationError(
            "bbox coordinates out of lon [-180,180] / lat [-90,90] range"
        )
    if minx >= maxx or miny >= maxy:
        raise ValidationError("bbox requires minx < maxx and miny < maxy")
    return float(minx), float(miny), float(maxx), float(maxy)


def compute_clip_window(
    dataset: rasterio.DatasetReaderBase,
    bbox: tuple[float, float, float, float],
) -> Window:
    """计算与 bbox 相交的像元窗口（四舍五入到完整像元）。"""
    minx, miny, maxx, maxy = bbox
    # from_bounds 给出覆盖该地理范围的最小像元窗口（取整）
    try:
        requested = from_bounds(
            minx, miny, maxx, maxy, transform=dataset.transform
        )
    except WindowError as exc:
        raise ValidationError(f"invalid bbox for this raster transform: {exc}") from exc

    full = Window(0, 0, dataset.width, dataset.height)
    try:
        window = intersection(full, requested)
    except WindowError as exc:
        raise NoIntersectionError("bbox does not intersect the raster extent") from exc

    window = window.round_lengths(op="ceil").round_offsets(op="floor")
    if int(window.width) <= 0 or int(window.height) <= 0:
        raise NoIntersectionError("bbox does not intersect any raster pixel")
    return window


# ---------------------------------------------------------------- 裁切
def clip_raster(
    source_path: str | Path,
    bbox: tuple[float, float, float, float],
    final_output_path: str | Path,
    is_cancelled: Callable[[], bool],
) -> Path:
    """执行裁切，成功后返回最终输出路径。

    :param is_cancelled: 每个块处理前调用，返回 True 时立即中止；
    :param final_output_path: 发布后的目标路径，实际先写同级 ``.part`` 文件。
    """
    source_path = Path(source_path)
    final_output_path = Path(final_output_path)
    part_path = final_output_path.with_name(final_output_path.name + ".part")
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    # 清理可能残留的同名临时文件
    if part_path.exists():
        part_path.unlink()

    dataset, _, _ = validate_raster(source_path)
    try:
        window = compute_clip_window(dataset, bbox)
        col_off, row_off = int(window.col_off), int(window.row_off)
        width, height = int(window.width), int(window.height)
        dst_transform = dataset.window_transform(window)

        profile = dataset.profile.copy()
        profile.update(
            driver="GTiff",
            width=width,
            height=height,
            transform=dst_transform,
        )
        # GeoTIFF 瓦片宽高必须为 16 的倍数；窗口过小时退化为条带存储
        tile = min(256, max(16, width // 16 * 16), max(16, height // 16 * 16))
        if width >= 16 and height >= 16:
            profile.update(tiled=True, blockxsize=tile, blockysize=tile)
        else:
            profile.update(tiled=False)
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)

        with rasterio.open(part_path, "w", **profile) as dst:
            # 逐源文件块读取、按交集写入目标窗口
            for _, src_window in dataset.block_windows(1):
                if is_cancelled():
                    raise CancelledError()

                try:
                    block = src_window.intersection(window)
                except WindowError:
                    # 该源块与裁切窗口不相交，跳过
                    continue
                if block.width <= 0 or block.height <= 0:
                    continue

                data = dataset.read(1, window=block)
                dst_window = Window(
                    block.col_off - col_off,
                    block.row_off - row_off,
                    block.width,
                    block.height,
                )
                dst.write(data, 1, window=dst_window)

                if config.CLIP_BLOCK_DELAY > 0:
                    # 可中断睡眠：把延迟切成小段以快速响应取消
                    deadline = time.monotonic() + config.CLIP_BLOCK_DELAY
                    while time.monotonic() < deadline:
                        if is_cancelled():
                            raise CancelledError()
                        time.sleep(min(0.02, deadline - time.monotonic()))
        # 退出 with 块时 GDAL 负责刷写并关闭数据集，随后 fsync 文件
        # fsync 数据内容，保证崩溃后文件完整
        _fsync_file(part_path)
    except CancelledError:
        dataset.close()
        _safe_unlink(part_path)
        raise
    except Exception:
        dataset.close()
        _safe_unlink(part_path)
        raise
    else:
        dataset.close()

    if is_cancelled():
        # 写盘已结束但尚未发布：取消仍然生效，绝不发布结果
        _safe_unlink(part_path)
        raise CancelledError()

    # 原子改名发布：同目录 rename 在本地卷上为原子操作
    os.replace(part_path, final_output_path)
    _fsync_directory(final_output_path.parent)
    return final_output_path


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
