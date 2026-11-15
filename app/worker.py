"""单名额任务槽与后台裁切线程。

名额语义：
- 全局最多一个裁切任务运行，``try_acquire_slot`` 非阻塞，忙时立即拒绝；
- 取消通过每任务 :class:`threading.Event` 协作中断，cancel 会等待线程
  真正停下并释放名额后才返回成功；
- 任务终态（completed/cancelled/failed）只在本模块落库，半成品文件先清理；
- 启动时把数据库中所有未终态任务重置为 failed，并清理半成品与孤儿文件。
"""

from __future__ import annotations

import threading
import traceback
import uuid
from pathlib import Path
from typing import Optional

from . import clipping, config
from .database import JobDatabase


class JobManager:
    def __init__(self, db: JobDatabase) -> None:
        self.db = db
        # 单任务名额：非阻塞获取，忙时拒绝
        self._slot = threading.Lock()
        # 当前运行任务的取消信号
        self._cancel_event: Optional[threading.Event] = None
        self._current_job_id: Optional[str] = None
        # 取消方等待线程实际停下的事件
        self._job_done_event: Optional[threading.Event] = None
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------- 生命周期
    def recover_on_startup(self) -> list[str]:
        """服务启动恢复：重置未终态任务、清理半成品、释放名额。

        已 completed 的任务保持不变，其结果文件仍可下载；
        若登记成功但文件丢失，降级为 failed（保证“文件完整才可下载”）。
        """
        config.ensure_dirs()
        interrupted = self.db.reset_interrupted_jobs()

        # 清理 outputs 下所有 .part 半成品（写盘中断的残留）
        for part in config.OUTPUT_DIR.glob("*.part"):
            try:
                part.unlink()
            except OSError:
                pass

        # 防御性核对：已完成任务的结果文件必须真实存在，否则降级 failed
        for job in self.db.list_jobs(limit=10000):
            if job["status"] == config.STATUS_COMPLETED:
                output = job["output_path"]
                if not output or not Path(output).is_file():
                    self.db.mark_failed(
                        job["job_id"], "result file missing after restart"
                    )
        return interrupted

    def shutdown(self) -> None:
        """进程退出前尽量中止正在运行的裁切（不阻塞过久）。"""
        with self._state_lock:
            event = self._cancel_event
        if event is not None:
            event.set()
        if self._job_done_event is not None:
            self._job_done_event.wait(timeout=5)

    # ---------------------------------------------------------------- 名额
    def is_busy(self) -> bool:
        return self._slot.locked()

    def try_acquire_slot(self) -> bool:
        return self._slot.acquire(blocking=False)

    def release_slot(self) -> None:
        """提交前校验失败时释放已占名额（仅持锁线程调用）。"""
        try:
            self._slot.release()
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- 提交
    def submit(
        self,
        source_path: Path,
        source_name: str,
        bbox: tuple[float, float, float, float],
    ) -> str:
        """名额已由调用方获取；登记任务并启动后台线程，返回 job_id。"""
        job_id = uuid.uuid4().hex
        self.db.create_job(
            job_id=job_id,
            source_name=source_name,
            source_path=str(source_path),
            bbox=",".join(str(v) for v in bbox),
        )
        cancel_event = threading.Event()
        done_event = threading.Event()
        with self._state_lock:
            self._cancel_event = cancel_event
            self._current_job_id = job_id
            self._job_done_event = done_event

        thread = threading.Thread(
            target=self._run,
            args=(job_id, source_path, bbox, cancel_event, done_event),
            name=f"clip-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return job_id

    # ---------------------------------------------------------------- 取消
    def request_cancel(self, job_id: str) -> str:
        """请求取消任务。

        返回值：
        - ``"cancelled"``：任务原本在运行，已确认停止并释放名额；
        - ``"completed"``：结果已完整发布，取消冲突（HTTP 409）；
        - ``"not_running"``：任务已是 cancelled/failed 等终态（HTTP 409）；
        - ``"unknown"``：任务不存在（HTTP 404）。
        """
        job = self.db.get_job(job_id)
        if job is None:
            return "unknown"
        if job["status"] == config.STATUS_COMPLETED:
            return "completed"
        if job["status"] in (config.STATUS_CANCELLED, config.STATUS_FAILED):
            return "not_running"

        with self._state_lock:
            # 锁内复查数据库终态，消除“线程在初次检查后刚好完成”的竞态
            fresh = self.db.get_job(job_id)
            if fresh is None:
                return "unknown"
            if fresh["status"] == config.STATUS_COMPLETED:
                return "completed"
            if fresh["status"] in (config.STATUS_CANCELLED, config.STATUS_FAILED):
                return "not_running"
            is_current = self._current_job_id == job_id
            event = self._cancel_event if is_current else None
            done = self._job_done_event if is_current else None

        if event is None:
            # 数据库未终态但没有活动线程（异常窗口）：直接收敛状态
            self.db.mark_cancelled(job_id)
            return "cancelled"

        event.set()
        # 等待裁切线程实际停下（块粒度中断，通常很快）。
        # done_event 在名额释放之后才置位，因此等到即代表已停止并释放名额。
        finished = done.wait(timeout=config.CANCEL_WAIT_TIMEOUT)
        if not finished:
            return "timeout"
        job = self.db.get_job(job_id)
        if job["status"] == config.STATUS_CANCELLED:
            return "cancelled"
        # 恰好赶在取消生效前完成发布
        if job["status"] == config.STATUS_COMPLETED:
            return "completed"
        return "not_running"

    # ---------------------------------------------------------------- 线程
    def _run(
        self,
        job_id: str,
        source_path: Path,
        bbox: tuple[float, float, float, float],
        cancel_event: threading.Event,
        done_event: threading.Event,
    ) -> None:
        output_path = config.OUTPUT_DIR / f"{job_id}.tif"
        try:
            clipping.clip_raster(
                source_path=source_path,
                bbox=bbox,
                final_output_path=output_path,
                is_cancelled=cancel_event.is_set,
            )
        except clipping.CancelledError:
            self._cleanup_output(output_path)
            self.db.mark_cancelled(job_id)
        except clipping.ValidationError as exc:
            self._cleanup_output(output_path)
            self.db.mark_failed(job_id, f"validation error: {exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要收敛为 failed
            self._cleanup_output(output_path)
            self.db.mark_failed(
                job_id, f"{type(exc).__name__}: {exc} | {traceback.format_exc(limit=3)}"
            )
        else:
            # 发布瞬间再次核对取消信号：取消生效后绝不登记成功
            if cancel_event.is_set():
                self._cleanup_output(output_path)
                self.db.mark_cancelled(job_id)
            elif not output_path.is_file() or output_path.stat().st_size == 0:
                # 只有文件完整落盘（原子 rename 已完成）后才登记成功与下载地址
                self._cleanup_output(output_path)
                self.db.mark_failed(job_id, "output file missing or empty after clip")
            else:
                self.db.mark_completed(
                    job_id,
                    output_path=str(output_path),
                    download_url=f"/api/jobs/{job_id}/download",
                )
        finally:
            # 名额先释放，再通知等待者：保证取消确认时计算已停止且名额已释放
            try:
                self._slot.release()
            except RuntimeError:
                pass
            with self._state_lock:
                if self._current_job_id == job_id:
                    self._current_job_id = None
                    self._cancel_event = None
                    self._job_done_event = None
            done_event.set()

    @staticmethod
    def _cleanup_output(output_path: Path) -> None:
        for p in (output_path, output_path.with_name(output_path.name + ".part")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
