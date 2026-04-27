#!/usr/bin/env python3
"""
Amnezia / WireGuard: трафик по peer из `wg show all dump` (или `awg show all dump`).

Кумулятивные байты (как в ядре):
- amnezia_wg_peer_receive_bytes{interface, peer_id, pubkey_short}
- amnezia_wg_peer_transmit_bytes{interface, peer_id, pubkey_short}

Служебные: amnezia_up, amnezia_wg_scrape_ok, amnezia_wg_peer_count, amnezia_wg_scrape_errors_total
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time

from prometheus_client import CollectorRegistry, Gauge, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

log = logging.getLogger(__name__)

HTTP_PORT = int(os.getenv("AMNEZIA_EXPORTER_PORT", "9352"))
SCRAPE_INTERVAL = float(os.getenv("AMNEZIA_SCRAPE_INTERVAL", "30"))
WG_ENABLE = os.getenv("AMNEZIA_WG_ENABLE", "1").lower() in ("1", "true", "yes")
WG_CMD = os.getenv("AMNEZIA_WG_CMD", "wg show all dump")
WG_TIMEOUT = int(os.getenv("AMNEZIA_WG_TIMEOUT", "5"))

# Источники имён клиентов (любой из, можно сразу несколько):
# 1) JSON-файл { "<pubkey>": "<name>", ... }
PEERS_JSON = os.getenv("AMNEZIA_PEERS_JSON", "/data/peers.json")
# 2) Конфиги wg/awg, где над [Peer] коммент вида "# Name: alice"
PEERS_CONF_DIR = os.getenv("AMNEZIA_PEERS_CONF_DIR", "/etc/wireguard")
# 3) clientsTable Amnezia-сервера (несколько путей через ":")
CLIENTS_TABLE_PATHS = [
    p.strip()
    for p in os.getenv(
        "AMNEZIA_CLIENTS_TABLE",
        "/opt/amnezia/awg/clientsTable:/opt/amnezia/wireguard/clientsTable",
    ).split(":")
    if p.strip()
]


def _peer_id(public_key: str) -> str:
    return f"p_{hashlib.sha256(public_key.encode('utf-8')).hexdigest()[:12]}"


def _pubkey_short(public_key: str) -> str:
    return public_key[:12] if len(public_key) >= 12 else public_key


_NAME_LINE_RE = re.compile(r"^\s*#\s*(?:Name|name)\s*[:=]\s*(.+?)\s*$")
_PUBKEY_LINE_RE = re.compile(r"^\s*PublicKey\s*=\s*([A-Za-z0-9+/=]+)\s*$")


def _names_from_clients_table(path: str) -> dict[str, str]:
    """
    Парсер реестра клиентов Amnezia-сервера.

    Поддерживаемые формы:
      - top-level: список клиентов
      - top-level: dict с ключом "clients" (список)
    В каждом элементе ищем pubkey в одном из:
        clientId | publicKey | public_key | wireguardConfig.clientPubKey
    Имя в одном из:
        userData.clientName | clientName | name
    """
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("can't parse %s: %s", path, e)
        return out

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("clients"), list):
        items = data["clients"]
    else:
        return out

    for it in items:
        if not isinstance(it, dict):
            continue
        wgc = it.get("wireguardConfig") if isinstance(it.get("wireguardConfig"), dict) else {}
        pk = (
            it.get("clientId")
            or it.get("publicKey")
            or it.get("public_key")
            or wgc.get("clientPubKey")
        )
        ud = it.get("userData") if isinstance(it.get("userData"), dict) else {}
        name = ud.get("clientName") or it.get("clientName") or it.get("name")
        if isinstance(pk, str) and isinstance(name, str) and name.strip():
            out.setdefault(pk, name.strip())
    return out


def load_peer_names() -> dict[str, str]:
    """
    Возвращает мапу public_key -> name.
    Источники (первый победил):
      1) Amnezia clientsTable (CLIENTS_TABLE_PATHS).
      2) Конфиги wg/awg в PEERS_CONF_DIR, где над [Peer] коммент "# Name: alice".
      3) JSON-файл PEERS_JSON {"<pubkey>": "<name>"} (ручные правки).
    """
    mapping: dict[str, str] = {}

    for path in CLIENTS_TABLE_PATHS:
        try:
            for k, v in _names_from_clients_table(path).items():
                mapping.setdefault(k, v)
        except Exception as e:
            log.warning("clientsTable %s: %s", path, e)

    try:
        if os.path.isdir(PEERS_CONF_DIR):
            for fname in os.listdir(PEERS_CONF_DIR):
                if not fname.endswith(".conf"):
                    continue
                path = os.path.join(PEERS_CONF_DIR, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        last_name: str | None = None
                        for line in f:
                            m_name = _NAME_LINE_RE.match(line)
                            if m_name:
                                last_name = m_name.group(1).strip()
                                continue
                            m_pk = _PUBKEY_LINE_RE.match(line)
                            if m_pk and last_name:
                                mapping.setdefault(m_pk.group(1), last_name)
                                last_name = None
                except Exception as e:
                    log.warning("can't read %s: %s", path, e)
    except Exception as e:
        log.warning("can't list %s: %s", PEERS_CONF_DIR, e)

    try:
        if os.path.isfile(PEERS_JSON):
            with open(PEERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(k, str) and isinstance(v, str):
                            mapping[k] = v
    except Exception as e:
        log.warning("can't read %s: %s", PEERS_JSON, e)

    return mapping


def parse_wg_dump(text: str) -> list[tuple[str, str, int, int]]:
    """
    `wg show all dump`: строка устройства — 5 полей; строка peer — 8 полей
    (public, psk, endpoint, allowed, handshake, rx, tx, keepalive).
    """
    rows: list[tuple[str, str, int, int]] = []
    current_if = "unknown"

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 5:
            current_if = parts[0] or "unknown"
            continue
        if len(parts) == 8:
            pubkey, _psk, _ep, _ips, _hs, rx_s, tx_s, _ka = parts
            try:
                rows.append((current_if, pubkey, int(rx_s), int(tx_s)))
            except ValueError:
                log.warning("bad peer line: %r", line[:100])
    return rows


def run_wg_dump() -> tuple[bool, str]:
    if not WG_ENABLE:
        return True, ""
    try:
        out = subprocess.check_output(
            WG_CMD,
            shell=True,
            stderr=subprocess.STDOUT,
            timeout=WG_TIMEOUT,
        )
        return True, out.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        log.warning("dump failed: %s", e)
        err = e.output.decode("utf-8", errors="replace") if e.output else str(e)
        return False, err
    except Exception as e:
        log.error("dump error: %s", e)
        return False, ""


class AmneziaCollector:
    def __init__(self):
        self._cache_text: str | None = None
        self._cache_ok: bool = True
        self._cache_ts: float = 0.0
        self._scrape_errors: int = 0
        self._names_ts: float = 0.0
        self._names_cache: dict[str, str] = {}

    def _get_names(self) -> dict[str, str]:
        now = time.time()
        if now - self._names_ts < SCRAPE_INTERVAL and self._names_ts > 0:
            return self._names_cache
        self._names_cache = load_peer_names()
        self._names_ts = now
        return self._names_cache

    def _get_dump(self) -> tuple[bool, str]:
        now = time.time()
        if (
            self._cache_text is not None
            and (now - self._cache_ts) < SCRAPE_INTERVAL
        ):
            return self._cache_ok, self._cache_text or ""
        ok, text = run_wg_dump()
        if not ok:
            self._scrape_errors += 1
        self._cache_ok = ok
        self._cache_text = text
        self._cache_ts = now
        return ok, text

    def collect(self):
        err_f = CounterMetricFamily(
            "amnezia_wg_scrape_errors_total",
            "Failed invocations of wg/awg dump",
        )
        err_f.add_metric([], float(self._scrape_errors))
        yield err_f

        ver_f = GaugeMetricFamily(
            "amnezia_exporter_info",
            "Exporter version (always 1)",
            labels=["version"],
        )
        ver_f.add_metric(["0.3.0"], 1.0)
        yield ver_f

        ok, text = self._get_dump()
        st_f = GaugeMetricFamily(
            "amnezia_wg_scrape_ok",
            "1 if last successful dump, else 0",
        )
        st_f.add_metric([], 1.0 if ok else 0.0)
        yield st_f

        names = self._get_names()
        labels_def = ["interface", "peer_id", "pubkey_short", "name"]
        rx_f = GaugeMetricFamily(
            "amnezia_wg_peer_receive_bytes",
            "Cumulative bytes received from peer (kernel counter)",
            labels=labels_def,
        )
        tx_f = GaugeMetricFamily(
            "amnezia_wg_peer_transmit_bytes",
            "Cumulative bytes sent to peer (kernel counter)",
            labels=labels_def,
        )

        if not ok or not (text and text.strip()):
            cnt_f = GaugeMetricFamily("amnezia_wg_peer_count", "Parsed peer rows")
            cnt_f.add_metric([], 0.0)
            yield cnt_f
            yield rx_f
            yield tx_f
            return

        peers = parse_wg_dump(text)
        cnt_f = GaugeMetricFamily("amnezia_wg_peer_count", "Parsed peer rows")
        cnt_f.add_metric([], float(len(peers)))
        yield cnt_f

        for iface, pubkey, rx, tx in peers:
            name = names.get(pubkey, "")
            labels = [iface, _peer_id(pubkey), _pubkey_short(pubkey), name]
            rx_f.add_metric(labels, float(rx))
            tx_f.add_metric(labels, float(tx))
        yield rx_f
        yield tx_f


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    reg = CollectorRegistry()
    reg.register(AmneziaCollector())
    g = Gauge("amnezia_up", "Exporter running", registry=reg)
    g.set(1)
    if not WG_ENABLE:
        log.warning("AMNEZIA_WG_ENABLE=0 — дамп не вызывается, peer-метрик не будет")

    start_http_server(HTTP_PORT, registry=reg)
    log.info("listen 0.0.0.0:%s cmd=%r", HTTP_PORT, WG_CMD)

    while True:
        time.sleep(3600)
