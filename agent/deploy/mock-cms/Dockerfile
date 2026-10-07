FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.30.*"

WORKDIR /app
COPY app.py /app/app.py

USER 65532:65532
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
