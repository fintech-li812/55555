# 编排网关 + MCP 工具层容器镜像（自有服务器部署用）
# 构建：docker build -t cashflow-risk-v10 -f deploy/v10/Dockerfile .
# 运行：见 deploy/v10/docker-compose.yml
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt 2>/dev/null || true
RUN pip install --no-cache-dir pymysql

COPY mcp-server /app/mcp-server
COPY qwen-integration /app/qwen-integration
COPY web /app/web
COPY sql /app/sql
COPY deploy /app/deploy

# 应用只听回环，公网访问由 Nginx 反代（见 nginx-v10.conf）
EXPOSE 8090 8100

# 【待替换项 TODO】镜像内不含任何口令；全部通过环境变量 / env_file 注入
#   CF_DB_*  CF_MCP_TOKEN  QWEN_API_BASE  QWEN_API_KEY
CMD ["sh", "-c", "\
     python /app/mcp-server/program_v1/src/v8_mcp_server.py --http --port 8090 & \
     cd /app/qwen-integration && python -m orchestrator.gateway"]
