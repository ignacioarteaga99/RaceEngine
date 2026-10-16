FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY packages/ packages/
COPY apps/ apps/
COPY web/ web/
COPY data/custom_regions.json data/custom_regions.json
COPY pyproject.toml .

ENV PYTHONPATH=/app/packages/contracts:/app/packages/routing:/app/packages/geodata:/app/packages/engine:/app/apps/api
ENV RACEENGINE_DATA=/app/data
ENV PORT=8000
EXPOSE 8000

# PORT lo fija el proveedor (Render usa 10000); en local queda en 8000.
CMD ["sh", "-c", "exec python -m uvicorn raceengine_api.main:app --host 0.0.0.0 --port ${PORT}"]
