# amnezi-exporter

Prometheus-экспортер для **WireGuard / AmneziaWG**: отдаёт трафик по каждому peer (кумулятивные `rx`/`tx` в байтах из счётчиков ядра) плюс label с человекочитаемым именем клиента.

Под капотом — `wg show all dump` или `awg show all dump`, разобранный в метрики. Парсер понимает оба формата:

- классический WireGuard: device-строка из 5 полей + peer-строка из 8;
- AmneziaWG: расширенная device-строка (10+ полей с обфускационными `Jc/Jmin/Jmax/S1..S2/H1..H4/I1..I5`) + peer-строка из 9 полей (первое — имя интерфейса).

## Метрики

| Имя | Тип | Описание |
|-----|-----|----------|
| `amnezia_up` | gauge | Экспортер жив (всегда 1) |
| `amnezia_exporter_info{version}` | gauge | Версия экспортера |
| `amnezia_wg_scrape_ok` | gauge | 1 если последний dump прошёл успешно, иначе 0 |
| `amnezia_wg_peer_count` | gauge | Сколько peer найдено в последнем dump |
| `amnezia_wg_peer_receive_bytes{interface,peer_id,pubkey_short,name}` | gauge | Кумулятивный rx (байты, счётчик ядра) |
| `amnezia_wg_peer_transmit_bytes{interface,peer_id,pubkey_short,name}` | gauge | Кумулятивный tx (байты, счётчик ядра) |
| `amnezia_wg_scrape_errors_total` | counter | Сколько раз dump падал |

Полезные запросы в Prometheus / Grafana:

```promql
rate(amnezia_wg_peer_receive_bytes[5m])    # скорость приёма, байт/с на peer
rate(amnezia_wg_peer_transmit_bytes[5m])   # скорость отправки, байт/с на peer

sum by (name) (rate(amnezia_wg_peer_receive_bytes[5m]) + rate(amnezia_wg_peer_transmit_bytes[5m]))

increase(amnezia_wg_peer_receive_bytes[24h]) + increase(amnezia_wg_peer_transmit_bytes[24h])
```

## Откуда берётся label `name`

В выводе `wg/awg show dump` имён клиентов нет. Экспортер сам ищет имя по `PublicKey` и проставляет его как label `name`. Источники проверяются **по приоритету сверху вниз** — побеждает первый найденный:

1. **`clientsTable`** Amnezia-сервера (если он у вас есть) — по умолчанию ищется в `/opt/amnezia/awg/clientsTable` и `/opt/amnezia/wireguard/clientsTable`. Поддерживается несколько встречающихся форматов файла (`clientId` / `publicKey` / `wireguardConfig.clientPubKey` + `userData.clientName` / `clientName` / `name`).
2. **Комментарии в `*.conf`** в каталоге `AMNEZIA_PEERS_CONF_DIR` (по умолчанию `/etc/amnezia/amneziawg`). Поддерживаются любые из форматов, которые встречаются у популярных GUI:
   ```ini
   # Name: alice
   [Peer]
   PublicKey = AbCdEf...

   ### Client bob          # формат, который пишет Amnezia GUI
   [Peer]
   PublicKey = GhIjKl...

   # Client: charlie
   [Peer]
   PublicKey = MnOpQr...
   ```
   Регексп: `^\s*#+\s*(Name|name|Client|client)\s*[:=]?\s*(.+)$`. Ассоциация с peer'ом — по ближайшему `PublicKey` ниже.
3. **JSON-файл `peers.json`** (внутри контейнера `/data/peers.json`), для ручных правок:
   ```json
   {
     "AbCdEf...pubkey1=": "alice",
     "GhIjKl...pubkey2=": "bob"
   }
   ```

Если ни в одном источнике имени не нашлось, label будет `name=""`. Все три источника можно использовать одновременно: например, из `clientsTable` имена подтягиваются автоматически, а конкретного клиента, для которого имя не нравится, можно переопределить в `peers.json`.

Кэш имён обновляется каждые `AMNEZIA_SCRAPE_INTERVAL` секунд — добавили `# Name:` в `awg0.conf`, через 30 секунд экспортер увидит. Никаких рестартов туннеля или контейнера не нужно.

## Конфигурация

Все настройки — через переменные окружения (`.env`). Пример: см. `.env.example`.

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `AMNEZIA_EXPORTER_PORT` | `9352` | HTTP-порт `/metrics` |
| `AMNEZIA_SCRAPE_INTERVAL` | `30` | Период между реальными вызовами `wg/awg dump` (кэш) |
| `AMNEZIA_WG_ENABLE` | `1` | `0` — не дёргать дамп, peer-метрик не будет (полезно для дебага HTTP) |
| `AMNEZIA_WG_CMD` | `wg show all dump` | Для AmneziaWG поменяйте на `awg show all dump` |
| `AMNEZIA_WG_TIMEOUT` | `5` | Таймаут на одну команду dump |
| `AMNEZIA_CLIENTS_TABLE` | `/opt/amnezia/awg/clientsTable:/opt/amnezia/wireguard/clientsTable` | Пути к реестру клиентов Amnezia (можно несколько через `:`) |
| `AMNEZIA_PEERS_CONF_DIR` | `/etc/wireguard` | Каталог `*.conf`. Для AmneziaWG обычно `/etc/amnezia/amneziawg` |
| `AMNEZIA_PEERS_JSON` | `/data/peers.json` | Ручной JSON-словарь pubkey → name |

## Запуск (Docker Compose)

```bash
cd /opt/amnezi-exporter
cp .env.example .env
nano .env                 # выставить AMNEZIA_WG_CMD под свою установку

# Без peers.json compose не стартует — создайте хотя бы пустой
echo '{}' > peers.json

docker compose up -d --build
docker logs --tail=30 amnezi-exporter
curl -s http://localhost:9352/metrics | grep '^amnezia_'
```

`docker-compose.yml` уже настроен под AmneziaWG-сервер и делает важные вещи:

- `network_mode: host` — иначе из контейнера не виден хостовый интерфейс `awg0`/`wg0` и `awg show` ничего не вернёт (порт `9352` при этом сам слушается на хосте, отдельный `ports:` не нужен).
- `cap_add: NET_ADMIN` — нужен `awg/wg show` для чтения статистики.
- Монтирование `/etc/amnezia/amneziawg`, `/opt/amnezia/awg` и `/usr/bin/awg` (read-only) — конфиги, реестр клиентов и сам бинарь `awg`, которого нет в `python:slim` образе.

Если у вас классический WireGuard, а не AmneziaWG:

1. `AMNEZIA_WG_CMD=wg show all dump` в `.env`.
2. В `docker-compose.yml` замените монтирование `/etc/amnezia/amneziawg` на `/etc/wireguard` и `AMNEZIA_PEERS_CONF_DIR` подкрутите аналогично; пробрасывать `/usr/bin/awg` не нужно, `wg` уже есть в образе (ставится через `wireguard-tools`).

## Проверка работоспособности

```bash
curl -s http://localhost:9352/metrics | grep -E '^amnezia_wg_(peer_count|scrape_ok|peer_receive)' | head
```

Ожидаемо:

```
amnezia_wg_scrape_ok 1.0
amnezia_wg_peer_count 8.0
amnezia_wg_peer_receive_bytes{interface="awg0",name="alice",peer_id="p_…",pubkey_short="BpSC/YYrBCs4"} 12345.0
```

### Если что-то не так

| Симптом | Где копать |
|---|---|
| `amnezia_wg_scrape_ok 1`, но `peer_count 0` при живом туннеле | Дамп вернул что-то, но парсер не распознал. Сравните количество полей в строке (`awg show all dump | awk -F'\t' 'NR<=3{print NF}'`) с теми, что описаны выше. Если формат не 5/8/9/10+ — заведите issue. |
| `amnezia_wg_scrape_ok 0` или `peer_count 0` при пустом дампе | `docker exec amnezi-exporter awg show all dump` — если пусто или ошибка, проверьте `network_mode: host`, `cap_add: NET_ADMIN`, проброс `/usr/bin/awg`. |
| `awg: command not found` внутри контейнера | На хосте: `which awg`. Если путь отличается — поправьте volume в compose. |
| `peer_count` корректный, но `name=""` для всех | Экспортер не видит источник имён. `docker exec amnezi-exporter ls /etc/amnezia/amneziawg` и `docker exec amnezi-exporter ls /opt/amnezia/awg`. Проверьте `AMNEZIA_PEERS_CONF_DIR` в `.env`. |
| Имена есть только для части пиров | Над оставшимися `[Peer]` в `awg0.conf` нет коммента `# Name: …`. Допишите — через 30 сек подтянется. |

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: amnezi-exporter
    static_configs:
      - targets: ['vpn-host:9352']
```
