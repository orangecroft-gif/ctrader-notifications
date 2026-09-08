import os
import threading
import time
import requests

from flask import Flask, request, jsonify, redirect

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
    ProtoHeartbeatEvent
)

from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

from twisted.internet import reactor


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CTRADER_CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID")
CTRADER_CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET")

# Optional.
#
# Once you have authorised cTrader, you can place your refresh
# token in Render as:
#
# CTRADER_REFRESH_TOKEN
#
# This allows the service to try reconnecting automatically
# after a Render restart.
#
CTRADER_REFRESH_TOKEN_ENV = os.environ.get(
    "CTRADER_REFRESH_TOKEN"
)

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
# BOT LABEL PREFIXES TO WATCH
# ============================================================

WATCHED_LABEL_PREFIXES = (
    "BTCUSD_MomentumTrend",
    "XAUUSD_MomentumHunter"
)


# ============================================================
# GLOBAL RUNTIME STATE
# ============================================================

CTRADER_ACCESS_TOKEN = None
CTRADER_REFRESH_TOKEN = CTRADER_REFRESH_TOKEN_ENV

ctrader_client = None

watcher_thread = None
watcher_started = False
watcher_connected = False

application_authorized = False

authorized_accounts = set()

symbol_maps = {}

notified_deals = set()

last_message_time = None
last_heartbeat_time = None
last_connection_time = None

state_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:

        print("TELEGRAM_BOT_TOKEN missing")

        return (
            False,
            "TELEGRAM_BOT_TOKEN missing"
        )

    if not TELEGRAM_CHAT_ID:

        print("TELEGRAM_CHAT_ID missing")

        return (
            False,
            "TELEGRAM_CHAT_ID missing"
        )

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

            print(
                "Telegram notification sent"
            )

            return (
                True,
                "Notification sent"
            )

        error_text = (
            "Telegram error "
            + str(response.status_code)
            + ": "
            + response.text
        )

        print(error_text)

        return (
            False,
            error_text
        )

    except Exception as exc:

        print(
            "Telegram exception:",
            str(exc)
        )

        return (
            False,
            str(exc)
        )


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
# TEST TELEGRAM
# ============================================================

@app.route("/test", methods=["GET"])
def test_notification():

    success, result = send_telegram(
        "✅ TEST: cTrader notification service is working"
    )

    if success:

        return (
            "Test notification sent successfully",
            200
        )

    return (
        "Test notification failed: "
        + str(result),
        500
    )


# ============================================================
# LEGACY /notify ENDPOINT
# ============================================================

@app.route("/notify", methods=["POST"])
def notify():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        message = data.get(
            "message"
        )

        if not message:

            return jsonify({
                "success": False,
                "message":
                    "No message supplied"
            }), 400

        success, result = send_telegram(
            message
        )

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

@app.route(
    "/ctrader/login",
    methods=["GET"]
)
def ctrader_login():

    if not CTRADER_CLIENT_ID:

        return (
            "CTRADER_CLIENT_ID is not configured",
            500
        )

    params = {
        "client_id":
            CTRADER_CLIENT_ID,

        "redirect_uri":
            CTRADER_REDIRECT_URI,

        "scope":
            "accounts",

        "product":
            "web"
    }

    prepared = requests.Request(
        "GET",
        CTRADER_AUTHORIZE_URL,
        params=params
    ).prepare()

    return redirect(
        prepared.url
    )


# ============================================================
# CTRADER OAUTH CALLBACK
# ============================================================

@app.route(
    "/callback",
    methods=["GET"]
)
def ctrader_callback():

    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN

    code = request.args.get(
        "code"
    )

    if not code:

        return (
            "No cTrader authorisation code received.",
            400
        )

    if not CTRADER_CLIENT_ID:

        return (
            "CTRADER_CLIENT_ID is not configured.",
            500
        )

    if not CTRADER_CLIENT_SECRET:

        return (
            "CTRADER_CLIENT_SECRET is not configured.",
            500
        )

    try:

        token_response = requests.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type":
                    "authorization_code",

                "code":
                    code,

                "redirect_uri":
                    CTRADER_REDIRECT_URI,

                "client_id":
                    CTRADER_CLIENT_ID,

                "client_secret":
                    CTRADER_CLIENT_SECRET
            },
            timeout=20
        )

        data = token_response.json()

        access_token = data.get(
            "accessToken"
        )

        refresh_token = data.get(
            "refreshToken"
        )

        if not access_token:

            print(
                "Token exchange failed:",
                data
            )

            return (
                "cTrader token exchange failed.<br><br>"
                + str(data),
                400
            )

        CTRADER_ACCESS_TOKEN = (
            access_token
        )

        if refresh_token:

            CTRADER_REFRESH_TOKEN = (
                refresh_token
            )

        print(
            "cTrader OAuth token received"
        )

        print(
            "Refresh token received:",
            bool(
                CTRADER_REFRESH_TOKEN
            )
        )

        start_ctrader_watcher()

        send_telegram(
            "✅ cTrader Open API authorised\n\n"
            "Trade watcher is starting.\n"
            "Access: account monitoring"
        )

        return (
            "<h2>cTrader connected successfully</h2>"
            "<p>The trade watcher is starting.</p>"
            "<p>You can close this page.</p>",
            200
        )

    except Exception as exc:

        print(
            "Callback error:",
            str(exc)
        )

        return (
            "cTrader connection error:<br><br>"
            + str(exc),
            500
        )


# ============================================================
# REFRESH CTRADER ACCESS TOKEN
# ============================================================

def refresh_ctrader_token():

    global CTRADER_ACCESS_TOKEN
    global CTRADER_REFRESH_TOKEN

    if not CTRADER_REFRESH_TOKEN:

        print(
            "No cTrader refresh token available"
        )

        return False

    if not CTRADER_CLIENT_ID:

        print(
            "CTRADER_CLIENT_ID missing"
        )

        return False

    if not CTRADER_CLIENT_SECRET:

        print(
            "CTRADER_CLIENT_SECRET missing"
        )

        return False

    try:

        print(
            "Refreshing cTrader access token..."
        )

        response = requests.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type":
                    "refresh_token",

                "refresh_token":
                    CTRADER_REFRESH_TOKEN,

                "client_id":
                    CTRADER_CLIENT_ID,

                "client_secret":
                    CTRADER_CLIENT_SECRET
            },
            timeout=20
        )

        data = response.json()

        new_access_token = data.get(
            "accessToken"
        )

        new_refresh_token = data.get(
            "refreshToken"
        )

        if not new_access_token:

            print(
                "cTrader token refresh failed:",
                data
            )

            return False

        CTRADER_ACCESS_TOKEN = (
            new_access_token
        )

        if new_refresh_token:

            CTRADER_REFRESH_TOKEN = (
                new_refresh_token
            )

        print(
            "cTrader access token refreshed successfully"
        )

        return True

    except Exception as exc:

        print(
            "Token refresh exception:",
            str(exc)
        )

        return False


# ============================================================
# START CTRADER WATCHER
# ============================================================

def start_ctrader_watcher():

    global watcher_thread
    global watcher_started

    with state_lock:

        if watcher_started:

            print(
                "Watcher already started"
            )

            if (
                ctrader_client
                and watcher_connected
                and CTRADER_ACCESS_TOKEN
            ):

                try:

                    reactor.callFromThread(
                        request_account_list
                    )

                except Exception as exc:

                    print(
                        "Account list restart error:",
                        str(exc)
                    )

            return

        watcher_started = True

    print(
        "Starting cTrader watcher thread"
    )

    watcher_thread = threading.Thread(
        target=run_ctrader_watcher,
        daemon=True
    )

    watcher_thread.start()


# ============================================================
# CTRADER WATCHER THREAD
# ============================================================

def run_ctrader_watcher():

    global ctrader_client

    try:

        print(
            "Connecting to cTrader LIVE Open API..."
        )

        host = (
            EndPoints.PROTOBUF_LIVE_HOST
        )

        port = (
            EndPoints.PROTOBUF_PORT
        )

        ctrader_client = Client(
            host,
            port,
            TcpProtocol
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

        ctrader_client.startService()

        print(
            "cTrader client service started"
        )

        reactor.callLater(
            10,
            heartbeat_loop
        )

        reactor.run(
            installSignalHandlers=False
        )

    except Exception as exc:

        print(
            "Watcher fatal error:",
            str(exc)
        )


# ============================================================
# CTRADER CONNECTED
# ============================================================

def on_ctrader_connected(client):

    global watcher_connected
    global last_connection_time

    watcher_connected = True

    last_connection_time = (
        time.time()
    )

    print(
        "Connected to cTrader Open API"
    )

    application_auth = (
        ProtoOAApplicationAuthReq()
    )

    application_auth.clientId = (
        CTRADER_CLIENT_ID
    )

    application_auth.clientSecret = (
        CTRADER_CLIENT_SECRET
    )

    deferred = client.send(
        application_auth
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# CTRADER DISCONNECTED
# ============================================================

def on_ctrader_disconnected(
    client,
    reason
):

    global watcher_connected
    global application_authorized

    watcher_connected = False
    application_authorized = False

    print(
        "cTrader disconnected:",
        reason
    )


# ============================================================
# CTRADER ERROR
# ============================================================

def on_ctrader_error(failure):

    print(
        "cTrader API error:",
        failure
    )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat_loop():

    global last_heartbeat_time

    try:

        if (
            ctrader_client
            and watcher_connected
        ):

            heartbeat = (
                ProtoHeartbeatEvent()
            )

            deferred = (
                ctrader_client.send(
                    heartbeat
                )
            )

            deferred.addErrback(
                on_ctrader_error
            )

            last_heartbeat_time = (
                time.time()
            )

            print(
                "cTrader heartbeat sent"
            )

    except Exception as exc:

        print(
            "Heartbeat error:",
            str(exc)
        )

    finally:

        reactor.callLater(
            10,
            heartbeat_loop
        )


# ============================================================
# REQUEST ACCOUNT LIST
# ============================================================

def request_account_list():

    if not ctrader_client:

        print(
            "Cannot request account list - "
            "client not ready"
        )

        return

    if not CTRADER_ACCESS_TOKEN:

        print(
            "Cannot request account list - "
            "no access token"
        )

        return

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

def authorize_account(
    account_id
):

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
# REQUEST SYMBOL LIST
# ============================================================

def request_symbol_list(
    account_id
):

    request_message = (
        ProtoOASymbolsListReq()
    )

    request_message.ctidTraderAccountId = int(
        account_id
    )

    request_message.includeArchivedSymbols = (
        False
    )

    deferred = ctrader_client.send(
        request_message
    )

    deferred.addErrback(
        on_ctrader_error
    )


# ============================================================
# HANDLE CTRADER MESSAGES
# ============================================================

def on_ctrader_message(
    client,
    message
):

    global application_authorized
    global last_message_time

    try:

        last_message_time = (
            time.time()
        )

        payload_type = (
            message.payloadType
        )


        # ----------------------------------------------------
        # HEARTBEAT
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoHeartbeatEvent().payloadType
        ):

            return


        # ----------------------------------------------------
        # APPLICATION AUTH SUCCESS
        # ----------------------------------------------------

        if (
            payload_type
            ==
            ProtoOAApplicationAuthRes().payloadType
        ):

            application_authorized = (
                True
            )

            print(
                "cTrader application authorised"
            )

            if CTRADER_ACCESS_TOKEN:

                request_account_list()

            elif CTRADER_REFRESH_TOKEN:

                print(
                    "No access token currently in memory - "
                    "attempting refresh"
                )

                threading.Thread(
                    target=refresh_and_request_accounts,
                    daemon=True
                ).start()

            else:

                print(
                    "No cTrader token available. "
                    "Visit /ctrader/login"
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
                "Authorized account count:",
                len(accounts)
            )

            live_accounts = []

            for account in accounts:

                is_live = getattr(
                    account,
                    "isLive",
                    True
                )

                if is_live:

                    live_accounts.append(
                        account
                    )

            print(
                "LIVE account count:",
                len(live_accounts)
            )

            if not live_accounts:

                send_telegram(
                    "⚠️ cTrader watcher connected, "
                    "but no authorised LIVE account "
                    "was found."
                )

                return

            for account in live_accounts:

                account_id = int(
                    account.ctidTraderAccountId
                )

                print(
                    "Authorising account:",
                    account_id
                )

                authorize_account(
                    account_id
                )

            return


        # ----------------------------------------------------
        # ACCOUNT AUTH SUCCESS
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
                "Account authorised:",
                account_id
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
                ] = (
                    symbol.symbolName
                )

            symbol_maps[
                account_id
            ] = account_symbols

            print(
                "Symbol list loaded for account:",
                account_id,
                "- symbols:",
                len(account_symbols)
            )

            btc_found = [
                name
                for name
                in account_symbols.values()
                if normalize_symbol(
                    name
                ).startswith(
                    "BTCUSD"
                )
            ]

            xau_found = [
                name
                for name
                in account_symbols.values()
                if normalize_symbol(
                    name
                ).startswith(
                    "XAUUSD"
                )
            ]

            print(
                "BTCUSD matches:",
                btc_found
            )

            print(
                "XAUUSD matches:",
                xau_found
            )

            send_telegram(
                "✅ cTrader trade watcher ACTIVE\n\n"
                "Watching:\n"
                "• BTCUSD MomentumTrend bots\n"
                "• XAUUSD MomentumHunter bots\n\n"
                "Waiting for new filled trades."
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


        # ----------------------------------------------------
        # CTRADER ERROR RESPONSE
        # ----------------------------------------------------

        try:

            extracted = (
                Protobuf.extract(
                    message
                )
            )

            print(
                "Unhandled cTrader message:",
                type(extracted).__name__
            )

        except Exception:

            pass

    except Exception as exc:

        print(
            "Message processing error:",
            str(exc)
        )


# ============================================================
# REFRESH TOKEN AND REQUEST ACCOUNTS
# ============================================================

def refresh_and_request_accounts():

    success = (
        refresh_ctrader_token()
    )

    if not success:

        send_telegram(
            "⚠️ cTrader watcher needs authorisation.\n\n"
            "Open /ctrader/login on the Render service."
        )

        return

    try:

        reactor.callFromThread(
            request_account_list
        )

    except Exception as exc:

        print(
            "Refresh/request account error:",
            str(exc)
        )


# ============================================================
# EXECUTION EVENT HANDLER
# ============================================================

def handle_execution_event(
    event
):

    try:

        # ----------------------------------------------------
        # ONLY FILLED ORDERS
        # ----------------------------------------------------

        if (
            event.executionType
            !=
            ProtoOAExecutionType.ORDER_FILLED
        ):

            return


        # ----------------------------------------------------
        # DEAL MUST EXIST
        # ----------------------------------------------------

        if not event.HasField(
            "deal"
        ):

            print(
                "ORDER_FILLED event has no deal"
            )

            return

        deal = event.deal


        # ----------------------------------------------------
        # IGNORE CLOSING DEALS
        # ----------------------------------------------------

        try:

            if deal.HasField(
                "closePositionDetail"
            ):

                print(
                    "Closing deal ignored:",
                    deal.dealId
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

            if (
                deal_id
                in notified_deals
            ):

                print(
                    "Duplicate deal ignored:",
                    deal_id
                )

                return

            notified_deals.add(
                deal_id
            )


        # ----------------------------------------------------
        # ACCOUNT
        # ----------------------------------------------------

        account_id = int(
            event.ctidTraderAccountId
        )


        # ----------------------------------------------------
        # LABEL
        # ----------------------------------------------------

        label = ""

        try:

            label = getattr(
                deal,
                "label",
                ""
            ) or ""

        except Exception:

            label = ""


        # Most commonly, label is available from order tradeData.
        if (
            not label
            and event.HasField(
                "order"
            )
        ):

            try:

                label = (
                    event.order
                    .tradeData
                    .label
                ) or ""

            except Exception:

                label = ""


        # ----------------------------------------------------
        # WATCHED BOT FILTER
        # ----------------------------------------------------

        if not label_is_watched(
            label
        ):

            print(
                "Filled deal ignored - "
                "label not watched:",
                label
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
            .get(
                account_id,
                {}
            )
            .get(
                symbol_id,
                "Symbol "
                + str(symbol_id)
            )
        )


        # ----------------------------------------------------
        # SYMBOL SAFETY FILTER
        # ----------------------------------------------------

        normalized_symbol = (
            normalize_symbol(
                symbol_name
            )
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
                "Filled deal ignored - "
                "symbol not watched:",
                symbol_name
            )

            return


        # ----------------------------------------------------
        # BUY / SELL
        # ----------------------------------------------------

        try:

            side = (
                ProtoOATradeSide.Name(
                    deal.tradeSide
                )
            )

        except Exception:

            if (
                deal.tradeSide
                ==
                ProtoOATradeSide.BUY
            ):

                side = "BUY"

            else:

                side = "SELL"


        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        try:

            volume = (
                float(
                    deal.filledVolume
                )
                / 100.0
            )

        except Exception:

            volume = 0


        # ----------------------------------------------------
        # EXECUTION PRICE
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

            price_text = (
                "N/A"
            )


        # ----------------------------------------------------
        # POSITION ID
        # ----------------------------------------------------

        position_id = "N/A"

        try:

            position_id = str(
                deal.positionId
            )

        except Exception:

            pass


        # ----------------------------------------------------
        # TELEGRAM ALERT
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
            "NEW WATCHED TRADE:"
        )

        print(
            telegram_message
        )

        success, result = (
            send_telegram(
                telegram_message
            )
        )

        if success:

            print(
                "Trade notification completed"
            )

        else:

            print(
                "Trade notification FAILED:",
                result
            )

    except Exception as exc:

        print(
            "Execution handler error:",
            str(exc)
        )


# ============================================================
# HELPERS
# ============================================================

def normalize_symbol(
    symbol_name
):

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


def label_is_watched(
    label
):

    if not label:

        return False

    for prefix in (
        WATCHED_LABEL_PREFIXES
    ):

        if label.startswith(
            prefix
        ):

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

    return jsonify({

        "service":
            "running",

        "client_id_configured":
            bool(
                CTRADER_CLIENT_ID
            ),

        "client_secret_configured":
            bool(
                CTRADER_CLIENT_SECRET
            ),

        "access_token_received":
            bool(
                CTRADER_ACCESS_TOKEN
            ),

        "refresh_token_available":
            bool(
                CTRADER_REFRESH_TOKEN
            ),

        "watcher_started":
            watcher_started,

        "watcher_connected":
            watcher_connected,

        "application_authorized":
            application_authorized,

        "authorized_accounts":
            len(
                authorized_accounts
            ),

        "symbol_maps_loaded":
            len(
                symbol_maps
            ),

        "notified_deals":
            len(
                notified_deals
            ),

        "last_connection_time":
            last_connection_time,

        "last_message_time":
            last_message_time,

        "last_heartbeat_time":
            last_heartbeat_time,

        "watching_labels":
            list(
                WATCHED_LABEL_PREFIXES
            )
    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    healthy = (
        watcher_connected
        and application_authorized
    )

    return jsonify({

        "service":
            "running",

        "ctrader_watcher_started":
            watcher_started,

        "ctrader_connected":
            watcher_connected,

        "application_authorized":
            application_authorized,

        "healthy":
            healthy
    })


# ============================================================
# STARTUP
# ============================================================

def startup():

    print(
        "============================================"
    )

    print(
        "cTrader Notification Service starting"
    )

    print(
        "============================================"
    )

    print(
        "Telegram configured:",
        bool(
            TELEGRAM_BOT_TOKEN
            and TELEGRAM_CHAT_ID
        )
    )

    print(
        "cTrader Client ID configured:",
        bool(
            CTRADER_CLIENT_ID
        )
    )

    print(
        "cTrader Client Secret configured:",
        bool(
            CTRADER_CLIENT_SECRET
        )
    )

    print(
        "cTrader Refresh Token configured:",
        bool(
            CTRADER_REFRESH_TOKEN
        )
    )


    # --------------------------------------------------------
    # IF REFRESH TOKEN EXISTS:
    # Try to obtain a fresh access token automatically.
    # --------------------------------------------------------

    if CTRADER_REFRESH_TOKEN:

        success = (
            refresh_ctrader_token()
        )

        if success:

            print(
                "Automatic cTrader authentication successful"
            )

        else:

            print(
                "Automatic token refresh failed"
            )


    # --------------------------------------------------------
    # START OPEN API CONNECTION
    # --------------------------------------------------------

    if (
        CTRADER_CLIENT_ID
        and CTRADER_CLIENT_SECRET
    ):

        start_ctrader_watcher()

    else:

        print(
            "cTrader credentials missing - "
            "watcher not started"
        )


# ============================================================
# RUN STARTUP ON MODULE LOAD
# ============================================================

startup()


# ============================================================
# LOCAL RUN
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
