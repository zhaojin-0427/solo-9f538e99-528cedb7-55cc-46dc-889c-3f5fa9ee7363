# 容器启动：确保示例栅格存在，然后启动 API
#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
SAMPLE="$DATA_DIR/sample/precip_sample.tif"

mkdir -p "$DATA_DIR/sample"
if [ ! -f "$SAMPLE" ]; then
    echo "[entrypoint] generating sample raster at $SAMPLE"
    python scripts/generate_sample.py "$SAMPLE" 256 256
else
    echo "[entrypoint] sample raster already exists: $SAMPLE"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
