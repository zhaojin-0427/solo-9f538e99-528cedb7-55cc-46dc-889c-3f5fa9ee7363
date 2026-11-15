"""端到端自测：启动真实 uvicorn 进程，用标准库调用全部关键流程。

覆盖：
1. 健康检查与示例栅格生成（合法 / 错误 CRS / 多波段）
2. 正常裁切 -> completed -> 下载，并核对像元值、NoData、地理定位
3. 忙时拒绝第二个任务（409）
4. 运行中取消 -> cancelled，名额释放，无结果文件
5. 无交集 / 非法 bbox -> 422；非法栅格 -> 422
6. 取消已完成任务 -> 409；下载未完成/不存在 -> 409/404
7. 服务重启恢复：运行中任务收敛 failed、.part 清理、名额释放，已完成文件仍可下载

用法：python scripts/selftest.py
注意：脚本会启动 uvicorn 子进程，请勿使用 `python -I` 运行（-I 会忽略
PYTHONPATH 导致子进程无法导入 app）。
环境变量 CLIP_BLOCK_DELAY 控制每个块的延迟（默认脚本内置 0.02s）。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = Path(os.environ.get("SELFTEST_DIR", "/tmp/clip_selftest"))
VENV_PYTHON = os.environ.get("SELFTEST_PYTHON", sys.executable)


# ------------------------------------------------------------ 工具
def log(ok: bool, msg: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
    if not ok:
        log.failed += 1


log.failed = 0


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(data_dir: Path, block_delay: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(data_dir),
            "CLIP_BLOCK_DELAY": block_delay,
            "CANCEL_WAIT_TIMEOUT": "10",
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
        [
            VENV_PYTHON, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not wait_port(port):
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:  # noqa: BLE001
            out = ""
        raise RuntimeError(f"server did not start on {port}\n{out}")
    return proc


def stop_server(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=5)
    return out or ""


def hard_kill_server(proc: subprocess.Popen) -> None:
    """模拟服务崩溃/掉电：SIGKILL 不执行任何 Python 清理逻辑。"""
    proc.kill()
    proc.communicate(timeout=10)


def http(method: str, url: str, fields: dict | None = None,
         file_field: tuple[str, Path] | None = None, timeout: float = 30.0):
    """发起 HTTP 请求，返回 (status, headers, body_bytes/json)。"""
    boundary = f"----clip{uuid.uuid4().hex}"
    data = None
    headers = {}
    if fields is not None or file_field is not None:
        buf = bytearray()
        for key, value in (fields or {}).items():
            buf.extend(f"--{boundary}\r\n".encode())
            buf.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            buf.extend(f"{value}\r\n".encode())
        if file_field is not None:
            name, path = file_field
            buf.extend(f"--{boundary}\r\n".encode())
            buf.extend(
                f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode()
            )
            buf.extend(b"Content-Type: image/tiff\r\n\r\n")
            buf.extend(path.read_bytes())
            buf.extend(b"\r\n")
        buf.extend(f"--{boundary}--\r\n".encode())
        data = bytes(buf)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            resp_headers = resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        resp_headers = exc.headers

    ctype = resp_headers.get("Content-Type", "") if resp_headers else ""
    parsed = json.loads(body) if "application/json" in ctype.lower() else body
    return status, parsed


def wait_status(base: str, job_id: str, target: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, body = http("GET", f"{base}/api/jobs/{job_id}")
        last = body
        if body.get("status") in ("completed", "cancelled", "failed"):
            if target is None or body.get("status") == target:
                return body
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach {target}; last={last}")


# ------------------------------------------------------------ 造数
def make_raster(path: Path, width: int, height: int, crs: str = "EPSG:4326",
                bands: int = 1, nodata: float | None = -9999.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    west, south, east, north = 100.0, 20.0, 110.0, 30.0
    transform = from_bounds(west, south, east, north, width, height)
    data = (np.arange(width * height, dtype=np.float32).reshape(height, width) % 1000)
    if bands > 1:
        data = np.stack([data] * bands)
    profile = {
        "driver": "GTiff", "width": width, "height": height, "count": bands,
        "dtype": "float32", "crs": crs, "transform": transform, "tiled": True,
        "blockxsize": min(64, width), "blockysize": min(64, height),
    }
    if nodata is not None:
        profile["nodata"] = nodata
        if bands == 1:
            data[0, 0] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        if bands == 1:
            dst.write(data, 1)
        else:
            dst.write(data)


def expected_window(src: Path, bbox):
    with rasterio.open(src) as ds:
        from rasterio.windows import from_bounds as fb, intersection, Window
        requested = fb(*bbox, transform=ds.transform)
        win = intersection(Window(0, 0, ds.width, ds.height), requested)
        win = win.round_lengths(op="ceil").round_offsets(op="floor")
        values = ds.read(1, window=win)
        return win, values, ds.window_transform(win), ds.nodata, ds.crs


# ------------------------------------------------------------ 主流程
def main() -> int:
    if TEST_ROOT.exists():
        import shutil
        shutil.rmtree(TEST_ROOT)
    data_dir = TEST_ROOT / "data"
    fixtures = TEST_ROOT / "fixtures"
    data_dir.mkdir(parents=True)
    fixtures.mkdir(parents=True)

    raster = fixtures / "precip.tif"
    big_raster = fixtures / "precip_big.tif"
    bad_crs = fixtures / "bad_crs.tif"
    multiband = fixtures / "multiband.tif"
    make_raster(raster, 256, 256)
    make_raster(big_raster, 1024, 1024)
    make_raster(bad_crs, 64, 64, crs="EPSG:3857")
    make_raster(multiband, 64, 64, bands=3)

    port = int(os.environ.get("SELFTEST_PORT", "18080"))
    base = f"http://127.0.0.1:{port}"
    block_delay = os.environ.get("CLIP_BLOCK_DELAY", "0.02")
    proc = start_server(data_dir, block_delay, port)
    bbox = {"minx": "103", "miny": "23", "maxx": "107", "maxy": "27"}

    try:
        # 1. 健康检查
        status, body = http("GET", f"{base}/health")
        log(status == 200 and body["status"] == "ok", f"health check -> {status} {body}")

        # 2. 正常裁切 + 下载 + 内容核对
        status, job = http("POST", f"{base}/api/jobs", fields=bbox,
                           file_field=("file", raster))
        log(status == 202 and job["status"] == "running", f"create job -> {status} {job.get('status')}")
        job_id = job["job_id"]
        done = wait_status(base, job_id, "completed")
        log(done["status"] == "completed" and done["download_url"],
            f"job completes with download_url: {done['download_url']}")

        status, content = http("GET", f"{base}{done['download_url']}")
        log(status == 200 and len(content) > 0, "download completed result file")
        out_tif = fixtures / "downloaded.tif"
        out_tif.write_bytes(content)

        win, exp_values, exp_transform, exp_nodata, exp_crs = expected_window(raster, (103, 23, 107, 27))
        with rasterio.open(out_tif) as ds:
            got = ds.read(1)
            ok = (
                ds.count == 1
                and ds.crs.to_epsg() == exp_crs.to_epsg() == 4326
                and ds.width == int(win.width) and ds.height == int(win.height)
                and np.allclose(ds.transform[:6], exp_transform[:6])
                and ds.nodata == exp_nodata
                and got.shape == exp_values.shape
                and np.array_equal(got, exp_values)
            )
        log(ok, "clipped raster preserves values, nodata and georeference")

        # 3. 忙时拒绝：先启动一个大影像长任务，确认 running 后再提交第二个
        status, long_job = http("POST", f"{base}/api/jobs", fields=bbox,
                                file_field=("file", big_raster))
        log(status == 202, f"create long-running job -> {status}")
        deadline = time.time() + 10
        while time.time() < deadline:
            st, cur = http("GET", f"{base}/api/jobs/{long_job['job_id']}")
            if cur["status"] == "running":
                break
            time.sleep(0.05)
        log(cur["status"] == "running", f"long job is running: {cur['status']}")

        status, busy_job = http("POST", f"{base}/api/jobs", fields=bbox,
                                file_field=("file", raster))
        log(status == 409, f"busy slot rejects second job -> {status}")
        if status == 202:  # 防御：万一没忙起来，避免泄漏任务
            wait_status(base, busy_job["job_id"], None)

        # 4. 运行中取消（长任务仍占着名额）
        job2 = long_job
        t0 = time.time()
        status, cancelled = http("POST", f"{base}/api/jobs/{job2['job_id']}/cancel")
        elapsed = time.time() - t0
        log(status == 200 and cancelled["status"] == "cancelled",
            f"cancel running job -> {status} {cancelled.get('status')} in {elapsed:.2f}s")
        log(elapsed < 5.0, "cancel returns promptly without waiting for natural finish")

        # 名额已释放：新任务可立即提交并成功
        status, job3 = http("POST", f"{base}/api/jobs", fields=bbox,
                            file_field=("file", raster))
        log(status == 202, f"slot released after cancel, new job accepted -> {status}")
        done3 = wait_status(base, job3["job_id"], "completed")
        log(done3["status"] == "completed", "job after cancel completes successfully")

        # 取消的任务无结果文件、无 .part 残留
        parts = list((data_dir / "outputs").glob("*.part"))
        cancelled_file = data_dir / "outputs" / f"{job2['job_id']}.tif"
        log(not parts and not cancelled_file.exists(),
            "no output file nor .part leftover for cancelled job")

        # 5. 错误输入
        far_bbox = {"minx": "0", "miny": "0", "maxx": "1", "maxy": "1"}
        status, body = http("POST", f"{base}/api/jobs", fields=far_bbox,
                            file_field=("file", raster))
        log(status == 422, f"no-intersection bbox rejected -> {status} {body.get('detail')}")

        bad_bbox = {"minx": "107", "miny": "27", "maxx": "103", "maxy": "23"}
        status, body = http("POST", f"{base}/api/jobs", fields=bad_bbox,
                            file_field=("file", raster))
        log(status == 422, f"invalid bbox rejected -> {status}")

        status, body = http("POST", f"{base}/api/jobs", fields=bbox,
                            file_field=("file", bad_crs))
        log(status == 422 and "4326" in str(body.get("detail", "")),
            f"non-EPSG4326 raster rejected -> {status}")

        status, body = http("POST", f"{base}/api/jobs", fields=bbox,
                            file_field=("file", multiband))
        log(status == 422 and "single-band" in str(body.get("detail", "")),
            f"multi-band raster rejected -> {status}")

        # 6. 冲突与不存在
        status, body = http("POST", f"{base}/api/jobs/{job_id}/cancel")
        log(status == 409, f"cancel completed job conflicts -> {status}")
        status, body = http("GET", f"{base}/api/jobs/nonexistent/download")
        log(status == 404, f"download unknown job -> {status}")
        status, body = http("GET", f"{base}/api/jobs/{job2['job_id']}/download")
        log(status == 409, f"download cancelled job rejected -> {status}")

        completed_id = job_id
        outputs_before = list((data_dir / "outputs").glob("*.tif"))
    finally:
        stop_server(proc)

    # 7. 重启恢复：先制造一个“运行中”的进程再 SIGKILL（模拟硬崩溃，无清理钩子）
    proc2 = start_server(data_dir, block_delay, port)
    try:
        status, kill_job = http("POST", f"{base}/api/jobs", fields=bbox,
                                file_field=("file", big_raster))
        assert status == 202, (status, kill_job)
        deadline = time.time() + 10
        while time.time() < deadline:
            st, cur = http("GET", f"{base}/api/jobs/{kill_job['job_id']}")
            if cur["status"] == "running":
                break
            time.sleep(0.05)
        assert cur["status"] == "running", cur
    finally:
        hard_kill_server(proc2)

    # 硬崩溃可能留下 .part 半成品；关键是重启后必须被清理，且任务不得显示成功
    interrupted_final = data_dir / "outputs" / f"{kill_job['job_id']}.tif"
    log(not interrupted_final.exists(),
        "hard-interrupted job never published a final result file")

    proc3 = start_server(data_dir, block_delay, port)
    try:
        status, recovered = http("GET", f"{base}/api/jobs/{kill_job['job_id']}")
        log(status == 200 and recovered["status"] == "failed"
            and "restart" in (recovered.get("error") or ""),
            f"interrupted job marked failed after restart -> {recovered.get('status')}")

        leftovers = list((data_dir / "outputs").glob("*.part"))
        log(not leftovers, f"startup cleanup removes .part leftovers ({len(leftovers)} remain)")

        status, body = http("GET", f"{base}/health")
        log(status == 200 and body["busy"] == "false", "slot released after restart")

        status, body = http("GET", f"{base}/api/jobs/{completed_id}/download")
        log(status == 200 and len(body) > 0, "previously completed file still downloadable")

        status, again = http("POST", f"{base}/api/jobs", fields=bbox,
                             file_field=("file", raster))
        log(status == 202, f"new job accepted after recovery -> {status}")
        if status == 202:
            wait_status(base, again["job_id"], "completed")
    finally:
        stop_server(proc3)

    print("\n" + ("=" * 60))
    if log.failed:
        print(f"SELFTEST FAILED: {log.failed} assertion(s) failed")
        return 1
    print("ALL SELFTEST CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
