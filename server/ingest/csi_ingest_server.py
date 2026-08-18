"""C6-only TCP ingest. Invalid lengths (including C5/490) never reach MySQL."""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
import uuid

import pymysql

from c6_contract import FrameDecoder

LOG = logging.getLogger("c6-ingest")
INSERT = """INSERT IGNORE INTO reports
 (connection_id,recv_ts,seq,rx_id,rssi,gain,csi_len,meta,csi,mode,session_id)
 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def connect_db():
    while True:
        try:
            return pymysql.connect(
                host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "3306")),
                user=os.getenv("DB_USER", "csi_c6"), password=os.getenv("DB_PASSWORD", "csi_c6_dev"),
                database=os.getenv("DB_NAME", "csi_c6"), autocommit=True,
            )
        except Exception as exc:
            LOG.warning("DB wait: %s", exc)
            time.sleep(2)


def current_mode(db):
    db.ping(reconnect=True)
    with db.cursor() as cur:
        cur.execute("SELECT mode,active_session_id FROM system_state WHERE singleton_id=1")
        row = cur.fetchone()
    return row if row else ("STANDBY", None)


def authenticate(sock: socket.socket) -> bool:
    token = os.getenv("CSI_TOKEN", "")
    if not token:
        return True
    sock.settimeout(10)
    line = bytearray()
    while len(line) < 512 and not line.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            return False
        line.extend(chunk)
    sock.settimeout(None)
    return line.rstrip(b"\r\n") == ("CSI-TOKEN " + token).encode()


def handle(sock: socket.socket, peer):
    connection_id = uuid.uuid4().bytes
    decoder = FrameDecoder()
    db = connect_db()
    accepted = 0
    try:
        if not authenticate(sock):
            LOG.warning("auth rejected: %s", peer)
            return
        while data := sock.recv(32768):
            reports = decoder.feed(data)
            if not reports:
                continue
            mode, session_id = current_mode(db)
            now = time.time()
            rows = [(connection_id, now, r.seq, r.rx_id, r.rssi, r.compensate_gain, r.csi_len,
                     r.raw_meta, struct.pack("<512b", *r.raw_csi), mode, session_id) for r in reports]
            with db.cursor() as cur:
                cur.executemany(INSERT, rows)
                cur.execute("UPDATE system_state SET last_seq=%s,updated_ts=%s WHERE singleton_id=1",
                            (reports[-1].seq, now))
            accepted += len(rows)
    except (ConnectionError, OSError) as exc:
        LOG.info("connection closed %s: %s", peer, exc)
    except Exception:
        LOG.exception("connection failed: %s", peer)
    finally:
        LOG.info("peer=%s accepted=%d rejected=%d", peer, accepted, decoder.rejected_frames)
        db.close()
        sock.close()


def purge_loop():
    db = connect_db()
    while True:
        try:
            now = time.time()
            with db.cursor() as cur:
                cur.execute("DELETE FROM reports WHERE mode='STANDBY' AND recv_ts < %s LIMIT 100000", (now - 3600,))
                cur.execute("DELETE FROM reports WHERE mode='INFERENCE' AND recv_ts < %s LIMIT 100000", (now - 86400,))
        except Exception:
            LOG.exception("retention purge failed")
            db = connect_db()
        time.sleep(60)


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=purge_loop, daemon=True).start()
    host, port = os.getenv("INGEST_HOST", "0.0.0.0"), int(os.getenv("INGEST_PORT", "9600"))
    with socket.create_server((host, port), reuse_port=False) as server:
        LOG.info("C6 ingest listening on %s:%d", host, port)
        while True:
            sock, peer = server.accept()
            threading.Thread(target=handle, args=(sock, peer), daemon=True).start()


if __name__ == "__main__":
    main()
