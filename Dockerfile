FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN UV_CONCURRENT_DOWNLOADS=4 UV_HTTP_RETRIES=10 uv sync --frozen --no-dev
COPY POLICY.md ./
COPY evals ./evals
EXPOSE 8002
CMD ["uv", "run", "--no-sync", "uvicorn", "support_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8002"]
