import pandas as pd
import pyodbc
import socket
from datetime import datetime, time
from pathlib import Path

# --- SECTION 1: DATABASE CONNECTIVITY ---

def test_connectivity():
    """Checks if the IP and Port 1433 are reachable."""
    server = '192.168.40.119'
    port = 1433
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    result = sock.connect_ex((server, port))
    sock.close()
    return result == 0

def connect_db():
    driver = '{ODBC Driver 18 for SQL Server}'
    server = '192.168.40.119'
    user = 'sa'
    password = 'essl'
    database = 'elkayemunit2'

    conn_str = (
        f"DRIVER={driver};SERVER={server};DATABASE={database};"
        f"UID={user};PWD={password};Encrypt=no;TrustServerCertificate=yes;LoginTimeout=10;"
    )
    return pyodbc.connect(conn_str)

def fetch_sql_data():
    """Fetches records based on current time:
    - Before 16:00  -> from 06:30 today to now
    - After 16:00   -> from 16:00 today to now
    """
    if not test_connectivity():
        print("Error: Network unreachable. Check VPN or Firewall.")
        return None

    try:
        conn = connect_db()
        print("Connected to Database. Downloading records...")

        # Table name based on current month and year
        now = datetime.now()
        table_name = f"DeviceLogs_{now.month}_{now.year}"

        today_str = now.strftime('%Y-%m-%d')
        cutoff_time = time(16, 0)

        if now.time() < cutoff_time:
            start_time = "06:30:00"
        else:
            start_time = "16:00:00"

        now_str = now.strftime('%H:%M:%S')

        print(f"Time window: {start_time} to {now_str}")

        query = (
            f"SELECT UserId, LogDate FROM {table_name} "
            f"WHERE CAST(LogDate AS DATE) = '{today_str}' "
            f"AND CAST(LogDate AS TIME) >= '{start_time}' "
            f"AND CAST(LogDate AS TIME) <= '{now_str}' "
            f"ORDER BY UserId, LogDate ASC"
        )
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"Database Error: {e}")
        return None

# --- SECTION 2: MAIN EXECUTION ---

if __name__ == "__main__":
    raw_sql_data = fetch_sql_data()

    if raw_sql_data is not None:
        print(f"Extracted {len(raw_sql_data)} records.")

        # Split LogDate into separate Date and Time columns
        raw_sql_data['LogDate'] = pd.to_datetime(raw_sql_data['LogDate'])
        raw_sql_data['Date'] = raw_sql_data['LogDate'].dt.date
        raw_sql_data['Time'] = raw_sql_data['LogDate'].dt.time

        # Save raw data directly to CSV
        output_path = Path(r"C:\Users\u2.ped2\Documents\code for daily manpower\raw_sql_data.csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_sql_data.to_csv(output_path, index=False)
        print(f"\nCompleted! Data saved to {output_path}")