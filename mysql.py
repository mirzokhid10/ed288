import pymysql

try:
    conn = pymysql.connect(
        host='yamanote.proxy.rlwy.net',
        port=47768,
        user='root',
        password='pZriTYeHjZUyBitnGRsdLsMDpXqsKIFX',
        database='railway',
        connect_timeout=10
    )
    print("✓ Connection successful!")
    conn.close()
except Exception as e:
    print(f"✗ Connection failed: {e}")