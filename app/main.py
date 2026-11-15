"""FastAPI 入口：任务上传、状态查询、取消与结果下载。"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import clipping, config
from .database import JobDatabase
from .worker import JobManager


# ---------------------------------------------------------------- 资源生命周期
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db = JobDatabase(config.DB_PATH)
    manager = JobManager(db)
    # 重启恢复：半成品收敛为 failed 并释放名额；已完成文件仍可下载
    manager.recover_on_startup()
    app.state.db = db
    app.state.manager = manager
    try:
        yield
    finally:
        manager.shutdown()
        db.close()


app = FastAPI(
    title="GeoTIFF Clipping Service",
    version="1.0.0",
    description="单名额、可取消、崩溃可恢复的降水 GeoTIFF 裁切 API",
    lifespan=lifespan,
)


def get_db(request: Request) -> JobDatabase:
    return request.app.state.db


def get_manager(request: Request) -> JobManager:
    return request.app.state.manager


# ---------------------------------------------------------------- 响应模型
def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "source_name": job["source_name"],
        "bbox": job["bbox"],
        "download_url": job["download_url"],
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


# ---------------------------------------------------------------- 路由
@app.get("/health")
def health(request: Request) -> dict[str, str]:
    manager = get_manager(request)
    return {"status": "ok", "busy": str(manager.is_busy()).lower()}


@app.post("/api/jobs", status_code=202)
async def create_job(
    request: Request,
    file: UploadFile = File(..., description="EPSG:4326 北向上单波段 GeoTIFF"),
    minx: float = Form(..., description="西经（度）"),
    miny: float = Form(..., description="南纬（度）"),
    maxx: float = Form(..., description="东经（度）"),
    maxy: float = Form(..., description="北纬（度）"),
):
    """上传栅格与矩形范围，异步裁切，立即返回任务号。"""
    manager = get_manager(request)
    db = get_db(request)

    # 1. 先校验范围参数（不占名额）
    try:
        bbox = clipping.parse_bbox(minx, miny, maxx, maxy)
    except clipping.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 2. 流式保存上传文件（带大小限制），不占裁切名额
    safe_name = Path(file.filename or "upload.tif").name
    upload_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
    try:
        total = 0
        with upload_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {config.MAX_UPLOAD_BYTES} bytes",
                    )
                out.write(chunk)
        await file.close()

        # 3. 栅格约束校验与无交集检查（不占名额）
        try:
            dataset, _, _ = clipping.validate_raster(upload_path)
            try:
                clipping.compute_clip_window(dataset, bbox)
            finally:
                dataset.close()
        except clipping.NoIntersectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except clipping.ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # 4. 非阻塞抢占唯一名额，忙时拒绝新任务
        if not manager.try_acquire_slot():
            raise HTTPException(
                status_code=409,
                detail="a clipping job is already running; retry after it finishes",
            )

        # 5. 登记并启动后台裁切
        try:
            job_id = manager.submit(
                source_path=upload_path,
                source_name=safe_name,
                bbox=bbox,
            )
        except Exception:
            manager.release_slot()
            raise
    except HTTPException:
        _safe_remove(upload_path)
        raise
    except Exception as exc:
        _safe_remove(upload_path)
        raise HTTPException(status_code=500, detail=f"failed to accept upload: {exc}") from exc

    job = db.get_job(job_id)
    return JSONResponse(status_code=202, content=_job_view(job))


@app.get("/api/jobs")
def list_jobs(request: Request) -> dict[str, list[dict[str, Any]]]:
    """列出任务，不等待裁切结束。"""
    db = get_db(request)
    return {"jobs": [_job_view(j) for j in db.list_jobs()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    """查询单个任务状态，不等待裁切结束。"""
    db = get_db(request)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_view(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    """取消运行中的任务；已完整发布则报冲突。"""
    manager = get_manager(request)
    result = manager.request_cancel(job_id)
    if result == "unknown":
        raise HTTPException(status_code=404, detail="job not found")
    if result == "completed":
        raise HTTPException(
            status_code=409,
            detail="job has already completed and published its result; cancel rejected",
        )
    if result in ("not_running",):
        raise HTTPException(
            status_code=409,
            detail="job is already in a terminal state and cannot be cancelled",
        )
    if result == "timeout":
        raise HTTPException(
            status_code=500,
            detail="cancel requested but the worker did not stop in time; check job status",
        )
    db = get_db(request)
    return _job_view(db.get_job(job_id))


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str, request: Request):
    """下载已完成的结果文件；文件完整且登记成功才可下载。"""
    db = get_db(request)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != config.STATUS_COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"job is not completed (current status: {job['status']})",
        )
    output_path = Path(job["output_path"] or "")
    # 再次核对文件真实存在且非空，防止“登记成功但文件丢失”
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="result file is missing or incomplete")
    return FileResponse(
        path=output_path,
        media_type="image/tiff",
        filename=f"clip_{job_id}.tif",
    )


def _safe_remove(path: Optional[Path]) -> None:
    try:
        if path and path.exists():
            path.unlink()
    except OSError:
        pass
