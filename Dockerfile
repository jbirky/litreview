# The litreview web UI, with the CLI it runs reviews with. To build and run it, see "Web UI" in README.md.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY litreview/web/ui/package.json litreview/web/ui/package-lock.json ./
RUN npm ci
COPY litreview/web/ui/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LITREVIEW_HOME=/app
COPY pyproject.toml README.md LICENSE ./
COPY litreview ./litreview
RUN pip install --no-cache-dir -e '.[web]'
COPY --from=ui /ui/dist ./litreview/web/ui/dist
EXPOSE 5070
CMD ["uvicorn", "litreview.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "5070"]
