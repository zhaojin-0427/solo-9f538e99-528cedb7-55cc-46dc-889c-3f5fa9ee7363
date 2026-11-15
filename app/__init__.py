"""GeoTIFF 裁切服务。

模块划分：
- config: 数据卷路径与可调参数
- database: SQLite 任务登记表（状态机的唯一事实来源）
- clipping: 栅格校验与可中断的分块裁切
- worker: 单名额任务槽、后台线程、取消与重启恢复
- main: FastAPI 路由
"""

__version__ = "1.0.0"
