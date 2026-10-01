FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY burst.py .
ENV PORT=8000
# single process: in-process Prometheus counters stay accurate
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --backlog 8192 --no-access-log"]
