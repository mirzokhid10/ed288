# -*- coding: utf-8 -*-
import os
import pymysql
import pymysql.cursors
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# ========================================
# CONFIG — Railway MySQL public credentialss
# ========================================
MYSQL_CONFIG = {
    'host':     os.getenv("MYSQLHOST",     "localhost"),
    'port':     int(os.getenv("MYSQLPORT", 3306)),
    'user':     os.getenv("MYSQLUSER",     "root"),
    'password': os.getenv("MYSQLPASSWORD", ""),
    'database': os.getenv("MYSQLDATABASE", "railway"),
    'charset':  'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
}

CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://max.ru/")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Egor Pyrikov")

# ========================================
# DB HELPERS
# ========================================

def get_db():
    """Open a fresh pymysql connection."""
    return pymysql.connect(
        host=MYSQL_CONFIG['host'],
        port=MYSQL_CONFIG['port'],
        user=MYSQL_CONFIG['user'],
        password=MYSQL_CONFIG['password'],
        database=MYSQL_CONFIG['database'],
        charset=MYSQL_CONFIG['charset'],
        cursorclass=MYSQL_CONFIG['cursorclass'],
        autocommit=True,
        connect_timeout=10,
    )


def check_subscription(user_id: int) -> bool:
    """Return True if user_id is in the subscribers table."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM subscribers WHERE user_id = %s LIMIT 1",
                    (user_id,)
                )
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[DB ERROR] check_subscription({user_id}): {e}")
        return True  # fail-open


def get_post_config(post_id: str) -> dict | None:
    """Fetch post config from MySQL by post_id."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT post_id, url, button_label FROM post_configs WHERE post_id = %s LIMIT 1",
                    (post_id,)
                )
                return cur.fetchone()  # returns dict or None
    except Exception as e:
        print(f"[DB ERROR] get_post_config({post_id}): {e}")
        return None

# ========================================
# ROUTES
# ========================================

@app.route('/api/check-sub', methods=['GET'])
def api_check_subscription():
    """
    Called by index.html mini-app.

    Query params:
        user_id  — MAX user id (integer)
        post_id  — 12-char hex token from post_configs table

    Response JSON:
        {
            "is_subscribed": true | false,
            "redirect_url":  "https://..." | null,
            "channel_link":  "https://max.ru/...",
            "channel_name":  "Channel Name"
        }
    """
    user_id_raw = request.args.get('user_id', '').strip()
    post_id     = request.args.get('post_id', '').strip()

    if not user_id_raw or not post_id:
        return jsonify({"error": "Missing parameters: user_id and post_id required"}), 400

    try:
        user_id = int(user_id_raw)
    except ValueError:
        return jsonify({"error": "Invalid user_id: must be integer"}), 400

    # Look up post in MySQL
    config = get_post_config(post_id)
    if not config:
        return jsonify({"error": f"Post '{post_id}' not found"}), 404

    # Check subscription in MySQL
    is_sub = check_subscription(user_id)

    return jsonify({
        "is_subscribed": is_sub,
        "redirect_url":  config['url'] if is_sub else None,
        "channel_link":  CHANNEL_LINK,
        "channel_name":  CHANNEL_NAME,
    })


@app.route('/health', methods=['GET'])
def health():
    """Health check — also verifies DB connection."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    return jsonify({
        "status":    "ok",
        "service":   "subscription_gate",
        "db":        db_status,
    })


# ========================================
# ENTRY POINT
# ========================================

if __name__ == '__main__':
    port = int(os.getenv("GATE_PORT", 5000))
    print(f"Gate server running on http://0.0.0.0:{port}")
    print(f"MySQL: {MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}")
    print(f"Channel: {CHANNEL_LINK}")
    app.run(host='0.0.0.0', port=port, debug=False)