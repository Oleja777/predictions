import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from . import config
from .tracker import (
    MarketSignal,
    MarketTracker,
    PolymarketClient,
    RuntimeConfigStore,
    SignalStore,
    TrackerService,
)


class TelegramNotifier:
    def __init__(self) -> None:
        self.enabled = config.TELEGRAM_ENABLED
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID

    def send(self, signal: MarketSignal) -> None:
        if not self.enabled or not self.token or not self.chat_id:
            return
        message = (
            f"🚨 Polymarket anomaly\n"
            f"Market: {signal.market_name}\n"
            f"Time: {signal.timestamp.isoformat()}\n"
            f"Vol 15m: {signal.volume_15m:.4f}\n"
            f"Trades 15m: {signal.trades_15m}\n"
            f"Price Δ: {signal.price_change_pct:.2f}%\n"
            f"Top addresses: {', '.join(addr for addr, _ in signal.top_addresses)}"
        )
        payload = {"chat_id": self.chat_id, "text": message}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage?{urlencode(payload)}"
        req = Request(url, headers={"User-Agent": "polymarket-insider-tracker"})
        with urlopen(req, timeout=20):
            return


runtime_config_store = RuntimeConfigStore(
    debug=False,
    anomaly_volume_threshold=config.ANOMALY_VOLUME_THRESHOLD,
    anomaly_trades_threshold=config.ANOMALY_TRADES_THRESHOLD,
)
store = SignalStore(config.SIGNAL_CSV_PATH)
client = PolymarketClient(config.POLYMARKET_BASE_URL)
tracker = MarketTracker(client, store, runtime_config_store)
notifier = TelegramNotifier()


def _notify(signals: List[MarketSignal]) -> None:
    for signal in signals:
        notifier.send(signal)


service = TrackerService(tracker, on_signals=_notify)


def _serialize_signals(signals: List[MarketSignal], include_all: bool) -> List[Dict[str, Any]]:
    data: List[Dict[str, Any]] = []
    for signal in signals:
        if not include_all and not signal.is_anomaly:
            continue
        data.append(
            {
                "market_id": signal.market_id,
                "market": signal.market_name,
                "timestamp": signal.timestamp.isoformat(),
                "vol_15m": signal.volume_15m,
                "avg_vol_15m": signal.avg_volume_15m,
                "trades_15m": signal.trades_15m,
                "avg_trades_15m": signal.avg_trades_15m,
                "price_change_pct": signal.price_change_pct,
                "top_addresses": [
                    {"address": addr, "count": count}
                    for addr, count in signal.top_addresses
                ],
                "anomaly_volume_ratio": signal.anomaly_volume_ratio,
                "anomaly_trades_ratio": signal.anomaly_trades_ratio,
                "reason": signal.reason,
                "is_anomaly": signal.is_anomaly,
                "passed": signal.is_anomaly,
            }
        )
    return data


def _serialize_runtime_config(config_obj: Any) -> Dict[str, Any]:
    return {
        "debug": config_obj.debug,
        "anomaly_volume_threshold": config_obj.anomaly_volume_threshold,
        "anomaly_trades_threshold": config_obj.anomaly_trades_threshold,
    }


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_index_html() -> bytes:
    template_path = Path(__file__).parent / "templates" / "index.html"
    return template_path.read_bytes()


class RequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Any, status_code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes(_load_index_html())
            return
        if parsed.path == "/api/signals":
            runtime_config = runtime_config_store.get()
            if runtime_config.debug:
                signals = _serialize_signals(tracker.get_debug_snapshot(), include_all=True)
            else:
                signals = _serialize_signals(store.list_signals(), include_all=False)
            self._send_json(signals)
            return
        if parsed.path == "/api/config":
            self._send_json(_serialize_runtime_config(runtime_config_store.get()))
            return
        if parsed.path == "/api/debug":
            self._send_json(tracker.get_stats())
            return
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw_body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid_json"}, status_code=400)
                return

            errors = []
            debug_value = None
            if "debug" in payload:
                debug_value = _parse_bool(payload.get("debug"))
                if debug_value is None:
                    errors.append("debug")

            volume_value = None
            if "anomaly_volume_threshold" in payload:
                raw_value = payload.get("anomaly_volume_threshold")
                if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                    volume_value = None
                else:
                    volume_value = _parse_float(raw_value)
                    if volume_value is None:
                        errors.append("anomaly_volume_threshold")

            trades_value = None
            if "anomaly_trades_threshold" in payload:
                raw_value = payload.get("anomaly_trades_threshold")
                if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                    trades_value = None
                else:
                    trades_value = _parse_float(raw_value)
                    if trades_value is None:
                        errors.append("anomaly_trades_threshold")

            if errors:
                self._send_json({"error": "invalid_fields", "fields": errors}, status_code=400)
                return

            updated = runtime_config_store.update(
                debug=debug_value,
                anomaly_volume_threshold=volume_value,
                anomaly_trades_threshold=trades_value,
            )
            self._send_json(_serialize_runtime_config(updated))
            return

        self.send_response(404)
        self.end_headers()


def run() -> None:
    if os.getenv("SKIP_TRACKER") != "1":
        service.start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8000"))), RequestHandler)
    server.serve_forever()


if __name__ == "__main__":
    run()
