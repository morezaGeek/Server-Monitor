import os
import sys
import sqlite3
from urllib.parse import urlparse

# Standalone script to migrate data from SQLite (metrics.db) to PostgreSQL.

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")
MONITOR_DB_DSN = os.environ.get("MONITOR_DB_DSN")

def parse_dsn(dsn):
    params = {}
    if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
        url = urlparse(dsn)
        if url.username:
            params['user'] = url.username
        if url.password:
            params['password'] = url.password
        if url.hostname:
            params['host'] = url.hostname
        if url.port:
            params['port'] = int(url.port)
        if url.path:
            params['database'] = url.path.lstrip('/')
    else:
        for part in dsn.split():
            if '=' in part:
                k, v = part.split('=', 1)
                if k == 'dbname':
                    params['database'] = v
                elif k == 'port':
                    params['port'] = int(v)
                else:
                    params[k] = v
    return params

def main():
    global MONITOR_DB_DSN
    print("=== Server Monitor Data Migration Tool ===")
    
    if not MONITOR_DB_DSN:
        print("MONITOR_DB_DSN environment variable is not set.")
        dsn_input = input("Please enter your PostgreSQL DSN (e.g., postgresql://user:pass@host:5432/dbname): ").strip()
        if not dsn_input:
            print("No DSN provided. Migration aborted.")
            sys.exit(1)
        MONITOR_DB_DSN = dsn_input

    if not os.path.exists(DB_PATH):
        print(f"SQLite metrics database not found at {DB_PATH}. Nothing to migrate.")
        sys.exit(0)

    print(f"Connecting to SQLite: {DB_PATH}")
    sqlite_conn = sqlite3.connect(DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()

    print("Connecting to PostgreSQL...")
    try:
        import pg8000
    except ImportError:
        print("pg8000 library is not installed in the current environment.")
        print("Please install it: pip install pg8000")
        sys.exit(1)

    try:
        pg_params = parse_dsn(MONITOR_DB_DSN)
        pg_conn = pg8000.connect(**pg_params)
        pg_cur = pg_conn.cursor()
    except Exception as e:
        print(f"Failed to connect to PostgreSQL: {e}")
        sys.exit(1)

    print("Checking and initializing PostgreSQL tables...")
    try:
        # Create metrics table
        pg_cur.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                cpu_percent REAL NOT NULL,
                ram_percent REAL NOT NULL,
                ram_used_gb REAL NOT NULL,
                ram_total_gb REAL NOT NULL,
                disk_percent REAL NOT NULL,
                disk_used_gb REAL NOT NULL,
                disk_total_gb REAL NOT NULL,
                net_sent_bytes DOUBLE PRECISION NOT NULL,
                net_recv_bytes DOUBLE PRECISION NOT NULL,
                net_sent_rate REAL NOT NULL DEFAULT 0,
                net_recv_rate REAL NOT NULL DEFAULT 0,
                conn_json TEXT DEFAULT '{}',
                extra_json TEXT DEFAULT '{}'
            )
        """)
        pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp)")
        
        # Create service_metrics table
        pg_cur.execute("""
            CREATE TABLE IF NOT EXISTS service_metrics (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                service TEXT NOT NULL,
                bytes_down DOUBLE PRECISION NOT NULL,
                bytes_up DOUBLE PRECISION NOT NULL
            )
        """)
        pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_service_metrics_ts ON service_metrics(timestamp)")
        pg_conn.commit()
    except Exception as e:
        print(f"Failed to initialize PostgreSQL tables: {e}")
        pg_conn.rollback()
        sys.exit(1)

    print("\n--- Migrating table: metrics ---")
    try:
        sqlite_cur.execute("SELECT COUNT(*) FROM metrics")
        total_metrics = sqlite_cur.fetchone()[0]
        print(f"Total metrics records to migrate: {total_metrics}")
        
        sqlite_cur.execute("SELECT * FROM metrics")
        inserted_count = 0
        chunk_size = 500
        
        while True:
            rows = sqlite_cur.fetchmany(chunk_size)
            if not rows:
                break
            
            # Prepare bulk insert parameters
            # pg8000 supports standard %s placeholders
            for row in rows:
                pg_cur.execute("""
                    INSERT INTO metrics 
                    (timestamp, cpu_percent, ram_percent, ram_used_gb, ram_total_gb,
                     disk_percent, disk_used_gb, disk_total_gb,
                     net_sent_bytes, net_recv_bytes, net_sent_rate, net_recv_rate, conn_json, extra_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['timestamp'], row['cpu_percent'], row['ram_percent'], row['ram_used_gb'], row['ram_total_gb'],
                    row['disk_percent'], row['disk_used_gb'], row['disk_total_gb'],
                    row['net_sent_bytes'], row['net_recv_bytes'], row['net_sent_rate'], row['net_recv_rate'],
                    row['conn_json'], row['extra_json']
                ))
            pg_conn.commit()
            inserted_count += len(rows)
            print(f"Progress: {inserted_count}/{total_metrics} metrics migrated...")
        
        print("metrics table migration completed successfully.")
    except Exception as e:
        print(f"Error migrating metrics table: {e}")
        pg_conn.rollback()

    print("\n--- Migrating table: service_metrics ---")
    try:
        sqlite_cur.execute("SELECT COUNT(*) FROM service_metrics")
        total_services = sqlite_cur.fetchone()[0]
        print(f"Total service metrics records to migrate: {total_services}")
        
        sqlite_cur.execute("SELECT * FROM service_metrics")
        inserted_count = 0
        
        while True:
            rows = sqlite_cur.fetchmany(chunk_size)
            if not rows:
                break
            
            for row in rows:
                pg_cur.execute("""
                    INSERT INTO service_metrics (timestamp, service, bytes_down, bytes_up)
                    VALUES (%s, %s, %s, %s)
                """, (row['timestamp'], row['service'], row['bytes_down'], row['bytes_up']))
            pg_conn.commit()
            inserted_count += len(rows)
            print(f"Progress: {inserted_count}/{total_services} service metrics migrated...")
        
        print("service_metrics table migration completed successfully.")
    except Exception as e:
        print(f"Error migrating service_metrics table: {e}")
        pg_conn.rollback()

    # Close connections
    sqlite_conn.close()
    pg_conn.close()
    
    print("\nMigration complete! You can now delete metrics.db if you want to save space on your host.")

if __name__ == "__main__":
    main()
