import os
import requests

from flask import Flask, request, jsonify, redirect

app = Flask(__name__)

# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ============================================================
# CTRADER OPEN API
# ============================================================

CTRADER_CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID")
CTRADER_CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET")

CTRADER_REDIRECT_URI = (
    "https://ctrader-notifications.onrender.com/callback"
)

CTRADER_AUTHORIZE_URL = (
    "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
)

CTRADER_TOKEN_URL = (
    "https://openapi.ctrader.com/apps/token"
)

# ============================================================
# TEMPORARY TOKEN STORAGE
# ============================================================
#
# This is enough for the first connection test.
# Later we will make token storage persistent.
#

CTRADER_ACCESS_TOKEN = None
CTRADER_REFRESH_TOKEN = None


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
# TELEGRAM SEND FUNCTION
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN missing"

    if not TELEGRAM_CHAT_ID:
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
            return True, "Notification sent"

        return (
            False,
            "Telegram error "
            + str(response.status_code)
            + ": "
            + response.text
        )

    except Exception as exc:
        return False, str(exc)


# ============================================================
# EXISTING NOTIFY ENDPOINT
# ============================================================

@app.route("/notify", methods=["POST"])
def notify():
    try:
        data = request.get_json(
            silent=True
        ) or {}

        message = data.get("message")

        if not message:
            return jsonify({
                "success": False,
                "message": "No message supplied"
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
# CTRADER AUTHORISATION START
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

    response = requests.Request(
        "GET",
        CTRADER_AUTHORIZE_URL,
        params=params
    ).prepare()

    return redirect(
        response.url
    )


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
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CTRADER_REDIRECT_URI,
                "client_id": CTRADER_CLIENT_ID,
                "client_secret": CTRADER_CLIENT_SECRET
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
            return (
                "cTrader token exchange failed:<br><br>"
                + str(data),
                400
            )

        CTRADER_ACCESS_TOKEN = (
            access_token
        )

        CTRADER_REFRESH_TOKEN = (
            refresh_token
        )

        send_telegram(
            "cTrader Open API connected successfully.\n\n"
            "View-only account access authorised."
        )

        return (
            "<h2>cTrader connected successfully</h2>"
            "<p>View-only account access has been authorised.</p>"
            "<p>You can close this page.</p>",
            200
        )

    except Exception as exc:
        return (
            "cTrader connection error:<br><br>"
            + str(exc),
            500
        )


# ============================================================
# CTRADER STATUS
# ============================================================

@app.route("/ctrader/status", methods=["GET"])
def ctrader_status():

    return jsonify({
        "client_id_configured":
            bool(CTRADER_CLIENT_ID),

        "client_secret_configured":
            bool(CTRADER_CLIENT_SECRET),

        "access_token_received":
            bool(CTRADER_ACCESS_TOKEN),

        "refresh_token_received":
            bool(CTRADER_REFRESH_TOKEN),

        "redirect_uri":
            CTRADER_REDIRECT_URI
    })


# ============================================================
# RUN LOCALLY
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
