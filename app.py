import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify, redirect, request

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *


app = Flask(__name__)
logger = logging.getLogger("ctrader_notifications")


# ============================================================
# SAFE LOGGING
# ============================================================

def log_message(*parts):
    """Log safely from Flask and the cTrader callback thread."""
    try:
        message = " ".join(str(part) for part in parts)
        logger.info("%s", message)
    except Exception:
        pass


def log_exception(context, exc):
    try:
        logger.exception("%s: %s", context, exc)
    except Exception:
        pass


# ============================================================
# SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CTRADER_CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID")
CTRADER_CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET")

CTRADER_REDIRECT_URI = os.environ.get(
    "CTRADER_REDIRECT_URI",
    "https://ctrader-notifications.onrender.com/callback"
)

CTRADER_AUTHORIZE_URL = (
    "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
)

CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"


# ============================================================
# WATCHED BOT LABELS
# ============================================================

WATCHED_LABEL_PREFIXES = (
    "BTCUSD_MomentumTrend_V7_6",
    "XAUUSD_Breakout_V1",
    "XAUUSD_Momentum_V3"
)


# ============================================================
# GLOBAL STATE
# ============================================================

CTRADER_ACCESS_TOKEN = None
CTRADER_REFRESH_TOKEN = os.environ.get("CTRADER_REFRESH_TOKEN")

ctrader_client = None
watcher_thread = None
watcher_started = False
watcher_connected = False
application_authorized = False
watcher_active_notified = False

authorized_accounts = set()
symbol_maps = {}
notified_deals = set()

last_connection_time = None
last_message_time = None
last_error = None

state_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        log_message("TELEGRAM_BOT_TOKEN missing")
        return False, "TELEGRAM_BOT_TOKEN missing"

    if not TELEGRAM_CHAT_ID:
        log_message("TELEGRAM_CHAT_ID missing")
        return False, "TELEGRAM_CHAT_ID missing"

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(url, json=payload, timeout=20)

        if response.ok:
            log_message("Telegram notification sent")
            return True, "Notification sent"

        error_text = (
            "Telegram error "
            + str(response.status_code)
            + ": "
            + response.text
        )

        log_message(error_text)
        return False, error_text

    except Exception as exc:
        log_exception("Telegram exception", exc)
        return False, str(exc)


# ============================================================
# HOME
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return "cTrader notification service is running", 200


# ============================================================
# TELEGRAM TEST
# ============================================================

@app.route("/test", methods=["GET"])
def test_notification():
    success, result = send_telegram(
        "✅ TEST: cTrader notification service is working"
    )

    if success:
        return "Test notification sent successfully", 200

    return "Test notification failed: " + str(result), 500


# ============================================================
# BOT NOTIFICATION ENDPOINT
# Accepts both:
# GET  /notify?message=...
# POST /notify with JSON {"message":"..."}
# ============================================================

@app.route("/notify", methods=["GET", "POST"])
def notify():
    try:
        if request.method == "GET":
            message = request.args.get("message", "").strip()
        else:
            data = request.get_json(silent=True) or {}
            message = str(data.get("message", "")).strip()

        if not message:
            return jsonify({
                "success": False,
                "message": "No message supplied"
            }), 400

        log_message(
            "Bot notification received via",
            request.method
        )

        success, result = send_telegram(message)

        if success:
            return jsonify({
                "success": True,
                "message": result
            }), 200

        return jsonify({
            "success": False,
            "message": result
        }), 500

    except Exception as exc:
        log_exception("Notify endpoint error", exc)
        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


# ============================================================
# CTRADER LOGIN
# ============================================================

@app.route("/ctrader/login", methods=["GET"])
def ctrader_login():
    if not CTRADER_CLIENT_ID:
        return "CTRADER_CLIENT_ID is not configured", 500

    params = {
        "client_id": CTRADER_CLIENT_ID,
        "redirect_uri": CTRADER_REDIRECT_URI,
        "scope": "accounts",
        "product": "web"
    }

    try:
        prepared = requests.Request(
            "GET",
            CTRADER_AUTHORIZE_URL,
            params=params
        ).prepare()

        log_message("Redirecting to cTrader authorisation")
        return redirect(prepared.url)

    except Exception as exc:
        log_exception("cTrader login redirect error", exc)
        return (
            "Unable to create cTrader login request:<br><br>"
            + str(exc),
            500
        )


# ============================================================
# CTRADER CALLBACK
# ============================================================

@app.route("/callback", methods=["GET"])
def ctrader_callback():
    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN
    global last_error

    code = request.args.get("code")

    if not code:
        return "No cTrader authorisation code received.", 400

    try:
        log_message("Exchanging cTrader authorisation code...")

        token_response = requests.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CTRADER_REDIRECT_URI,
                "client_id": CTRADER_CLIENT_ID,
                "client_secret": CTRADER_CLIENT_SECRET
            },
            timeout=20
        )

        try:
            data = token_response.json()
        except Exception:
            last_error = (
                "Token endpoint returned invalid JSON. HTTP "
                + str(token_response.status_code)
            )
            log_message(last_error)
            return "cTrader token endpoint returned an invalid response.", 502

        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")

        if not access_token:
            last_error = (
                "Token exchange failed. HTTP "
                + str(token_response.status_code)
                + ": "
                + str(data)
            )
            log_message(last_error)
            return (
                "cTrader token exchange failed.<br><br>" + str(data),
                400
            )

        CTRADER_ACCESS_TOKEN = access_token

        if refresh_token:
            CTRADER_REFRESH_TOKEN = refresh_token

        last_error = None
        log_message("cTrader OAuth token received")

        start_ctrader_watcher()

        send_telegram(
            "✅ cTrader authorised\n\n"
            "Open API trade watcher is starting."
        )

        return (
            "<h2>cTrader connected successfully</h2>"
            "<p>The Open API trade watcher is starting.</p>"
            "<p>Wait approximately 10 seconds, then open "
            "<a href='/ctrader/status'>/ctrader/status</a>.</p>",
            200
        )

    except Exception as exc:
        last_error = "Callback error: " + repr(exc)
        log_exception("Callback error", exc)
        return "cTrader connection error:<br><br>" + str(exc), 500


# ============================================================
# REFRESH ACCESS TOKEN
# ============================================================

def refresh_ctrader_token():
    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN
    global last_error

    if not CTRADER_REFRESH_TOKEN:
        log_message("No cTrader refresh token available")
        return False

    try:
        log_message("Refreshing cTrader token...")

        response = requests.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": CTRADER_REFRESH_TOKEN,
                "client_id": CTRADER_CLIENT_ID,
                "client_secret": CTRADER_CLIENT_SECRET
            },
            timeout=20
        )

        try:
            data = response.json()
        except Exception:
            last_error = (
                "Token refresh returned invalid JSON. HTTP "
                + str(response.status_code)
            )
            log_message(last_error)
            return False

        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")

        if not access_token:
            last_error = "Token refresh failed: " + str(data)
            log_message(last_error)
            return False

        CTRADER_ACCESS_TOKEN = access_token

        if refresh_token:
            CTRADER_REFRESH_TOKEN = refresh_token

        last_error = None
        log_message("cTrader token refreshed")
        return True

    except Exception as exc:
        last_error = "Token refresh exception: " + repr(exc)
        log_exception("Token refresh exception", exc)
        return False


# ============================================================
# START WATCHER
# ============================================================

def start_ctrader_watcher():
    global watcher_thread
    global watcher_started

    with state_lock:
        if watcher_thread is not None and watcher_thread.is_alive():
            log_message("cTrader watcher thread already alive")
            return

        watcher_started = True
        log_message("Creating cTrader watcher thread...")

        watcher_thread = threading.Thread(
            target=run_ctrader_watcher,
            name="ctrader-open-api",
            daemon=True
        )

        watcher_thread.start()
        log_message("cTrader watcher thread launched")


# ============================================================
# OPEN API THREAD
# ============================================================

def run_ctrader_watcher():
    global ctrader_client
    global watcher_connected
    global application_authorized
    global last_error

    try:
        from twisted.internet import reactor

        log_message("Open API thread entered")
        log_message("LIVE host:", EndPoints.PROTOBUF_LIVE_HOST)
        log_message("Open API port:", EndPoints.PROTOBUF_PORT)
        log_message("Creating cTrader Client...")

        ctrader_client = Client(
            EndPoints.PROTOBUF_LIVE_HOST,
            EndPoints.PROTOBUF_PORT,
            TcpProtocol
        )

        log_message("cTrader Client created")

        ctrader_client.setConnectedCallback(on_ctrader_connected)
        ctrader_client.setDisconnectedCallback(on_ctrader_disconnected)
        ctrader_client.setMessageReceivedCallback(on_ctrader_message)

        log_message("cTrader callbacks installed")
        log_message("Calling client.startService()...")

        ctrader_client.startService()

        log_message("client.startService() returned")
        log_message("Starting Twisted reactor...")

        reactor.run(installSignalHandlers=False)

        watcher_connected = False
        application_authorized = False
        log_message("Twisted reactor stopped")

    except Exception as exc:
        watcher_connected = False
        application_authorized = False
        last_error = "Watcher fatal error: " + repr(exc)
        log_exception("Watcher fatal error", exc)


# ============================================================
# CONNECTED CALLBACK
# ============================================================

def on_ctrader_connected(client):
    global watcher_connected
    global last_connection_time
    global last_error

    watcher_connected = True
    last_connection_time = time.time()
    last_error = None

    log_message("======================================")
    log_message("CONNECTED TO CTRADER OPEN API")
    log_message("======================================")

    request_message = ProtoOAApplicationAuthReq()
    request_message.clientId = CTRADER_CLIENT_ID
    request_message.clientSecret = CTRADER_CLIENT_SECRET

    deferred = client.send(request_message)
    deferred.addErrback(on_ctrader_error)


# ============================================================
# DISCONNECTED CALLBACK
# ============================================================

def on_ctrader_disconnected(client, reason):
    global watcher_connected
    global application_authorized
    global last_error

    watcher_connected = False
    application_authorized = False
    last_error = "Disconnected: " + str(reason)

    log_message("CTRADER DISCONNECTED:", reason)


# ============================================================
# ERROR CALLBACK
# ============================================================

def on_ctrader_error(failure):
    global last_error

    last_error = str(failure)
    log_message("CTRADER API ERROR:", failure)


# ============================================================
# REQUEST ACCOUNT LIST
# ============================================================

def request_account_list():
    if not CTRADER_ACCESS_TOKEN:
        log_message("No cTrader access token")
        return

    log_message("Requesting authorised account list...")

    request_message = ProtoOAGetAccountListByAccessTokenReq()
    request_message.accessToken = CTRADER_ACCESS_TOKEN

    deferred = ctrader_client.send(request_message)
    deferred.addErrback(on_ctrader_error)


# ============================================================
# AUTHORISE ACCOUNT
# ============================================================

def authorize_account(account_id):
    log_message("Authorising cTrader account:", account_id)

    request_message = ProtoOAAccountAuthReq()
    request_message.ctidTraderAccountId = int(account_id)
    request_message.accessToken = CTRADER_ACCESS_TOKEN

    deferred = ctrader_client.send(request_message)
    deferred.addErrback(on_ctrader_error)


# ============================================================
# REQUEST SYMBOLS
# ============================================================

def request_symbol_list(account_id):
    log_message("Requesting symbols for account:", account_id)

    request_message = ProtoOASymbolsListReq()
    request_message.ctidTraderAccountId = int(account_id)
    request_message.includeArchivedSymbols = False

    deferred = ctrader_client.send(request_message)
    deferred.addErrback(on_ctrader_error)


# ============================================================
# MESSAGE HANDLER
# ============================================================

def on_ctrader_message(client, message):
    global application_authorized
    global last_message_time
    global watcher_active_notified

    try:
        last_message_time = time.time()
        payload_type = message.payloadType

        if payload_type == ProtoOAApplicationAuthRes().payloadType:
            application_authorized = True
            log_message("CTRADER APPLICATION AUTHORISED")

            if CTRADER_ACCESS_TOKEN:
                request_account_list()
            elif CTRADER_REFRESH_TOKEN:
                if refresh_ctrader_token():
                    request_account_list()
            else:
                log_message("No access token available - open /ctrader/login")

            return

        if payload_type == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            response = Protobuf.extract(message)
            accounts = list(response.ctidTraderAccount)

            log_message("Authorised accounts returned:", len(accounts))

            live_accounts = []

            for account in accounts:
                if getattr(account, "isLive", True):
                    live_accounts.append(account)

            log_message("LIVE accounts:", len(live_accounts))

            if not live_accounts:
                send_telegram(
                    "⚠️ Open API connected but no authorised LIVE account was found."
                )
                return

            for account in live_accounts:
                authorize_account(int(account.ctidTraderAccountId))

            return

        if payload_type == ProtoOAAccountAuthRes().payloadType:
            response = Protobuf.extract(message)
            account_id = int(response.ctidTraderAccountId)

            authorized_accounts.add(account_id)
            log_message("ACCOUNT AUTHORISED:", account_id)

            request_symbol_list(account_id)
            return

        if payload_type == ProtoOASymbolsListRes().payloadType:
            response = Protobuf.extract(message)
            account_id = int(response.ctidTraderAccountId)
            account_symbols = {}

            for symbol in response.symbol:
                account_symbols[int(symbol.symbolId)] = symbol.symbolName

            symbol_maps[account_id] = account_symbols

            log_message(
                "SYMBOL MAP LOADED:",
                account_id,
                "symbols:",
                len(account_symbols)
            )

            btc_found = [
                name
                for name in account_symbols.values()
                if normalize_symbol(name).startswith("BTCUSD")
            ]

            xau_found = [
                name
                for name in account_symbols.values()
                if normalize_symbol(name).startswith("XAUUSD")
            ]

            log_message("BTCUSD symbols:", btc_found)
            log_message("XAUUSD symbols:", xau_found)

            if not watcher_active_notified:
                watcher_active_notified = True
                send_telegram(
                    "✅ cTrader TRADE WATCHER ACTIVE\n\n"
                    "Watching cloud trades from:\n"
                    "• BTCUSD MomentumTrend V7.6\n"
                    "• XAUUSD Breakout V1\n"
                    "• XAUUSD Momentum V3\n\n"
                    "Waiting for a new trade."
                )

            return

        if payload_type == ProtoOAExecutionEvent().payloadType:
            event = Protobuf.extract(message)
            handle_execution_event(event)
            return

    except Exception as exc:
        log_exception("Message processing error", exc)


# ============================================================
# EXECUTION HANDLER
# ============================================================

def handle_execution_event(event):
    try:
        if event.executionType != ProtoOAExecutionType.ORDER_FILLED:
            return

        if not event.HasField("deal"):
            log_message("Filled event without deal")
            return

        deal = event.deal

        try:
            if deal.HasField("closePositionDetail"):
                log_message("Closing deal ignored:", deal.dealId)
                return
        except Exception:
            pass

        account_id = int(event.ctidTraderAccountId)
        label = ""

        try:
            label = getattr(deal, "label", "") or ""
        except Exception:
            pass

        if not label and event.HasField("order"):
            try:
                label = event.order.tradeData.label or ""
            except Exception:
                pass

        deal_id = int(deal.dealId)

        log_message("FILLED DEAL:", deal_id, "LABEL:", label)

        if not label_is_watched(label):
            log_message("Ignored - label not watched:", label)
            return

        symbol_id = int(deal.symbolId)
        symbol_name = symbol_maps.get(account_id, {}).get(
            symbol_id,
            "Symbol " + str(symbol_id)
        )

        normalized_symbol = normalize_symbol(symbol_name)

        if not (
            normalized_symbol.startswith("BTCUSD")
            or normalized_symbol.startswith("XAUUSD")
        ):
            log_message("Ignored - symbol not watched:", symbol_name)
            return

        with state_lock:
            if deal_id in notified_deals:
                return
            notified_deals.add(deal_id)

        try:
            side = ProtoOATradeSide.Name(deal.tradeSide)
        except Exception:
            side = (
                "BUY"
                if deal.tradeSide == ProtoOATradeSide.BUY
                else "SELL"
            )

        try:
            volume = float(deal.filledVolume) / 100.0
        except Exception:
            volume = 0

        try:
            price = float(deal.executionPrice)
            price_text = f"{price:.5f}".rstrip("0").rstrip(".")
        except Exception:
            price_text = "N/A"

        try:
            position_id = str(deal.positionId)
        except Exception:
            position_id = "N/A"

        telegram_message = (
            "🚨 cTrader TRADE OPENED\n\n"
            f"Symbol: {symbol_name}\n"
            f"Side: {side}\n"
            f"Volume: {volume:g}\n"
            f"Price: {price_text}\n"
            f"Bot: {label}\n"
            f"Position ID: {position_id}\n"
            f"Deal ID: {deal_id}"
        )

        log_message(telegram_message)
        send_telegram(telegram_message)

    except Exception as exc:
        log_exception("Execution handler error", exc)


# ============================================================
# HELPERS
# ============================================================

def normalize_symbol(symbol_name):
    if not symbol_name:
        return ""

    return (
        symbol_name
        .upper()
        .replace("/", "")
        .replace("\\", "")
        .replace("-", "")
        .replace("_", "")
        .replace(".", "")
        .replace(" ", "")
    )


def label_is_watched(label):
    if not label:
        return False

    for prefix in WATCHED_LABEL_PREFIXES:
        if label.startswith(prefix):
            return True

    return False


# ============================================================
# STATUS
# ============================================================

@app.route("/ctrader/status", methods=["GET"])
def ctrader_status():
    thread_alive = (
        watcher_thread is not None
        and watcher_thread.is_alive()
    )

    return jsonify({
        "service": "running",
        "client_id_configured": bool(CTRADER_CLIENT_ID),
        "client_secret_configured": bool(CTRADER_CLIENT_SECRET),
        "access_token_received": bool(CTRADER_ACCESS_TOKEN),
        "refresh_token_available": bool(CTRADER_REFRESH_TOKEN),
        "watcher_started": watcher_started,
        "watcher_thread_alive": thread_alive,
        "watcher_connected": watcher_connected,
        "application_authorized": application_authorized,
        "authorized_accounts": len(authorized_accounts),
        "symbol_maps_loaded": len(symbol_maps),
        "notified_deals": len(notified_deals),
        "last_connection_time": last_connection_time,
        "last_message_time": last_message_time,
        "last_error": last_error,
        "watching_labels": list(WATCHED_LABEL_PREFIXES)
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "service": "running",
        "watcher_started": watcher_started,
        "watcher_connected": watcher_connected,
        "application_authorized": application_authorized,
        "healthy": watcher_connected and application_authorized
    })


# ============================================================
# STARTUP
# ============================================================

def startup():
    log_message("========================================")
    log_message("cTrader Notification Service starting")
    log_message("========================================")

    log_message(
        "Telegram configured:",
        bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    )
    log_message("cTrader Client ID configured:", bool(CTRADER_CLIENT_ID))
    log_message(
        "cTrader Client Secret configured:",
        bool(CTRADER_CLIENT_SECRET)
    )
    log_message(
        "cTrader Refresh Token configured:",
        bool(CTRADER_REFRESH_TOKEN)
    )

    if CTRADER_REFRESH_TOKEN:
        refresh_ctrader_token()

    if CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET:
        start_ctrader_watcher()
    else:
        log_message("cTrader credentials missing")


# ============================================================
# RUN STARTUP
# ============================================================

startup()


# ============================================================
# LOCAL FLASK
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
