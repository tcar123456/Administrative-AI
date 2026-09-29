FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock
COPY app ./app
COPY static ./static
COPY knowledge ./knowledge
RUN useradd --create-home appuser && mkdir /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --no-proxy-headers"]
