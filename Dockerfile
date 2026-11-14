FROM python:3.11-alpine

WORKDIR /app

COPY tarangplus.py /app/tarangplus.py
COPY config.yml /app/config.yml

EXPOSE 8000

ENTRYPOINT ["python3", "/app/tarangplus.py", "--serve", "--port", "8000"]
