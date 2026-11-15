"""运行期配置。所有持久化数据都位于数据卷 DATA_DIR 下。"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


# 数据卷根目录（compose 中挂载为命名卷 /data）
DATA_DIR = _env_path("DATA_DIR", Path("/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "clip.db"

# 已登记任务允许的状态值
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED)

# 裁切时每个块之间的可中断睡眠（秒）。
# 生产为 0；测试可调大以便在运行中取消。
CLIP_BLOCK_DELAY = float(os.environ.get("CLIP_BLOCK_DELAY", "0"))

# 取消接口等待裁切线程实际停下的最长时间（秒）
CANCEL_WAIT_TIMEOUT = float(os.environ.get("CANCEL_WAIT_TIMEOUT", "10"))

# 上传文件大小上限（字节），默认 200 MB
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))


def ensure_dirs() -> None:
    """创建数据卷子目录。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
