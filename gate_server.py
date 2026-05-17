import os
import json
import pymysql
import pymysql.cursors
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ========================================
# APP INIT
# ========================================
app = Flask(__name__)
CORS(app)  # ✅ FIXED: CORS applied AFTER app is created

# ========================================
# CONFIG
# ========================================
MYSQL_CONFIG = {
    'host':     os.getenv("MYSQL_HOST", "localhost"),
    'port':     int(os.getenv("MYSQL_PORT", 3306)),
    'user':     os.getenv("MYSQL_USER", "root"),
    'password': os.getenv("MYSQL_PASSWORD", ""),
    'db':       os.getenv("MYSQL_DATABASE", "max_bot_db"),
    'charset':  'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
}

CHANNEL_LINK  = os.getenv("CHANNEL_LINK", "https://max.ru/")
CHANNEL_NAME  = os.getenv("CHANNEL_NAME", "Yulia Karetsman")
POST_CONFIGS_FILE = "post_configs.json"

# ========================================
# DB HELPERS  (sync — no asyncio needed in Flask)
# ========================================

def get_db_connection():
    """Open a fresh pymysql connection."""
    return pymysql.connect(
        host=MYSQL_CONFIG['host'],
        port=MYSQL_CONFIG['port'],
        user=MYSQL_CONFIG['user'],
        password=MYSQL_CONFIG['password'],
        database=MYSQL_CONFIG['db'],
        charset=MYSQL_CONFIG['charset'],
        cursorclass=MYSQL_CONFIG['cursorclass'],
        autocommit=True,
        connect_timeout=5,
    )


def check_subscription(user_id: int) -> bool:
    """Return True if user_id exists in the subscribers table."""
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id FROM subscribers WHERE user_id = %s LIMIT 1",
                    (user_id,)
                )
                return cursor.fetchone() is not None
    except Exception as e:
        print(f"[DB ERROR] check_subscription: {e}")
        # fail-open: if DB is down, let the user through rather than
        # permanently blocking everyone
        return True


# ========================================
# POST CONFIG LOADER
# ========================================

def load_post_configs() -> dict:
    """Load post configs from JSON file that nwbot.py writes."""
    try:
        if os.path.exists(POST_CONFIGS_FILE):
            with open(POST_CONFIGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[ERROR] load_post_configs: {e}")
    return {}


# ========================================
# ROUTES
# ========================================

@app.route('/api/check-sub', methods=['GET'])
def api_check_subscription():
    """
    Called by index.html mini-app.

    Query params:
        user_id  — MAX user id (integer)
        post_id  — post token from post_configs.json

    Response JSON:
        {
            "is_subscribed": true | false,
            "redirect_url":  "https://..." | null,
            "channel_link":  "https://max.ru/..."
        }
    """
    user_id_raw = request.args.get('user_id', '').strip()
    post_id     = request.args.get('post_id', '').strip()

    # --- validate inputs ---
    if not user_id_raw or not post_id:
        return jsonify({"error": "Missing parameters: user_id and post_id are required"}), 400

    try:
        user_id = int(user_id_raw)
    except ValueError:
        return jsonify({"error": "Invalid user_id: must be an integer"}), 400

    # --- load post configs ---
    post_configs = load_post_configs()
    if post_id not in post_configs:
        return jsonify({"error": f"Post '{post_id}' not found"}), 404

    # --- check subscription in MySQL ---
    is_sub = check_subscription(user_id)

    config = post_configs[post_id]
    return jsonify({
        "is_subscribed": is_sub,
        "redirect_url":  config['url'] if is_sub else None,
        "channel_link":  CHANNEL_LINK,
        "channel_name":  CHANNEL_NAME,
    })
    
@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "service": "subscription_gate"
    })


@app.route('/health', methods=['GET'])
def health():
    """Simple health-check used by monitoring / cloudflared."""
    return jsonify({"status": "ok", "service": "subscription_gate"})


# ========================================
# ENTRY POINT
# ========================================

if __name__ == '__main__':
    port = int(os.getenv("GATE_PORT", 5000))
    print(f"🚀 Gate server running on http://0.0.0.0:{port}")
    print(f"🔗 Channel : {CHANNEL_LINK}")
    print(f"📄 Configs : {POST_CONFIGS_FILE}")
    app.run(host='0.0.0.0', port=port, debug=False)