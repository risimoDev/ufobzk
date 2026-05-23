FROM teddysun/xray:latest AS xray-source

FROM python:3.12-slim

WORKDIR /project

# Xray бинарник нужен для вызова stats API (xray api statsquery --server=xray:10085)
COPY --from=xray-source /usr/bin/xray /usr/bin/xray

# Зависимости отдельным слоем для кэша
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Данные хранятся в volume
VOLUME ["/project/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/')"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
