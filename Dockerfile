FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY packages/ packages/
COPY apps/ apps/
COPY web/ web/
COPY pyproject.toml .

ENV PYTHONPATH=/app/packages/contracts:/app/packages/routing:/app/packages/geodata:/app/packages/engine:/app/apps/api
ENV RACEENGINE_DATA=/app/data
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "raceengine_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
