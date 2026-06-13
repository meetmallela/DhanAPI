import sqlite3
from master_resource import MasterResource

def inspect_db():
    db_path = MasterResource.get_trading_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check signals table
    cursor.execute("PRAGMA table_info(signals)")
    cols = cursor.fetchall()
    print("\nsignals columns:")
    for col in cols:
        print(f" - {col[1]} ({col[2]})")

    # Check orders table
    cursor.execute("PRAGMA table_info(orders)")
    cols = cursor.fetchall()
    print("\norders columns:")
    for col in cols:
        print(f" - {col[1]} ({col[2]})")
        
    conn.close()

if __name__ == "__main__":
    inspect_db()
