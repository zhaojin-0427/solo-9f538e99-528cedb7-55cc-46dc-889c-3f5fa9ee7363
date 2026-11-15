# GeoTIFF 裁切服务（FastAPI + Rasterio + SQLite）

把降水 GeoTIFF 按矩形研究区裁切的异步 API。全服务**同一时刻只允许一个裁切任务运行**，
忙时拒绝新任务；运行中可取消，取消会真正中断计算并释放名额；服务重启后自动清理半成品，
已完成的结果仍可下载。

## 目录结构

```
.
├── app/
│   ├── config.py       # 数据卷路径与可调参数（环境变量覆盖）
│   ├── database.py     # SQLite 任务登记表（状态唯一事实来源，WAL）
│   ├── clipping.py     # 栅格校验 + 可中断分块裁切（.part -> 原子 rename）
│   ├── worker.py       # 单名额任务槽、后台线程、取消、启动恢复
│   └── main.py         # FastAPI 路由
├── scripts/
│   ├── generate_sample.py  # 生成小型示例 GeoTIFF
│   └── selftest.py         # 端到端自测（真实 uvicorn + HTTP + 重启恢复）
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh    # 容器启动时确保示例栅格存在
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互式文档: http://localhost:8000/docs
```

容器首次启动会在数据卷 `/data/sample/precip_sample.tif` 生成一个
256×256、EPSG:4326、北向上、单波段、含 NoData(-9999) 的示例降水栅格。
所有持久化数据（上传文件、结果文件、SQLite、示例栅格）都在命名卷 `clip-data` 中。

## 本地运行（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data
python scripts/generate_sample.py ./data/sample/precip_sample.tif
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET`  | `/health` | 健康检查，含 `busy` 名额状态 |
| `POST` | `/api/jobs` | 上传栅格 + bbox，异步裁切，返回 `202` 与任务号 |
| `GET`  | `/api/jobs` | 任务列表（不等待裁切） |
| `GET`  | `/api/jobs/{id}` | 单任务状态查询（不等待裁切） |
| `POST` | `/api/jobs/{id}/cancel` | 取消运行中任务；已发布结果返回 `409` |
| `GET`  | `/api/jobs/{id}/download` | 下载已完成结果（文件完整且登记成功才可下载） |

任务状态：`running` → `completed` / `cancelled` / `failed`。

### 调用示例

```bash
# 1) 提交裁切（multipart 表单：file + bbox 四个坐标）
curl -X POST http://localhost:8000/api/jobs \
  -F "file=@data/sample/precip_sample.tif" \
  -F "minx=103" -F "miny=23" -F "maxx=107" -F "maxy=27"
# -> 202 {"job_id":"...","status":"running",...}

# 2) 查询状态（立即返回，不等待裁切结束）
curl http://localhost:8000/api/jobs/<job_id>

# 3) 忙时再提交 -> 409；运行中取消
curl -X POST http://localhost:8000/api/jobs/<job_id>/cancel

# 4) 完成后下载
curl -OJ http://localhost:8000/api/jobs/<job_id>/download
```

## 关键语义与实现保证

### 输入约束（`app/clipping.py`）
- 仅接受 **EPSG:4326、北向上（无旋转、像元高度为负）、单波段** GeoTIFF，违反返回 `422`；
- bbox 为 `minx < maxx`、`miny < maxy` 的经纬度矩形；
- 用 `rasterio.windows.from_bounds` + 窗口交集求与范围相交的像元窗口，
  **无交集返回 `422`**；
- 输出保留原始像元数值、NoData 和地理定位（窗口 transform 由 `window_transform` 推导）。

### 单名额与异步（`app/worker.py`）
- 一个全局非阻塞锁作为任务槽，`acquire(blocking=False)` 失败立即 `409`，不排队；
- 裁切在 daemon 线程执行，接口只负责登记并返回任务号；
- 状态查询与下载均只读 SQLite/文件，不触碰任务槽。

### 可中断取消
- 每任务一个 `threading.Event`，裁切**逐像元块**检查信号，可中断睡眠保证快速响应；
- 取消接口等待线程真正结束：线程先释放名额、再置位完成事件，因此
  **确认取消成功时计算已停止且名额已释放**；
- 取消生效后删除 `.part` 并登记 `cancelled`，**此后绝不发布结果**；
- 已完整发布（completed）的任务取消返回 `409 Conflict`。

### 崩溃安全与重启恢复
- 结果先写 `<job_id>.tif.part`，fsync 后 `os.replace` **原子改名**，再登记 completed + 下载地址；
- 因此“写盘失败 / 登记失败 / 服务中断”时，未完成任务在数据库里只会停留在 running，
  **不可能显示成功**；
- 启动时（`recover_on_startup`）：
  1. 所有非终态任务重置为 `failed`（error: interrupted by service restart），名额随之释放；
  2. 清理 `outputs/` 下全部 `*.part` 半成品；
  3. 防御性核对：已 completed 但文件丢失的任务降级 failed，保证“文件完整才可下载”；
  4. 已 completed 且文件完好的任务不受影响，仍可下载。

### 下载保护
下载接口同时要求数据库状态为 `completed` **且**结果文件真实存在、非空，否则 `409/404`。

## 自测

```bash
pip install -r requirements.txt   # 含 numpy/rasterio
SELFTEST_PYTHON=$(which python) python scripts/selftest.py
```

脚本会在临时目录启动**真实 uvicorn 进程**，覆盖：正常裁切与内容核对、忙时 409、
并发抢名额、运行中取消的及时性与名额释放、无交集/非法 bbox/错误 CRS/多波段拒绝、
取消已完成任务冲突、下载保护，以及 SIGKILL 硬崩溃后的重启恢复（半成品清理、
任务收敛 failed、已完成文件仍可下载）。当前全部 25 项断言通过。

> 注意：自测脚本不要用 `python -I` 运行（隔离模式会忽略 PYTHONPATH，
> 导致 uvicorn 子进程无法导入 `app`）。

## 可调环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | 数据卷根目录 |
| `CLIP_BLOCK_DELAY` | `0` | 每个块后的延迟秒数；调大便于观察/测试取消 |
| `CANCEL_WAIT_TIMEOUT` | `10` | 取消接口等待线程停下的上限 |
| `MAX_UPLOAD_BYTES` | `209715200` | 上传大小上限（200 MB） |
