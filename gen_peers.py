#!/usr/bin/env python3
"""
Генератор peers.json для amnezi-exporter.

Источники имён (по приоритету; найденное первым побеждает):
  1) Amnezia clientsTable (JSON-реестр Amnezia-сервера)
  2) Комментарии "# Name: alice" над [Peer] в *.conf
  3) Уже существующий peers.json (чтобы не терять руками вписанные имена)
  4) Фоллбэк: peer-001, peer-002, ...

Запуск:
  sudo python3 gen_peers.py
  sudo python3 gen_peers.py --out peers.json --merge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

DEFAULT_CLIENTS_TABLE_PATHS = [
    "/opt/amnezia/awg/clientsTable",
    "/opt/amnezia/wireguard/clientsTable",
]
DEFAULT_CONF_DIR = "/etc/amnezia/amneziawg"
DEFAULT_WG_CMD = "awg show all dump"

_NAME_LINE_RE = re.compile(r"^\s*#\s*(?:Name|name)\s*[:=]\s*(.+?)\s*$")
_PUBKEY_LINE_RE = re.compile(r"^\s*PublicKey\s*=\s*([A-Za-z0-9+/=]+)\s*$")


def list_peers(cmd: str) -> list[str]:
    out = subprocess.check_output(cmd, shell=True, text=True)
    pubkeys: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 8:
            pubkeys.append(parts[0])
    seen: set[str] = set()
    uniq: list[str] = []
    for pk in pubkeys:
        if pk not in seen:
            seen.add(pk)
            uniq.append(pk)
    return uniq


def names_from_clients_table(paths: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[warn] can't parse {path}: {e}", file=sys.stderr)
            continue

        items = data if isinstance(data, list) else data.get("clients") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue

        for it in items:
            if not isinstance(it, dict):
                continue
            pk = (
                it.get("clientId")
                or it.get("publicKey")
                or it.get("public_key")
                or (it.get("wireguardConfig") or {}).get("clientPubKey")
            )
            ud = it.get("userData") if isinstance(it.get("userData"), dict) else {}
            name = (
                ud.get("clientName")
                or it.get("clientName")
                or it.get("name")
            )
            if isinstance(pk, str) and isinstance(name, str) and name.strip():
                mapping.setdefault(pk, name.strip())
    return mapping


def names_from_conf_dir(conf_dir: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not os.path.isdir(conf_dir):
        return mapping
    for fname in os.listdir(conf_dir):
        if not fname.endswith(".conf"):
            continue
        path = os.path.join(conf_dir, fname)
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
            print(f"[warn] can't read {path}: {e}", file=sys.stderr)
    return mapping


def load_existing(path: str) -> dict[str, str]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception as e:
        print(f"[warn] can't read existing {path}: {e}", file=sys.stderr)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="peers.json")
    ap.add_argument("--cmd", default=os.getenv("AMNEZIA_WG_CMD", DEFAULT_WG_CMD))
    ap.add_argument("--conf-dir", default=os.getenv("AMNEZIA_PEERS_CONF_DIR", DEFAULT_CONF_DIR))
    ap.add_argument(
        "--clients-table",
        action="append",
        default=None,
        help="Путь к Amnezia clientsTable. Можно указать несколько раз.",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="Не перезатирать имена в существующем peers.json",
    )
    args = ap.parse_args()

    ct_paths = args.clients_table or DEFAULT_CLIENTS_TABLE_PATHS

    try:
        peers = list_peers(args.cmd)
    except subprocess.CalledProcessError as e:
        print(f"[err] {args.cmd!r} failed: {e}", file=sys.stderr)
        return 2

    if not peers:
        print(f"[err] no peers found via {args.cmd!r} (нужен root и поднятый интерфейс)", file=sys.stderr)
        return 3

    src_table = names_from_clients_table(ct_paths)
    src_conf = names_from_conf_dir(args.conf_dir)
    src_existing = load_existing(args.out) if args.merge else {}

    result: dict[str, str] = {}
    placeholders = 0
    for idx, pk in enumerate(sorted(peers), start=1):
        name = src_existing.get(pk) or src_table.get(pk) or src_conf.get(pk)
        if not name:
            placeholders += 1
            name = f"peer-{idx:03d}"
        result[pk] = name

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, args.out)

    print(
        f"[ok] {args.out}: {len(result)} peers "
        f"(table={len(src_table)}, conf={len(src_conf)}, kept={len(src_existing)}, placeholders={placeholders})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
