#!/usr/bin/env python3
"""
Elkayem Auto Ancillaries - Manpower Allocation Server (Merged)
Combines v4's feature set (Allocation, Attendance, OT, Gas Flow, Targets,
Leave, Skills, Reports) with the original server's safer static-file
handling and environment-variable configuration (PORT, DATABASE_PATH).

Usage:
  python server_merged.py

Then open in browser:
  http://localhost:8080
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import sqlite3
import socket
import os
import hashlib
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

try:
    import pyodbc
except ImportError:
    pyodbc = None

# Read PORT from environment (for cloud hosting), default to 8080
PORT = int(os.environ.get('PORT', 8080))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
# Use persistent storage path if available (Render.com)
DB_FILE = os.path.join(os.environ.get('DATABASE_PATH', DIRECTORY), 'elkayem_v4.db')

VALID_ROLES = {
    'supervisor': 'Working Area Supervisor',
    'am': 'Area Manager',
    'admin': 'Administrator',
    'higher_auth': 'Higher Authority'
}


# ════════════════════════════════════════════════════════════
# eSSL BIOMETRIC DATABASE (SQL Server)
# Set ESSL_PASSWORD as an environment variable instead of hard-coding it.
# ════════════════════════════════════════════════════════════
ESSL_SERVER   = os.environ.get('ESSL_SERVER', '192.168.40.119')
ESSL_DATABASE = os.environ.get('ESSL_DATABASE', 'elkayemunit2')
ESSL_USER     = os.environ.get('ESSL_USER', 'sa')
ESSL_PASSWORD = os.environ.get('ESSL_PASSWORD', 'essl')
ESSL_DRIVER   = os.environ.get('ESSL_DRIVER', '{ODBC Driver 18 for SQL Server}')

# Employee master table in the eSSL database (check names with: SELECT TOP 5 * FROM Employees)
ESSL_EMP_TABLE = 'Employees'
ESSL_EMP_CODE  = 'EmployeeCode'   # matches DeviceLogs.UserId
ESSL_EMP_NAME  = 'EmployeeName'

# Shift time windows (start, end) on the selected date
SHIFT_WINDOWS = {
    '1': ('06:30:00', '15:59:59'),
    '2': ('16:00:00', '23:59:59'),
    '3': ('00:00:00', '06:29:59'),
}


def essl_connect():
    if pyodbc is None:
        raise RuntimeError('pyodbc is not installed. Run: python -m pip install pyodbc')
    conn_str = (
        f'DRIVER={ESSL_DRIVER};SERVER={ESSL_SERVER};DATABASE={ESSL_DATABASE};'
        f'UID={ESSL_USER};PWD={ESSL_PASSWORD};Encrypt=no;TrustServerCertificate=yes;LoginTimeout=10;'
    )
    return pyodbc.connect(conn_str)


def essl_attendance(date_str: str, shift: str) -> List[Dict]:
    """First punch = clock in, last punch = clock out, per employee, for the shift window."""
    d = datetime.strptime(date_str, '%Y-%m-%d')          # validates input
    start, end = SHIFT_WINDOWS.get(str(shift), SHIFT_WINDOWS['1'])
    table = f'DeviceLogs_{d.month}_{d.year}'               # built from validated ints only
    conn = essl_connect()
    try:
        cur = conn.cursor()
        sql = (
            f'SELECT l.UserId, e.{ESSL_EMP_NAME}, MIN(l.LogDate), MAX(l.LogDate), COUNT(*) '
            f'FROM {table} l LEFT JOIN {ESSL_EMP_TABLE} e ON l.UserId = e.{ESSL_EMP_CODE} '
            f'WHERE CAST(l.LogDate AS DATE) = ? '
            f'AND CAST(l.LogDate AS TIME) >= ? AND CAST(l.LogDate AS TIME) <= ? '
            f'GROUP BY l.UserId, e.{ESSL_EMP_NAME} ORDER BY l.UserId'
        )
        cur.execute(sql, (d.strftime('%Y-%m-%d'), start, end))
        rows = []
        for uid, name, first, last, n in cur.fetchall():
            rows.append({
                'empId': str(uid),
                'name': name or f'ID {uid}',
                'clockIn': first.strftime('%H:%M'),
                'clockOut': last.strftime('%H:%M') if n > 1 else '',
                'punches': n,
            })
        return rows
    finally:
        conn.close()


def essl_employees() -> List[Dict]:
    conn = essl_connect()
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT {ESSL_EMP_CODE}, {ESSL_EMP_NAME} FROM {ESSL_EMP_TABLE} ORDER BY {ESSL_EMP_NAME}')
        return [{'empId': str(c), 'name': (n or '').strip()} for c, n in cur.fetchall() if n]
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════
class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'type1',
            email TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            token TEXT UNIQUE,
            expires_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            phone TEXT,
            skills TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY,
            cell_id TEXT NOT NULL,
            date TEXT NOT NULL,
            shift INTEGER DEFAULT 1,
            process_name TEXT NOT NULL,
            category TEXT,
            plan_count INTEGER DEFAULT 1,
            assigned_employee TEXT,
            remark TEXT,
            status TEXT DEFAULT 'pending',
            created_by INTEGER,
            approval_status TEXT DEFAULT 'pending',
            approved_by INTEGER,
            approved_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cell_id, date, shift, process_name)
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            shift INTEGER DEFAULT 1,
            employee_name TEXT NOT NULL,
            process_name TEXT,
            cell TEXT,
            clock_in TEXT,
            clock_out TEXT,
            status TEXT DEFAULT 'Present',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, shift, employee_name)
        );
        CREATE TABLE IF NOT EXISTS ot_logs (
            id INTEGER PRIMARY KEY,
            employee_name TEXT NOT NULL,
            cell TEXT,
            date TEXT NOT NULL,
            hours REAL DEFAULT 0,
            reason TEXT,
            approved TEXT DEFAULT 'pending',
            month TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS gas_flow_audits (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            shift_block TEXT NOT NULL,
            cell_id TEXT,
            model_name TEXT,
            process_name TEXT,
            mc_no TEXT,
            mc_name TEXT,
            actual_flow REAL,
            revised_flow REAL,
            reading_name TEXT,
            reason TEXT,
            supervisor TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, shift_block, cell_id, process_name, mc_no)
        );
        CREATE TABLE IF NOT EXISTS production_targets (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            shift INTEGER DEFAULT 1,
            cell TEXT,
            product TEXT,
            target INTEGER DEFAULT 0,
            actual INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, shift, cell)
        );
        CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY,
            employee_name TEXT NOT NULL,
            leave_type TEXT,
            from_date TEXT,
            to_date TEXT,
            days INTEGER DEFAULT 1,
            reason TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY,
            employee_name TEXT UNIQUE NOT NULL,
            skill_list TEXT,
            experience INTEGER DEFAULT 1,
            score INTEGER DEFAULT 7,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS allocation_history (
            id INTEGER PRIMARY KEY,
            allocation_id INTEGER,
            date TEXT,
            cell_id TEXT,
            process_name TEXT,
            assigned_employee TEXT,
            status TEXT,
            shift TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(allocation_id) REFERENCES allocations(id)
        );
        ''')

        conn.commit()
        self.insert_sample_data(cursor)
        conn.commit()
        conn.close()

    def insert_sample_data(self, cursor):
        try:
            cursor.execute('SELECT COUNT(*) FROM users')
            if cursor.fetchone()[0] == 0:
                sample_users = [
                    ('admin', self.hash_password('admin123'), 'admin', 'admin@elkayem.com'),
                    ('supervisor', self.hash_password('super123'), 'supervisor', 'sup@elkayem.com'),
                    ('operator', self.hash_password('oper123'), 'type1', 'op@elkayem.com'),
                ]
                cursor.executemany(
                    'INSERT INTO users (username, password_hash, role, email) VALUES (?, ?, ?, ?)',
                    sample_users
                )

            cursor.execute('SELECT COUNT(*) FROM employees')
            if cursor.fetchone()[0] == 0:
                sample_employees = [
                    ('Rajesh Kumar', 'Robot Op', '+91 9876543210', 'Robot Op, MIG Welding', 'active'),
                    ('Priya Singh', 'Welder', '+91 9876543211', 'MIG Welding, TIG Welding', 'active'),
                    ('Amit Patel', 'Helper', '+91 9876543212', 'Material Handling, Cleaning', 'active'),
                    ('Neha Sharma', 'Operator', '+91 9876543213', 'Boring, Revising, Inspection', 'active'),
                    ('Suresh Reddy', 'Robot Op', '+91 9876543214', 'Robot Op, FANUC', 'active'),
                    ('Meena Devi', 'Helper', '+91 9876543215', 'Spatter Cleaning', 'active'),
                    ('Karthik Raja', 'Welder', '+91 9876543216', 'Full Welding, Rework', 'active'),
                    ('Divya Lakshmi', 'Operator', '+91 9876543217', 'Gauge Inspection, Visual', 'active'),
                ]
                cursor.executemany(
                    'INSERT INTO employees (name, category, phone, skills, status) VALUES (?, ?, ?, ?, ?)',
                    sample_employees
                )
        except Exception as e:
            print(f'  Sample data already exists or error: {e}')

    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, sql: str, params: tuple = ()) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results

    def execute(self, sql: str, params: tuple = ()) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        return last_id

    # ── auth / sessions ──
    def authenticate_user(self, username: str, password: str) -> Optional[Dict]:
        results = self.query(
            'SELECT * FROM users WHERE username = ? AND password_hash = ? AND active = 1',
            (username, self.hash_password(password))
        )
        return results[0] if results else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        results = self.query('SELECT * FROM users WHERE id = ?', (user_id,))
        return results[0] if results else None

    def create_session(self, user_id: int) -> str:
        token = hashlib.sha256(f'{user_id}{time.time()}'.encode()).hexdigest()
        expires_at = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        self.execute(
            'INSERT INTO sessions (user_id, token, expires_at) VALUES (?, ?, ?)',
            (user_id, token, expires_at)
        )
        return token

    def verify_token(self, token: str) -> Optional[int]:
        results = self.query(
            'SELECT user_id FROM sessions WHERE token = ? AND expires_at > CURRENT_TIMESTAMP',
            (token,)
        )
        return results[0]['user_id'] if results else None

    # ── employees ──
    def get_employees(self) -> List[Dict]:
        return self.query('SELECT * FROM employees ORDER BY name')

    def add_employee(self, name, category, phone, skills, status) -> int:
        return self.execute(
            'INSERT INTO employees (name, category, phone, skills, status) VALUES (?, ?, ?, ?, ?)',
            (name, category, phone, skills, status)
        )

    def update_employee(self, emp_id, name, category, phone, skills, status):
        self.execute(
            'UPDATE employees SET name=?, category=?, phone=?, skills=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (name, category, phone, skills, status, emp_id)
        )

    def delete_employee(self, emp_id: int):
        self.execute('DELETE FROM employees WHERE id=?', (emp_id,))

    # ── allocations ──
    def save_allocation(self, cell_id, date, shift, rows, user_id):
        for row in rows:
            self.execute(
                '''INSERT OR REPLACE INTO allocations
                   (cell_id, date, shift, process_name, category, plan_count,
                    assigned_employee, remark, status, approval_status, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (cell_id, date, shift, row.get('process', ''), row.get('category', ''),
                 row.get('plan', 1), row.get('assigned', ''), row.get('remark', ''),
                 row.get('status', 'pending'), 'pending', user_id)
            )
            if row.get('assigned', '').strip():
                self.execute(
                    '''INSERT INTO allocation_history
                       (date, cell_id, process_name, assigned_employee, status, shift)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (date, cell_id, row.get('process', ''), row.get('assigned', ''),
                     row.get('status', 'pending'), shift)
                )

    def get_allocations(self, cell_id=None, date=None) -> List[Dict]:
        sql = 'SELECT * FROM allocations WHERE 1=1'
        params = []
        if cell_id:
            sql += ' AND cell_id = ?'
            params.append(cell_id)
        if date:
            sql += ' AND date = ?'
            params.append(date)
        return self.query(sql, tuple(params))

    def get_allocation_history(self, date=None, cell_id=None) -> List[Dict]:
        sql = 'SELECT * FROM allocation_history WHERE 1=1'
        params = []
        if date:
            sql += ' AND date = ?'
            params.append(date)
        if cell_id:
            sql += ' AND cell_id = ?'
            params.append(cell_id)
        sql += ' ORDER BY created_at DESC LIMIT 500'
        return self.query(sql, tuple(params))

    def get_pending_approvals(self) -> List[Dict]:
        return self.query(
            "SELECT * FROM allocations WHERE approval_status = 'pending' ORDER BY created_at DESC LIMIT 500"
        )

    def approve_allocation(self, allocation_id: int, approved_by: int) -> bool:
        try:
            self.execute(
                '''UPDATE allocations SET approval_status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (approved_by, allocation_id)
            )
            return True
        except Exception as e:
            print(f'Error approving allocation: {e}')
            return False

    def reject_allocation(self, allocation_id: int, approved_by: int) -> bool:
        try:
            self.execute(
                '''UPDATE allocations SET approval_status='rejected', approved_by=?, approved_at=CURRENT_TIMESTAMP
                   WHERE id=?''',
                (approved_by, allocation_id)
            )
            return True
        except Exception as e:
            print(f'Error rejecting allocation: {e}')
            return False

    # ── attendance ──
    def save_attendance(self, date, shift, rows):
        for r in rows:
            self.execute(
                '''INSERT OR REPLACE INTO attendance
                   (date, shift, employee_name, process_name, cell, clock_in, clock_out, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, shift, r.get('name', ''), r.get('process', ''), r.get('cell', ''),
                 r.get('clockIn', ''), r.get('clockOut', ''), r.get('status', 'Present'))
            )

    def get_attendance(self, date=None, shift=None) -> List[Dict]:
        sql = 'SELECT * FROM attendance WHERE 1=1'
        params = []
        if date:
            sql += ' AND date = ?'
            params.append(date)
        if shift:
            sql += ' AND shift = ?'
            params.append(shift)
        return self.query(sql, tuple(params))

    # ── OT logs ──
    def save_ot(self, month, rows):
        self.execute('DELETE FROM ot_logs WHERE month = ?', (month,))
        for r in rows:
            self.execute(
                'INSERT INTO ot_logs (employee_name, cell, date, hours, reason, approved, month) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (r.get('name', ''), r.get('cell', ''), r.get('date', ''),
                 r.get('hours', 0), r.get('reason', ''), r.get('approved', 'pending'), month)
            )

    def get_ot(self, month=None) -> List[Dict]:
        sql = 'SELECT * FROM ot_logs WHERE 1=1'
        params = []
        if month:
            sql += ' AND month = ?'
            params.append(month)
        sql += ' ORDER BY date DESC'
        return self.query(sql, tuple(params))

    # ── gas flow ──
    def save_gas_flow_audit(self, date, shift_block, rows, user_id):
        for row in rows:
            self.execute(
                '''INSERT OR REPLACE INTO gas_flow_audits
                   (date, shift_block, cell_id, model_name, process_name, mc_no, mc_name,
                    actual_flow, revised_flow, reading_name, reason, supervisor, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, shift_block, row.get('cell', ''), row.get('model', ''), row.get('process', ''),
                 row.get('mcNo', ''), row.get('mcName', ''), row.get('actualFlow'),
                 row.get('revisedFlow'), row.get('name', ''), row.get('reason', ''),
                 row.get('supervisor', ''), user_id)
            )

    def get_gas_flow_audit(self, date=None) -> List[Dict]:
        if date:
            return self.query('SELECT * FROM gas_flow_audits WHERE date = ?', (date,))
        return self.query('SELECT * FROM gas_flow_audits')

    # ── targets ──
    def save_targets(self, date, shift, rows):
        for r in rows:
            self.execute(
                '''INSERT OR REPLACE INTO production_targets
                   (date, shift, cell, product, target, actual)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (date, shift, r.get('cell'), r.get('product', ''), r.get('target', 0), r.get('actual', 0))
            )

    def get_targets(self, date=None, shift=None) -> List[Dict]:
        sql = 'SELECT * FROM production_targets WHERE 1=1'
        params = []
        if date:
            sql += ' AND date = ?'
            params.append(date)
        if shift:
            sql += ' AND shift = ?'
            params.append(shift)
        return self.query(sql, tuple(params))

    # ── leaves ──
    def add_leave(self, emp, leave_type, from_date, to_date, days, reason, status) -> int:
        return self.execute(
            'INSERT INTO leaves (employee_name, leave_type, from_date, to_date, days, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (emp, leave_type, from_date, to_date, days, reason, status)
        )

    def get_leaves(self, month=None) -> List[Dict]:
        sql = 'SELECT * FROM leaves WHERE 1=1'
        params = []
        if month:
            sql += " AND strftime('%Y-%m', from_date) = ?"
            params.append(month)
        sql += ' ORDER BY created_at DESC'
        return self.query(sql, tuple(params))

    def set_leave_status(self, leave_id, status):
        self.execute('UPDATE leaves SET status=? WHERE id=?', (status, leave_id))

    def delete_leave(self, leave_id):
        self.execute('DELETE FROM leaves WHERE id=?', (leave_id,))

    # ── skills ──
    def save_skill(self, employee_name, skill_list, experience, score):
        self.execute(
            '''INSERT OR REPLACE INTO skills (employee_name, skill_list, experience, score, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)''',
            (employee_name, skill_list, experience, score)
        )

    def get_skills(self) -> List[Dict]:
        return self.query('SELECT * FROM skills ORDER BY employee_name')

    # ── analytics / reports ──
    def get_analytics(self) -> Dict:
        today = datetime.now().strftime('%Y-%m-%d')
        month = datetime.now().strftime('%Y-%m')

        total_employees = self.query('SELECT COUNT(*) as c FROM employees')[0]['c']
        allocations_today = self.query('SELECT COUNT(*) as c FROM allocations WHERE date=?', (today,))[0]['c']
        filled = self.query(
            "SELECT COUNT(*) as c FROM allocations WHERE date=? AND assigned_employee IS NOT NULL AND assigned_employee!=''",
            (today,)
        )[0]['c']
        ot_hours = self.query("SELECT COALESCE(SUM(hours),0) as h FROM ot_logs WHERE month=?", (month,))[0]['h']
        absent = self.query("SELECT COUNT(*) as c FROM attendance WHERE date=? AND status='Absent'", (today,))[0]['c']
        on_leave = self.query(
            "SELECT COUNT(*) as c FROM leaves WHERE status='Approved' AND from_date<=? AND to_date>=?",
            (today, today)
        )[0]['c']
        util = round(filled / allocations_today * 100, 1) if allocations_today > 0 else 0

        return {
            'totalEmployees': total_employees,
            'allocationsToday': allocations_today,
            'avgUtilization': util,
            'otHours': float(ot_hours),
            'absentToday': absent,
            'onLeave': on_leave
        }

    def get_monthly_report(self, month: str) -> List[Dict]:
        rows = []
        for c in range(1, 12):
            total = self.query(
                "SELECT COUNT(*) as n FROM allocations WHERE cell_id=? AND strftime('%Y-%m',date)=?",
                (str(c), month)
            )[0]['n']
            filled = self.query(
                "SELECT COUNT(*) as n FROM allocations WHERE cell_id=? AND strftime('%Y-%m',date)=? AND assigned_employee!=''",
                (str(c), month)
            )[0]['n']
            absent = self.query(
                "SELECT COUNT(*) as n FROM allocations WHERE cell_id=? AND strftime('%Y-%m',date)=? AND remark='Absent'",
                (str(c), month)
            )[0]['n']
            ot = self.query(
                "SELECT COALESCE(SUM(hours),0) as h FROM ot_logs WHERE cell=? AND month=?",
                (str(c), month)
            )[0]['h']
            util = round(filled / total * 100, 1) if total > 0 else 0
            rows.append({'cell': c, 'total': total, 'filled': filled, 'absent': absent,
                         'otHours': float(ot), 'util': util})
        return rows


# ════════════════════════════════════════════════════════════
# HTTP HANDLER
# ════════════════════════════════════════════════════════════
class APIHandler(BaseHTTPRequestHandler):
    db = Database(DB_FILE)
#   from supabase_db import SupabaseDatabase
# db = SupabaseDatabase()

    def log_message(self, format, *args):
        client = self.client_address[0]
        print(f'  [{client}] {format % args}')

    def send_json_response(self, data: Dict, status: int = 200):
        payload = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, status: int = 400):
        self.send_json_response({'error': message, 'status': 'error'}, status)

    def get_auth_user(self) -> Optional[Dict]:
        """Return the authenticated user's row (or None)."""
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            user_id = self.db.verify_token(token)
            if user_id:
                return self.db.get_user_by_id(user_id)
        return None

    def read_body(self) -> Dict:
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            return json.loads(body) if body else {}
        except Exception:
            return None

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    # ══════════════════════════════════════════════════════
    # GET
    # ══════════════════════════════════════════════════════
    def do_GET(self):
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        path = parsed.path

        # Static / index - unauthenticated
        if path in ('/', '/index.html', '/elkayem_v4.html'):
            self.serve_static(path)
            return

        user = self.get_auth_user()
        if not user:
            if not path.startswith('/api/'):
                self.serve_static(path)
                return
            self.send_error_json('Unauthorized', 401)
            return

        if path == '/api/employees':
            self.send_json_response({'employees': self.db.get_employees()}); return

        if path == '/api/allocations':
            cell_id = query_params.get('cellId', [None])[0]
            date = query_params.get('date', [None])[0]
            self.send_json_response({'allocations': self.db.get_allocations(cell_id, date)}); return

        if path == '/api/allocations/history':
            date = query_params.get('date', [None])[0]
            cell_id = query_params.get('cellId', [None])[0]
            self.send_json_response({'history': self.db.get_allocation_history(date, cell_id)}); return

        if path == '/api/allocations/pending':
            if user['role'] != 'admin':
                self.send_error_json('Admin access required', 403); return
            self.send_json_response({'pending': self.db.get_pending_approvals()}); return

        if path == '/api/attendance':
            date = query_params.get('date', [None])[0]
            shift = query_params.get('shift', [None])[0]
            self.send_json_response({'attendance': self.db.get_attendance(date, shift)}); return

        if path == '/api/ot':
            month = query_params.get('month', [None])[0]
            self.send_json_response({'otLogs': self.db.get_ot(month)}); return

        if path == '/api/gasflow':
            date = query_params.get('date', [None])[0]
            self.send_json_response({'rows': self.db.get_gas_flow_audit(date)}); return

        if path == '/api/targets':
            date = query_params.get('date', [None])[0]
            shift = query_params.get('shift', [None])[0]
            self.send_json_response({'targets': self.db.get_targets(date, shift)}); return

        if path == '/api/leaves':
            month = query_params.get('month', [None])[0]
            self.send_json_response({'leaves': self.db.get_leaves(month)}); return

        if path == '/api/skills':
            self.send_json_response({'skills': self.db.get_skills()}); return

        if path == '/api/essl/attendance':
            date = query_params.get('date', [datetime.now().strftime('%Y-%m-%d')])[0]
            shift = query_params.get('shift', ['1'])[0]
            try:
                self.send_json_response({'rows': essl_attendance(date, shift)})
            except Exception as e:
                self.send_error_json(f'Biometric DB error: {e}', 500)
            return

        if path == '/api/essl/employees':
            try:
                self.send_json_response({'employees': essl_employees()})
            except Exception as e:
                self.send_error_json(f'Biometric DB error: {e}', 500)
            return

        if path == '/api/analytics':
            self.send_json_response(self.db.get_analytics()); return

        if path == '/api/reports/monthly':
            month = query_params.get('month', [datetime.now().strftime('%Y-%m')])[0]
            self.send_json_response({'month': month, 'rows': self.db.get_monthly_report(month)}); return

        self.serve_static(path)

    # ══════════════════════════════════════════════════════
    # POST
    # ══════════════════════════════════════════════════════
    def do_POST(self):
        parsed = urlparse(self.path)
        data = self.read_body()
        if data is None:
            self.send_error_json('Invalid JSON', 400); return

        if parsed.path == '/api/login':
            username = data.get('username')
            password = data.get('password')
            if not username or not password:
                self.send_error_json('Username and password required', 400); return

            user = self.db.authenticate_user(username, password)
            if not user:
                self.send_error_json('Invalid credentials', 401); return

            token = self.db.create_session(user['id'])
            self.send_json_response({
                'status': 'ok',
                'token': token,
                'user': {
                    'id': user['id'],
                    'username': user['username'],
                    'role': user['role'],  # always the real DB role - never client-selectable
                    'email': user['email']
                }
            }, 200)
            return

        user = self.get_auth_user()
        if not user:
            self.send_error_json('Unauthorized', 401); return
        user_id = user['id']

        if parsed.path == '/api/employees':
            emp_id = self.db.add_employee(
                data.get('name'), data.get('category'), data.get('phone'),
                data.get('skills', ''), data.get('status', 'active')
            )
            self.send_json_response({'status': 'ok', 'id': emp_id}, 201); return

        if parsed.path == '/api/allocations':
            self.db.save_allocation(
                data.get('cellId'), data.get('date'), data.get('shift', 1),
                data.get('rows', []), user_id
            )
            self.send_json_response({'status': 'ok', 'message': 'Allocation saved'}); return

        if parsed.path == '/api/attendance':
            self.db.save_attendance(data.get('date'), data.get('shift', 1), data.get('rows', []))
            self.send_json_response({'status': 'ok'}); return

        if parsed.path == '/api/ot':
            self.db.save_ot(data.get('month'), data.get('rows', []))
            self.send_json_response({'status': 'ok'}); return

        if parsed.path == '/api/gasflow':
            self.db.save_gas_flow_audit(
                data.get('date'), data.get('shiftBlock'), data.get('rows', []), user_id
            )
            self.send_json_response({'status': 'ok', 'message': 'Gas flow audit saved'}); return

        if parsed.path == '/api/targets':
            self.db.save_targets(data.get('date'), data.get('shift', 1), data.get('rows', []))
            self.send_json_response({'status': 'ok'}); return

        if parsed.path == '/api/leaves':
            lid = self.db.add_leave(
                data.get('emp', ''), data.get('type', 'Casual'), data.get('from'),
                data.get('to'), data.get('days', 1), data.get('reason', ''),
                data.get('status', 'Pending')
            )
            self.send_json_response({'id': lid}, 201); return

        if parsed.path == '/api/skills':
            self.db.save_skill(
                data.get('employeeName'), data.get('skills', ''),
                data.get('exp', 1), data.get('score', 7)
            )
            self.send_json_response({'status': 'ok'}); return

        self.send_error_json('Not found', 404)

    # ══════════════════════════════════════════════════════
    # PUT
    # ══════════════════════════════════════════════════════
    def do_PUT(self):
        parsed = urlparse(self.path)
        data = self.read_body()
        if data is None:
            self.send_error_json('Invalid JSON', 400); return

        user = self.get_auth_user()
        if not user:
            self.send_error_json('Unauthorized', 401); return

        if parsed.path.startswith('/api/employees/'):
            emp_id = int(parsed.path.split('/')[-1])
            self.db.update_employee(
                emp_id, data.get('name'), data.get('category'), data.get('phone'),
                data.get('skills', ''), data.get('status', 'active')
            )
            self.send_json_response({'status': 'ok'}); return

        if parsed.path.startswith('/api/allocations/approve/'):
            if user['role'] != 'admin':
                self.send_error_json('Admin access required', 403); return
            allocation_id = int(parsed.path.split('/')[-1])
            ok = self.db.approve_allocation(allocation_id, user['id'])
            if ok:
                self.send_json_response({'status': 'ok', 'message': 'Allocation approved'})
            else:
                self.send_error_json('Failed to approve', 500)
            return

        if parsed.path.startswith('/api/allocations/reject/'):
            if user['role'] != 'admin':
                self.send_error_json('Admin access required', 403); return
            allocation_id = int(parsed.path.split('/')[-1])
            ok = self.db.reject_allocation(allocation_id, user['id'])
            if ok:
                self.send_json_response({'status': 'ok', 'message': 'Allocation rejected'})
            else:
                self.send_error_json('Failed to reject', 500)
            return

        if parsed.path.startswith('/api/leaves/'):
            parts = parsed.path.split('/')
            if parts[-1] in ('approve', 'reject'):
                leave_id = parts[-2]
                status = 'Approved' if parts[-1] == 'approve' else 'Rejected'
            else:
                leave_id = parts[-1]
                status = data.get('status', 'Pending')
            self.db.set_leave_status(leave_id, status)
            self.send_json_response({'status': 'ok'}); return

        self.send_error_json('Not found', 404)

    # ══════════════════════════════════════════════════════
    # DELETE
    # ══════════════════════════════════════════════════════
    def do_DELETE(self):
        parsed = urlparse(self.path)

        user = self.get_auth_user()
        if not user:
            self.send_error_json('Unauthorized', 401); return

        if parsed.path.startswith('/api/employees/'):
            emp_id = int(parsed.path.split('/')[-1])
            self.db.delete_employee(emp_id)
            self.send_json_response({'status': 'ok'}); return

        if parsed.path.startswith('/api/leaves/'):
            leave_id = int(parsed.path.split('/')[-1])
            self.db.delete_leave(leave_id)
            self.send_json_response({'status': 'ok'}); return

        self.send_error_json('Not found', 404)

    # ══════════════════════════════════════════════════════
    # STATIC FILE SERVING (v1's hardened version, kept as-is)
    # ══════════════════════════════════════════════════════
    def serve_static(self, path):
        if path == '/' or path == '/index.html':
            candidate = os.path.join(DIRECTORY, 'elkayem_v4.html')
            file_path = candidate if os.path.isfile(candidate) else os.path.join(DIRECTORY, 'index_advanced.html')
        else:
            # SECURITY: resolve the requested path and verify it stays
            # inside DIRECTORY before touching the filesystem. Without
            # this check, a path like /../server_merged.py or
            # /../elkayem_v4.db lets a user read any file on the server
            # (source code, secrets, the database).
            requested = os.path.normpath(path.lstrip('/'))
            if requested.startswith('..') or os.path.isabs(requested):
                self.send_response(403); self.end_headers(); return
            base = os.path.realpath(DIRECTORY)
            file_path = os.path.realpath(os.path.join(base, requested))
            if not (file_path == base or file_path.startswith(base + os.sep)):
                self.send_response(403); self.end_headers(); return
            # SECURITY: whitelist static-asset extensions only - don't
            # serve source code, the .db file, docs, or scripts that
            # happen to live next to the server.
            ALLOWED_EXTENSIONS = ('.html', '.css', '.js', '.png', '.jpg',
                                   '.jpeg', '.svg', '.ico', '.woff', '.woff2')
            if not file_path.lower().endswith(ALLOWED_EXTENSIONS):
                self.send_response(403); self.end_headers(); return

        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_response(404); self.end_headers(); return

        try:
            with open(file_path, 'rb') as f:
                content = f.read()

            content_type = 'text/html'
            if file_path.endswith('.css'):
                content_type = 'text/css'
            elif file_path.endswith('.js'):
                content_type = 'application/javascript'

            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500); self.end_headers()
            print(f'  Error serving file: {e}')


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    local_ip = get_local_ip()
    is_cloud = 'RENDER' in os.environ

    print('=' * 60)
    print('  ELKAYEM MANPOWER ALLOCATION SERVER (MERGED)')
    print('=' * 60)
    print(f'  Database:  {DB_FILE}')
    if is_cloud:
        print('  Cloud Deployed (Render.com)')
        print('  Access at: https://YOUR-APP-NAME.onrender.com')
    else:
        print(f'  Local:     http://localhost:{PORT}')
        print(f'  Network:   http://{local_ip}:{PORT}')
    print('=' * 60)
    print('  Default Logins:')
    print('    admin      / admin123  (full access)')
    print('    supervisor / super123  (manage allocations)')
    print('    operator   / oper123   (view only)')
    print('  Change credentials in database after first login!')
    print('=' * 60)
    print('  API Endpoints:')
    print('    POST      /api/login')
    print('    GET/POST  /api/employees')
    print('    GET/POST  /api/allocations')
    print('    GET       /api/allocations/history')
    print('    GET       /api/allocations/pending      (admin)')
    print('    PUT       /api/allocations/approve/<id> (admin)')
    print('    PUT       /api/allocations/reject/<id>  (admin)')
    print('    GET/POST  /api/attendance')
    print('    GET/POST  /api/ot')
    print('    GET/POST  /api/gasflow')
    print('    GET/POST  /api/targets')
    print('    GET/POST  /api/leaves')
    print('    GET/POST  /api/skills')
    print('    GET       /api/analytics')
    print('    GET       /api/reports/monthly')
    print('=' * 60)
    print('  Press Ctrl+C to stop the server')
    print()

    server = HTTPServer(('', PORT), APIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Server stopped.')
