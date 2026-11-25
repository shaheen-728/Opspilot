FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data ./data
RUN uv sync --locked --no-dev --extra postgres --extra tracing \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir /app/var && chown app:app /app/var
USER 10001:10001
EXPOSE 8001
CMD ["uvicorn", "opspilot.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
