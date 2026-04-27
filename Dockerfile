FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools \
    && rm -rf /var/lib/apt/lists/*
# awg (Amnezia fork) в образе slim обычно нет; при необходимости замени base-image или
# поставь пакет с awg. Для `wg show` достаточно wireguard-tools.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

ENV AMNEZIA_EXPORTER_PORT=9352
ENV AMNEZIA_WG_ENABLE=0
ENV AMNEZIA_WG_CMD="wg show all dump"
ENV AMNEZIA_WG_TIMEOUT=5
ENV AMNEZIA_SCRAPE_INTERVAL=30

EXPOSE 9352

CMD ["python", "exporter.py"]
