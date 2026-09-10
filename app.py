import os
import threading
import time
import requests

from flask import Flask, request, jsonify, redirect

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

app = Flask(__name__)


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

CTRADER_TOKEN_URL = (
    "https://openapi.ctrader.com/apps/token"
)


# ============================================================
# WATCHED BOT LABELS
# ============================================================

WATCHED_LABEL_PREFIXES = (
    "BTCUSD_MomentumTrend_V7_6",
    "XAUUSD_Breakout_V1",
    "XAUUSD_MomentumHunter_V3"
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
        print("TELEGRAM_BOT_TOKEN missing", flush=True)
        return False, "TELEGRAM_BOT_TOKEN missing"

    if not TELEGRAM_CHAT_ID:
        print("TELEGRAM_CHAT_ID missing", flush=True)
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

        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if response.ok:
            print("Telegram notification sent", flush=True)
            return True, "Notification sent"

        error_text = (
            "Telegram error "
            + str(response.status_code)
            + ": "
            + response.text
        )

        print(error_text, flush=True)

        return False, error_text

    except Exception as exc:

        print(
            "Telegram exception:",
            str(exc),
            flush=True
        )

        return False, str(exc)


# ============================================================
# HOME
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return (
        "cTrader notification service is running",
        200
    )


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

    return (
        "Test notification failed: " + str(result),
        500
    )


# ============================================================
# LEGACY NOTIFY ENDPOINT
# ============================================================

@app.route("/notify", methods=["POST"])
def notify():

    try:

        data = request.get_json(silent=True) or {}

        message = data.get("message")

        if not message:

            return jsonify({
                "success": False,
                "message": "No message supplied"
            }), 400

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

        return (
            "CTRADER_CLIENT_ID is not configured",
            500
        )

    params = {
        "client_id": CTRADER_CLIENT_ID,
        "redirect_uri": CTRADER_REDIRECT_URI,
        "scope": "accounts",
        "product": "web"
    }

    prepared = requests.Request(
        "GET",
        CTRADER_AUTHORIZE_URL,
        params=params
    ).prepare()

    return redirect(prepared.url)


# ============================================================
# CTRADER CALLBACK
# ============================================================

@app.route("/callback", methods=["GET"])
def ctrader_callback():

    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN

    code = request.args.get("code")

    if not code:

        return (
            "No cTrader authorisation code received.",
            400
        )

    try:

        print(
            "Exchanging cTrader authorisation code...",
            flush=True
        )

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

        data = token_response.json()

        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")

        if not access_token:

            print(
                "Token exchange failed:",
                data,
                flush=True
            )

            return (
                "cTrader token exchange failed.",
                400
            )

        CTRADER_ACCESS_TOKEN = access_token

        if refresh_token:
            CTRADER_REFRESH_TOKEN = refresh_token

        print(
            "cTrader OAuth token received",
            flush=True
        )

        start_ctrader_watcher()

        send_telegram(
            "✅ cTrader authorised\n\n"
            "Open API trade watcher is starting."
        )

        return (
            "<h2>cTrader connected successfully</h2>"
            "<p>The Open API trade watcher is starting.</p>"
            "<p>Wait about 10 seconds, then open "
            "/ctrader/status.</p>",
            200
        )

    except Exception as exc:

        print(
            "Callback error:",
            repr(exc),
            flush=True
        )

        return (
            "cTrader connection error:<br><br>"
            + str(exc),
            500
        )


# ============================================================
# REFRESH ACCESS TOKEN
# ============================================================

def refresh_ctrader_token():

    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN
    global last_error

    if not CTRADER_REFRESH_TOKEN:
        return False

    try:

        print(
            "Refreshing cTrader token...",
            flush=True
        )

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

        data = response.json()

        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")

        if not access_token:

            last_error = (
                "Token refresh failed: "
                + str(data)
            )

            print(last_error, flush=True)

            return False

        CTRADER_ACCESS_TOKEN = access_token

        if refresh_token:
            CTRADER_REFRESH_TOKEN = refresh_token

        print(
            "cTrader token refreshed",
            flush=True
        )

        return True

    except Exception as exc:

        last_error = (
            "Token refresh exception: "
            + repr(exc)
        )

        print(last_error, flush=True)

        return False


# ============================================================
# START WATCHER
# ============================================================

def start_ctrader_watcher():

    global watcher_thread
    global watcher_started

    with state_lock:

        if watcher_thread is not None and watcher_thread.is_alive():

            print(
                "cTrader watcher thread already alive",
                flush=True
            )

            return

        watcher_started = True

        print(
            "Creating cTrader watcher thread...",
            flush=True
        )

        watcher_thread = threading.Thread(
            target=run_ctrader_watcher,
            name="ctrader-open-api",
            daemon=True
        )

        watcher_thread.start()

        print(
            "cTrader watcher thread launched",
            flush=True
        )


# ============================================================
# OPEN API THREAD
# ============================================================

def run_ctrader_watcher():

    global ctrader_client
    global watcher_connected
    global application_authorized
    global last_error

    try:

        # IMPORTANT:
        # Import the reactor inside the Open API thread.
        from twisted.internet import reactor

        print(
            "Open API thread entered",
            flush=True
        )

        print(
            "LIVE host:",
            EndPoints.PROTOBUF_LIVE_HOST,
            flush=True
        )

        print(
            "Open API port:",
            EndPoints.PROTOBUF_PORT,
            flush=True
        )

        print(
            "Creating cTrader Client...",
            flush=True
        )

        ctrader_client = Client(
            EndPoints.PROTOBUF_LIVE_HOST,
            EndPoints.PROTOBUF_PORT,
            TcpProtocol
        )

        print(
            "cTrader Client created",
            flush=True
        )

        ctrader_client.setConnectedCallback(
            on_ctrader_connected
        )

        ctrader_client.setDisconnectedCallback(
            on_ctrader_disconnected
        )

        ctrader_client.setMessageReceivedCallback(
            on_ctrader_message
        )

        print(
            "cTrader callbacks installed",
            flush=True
        )

        print(
            "Calling client.startService()...",
            flush=True
        )

        ctrader_client.startService()

        print(
            "client.startService() returned",
            flush=True
        )

        print(
            "Starting Twisted reactor...",
            flush=True
        )

        reactor.run(
            installSignalHandlers=False
        )

        print(
            "Twisted reactor stopped",
            flush=True
        )

    except Exception as exc:

        watcher_connected = False
        application_authorized = False

        last_error = (
            "Watcher fatal error: "
            + repr(exc)
        )

        print(
            last_error,
            flush=True
        )


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

    print(
        "======================================",
        flush=True
    )

    print(
        "CONNECTED TO CTRADER OPEN API",
        flush=True
    )

    print(
        "======================================",
        flush=True
    )

    request_message = (
        ProtoOAApplicationAuthReq()
    )

    request_message.clientId = (
        CTRADER_CLIENT_ID
    )

    request_message.clientSecret = (
        CTRADER_CLIENT_SECRET
    )

    deferred = client.send(
        request_message
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# DISCONNECTED CALLBACK
# ============================================================

def on_ctrader_disconnected(client, reason):

    global watcher_connected
    global application_authorized
    global last_error

    watcher_connected = False
    application_authorized = False

    last_error = (
        "Disconnected: "
        + str(reason)
    )

    print(
        "CTRADER DISCONNECTED:",
        reason,
        flush=True
    )


# ============================================================
# ERROR CALLBACK
# ============================================================

def on_ctrader_error(failure):

    global last_error

    last_error = str(failure)

    print(
        "CTRADER API ERROR:",
        failure,
        flush=True
    )


# ============================================================
# REQUEST ACCOUNT LIST
# ============================================================

def request_account_list():

    if not CTRADER_ACCESS_TOKEN:

        print(
            "No cTrader access token",
            flush=True
        )

        return

    print(
        "Requesting authorised account list...",
        flush=True
    )

    request_message = (
        ProtoOAGetAccountListByAccessTokenReq()
    )

    request_message.accessToken = (
        CTRADER_ACCESS_TOKEN
    )

    deferred = ctrader_client.send(
        request_message
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# AUTHORIZE ACCOUNT
# ============================================================

def authorize_account(account_id):

    print(
        "Authorising cTrader account:",
        account_id,
        flush=True
    )

    request_message = (
        ProtoOAAccountAuthReq()
    )

    request_message.ctidTraderAccountId = int(
        account_id
    )

    request_message.accessToken = (
        CTRADER_ACCESS_TOKEN
    )

    deferred = ctrader_client.send(
        request_message
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# REQUEST SYMBOLS
# ============================================================

def request_symbol_list(account_id):

    print(
        "Requesting symbols for account:",
        account_id,
        flush=True
    )

    request_message = (
        ProtoOASymbolsListReq()
    )

    request_message.ctidTraderAccountId = int(
        account_id
    )

    request_message.includeArchivedSymbols = False

    deferred = ctrader_client.send(
        request_message
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

def on_ctrader_message(client, message):

    global application_authorized
    global last_message_time

    try:

        last_message_time = time.time()

        payload_type = message.payloadType


        # ----------------------------------------------------
        # APPLICATION AUTH
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOAApplicationAuthRes().payloadType
        ):

            application_authorized = True

            print(
                "CTRADER APPLICATION AUTHORISED",
                flush=True
            )

            if CTRADER_ACCESS_TOKEN:

                request_account_list()

            elif CTRADER_REFRESH_TOKEN:

                if refresh_ctrader_token():
                    request_account_list()

            else:

                print(
                    "No access token available - "
                    "open /ctrader/login",
                    flush=True
                )

            return


        # ----------------------------------------------------
        # ACCOUNT LIST
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOAGetAccountListByAccessTokenRes().payloadType
        ):

            response = Protobuf.extract(
                message
            )

            accounts = list(
                response.ctidTraderAccount
            )

            print(
                "Authorised accounts returned:",
                len(accounts),
                flush=True
            )

            live_accounts = []

            for account in accounts:

                is_live = getattr(
                    account,
                    "isLive",
                    True
                )

                if is_live:
                    live_accounts.append(account)

            print(
                "LIVE accounts:",
                len(live_accounts),
                flush=True
            )

            if not live_accounts:

                send_telegram(
                    "⚠️ Open API connected but "
                    "no authorised LIVE account was found."
                )

                return

            for account in live_accounts:

                authorize_account(
                    int(
                        account.ctidTraderAccountId
                    )
                )

            return


        # ----------------------------------------------------
        # ACCOUNT AUTH
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOAAccountAuthRes().payloadType
        ):

            response = Protobuf.extract(
                message
            )

            account_id = int(
                response.ctidTraderAccountId
            )

            authorized_accounts.add(
                account_id
            )

            print(
                "ACCOUNT AUTHORISED:",
                account_id,
                flush=True
            )

            request_symbol_list(
                account_id
            )

            return


        # ----------------------------------------------------
        # SYMBOL LIST
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOASymbolsListRes().payloadType
        ):

            response = Protobuf.extract(
                message
            )

            account_id = int(
                response.ctidTraderAccountId
            )

            account_symbols = {}

            for symbol in response.symbol:

                account_symbols[
                    int(symbol.symbolId)
                ] = symbol.symbolName

            symbol_maps[
                account_id
            ] = account_symbols

            print(
                "SYMBOL MAP LOADED:",
                account_id,
                "symbols:",
                len(account_symbols),
                flush=True
            )

            btc_found = [
                name
                for name in account_symbols.values()
                if normalize_symbol(name).startswith(
                    "BTCUSD"
                )
            ]

            xau_found = [
                name
                for name in account_symbols.values()
                if normalize_symbol(name).startswith(
                    "XAUUSD"
                )
            ]

            print(
                "BTCUSD symbols:",
                btc_found,
                flush=True
            )

            print(
                "XAUUSD symbols:",
                xau_found,
                flush=True
            )

            send_telegram(
                "✅ cTrader TRADE WATCHER ACTIVE\n\n"
                "Watching cloud trades from:\n"
                "• BTCUSD MomentumTrend\n"
                "• XAUUSD MomentumHunter\n\n"
                "Waiting for a new trade."
            )

            return


        # ----------------------------------------------------
        # EXECUTION EVENT
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOAExecutionEvent().payloadType
        ):

            event = Protobuf.extract(
                message
            )

            handle_execution_event(
                event
            )

            return


    except Exception as exc:

        print(
            "Message processing error:",
            repr(exc),
            flush=True
        )


# ============================================================
# EXECUTION HANDLER
# ============================================================

def handle_execution_event(event):

    try:

        if (
            event.executionType
            != ProtoOAExecutionType.ORDER_FILLED
        ):

            return

        if not event.HasField("deal"):

            print(
                "Filled event without deal",
                flush=True
            )

            return

        deal = event.deal


        # ----------------------------------------------------
        # IGNORE CLOSING DEAL
        # ----------------------------------------------------

        try:

            if deal.HasField(
                "closePositionDetail"
            ):

                print(
                    "Closing deal ignored:",
                    deal.dealId,
                    flush=True
                )

                return

        except Exception:
            pass


        # ----------------------------------------------------
        # DUPLICATE PROTECTION
        # ----------------------------------------------------

        deal_id = int(
            deal.dealId
        )

        with state_lock:

            if deal_id in notified_deals:
                return

            notified_deals.add(
                deal_id
            )


        account_id = int(
            event.ctidTraderAccountId
        )


        # ----------------------------------------------------
        # BOT LABEL
        # ----------------------------------------------------

        label = ""

        try:

            label = getattr(
                deal,
                "label",
                ""
            ) or ""

        except Exception:
            pass


        if (
            not label
            and event.HasField("order")
        ):

            try:

                label = (
                    event.order
                    .tradeData
                    .label
                ) or ""

            except Exception:
                pass


        print(
            "FILLED DEAL:",
            deal_id,
            "LABEL:",
            label,
            flush=True
        )


        # ----------------------------------------------------
        # WATCH ONLY OUR BOT FAMILIES
        # ----------------------------------------------------

        if not label_is_watched(label):

            print(
                "Ignored - label not watched:",
                label,
                flush=True
            )

            return


        # ----------------------------------------------------
        # SYMBOL
        # ----------------------------------------------------

        symbol_id = int(
            deal.symbolId
        )

        symbol_name = (
            symbol_maps
            .get(account_id, {})
            .get(
                symbol_id,
                "Symbol " + str(symbol_id)
            )
        )

        normalized_symbol = normalize_symbol(
            symbol_name
        )

        if not (
            normalized_symbol.startswith(
                "BTCUSD"
            )
            or
            normalized_symbol.startswith(
                "XAUUSD"
            )
        ):

            print(
                "Ignored - symbol not watched:",
                symbol_name,
                flush=True
            )

            return


        # ----------------------------------------------------
        # SIDE
        # ----------------------------------------------------

        try:

            side = ProtoOATradeSide.Name(
                deal.tradeSide
            )

        except Exception:

            side = (
                "BUY"
                if deal.tradeSide
                == ProtoOATradeSide.BUY
                else "SELL"
            )


        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        try:

            volume = (
                float(deal.filledVolume)
                / 100.0
            )

        except Exception:

            volume = 0


        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        try:

            price = float(
                deal.executionPrice
            )

            price_text = (
                f"{price:.5f}"
                .rstrip("0")
                .rstrip(".")
            )

        except Exception:

            price_text = "N/A"


        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        try:

            position_id = str(
                deal.positionId
            )

        except Exception:

            position_id = "N/A"


        # ----------------------------------------------------
        # ALERT
        # ----------------------------------------------------

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

        print(
            telegram_message,
            flush=True
        )

        send_telegram(
            telegram_message
        )

    except Exception as exc:

        print(
            "Execution handler error:",
            repr(exc),
            flush=True
        )


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

@app.route(
    "/ctrader/status",
    methods=["GET"]
)
def ctrader_status():

    thread_alive = (
        watcher_thread is not None
        and watcher_thread.is_alive()
    )

    return jsonify({

        "service":
            "running",

        "client_id_configured":
            bool(CTRADER_CLIENT_ID),

        "client_secret_configured":
            bool(CTRADER_CLIENT_SECRET),

        "access_token_received":
            bool(CTRADER_ACCESS_TOKEN),

        "refresh_token_available":
            bool(CTRADER_REFRESH_TOKEN),

        "watcher_started":
            watcher_started,

        "watcher_thread_alive":
            thread_alive,

        "watcher_connected":
            watcher_connected,

        "application_authorized":
            application_authorized,

        "authorized_accounts":
            len(authorized_accounts),

        "symbol_maps_loaded":
            len(symbol_maps),

        "notified_deals":
            len(notified_deals),

        "last_connection_time":
            last_connection_time,

        "last_message_time":
            last_message_time,

        "last_error":
            last_error,

        "watching_labels":
            list(WATCHED_LABEL_PREFIXES)
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return jsonify({

        "service":
            "running",

        "watcher_started":
            watcher_started,

        "watcher_connected":
            watcher_connected,

        "application_authorized":
            application_authorized,

        "healthy":
            (
                watcher_connected
                and application_authorized
            )
    })


# ============================================================
# STARTUP
# ============================================================

def startup():

    print(
        "========================================",
        flush=True
    )

    print(
        "cTrader Notification Service starting",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )

    print(
        "Telegram configured:",
        bool(
            TELEGRAM_BOT_TOKEN
            and TELEGRAM_CHAT_ID
        ),
        flush=True
    )

    print(
        "cTrader Client ID configured:",
        bool(CTRADER_CLIENT_ID),
        flush=True
    )

    print(
        "cTrader Client Secret configured:",
        bool(CTRADER_CLIENT_SECRET),
        flush=True
    )

    print(
        "cTrader Refresh Token configured:",
        bool(CTRADER_REFRESH_TOKEN),
        flush=True
    )


    # If a permanent refresh token has been placed in
    # Render's environment, try to authenticate automatically.

    if CTRADER_REFRESH_TOKEN:

        refresh_ctrader_token()


    # Start Open API regardless of whether an account token
    # currently exists. Application authentication can still
    # establish the connection.

    if (
        CTRADER_CLIENT_ID
        and CTRADER_CLIENT_SECRET
    ):

        start_ctrader_watcher()

    else:

        print(
            "cTrader credentials missing",
            flush=True
        )


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
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )
