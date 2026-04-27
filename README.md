# amnezi-exporter

Prometheus exporter: трафик **по каждому WireGuard/AmneziaWG peer** из `wg show all dump` (кумулятивные `rx`/`tx` в байтах).

## Метрики

| Имя | Описание |
|-----|----------|
| `amnezia_up` | Сервис жив (1) |
| `amnezia_wg_scrape_ok` | Успешен ли последний dump (1/0) |
| `amnezia_wg_peer_count` | Сколько peer в дампе |
| `amnezia_wg_peer_receive_bytes` | Кумулятив rx, labels: `interface`, `peer_id`, `pubkey_short` |
| `amnezia_wg_peer_transmit_bytes` | Кумулятив tx, те же labels |
| `amnezia_wg_scrape_errors_total` | Счётчик неудачных dump |

**Скорость (байт/с) на peer:**
```promql
rate(amnezia_wg_peer_receive_bytes[5m])
rate(amnezia_wg_peer_transmit_bytes[5m])
```

### Имя пользователя (label `name`)

В `wg show dump` имени нет. Экспортер подмешивает label `name` из любого источника:

**Вариант A — JSON-файл `peers.json`** (примонтирован в `/data/peers.json`):
```json
{
  "AbCdEf...pubkey1=": "alice",
  "GhIjKl...pubkey2=": "bob"
}
```

**Вариант B — комментарии в `*.conf`** (`/etc/wireguard` примонтирован read-only):
```
# Name: alice
[Peer]
PublicKey = AbCdEf...pubkey1=
AllowedIPs = 10.0.0.2/32
```

Если имя для пира не найдено, label `name=""`.

## Запуск

```bash
cd /opt/amnezi-exporter
cp .env.example .env
# на хосте с интерфейсом wg, часто: network_mode: host + AMNEZIA_WG_ENABLE=1
docker compose up -d --build
curl -s http://localhost:9352/metrics
```

## awg вместо wg

В образе — `wireguard-tools` (`wg`). Для AmneziaWG команда: `amnezia_wg`/`awg` — поставь пакет или смонтируй бинарь; в `.env` задай `AMNEZIA_WG_CMD=awg show all dump`.
