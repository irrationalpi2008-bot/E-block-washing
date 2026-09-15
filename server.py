from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
import sqlite3
import json
import os
import re
import secrets
import hashlib
import threading
import socket
import sys
import csv
import io
import time
from urllib.parse import parse_qs, urlparse

from pdf_export import generate_allocations_pdf

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('LAUNDRY_DB', APP_DIR / 'laundry.db'))
ADMIN_CSV_PATH = APP_DIR / 'admin_roster.csv'
ADMIN_KEY_PATH = APP_DIR / 'admin_key.txt'

_default_roster = APP_DIR / 'roster.csv'
if not _default_roster.exists() and (APP_DIR / 'Washing Machine.csv').exists():
    _default_roster = APP_DIR / 'Washing Machine.csv'
ROSTER_CSV_PATH = Path(os.environ.get('ROSTER_CSV', _default_roster))
os.chdir(APP_DIR)

# ----------------------------------------------------------------------------
# Schedule definition — MUST match the frontend's DAYS / TIMES / NUM_PREFS.
# ----------------------------------------------------------------------------
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NUM_TIMESLOTS = 16          # 1.5h cycles (1h wash + 30min buffer), 24/7
MACHINES_PER_SLOT = 3       # 3 washing machines
NUM_PREFS = 7                # how many ranked choices each person submits
FALLBACK_COST = 100         # cost of landing on a slot the person never ranked
TIMES = [
    ("00:00", "01:30"), ("01:30", "03:00"), ("03:00", "04:30"), ("04:30", "06:00"),
    ("06:00", "07:30"), ("07:30", "09:00"), ("09:00", "10:30"), ("10:30", "12:00"),
    ("12:00", "13:30"), ("13:30", "15:00"), ("15:00", "16:30"), ("16:30", "18:00"),
    ("18:00", "19:30"), ("19:30", "21:00"), ("21:00", "22:30"), ("22:30", "00:00"),
]

ALL_SLOTS = [f"{d}-{t}" for d in DAYS for t in range(NUM_TIMESLOTS)]
ALL_SLOTS_SET = set(ALL_SLOTS)
SLOT_RE = re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)-(\d{1,2})$')

_db_lock = threading.Lock()  # sqlite writes lock
_rate_limit_lock = threading.Lock()
_rate_limit_failures = {}
_admin_csv_lock = threading.Lock()
RATE_LIMIT_WINDOW = 300
RATE_LIMIT_MAX_FAILURES = 5


# ----------------------------------------------------------------------------
# Admin key
# ----------------------------------------------------------------------------
def get_admin_key():
    if ADMIN_KEY_PATH.exists():
        key = ADMIN_KEY_PATH.read_text().strip()
        if key:
            return key
    key = secrets.token_urlsafe(16)
    ADMIN_KEY_PATH.write_text(key)
    return key


ADMIN_KEY = get_admin_key()


# ----------------------------------------------------------------------------
# Network helpers
# ----------------------------------------------------------------------------
def get_local_ip():
    """Detect local IP address on IISc WLAN or local network interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = '127.0.0.1'
    finally:
        s.close()
    return ip


# ----------------------------------------------------------------------------
# DB Helpers & Migrations
# ----------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users
                        (room_name TEXT PRIMARY KEY,
                         name TEXT,
                         room TEXT,
                         pin_hash TEXT,
                         pin_salt TEXT,
                         session_token TEXT,
                         created_at TEXT,
                         last_login TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS picks
                        (room_name TEXT PRIMARY KEY,
                         name TEXT,
                         room TEXT,
                         data TEXT,
                         updated_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS assignments
                        (room_name TEXT PRIMARY KEY,
                         name TEXT,
                         room TEXT,
                         slot_id TEXT,
                         machine_num INTEGER,
                         priority_awarded INTEGER,
                         assigned_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS assignment_meta
                        (id INTEGER PRIMARY KEY CHECK (id = 1),
                         computed_at TEXT,
                         stats TEXT,
                         total_people INTEGER)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS residents
                        (resident_id TEXT PRIMARY KEY,
                         room TEXT NOT NULL,
                         name TEXT NOT NULL,
                         phone TEXT,
                         payment_status TEXT,
                         payment_method TEXT,
                         using_machine INTEGER NOT NULL DEFAULT 1,
                         source_note TEXT,
                         imported_at TEXT NOT NULL)''')

        # Check if machine_num column exists in assignments
        cursor = conn.execute("PRAGMA table_info(assignments)")
        columns = [row['name'] for row in cursor.fetchall()]
        if 'machine_num' not in columns and len(columns) > 0:
            conn.execute("ALTER TABLE assignments ADD COLUMN machine_num INTEGER DEFAULT 1")
        conn.commit()


def _parse_roster_entry(value):
    value = (value or '').strip()
    source_note = value
    if not value or value.upper() in {'NO ROOMMATE', 'TEMPLATE'}:
        return None

    lowered = value.lower()
    if lowered in {'not paying', 'pending for today', 'pending for tom'}:
        return None
    using_machine = 0 if 'not using' in lowered else 1
    payment_status = 'paid' if lowered.startswith('paid-') else (
        'not paying' if 'not paying' in lowered else 'pending'
    )
    payment_method = ''
    phone = ''
    if ',' in value:
        details = [part.strip() for part in value.split(',')]
        value = details[0]
        if len(details) > 1:
            phone = re.sub(r'\D', '', details[1])[-10:]
        if len(details) > 2:
            payment_method = details[2]
    value = re.sub(r'\s*-\s*(not using|pending.*|not paying)\s*$', '', value, flags=re.IGNORECASE).strip()
    value = re.sub(r'^paid\s*-\s*', '', value, flags=re.IGNORECASE).strip()
    if not phone:
        phone_match = re.search(r'(?:\+?91\s*)?(\d[\d\s\u200b\u202a\u202c-]{8,}\d)', value)
        if phone_match:
            phone = re.sub(r'\D', '', phone_match.group(0))[-10:]
            value = value[:phone_match.start()].rstrip(' -')
    value = re.sub(r'\s+', ' ', value).strip()
    if not value:
        return None
    return {
        'name': value,
        'phone': phone,
        'payment_status': payment_status,
        'payment_method': payment_method,
        'using_machine': using_machine,
        'source_note': source_note,
    }


def import_roster():
    if not ROSTER_CSV_PATH.exists():
        return
    imported_at = datetime.now(timezone.utc).isoformat()
    with ROSTER_CSV_PATH.open(newline='', encoding='utf-8-sig') as csv_file:
        rows = csv.DictReader(csv_file)
        imported = []
        for row in rows:
            room = (row.get('Rooms') or '').strip()
            if not room.isdigit():
                continue
            for position, column in enumerate(('Person A', 'Person B'), start=1):
                resident = _parse_roster_entry(row.get(column))
                if resident:
                    imported.append((
                        f'{room}-{position}', room, resident['name'], resident['phone'],
                        resident['payment_status'], resident['payment_method'],
                        resident['using_machine'], resident['source_note'], imported_at
                    ))

    with _db_lock:
        with get_db_connection() as conn:
            imported_ids = {row[0] for row in imported}
            existing_ids = {
                row[0] for row in conn.execute("SELECT resident_id FROM residents")
            }
            conn.executemany(
                "INSERT OR IGNORE INTO residents "
                "(resident_id, room, name, phone, payment_status, payment_method, "
                "using_machine, source_note, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                imported
            )
            conn.commit()


def backfill_roster_phones():
    if not ROSTER_CSV_PATH.exists():
        return
    imported_at = datetime.now(timezone.utc).isoformat()
    with ROSTER_CSV_PATH.open(newline='', encoding='utf-8-sig') as csv_file:
        rows = csv.DictReader(csv_file)
        phones = []
        for row in rows:
            room = (row.get('Rooms') or '').strip()
            if not room.isdigit():
                continue
            for position, column in enumerate(('Person A', 'Person B'), start=1):
                resident = _parse_roster_entry(row.get(column))
                if resident and resident['phone']:
                    phones.append((resident['phone'], imported_at, f'{room}-{position}'))
    with _db_lock:
        with get_db_connection() as conn:
            conn.executemany(
                "UPDATE residents SET phone = ?, imported_at = ? WHERE resident_id = ? AND (phone IS NULL OR phone = '')",
                phones
            )
            conn.commit()


init_db()
with get_db_connection() as _conn:
    _resident_count = _conn.execute("SELECT count(*) FROM residents").fetchone()[0]
if _resident_count == 0:
    import_roster()
else:
    backfill_roster_phones()


def sanitize_key(s):
    """Normalize a name/room into a stable lowercase key so 'Aditi' and
    'aditi ' map to the exact same identifier."""
    s = (s or "").strip().lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


def hash_pin(pin: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    pin_bytes = pin.strip().encode('utf-8')
    salt_bytes = salt.encode('utf-8')
    hash_hex = hashlib.pbkdf2_hmac('sha256', pin_bytes, salt_bytes, 100000).hex()
    return hash_hex, salt


def verify_pin(pin: str, stored_hash: str, salt: str):
    computed_hash, _ = hash_pin(pin, salt)
    return secrets.compare_digest(computed_hash, stored_hash)


def _rate_limit_retry_after(bucket_keys, max_failures=RATE_LIMIT_MAX_FAILURES):
    now = time.monotonic()
    with _rate_limit_lock:
        retry_after = 0
        for bucket_key in bucket_keys:
            failures = _rate_limit_failures.setdefault(bucket_key, deque())
            while failures and now - failures[0] >= RATE_LIMIT_WINDOW:
                failures.popleft()
            if len(failures) >= max_failures:
                retry_after = max(retry_after, int(RATE_LIMIT_WINDOW - (now - failures[0])) + 1)
        return retry_after


def _record_rate_limit_failure(bucket_keys):
    now = time.monotonic()
    with _rate_limit_lock:
        for bucket_key in bucket_keys:
            failures = _rate_limit_failures.setdefault(bucket_key, deque())
            while failures and now - failures[0] >= RATE_LIMIT_WINDOW:
                failures.popleft()
            failures.append(now)


def _clear_rate_limit_failures(bucket_keys):
    with _rate_limit_lock:
        for bucket_key in bucket_keys:
            _rate_limit_failures.pop(bucket_key, None)


def _send_rate_limited(handler, retry_after):
    body = json.dumps({"error": "Too many attempts. Try again later."}).encode('utf-8')
    handler.send_response(429)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
    handler.send_header('Retry-After', str(retry_after))
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _verify_admin_key(handler, key):
    bucket_key = ('admin-ip', handler.client_address[0])
    retry_after = _rate_limit_retry_after([bucket_key])
    if retry_after:
        _send_rate_limited(handler, retry_after)
        return False
    if not secrets.compare_digest(key, ADMIN_KEY):
        _record_rate_limit_failure([bucket_key])
        _send_json(handler, {"error": "Invalid or missing admin key"}, status=403)
        return False
    _clear_rate_limit_failures([bucket_key])
    return True


def format_slot_label(slot_id):
    m = SLOT_RE.match(slot_id or '')
    if not m:
        return slot_id or ''
    day, ti_str = m.group(1), int(m.group(2))
    if 0 <= ti_str < len(TIMES):
        t0, t1 = TIMES[ti_str]
        return f"{day} {t0}–{t1}"
    return slot_id


# ----------------------------------------------------------------------------
# Authentication Verification
# ----------------------------------------------------------------------------
def get_auth_token_from_handler(handler):
    auth_header = handler.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    return handler.headers.get('X-Session-Token', '').strip()


def is_authenticated_user(handler, room_name: str) -> bool:
    token = get_auth_token_from_handler(handler)
    if not token or not room_name:
        return False
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT session_token FROM users WHERE room_name = ?", (room_name,))
        row = c.fetchone()
        if row and row['session_token'] and secrets.compare_digest(row['session_token'], token):
            return True
    finally:
        conn.close()
    return False


def get_roster_resident(name, room):
    target_name = sanitize_key(name)
    target_room = sanitize_key(room)
    if not target_name or not target_room:
        return None
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM residents").fetchall()
    finally:
        conn.close()
    for row in rows:
        if sanitize_key(row['name']) == target_name and sanitize_key(row['room']) == target_room:
            return row
    return None


# ----------------------------------------------------------------------------
# Min-Cost Max-Flow
# ----------------------------------------------------------------------------
class MinCostFlow:
    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost):
        self.graph[u].append([v, cap, cost, len(self.graph[v])])
        self.graph[v].append([u, 0, -cost, len(self.graph[u]) - 1])

    def solve(self, s, t, max_flow):
        n = self.n
        res_flow = 0
        res_cost = 0
        while res_flow < max_flow:
            dist = [float('inf')] * n
            in_queue = [False] * n
            prev_edge = [None] * n
            dist[s] = 0
            dq = deque([s])
            in_queue[s] = True
            while dq:
                u = dq.popleft()
                in_queue[u] = False
                for i, e in enumerate(self.graph[u]):
                    v, cap, cost, _rev = e
                    if cap > 0 and dist[u] + cost < dist[v]:
                        dist[v] = dist[u] + cost
                        prev_edge[v] = (u, i)
                        if not in_queue[v]:
                            dq.append(v)
                            in_queue[v] = True
            if dist[t] == float('inf'):
                break
            d = max_flow - res_flow
            v = t
            while v != s:
                u, i = prev_edge[v]
                d = min(d, self.graph[u][i][1])
                v = u
            v = t
            while v != s:
                u, i = prev_edge[v]
                self.graph[u][i][1] -= d
                rev = self.graph[u][i][3]
                self.graph[v][rev][1] += d
                v = u
            res_flow += d
            res_cost += d * dist[t]
        return res_flow, res_cost


def compute_assignment(persist=True):
    """Solve the capacitated assignment, optionally persisting the result."""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT room_name, name, room, data FROM picks")
        rows = c.fetchall()
    finally:
        conn.close()

    people = []
    for row in rows:
        room_name = row['room_name']
        name = row['name']
        room = row['room']
        data = row['data']
        try:
            payload = json.loads(data)
        except Exception:
            continue
        raw_prios = payload.get('priorities', {}) or {}
        clean = {}
        seen_ranks = set()
        for sid, p in raw_prios.items():
            if sid not in ALL_SLOTS_SET:
                continue
            try:
                p = int(p)
            except Exception:
                continue
            if not (1 <= p <= NUM_PREFS) or p in seen_ranks:
                continue
            seen_ranks.add(p)
            clean[sid] = p
        people.append({'room_name': room_name, 'name': name, 'room': room, 'priorities': clean})

    result = {
        'computed_at': datetime.now(timezone.utc).isoformat(),
        'total_people': len(people),
        'stats': {},
        'slots': {},
        'people': {},
    }

    if not people:
        if persist:
            _persist_assignment([], result)
        return result

    first_choice_claims = {}
    for person in people:
        for sid, priority in person['priorities'].items():
            if priority == 1:
                first_choice_claims.setdefault(sid, []).append(person)

    reserved = []
    reserved_people = set()
    reserved_capacity = {}
    for sid, claimants in first_choice_claims.items():
        if len(claimants) <= MACHINES_PER_SLOT:
            reserved_capacity[sid] = len(claimants)
            for person in claimants:
                reserved.append((person, sid))
                reserved_people.add(person['room_name'])

    remaining_people = [person for person in people if person['room_name'] not in reserved_people]
    P = len(remaining_people)
    S = len(ALL_SLOTS)
    slot_index = {sid: i for i, sid in enumerate(ALL_SLOTS)}

    SRC = 0
    PERSON0 = 1
    SLOT0 = PERSON0 + P
    SINK = SLOT0 + S

    mcmf = MinCostFlow(SINK + 1)
    for i, person in enumerate(remaining_people):
        mcmf.add_edge(SRC, PERSON0 + i, 1, 0)
        prios = person['priorities']
        chosen = set(prios.keys())
        for sid, p in prios.items():
            cost = p * p
            mcmf.add_edge(PERSON0 + i, SLOT0 + slot_index[sid], 1, cost)
        for sid in ALL_SLOTS:
            if sid not in chosen:
                mcmf.add_edge(PERSON0 + i, SLOT0 + slot_index[sid], 1, FALLBACK_COST)

    for sid in ALL_SLOTS:
        mcmf.add_edge(SLOT0 + slot_index[sid], SINK, MACHINES_PER_SLOT - reserved_capacity.get(sid, 0), 0)

    flow, _total_cost = mcmf.solve(SRC, SINK, P)

    assigned_rows = []
    stats = {}
    slots_out = {}
    people_out = {}

    slot_machine_counter = {sid: reserved_capacity.get(sid, 0) + 1 for sid in ALL_SLOTS}

    for person, sid_assigned in reserved:
        p_awarded = 1
        stats['1'] = stats.get('1', 0) + 1
        mach = slot_machine_counter[sid_assigned] - reserved_capacity[sid_assigned]
        slot_machine_counter[sid_assigned] += 1
        slot_label = format_slot_label(sid_assigned)
        slots_out.setdefault(sid_assigned, []).append({
            'name': person['name'],
            'room': person['room'],
            'priority_awarded': p_awarded,
            'machine': mach
        })
        people_out[person['room_name']] = {
            'name': person['name'],
            'room': person['room'],
            'slot': sid_assigned,
            'slot_label': slot_label,
            'priority_awarded': p_awarded,
            'machine': mach,
        }
        assigned_rows.append((person['room_name'], person['name'], person['room'],
                              sid_assigned, mach, p_awarded))

    for i, person in enumerate(remaining_people):
        node = PERSON0 + i
        sid_assigned = None
        for e in mcmf.graph[node]:
            v, cap, _cost, _rev = e
            if SLOT0 <= v < SINK and cap == 0:
                sid_assigned = ALL_SLOTS[v - SLOT0]
                break
        if sid_assigned is None:
            continue

        p_awarded = person['priorities'].get(sid_assigned)
        stat_key = str(p_awarded) if p_awarded is not None else 'fallback'
        stats[stat_key] = stats.get(stat_key, 0) + 1

        mach = slot_machine_counter[sid_assigned]
        slot_machine_counter[sid_assigned] += 1

        slot_label = format_slot_label(sid_assigned)
        slots_out.setdefault(sid_assigned, []).append({
            'name': person['name'],
            'room': person['room'],
            'priority_awarded': p_awarded,
            'machine': mach
        })
        people_out[person['room_name']] = {
            'name': person['name'],
            'room': person['room'],
            'slot': sid_assigned,
            'slot_label': slot_label,
            'priority_awarded': p_awarded,
            'machine': mach,
        }
        assigned_rows.append((person['room_name'], person['name'], person['room'],
                               sid_assigned, mach, p_awarded))

    result['stats'] = stats
    result['slots'] = slots_out
    result['people'] = people_out
    if persist:
        _persist_assignment(assigned_rows, result)
    return result


def _persist_assignment(rows, result):
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("DELETE FROM assignments")
            c.executemany(
                "INSERT INTO assignments (room_name, name, room, slot_id, machine_num, priority_awarded, assigned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(rn, nm, rm, sid, mach, p, now) for (rn, nm, rm, sid, mach, p) in rows]
            )
            c.execute("DELETE FROM assignment_meta")
            c.execute(
                "INSERT INTO assignment_meta (id, computed_at, stats, total_people) VALUES (1, ?, ?, ?)",
                (result['computed_at'], json.dumps(result['stats']), result['total_people'])
            )
            conn.commit()
        finally:
            conn.close()
    sync_admin_csv()


def load_latest_assignment():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT computed_at, stats, total_people FROM assignment_meta WHERE id = 1")
        meta = c.fetchone()
        if not meta or not meta['computed_at']:
            return {'computed_at': None, 'total_people': 0, 'stats': {}, 'slots': {}, 'people': {}}

        computed_at = meta['computed_at']
        stats_json = meta['stats']
        total_people = meta['total_people']

        c.execute("SELECT room_name, name, room, slot_id, machine_num, priority_awarded, assigned_at FROM assignments ORDER BY slot_id, machine_num")
        rows = c.fetchall()
    finally:
        conn.close()

    slots_out = {}
    people_out = {}
    for row in rows:
        rn = row['room_name']
        nm = row['name']
        rm = row['room']
        sid = row['slot_id']
        mach = row['machine_num'] or 1
        p_awarded = row['priority_awarded']
        entry = {'name': nm, 'room': rm, 'priority_awarded': p_awarded}
        slots_out.setdefault(sid, []).append(entry)
        people_out[rn] = {
            'name': nm,
            'room': rm,
            'slot': sid,
            'slot_label': format_slot_label(sid),
            'priority_awarded': p_awarded,
            'assigned_at': row['assigned_at']
        }

    return {
        'computed_at': computed_at,
        'total_people': total_people,
        'stats': json.loads(stats_json) if stats_json else {},
        'slots': slots_out,
        'people': people_out,
    }


def generate_assignment_csv():
    assignment = load_latest_assignment()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Room", "Name", "Assigned Day", "Time Slot", "Priority Awarded", "Slot Code"])
    
    people_list = list(assignment.get('people', {}).values())
    people_list.sort(key=lambda x: (x.get('room', ''), x.get('name', '')))
    
    for p in people_list:
        slot = p.get('slot', '')
        parts = slot.split('-')
        day = parts[0] if len(parts) > 0 else ''
        ti = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
        time_str = f"{TIMES[ti][0]} - {TIMES[ti][1]}" if 0 <= ti < len(TIMES) else slot
        p_awarded = p.get('priority_awarded')
        p_text = f"Choice #{p_awarded}" if p_awarded is not None else "Fallback"
        writer.writerow([
            p.get('room', ''),
            p.get('name', ''),
            day,
            time_str,
            p_text,
            slot
        ])
    return output.getvalue()


def generate_admin_roster_csv():
    conn = get_db_connection()
    try:
        residents = conn.execute(
            "SELECT resident_id, room, name, phone, payment_status, payment_method, "
            "using_machine, source_note, imported_at FROM residents "
            "ORDER BY CAST(room AS INTEGER), resident_id"
        ).fetchall()
        users = conn.execute(
            "SELECT room_name, created_at, last_login FROM users"
        ).fetchall()
        picks = conn.execute(
            "SELECT room_name, data, updated_at FROM picks"
        ).fetchall()
        assignments = conn.execute(
            "SELECT room_name, slot_id, priority_awarded, assigned_at FROM assignments"
        ).fetchall()
    finally:
        conn.close()

    users_by_key = {row['room_name']: row for row in users}
    picks_by_key = {row['room_name']: row for row in picks}
    assignments_by_key = {row['room_name']: row for row in assignments}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Resident ID', 'Room', 'Name', 'Phone', 'Payment Status', 'Payment Method',
        'Using Machine', 'Source Note', 'Imported At', 'Account Registered',
        'PIN Status', 'Account Created At', 'Last Login', 'Chosen Slots',
        'Preferences Updated', 'Assigned Slot', 'Priority Awarded', 'Assigned At'
    ])

    for resident in residents:
        account_key = f"{sanitize_key(resident['room'])}__{sanitize_key(resident['name'])}"
        user = users_by_key.get(account_key)
        pick = picks_by_key.get(account_key)
        assignment = assignments_by_key.get(account_key)
        priorities = {}
        if pick:
            try:
                priorities = json.loads(pick['data']).get('priorities', {}) or {}
            except (TypeError, ValueError, AttributeError):
                priorities = {}
        chosen_slots = '; '.join(
            f"P{rank} {format_slot_label(slot)}"
            for slot, rank in sorted(priorities.items(), key=lambda item: int(item[1]))
        )
        writer.writerow([
            resident['resident_id'], resident['room'], resident['name'], resident['phone'] or '',
            resident['payment_status'] or '', resident['payment_method'] or '',
            'yes' if resident['using_machine'] else 'no', resident['source_note'] or '',
            resident['imported_at'], 'yes' if user else 'no',
            'set' if user else 'not set', user['created_at'] if user else '',
            user['last_login'] if user else '', chosen_slots, pick['updated_at'] if pick else '',
            format_slot_label(assignment['slot_id']) if assignment else '',
            assignment['priority_awarded'] if assignment else '',
            assignment['assigned_at'] if assignment else ''
        ])
    return output.getvalue()


def sync_admin_csv():
    csv_text = generate_admin_roster_csv()
    temp_path = ADMIN_CSV_PATH.with_name(f'.{ADMIN_CSV_PATH.name}.tmp')
    with _admin_csv_lock:
        temp_path.write_text(csv_text, encoding='utf-8', newline='')
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, ADMIN_CSV_PATH)


# ----------------------------------------------------------------------------
# HTTP Handlers
# ----------------------------------------------------------------------------
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _send_json(handler, obj, status=200):
    body = json.dumps(obj).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_csv(handler, csv_text, filename="e_block_laundry_assignments.csv"):
    body = csv_text.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/csv; charset=utf-8')
    handler.send_header('Content-Disposition', f'attachment; filename="{filename}"')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_pdf(handler, pdf_bytes, filename="e_block_laundry_allocations.pdf"):
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/pdf')
    handler.send_header('Content-Disposition', f'attachment; filename="{filename}"')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(pdf_bytes)))
    handler.end_headers()
    handler.wfile.write(pdf_bytes)


class LaundryHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        msg = fmt % args
        msg = msg.replace(ADMIN_KEY, '***')
        super().log_message('%s', msg)

    # ---------------- GET ----------------
    def do_GET(self):
        path = urlparse(self.path)
        query = parse_qs(path.query)

        if path.path == '/status':
            return self._handle_status()

        if path.path == '/counts':
            return self._handle_counts()

        if path.path == '/pick':
            return self._handle_get_pick(query)

        if path.path == '/preview':
            return self._handle_user_preview()

        if path.path == '/assign':
            token = get_auth_token_from_handler(self)
            conn = get_db_connection()
            try:
                resident_session = conn.execute(
                    "SELECT 1 FROM users WHERE session_token = ?", (token,)
                ).fetchone() if token else None
            finally:
                conn.close()
            if not resident_session:
                return _send_json(self, {"error": "Authentication required"}, status=401)
            return _send_json(self, load_latest_assignment())

        if path.path == '/myassignment':
            return self._handle_myassignment(query)

        if path.path in ('/export.pdf', '/admin/export.pdf'):
            return self._handle_export_pdf(query)

        if path.path in ('/export.csv', '/admin/export.csv'):
            key = (query.get('key', [''])[0] or '').strip()
            is_admin = bool(key and secrets.compare_digest(key, get_admin_key()))
            if not is_admin:
                token = get_auth_token_from_handler(self) or (query.get('token', [''])[0] or '').strip()
                conn = get_db_connection()
                try:
                    resident_session = conn.execute(
                        "SELECT 1 FROM users WHERE session_token = ?", (token,)
                    ).fetchone() if token else None
                finally:
                    conn.close()
                if not resident_session:
                    return _send_json(self, {"error": "Authentication required"}, status=401)
            return self._handle_export_csv(query)

        if path.path in ('/admin', '/ARYA'):
            self.path = '/admin.html'
            return super().do_GET()

        # Static file security whitelist
        req_path = path.path.strip('/')
        if req_path in ('', 'index.html', 'THE-DICTATOR'):
            self.path = '/index.html'
            return super().do_GET()
        elif req_path == 'admin.html':
            return super().do_GET()
        elif req_path == 'favicon.ico':
            if (APP_DIR / 'favicon.ico').exists():
                return super().do_GET()
            self.send_response(204)
            self.end_headers()
            return
        else:
            _send_json(self, {"error": "Not found"}, status=404)

    def _handle_status(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT count(*) as count FROM picks")
            total_submitted = c.fetchone()['count']
            c.execute("SELECT count(*) as count FROM residents")
            resident_count = c.fetchone()['count']
            c.execute("SELECT computed_at, stats, total_people FROM assignment_meta WHERE id = 1")
            meta = c.fetchone()
        finally:
            conn.close()

        has_assigned = bool(meta and meta['computed_at'])
        stats = json.loads(meta['stats']) if (meta and meta['stats']) else {}
        _send_json(self, {
            "lifecycle": "assigned" if has_assigned else "open",
            "computed_at": meta['computed_at'] if meta else None,
            "total_submitted": total_submitted,
            "total_assigned": meta['total_people'] if meta else 0,
            "resident_count": resident_count,
            "target_residents": resident_count,
            "stats": stats
        })

    def _handle_counts(self):
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT data FROM picks")
            rows = c.fetchall()
        finally:
            conn.close()

        counts = {}
        for row in rows:
            try:
                person = json.loads(row['data'])
            except Exception:
                continue
            prios = person.get("priorities", {}) or {}
            for sid, p in prios.items():
                if sid not in ALL_SLOTS_SET:
                    continue
                try:
                    p = int(p)
                except Exception:
                    continue
                if not (1 <= p <= NUM_PREFS):
                    continue
                bucket = counts.setdefault(sid, {"total": 0})
                key = str(p)
                bucket[key] = bucket.get(key, 0) + 1
                bucket["total"] += 1

        _send_json(self, counts)

    def _handle_get_pick(self, query):
        room = (query.get('room', [''])[0] or '').strip()
        name = (query.get('name', [''])[0] or '').strip()
        room_name = sanitize_key(room) + "__" + sanitize_key(name)

        if not is_authenticated_user(self, room_name):
            return _send_json(self, {"error": "Unauthorized. Please log in with your PIN."}, status=401)

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT data FROM picks WHERE room_name = ?", (room_name,))
            row = c.fetchone()
        finally:
            conn.close()

        payload = {"priorities": {}}
        if row is not None:
            try:
                payload = json.loads(row['data'])
            except Exception:
                payload = {"priorities": {}}

        _send_json(self, payload)

    def _handle_myassignment(self, query):
        room = (query.get('room', [''])[0] or '').strip()
        name = (query.get('name', [''])[0] or '').strip()
        room_name = sanitize_key(room) + "__" + sanitize_key(name)

        if not is_authenticated_user(self, room_name):
            return _send_json(self, {"error": "Unauthorized. Please log in with your PIN."}, status=401)

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT slot_id, machine_num, priority_awarded, assigned_at FROM assignments WHERE room_name = ?", (room_name,))
            row = c.fetchone()
        finally:
            conn.close()

        if not row:
            return _send_json(self, {"assigned": False})

        slot_id = row['slot_id']
        mach = row['machine_num'] or 1
        p_awarded = row['priority_awarded']
        assigned_at = row['assigned_at']
        
        parts = (slot_id or '').split('-')
        day = parts[0] if len(parts) > 0 else ''
        ti = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
        time_str = f"{TIMES[ti][0]}–{TIMES[ti][1]}" if 0 <= ti < len(TIMES) else slot_id

        _send_json(self, {
            "assigned": True,
            "slot": slot_id,
            "slot_label": format_slot_label(slot_id),
            "day": day,
            "time_range": time_str,
            "priority_awarded": p_awarded,
            "assigned_at": assigned_at,
        })

    def _handle_user_preview(self):
        token = get_auth_token_from_handler(self)
        conn = get_db_connection()
        try:
            resident_session = conn.execute(
                "SELECT 1 FROM users WHERE session_token = ?", (token,)
            ).fetchone() if token else None
        finally:
            conn.close()
        if not resident_session:
            return _send_json(self, {"error": "Authentication required"}, status=401)
        result = compute_assignment(persist=False)
        result['preview'] = True
        return _send_json(self, result)

    def _handle_export_csv(self, query):
        sync_admin_csv()
        csv_data = ADMIN_CSV_PATH.read_text(encoding='utf-8')
        _send_csv(self, csv_data)

    def _handle_export_pdf(self, query):
        key = (query.get('key', [''])[0] or '').strip()
        body = {}
        if not key and self.command == 'POST':
            try:
                body = self._read_json_body()
                key = str(body.get('key', '')).strip()
            except Exception:
                pass

        is_admin = bool(key and secrets.compare_digest(key, get_admin_key()))
        is_resident = False
        if not is_admin:
            token = get_auth_token_from_handler(self) or (query.get('token', [''])[0] or '').strip()
            if token:
                conn = get_db_connection()
                try:
                    is_resident = bool(conn.execute(
                        "SELECT 1 FROM users WHERE session_token = ?", (token,)
                    ).fetchone())
                finally:
                    conn.close()

        if not is_admin and not is_resident:
            return _send_json(self, {"error": "Authentication required"}, status=401)

        is_preview_req = (query.get('preview', ['0'])[0] in ('1', 'true', 'yes')) or bool(body.get('preview'))
        if is_preview_req:
            if not is_admin:
                return _send_json(self, {"error": "Admin authentication required for preview export"}, status=403)
            assignment_data = compute_assignment(persist=False)
            filename = "e_block_laundry_simulation_preview.pdf"
            is_preview = True
        else:
            assignment_data = load_latest_assignment()
            filename = "e_block_laundry_allocations.pdf"
            is_preview = False

        view = (query.get('view', ['all'])[0] or body.get('view') or 'all').strip().lower()
        if view not in ('all', 'roster', 'schedule'):
            view = 'all'

        try:
            pdf_bytes = generate_allocations_pdf(assignment_data, view=view, is_preview=is_preview)
            return _send_pdf(self, pdf_bytes, filename=filename)
        except Exception as e:
            return _send_json(self, {"error": f"Failed to generate PDF: {str(e)}"}, status=500)

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path)
        query = parse_qs(path.query)

        if path.path in ('/export.pdf', '/admin/export.pdf'):
            return self._handle_export_pdf(query)

        if path.path == '/preview':
            return self._handle_user_preview()

        if path.path == '/auth/status':
            return self._handle_auth_status()

        if path.path == '/auth/register':
            return self._handle_auth_register()

        if path.path == '/auth/login':
            return self._handle_auth_login()

        if path.path == '/save':
            return self._handle_save()

        if path.path == '/assign':
            return self._handle_run_assign(query)

        if path.path == '/admin/preview':
            return self._handle_preview_assign(query)

        if path.path == '/admin/reset':
            return self._handle_admin_reset(query)

        if path.path == '/admin/reset-pin':
            return self._handle_admin_reset_pin(query)

        if path.path == '/admin/roster':
            return self._handle_admin_roster()

        if path.path == '/admin/manage':
            return self._handle_admin_manage()

        _send_json(self, {"error": "Not found"}, status=404)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0 or length > 20000:
            raise ValueError("bad content length")
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8'))

    def _handle_auth_status(self):
        try:
            data = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)

        name = str(data.get('name', '')).strip()
        room = str(data.get('room', '')).strip()
        if not get_roster_resident(name, room):
            return _send_json(self, {"error": "Name and room must match a resident in the database"}, status=403)
        room_name = sanitize_key(room) + "__" + sanitize_key(name)
        if not room_name or '__' not in room_name:
            return _send_json(self, {"error": "Valid name and room are required"}, status=400)

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT room_name, pin_hash FROM users WHERE room_name = ?", (room_name,))
            row = c.fetchone()
            registered = bool(row and row['pin_hash'])
        finally:
            conn.close()

        _send_json(self, {
            "registered": registered,
            "room_name": room_name,
            "name": name,
            "room": room
        })

    def _handle_auth_register(self):
        try:
            data = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)

        name = str(data.get('name', '')).strip()
        room = str(data.get('room', '')).strip()
        pin = str(data.get('pin', '')).strip()

        if not name or not room:
            return _send_json(self, {"error": "Name and room are required"}, status=400)
        if len(pin) < 4 or len(pin) > 12:
            return _send_json(self, {"error": "PIN must be between 4 and 12 characters"}, status=400)
        if not get_roster_resident(name, room):
            return _send_json(self, {"error": "Name and room must match a resident in the database"}, status=403)

        room_key = sanitize_key(room)
        name_key = sanitize_key(name)
        if not room_key or not name_key:
            return _send_json(self, {"error": "Name and room must contain letters or numbers"}, status=400)
        room_name = room_key + "__" + name_key

        now = datetime.now(timezone.utc).isoformat()
        pin_hash_val, pin_salt_val = hash_pin(pin)
        session_token = secrets.token_urlsafe(24)

        with _db_lock:
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT room_name FROM users WHERE room_name = ?", (room_name,))
                if c.fetchone():
                    return _send_json(self, {"error": "Account already exists. Please log in with your PIN."}, status=400)

                c.execute(
                    "INSERT INTO users (room_name, name, room, pin_hash, pin_salt, session_token, created_at, last_login) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (room_name, name, room, pin_hash_val, pin_salt_val, session_token, now, now)
                )
                conn.commit()
            finally:
                conn.close()

            sync_admin_csv()
        _send_json(self, {
            "status": "success",
            "session_token": session_token,
            "name": name,
            "room": room
        })

    def _handle_auth_login(self):
        try:
            data = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)

        name = str(data.get('name', '')).strip()
        room = str(data.get('room', '')).strip()
        pin = str(data.get('pin', '')).strip()

        if not get_roster_resident(name, room):
            return _send_json(self, {"error": "Name and room must match a resident in the database"}, status=403)

        room_name = sanitize_key(room) + "__" + sanitize_key(name)
        if not room_name or not pin:
            return _send_json(self, {"error": "Name, room, and PIN are required"}, status=400)

        rate_keys = [
            ('resident-ip', self.client_address[0]),
            ('resident-account', room_name),
        ]
        retry_after = _rate_limit_retry_after(rate_keys)
        if retry_after:
            return _send_rate_limited(self, retry_after)

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT pin_hash, pin_salt FROM users WHERE room_name = ?", (room_name,))
            row = c.fetchone()
            if not row or not row['pin_hash']:
                return _send_json(self, {"error": "Resident not registered yet. Please set a PIN first."}, status=404)

            if not verify_pin(pin, row['pin_hash'], row['pin_salt']):
                _record_rate_limit_failure(rate_keys)
                return _send_json(self, {"error": "Incorrect security PIN. Please try again."}, status=401)

            session_token = secrets.token_urlsafe(24)
            now = datetime.now(timezone.utc).isoformat()
            with _db_lock:
                c.execute("UPDATE users SET session_token = ?, last_login = ? WHERE room_name = ?",
                          (session_token, now, room_name))
                conn.commit()
        finally:
            conn.close()

        _clear_rate_limit_failures(rate_keys)
        sync_admin_csv()
        _send_json(self, {
            "status": "success",
            "session_token": session_token,
            "name": name,
            "room": room
        })

    def _handle_save(self):
        try:
            data = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)

        name = str(data.get('name', '')).strip()
        room = str(data.get('room', '')).strip()
        priorities = data.get('priorities', {})

        if not name or not room:
            return _send_json(self, {"error": "Name and room are required"}, status=400)

        room_name = sanitize_key(room) + "__" + sanitize_key(name)
        if not is_authenticated_user(self, room_name):
            return _send_json(self, {"error": "Unauthorized session. Please log in with your PIN."}, status=401)

        if not isinstance(priorities, dict):
            return _send_json(self, {"error": "priorities must be an object"}, status=400)

        clean_priorities = {}
        seen_ranks = set()
        for sid, p in priorities.items():
            if sid not in ALL_SLOTS_SET:
                return _send_json(self, {"error": f"Unknown slot: {sid}"}, status=400)
            try:
                p = int(p)
            except Exception:
                return _send_json(self, {"error": f"Invalid priority for {sid}"}, status=400)
            if not (1 <= p <= NUM_PREFS):
                return _send_json(self, {"error": f"Priority must be 1-{NUM_PREFS}"}, status=400)
            if p in seen_ranks:
                return _send_json(self, {"error": f"Priority {p} used more than once"}, status=400)
            seen_ranks.add(p)
            clean_priorities[sid] = p

        payload = {
            "name": name, "room": room, "priorities": clean_priorities,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

        with _db_lock:
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute(
                    "REPLACE INTO picks (room_name, name, room, data, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (room_name, name, room, json.dumps(payload), payload["updatedAt"])
                )
                conn.commit()
            finally:
                conn.close()

            sync_admin_csv()
        _send_json(self, {"status": "success", "total_picks": len(clean_priorities)})

    def _handle_run_assign(self, query):
        key = (query.get('key', [''])[0] or '')
        if not key:
            try:
                body = self._read_json_body()
                key = body.get('key', '')
            except Exception:
                key = ''
        if not _verify_admin_key(self, key):
            return
        result = compute_assignment()
        _send_json(self, result)

    def _handle_preview_assign(self, query):
        key = (query.get('key', [''])[0] or '')
        if not key:
            try:
                body = self._read_json_body()
                key = body.get('key', '')
            except Exception:
                key = ''
        if not _verify_admin_key(self, key):
            return
        result = compute_assignment(persist=False)
        result['preview'] = True
        _send_json(self, result)

    def _handle_admin_reset(self, query):
        key = (query.get('key', [''])[0] or '')
        if not key:
            try:
                body = self._read_json_body()
                key = body.get('key', '')
            except Exception:
                key = ''
        if not _verify_admin_key(self, key):
            return

        with _db_lock:
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute("DELETE FROM assignments")
                c.execute("DELETE FROM assignment_meta")
                conn.commit()
            finally:
                conn.close()

        sync_admin_csv()
        _send_json(self, {"status": "success", "message": "Assignments cleared. Submissions reopened."})

    def _handle_admin_reset_pin(self, query):
        key = (query.get('key', [''])[0] or '')
        body = {}
        try:
            body = self._read_json_body()
            if not key:
                key = body.get('key', '')
        except Exception:
            pass

        if not _verify_admin_key(self, key):
            return

        target = (query.get('room_name', [''])[0] or body.get('room_name', '')).strip()
        if not target:
            return _send_json(self, {"error": "room_name parameter required"}, status=400)

        with _db_lock:
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT room_name, name, room FROM users")
                target_key = sanitize_key(target)
                matches = [
                    row['room_name'] for row in c.fetchall()
                    if row['room_name'] == target
                    or sanitize_key(row['name']) == target_key
                    or sanitize_key(row['room']) == target_key
                ]
                if not matches:
                    return _send_json(self, {"error": "No resident matched that room or name"}, status=404)
                c.executemany("DELETE FROM users WHERE room_name = ?", [(room_name,) for room_name in matches])
                conn.commit()
            finally:
                conn.close()

        sync_admin_csv()
        _send_json(self, {"status": "success", "message": f"PIN reset for {len(matches)} resident(s). Resident can set a new PIN on next login."})

    def _handle_admin_manage(self):
        try:
            data = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)

        key = str(data.get('key', '')).strip()
        if not _verify_admin_key(self, key):
            return

        action = str(data.get('action', '')).strip()
        if action == 'clear_preferences':
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute("DELETE FROM picks")
                    conn.execute("DELETE FROM assignments")
                    conn.execute("DELETE FROM assignment_meta")
                    conn.commit()
                    sync_admin_csv()
            return _send_json(self, {"status": "success", "message": "All preferences and published allocations cleared."})

        resident_id = str(data.get('resident_id', '')).strip()
        if action in {'delete_account', 'delete_resident'} and not resident_id:
            return _send_json(self, {"error": "resident_id is required"}, status=400)

        with _db_lock:
            with get_db_connection() as conn:
                resident = conn.execute("SELECT * FROM residents WHERE resident_id = ?", (resident_id,)).fetchone()
                if action in {'delete_account', 'delete_resident'} and not resident:
                    return _send_json(self, {"error": "Resident not found"}, status=404)

                if action == 'delete_account':
                    account_key = f"{sanitize_key(resident['room'])}__{sanitize_key(resident['name'])}"
                    conn.execute("DELETE FROM users WHERE room_name = ?", (account_key,))
                    conn.commit()
                    sync_admin_csv()
                    return _send_json(self, {"status": "success", "message": "Resident account deleted. Their preferences and roster record were kept."})

                if action == 'delete_resident':
                    account_key = f"{sanitize_key(resident['room'])}__{sanitize_key(resident['name'])}"
                    conn.execute("DELETE FROM users WHERE room_name = ?", (account_key,))
                    conn.execute("DELETE FROM picks WHERE room_name = ?", (account_key,))
                    conn.execute("DELETE FROM assignments WHERE room_name = ?", (account_key,))
                    conn.execute("DELETE FROM residents WHERE resident_id = ?", (resident_id,))
                    conn.commit()
                    sync_admin_csv()
                    return _send_json(self, {"status": "success", "message": "Resident and linked account data deleted."})

                if action not in {'add', 'update'}:
                    return _send_json(self, {"error": "Unknown admin action"}, status=400)

                name = str(data.get('name', '')).strip()
                room = str(data.get('room', '')).strip()
                if not name or not room:
                    return _send_json(self, {"error": "Name and room are required"}, status=400)
                name_key = sanitize_key(name)
                room_key = sanitize_key(room)
                if not name_key or not room_key:
                    return _send_json(self, {"error": "Name and room must contain letters or numbers"}, status=400)

                duplicate = conn.execute(
                    "SELECT resident_id FROM residents WHERE lower(name) = lower(?) AND lower(room) = lower(?) AND resident_id != ?",
                    (name, room, resident_id if action == 'update' else '')
                ).fetchone()
                if duplicate:
                    return _send_json(self, {"error": "A resident with that name and room already exists"}, status=409)

                payment_status = str(data.get('payment_status', 'pending')).strip() or 'pending'
                payment_method = str(data.get('payment_method', '')).strip()
                phone = str(data.get('phone', '')).strip()
                source_note = str(data.get('source_note', '')).strip()
                using_machine = 1 if data.get('using_machine', True) in (True, 1, '1', 'true', 'on') else 0
                now = datetime.now(timezone.utc).isoformat()

                if action == 'add':
                    resident_id = secrets.token_hex(8)
                    conn.execute(
                        "INSERT INTO residents (resident_id, room, name, phone, payment_status, payment_method, using_machine, source_note, imported_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (resident_id, room, name, phone, payment_status, payment_method, using_machine, source_note, now)
                    )
                    conn.commit()
                    sync_admin_csv()
                    return _send_json(self, {"status": "success", "message": "Resident added."})

                if not resident:
                    return _send_json(self, {"error": "Resident not found"}, status=404)
                old_key = f"{sanitize_key(resident['room'])}__{sanitize_key(resident['name'])}"
                new_key = f"{room_key}__{name_key}"
                if old_key != new_key:
                    linked = conn.execute(
                        "SELECT 1 FROM users WHERE room_name = ? UNION SELECT 1 FROM picks WHERE room_name = ? UNION SELECT 1 FROM assignments WHERE room_name = ?",
                        (new_key, new_key, new_key)
                    ).fetchone()
                    if linked:
                        return _send_json(self, {"error": "The new name and room already have linked account data"}, status=409)
                    conn.execute("UPDATE users SET room_name = ?, name = ?, room = ? WHERE room_name = ?", (new_key, name, room, old_key))
                    conn.execute("UPDATE picks SET room_name = ?, name = ?, room = ? WHERE room_name = ?", (new_key, name, room, old_key))
                    conn.execute("UPDATE assignments SET room_name = ?, name = ?, room = ? WHERE room_name = ?", (new_key, name, room, old_key))
                conn.execute(
                    "UPDATE residents SET room = ?, name = ?, phone = ?, payment_status = ?, payment_method = ?, using_machine = ?, source_note = ?, imported_at = ? WHERE resident_id = ?",
                    (room, name, phone, payment_status, payment_method, using_machine, source_note, now, resident_id)
                )
                conn.commit()
                sync_admin_csv()
                return _send_json(self, {"status": "success", "message": "Resident details updated."})

    def _handle_admin_roster(self):
        try:
            body = self._read_json_body()
        except Exception:
            return _send_json(self, {"error": "Invalid request body"}, status=400)
        key = str(body.get('key', '')).strip()
        if not _verify_admin_key(self, key):
            return

        conn = get_db_connection()
        try:
            residents = conn.execute(
                "SELECT resident_id, room, name, phone, payment_status, payment_method, "
                "using_machine, source_note FROM residents ORDER BY CAST(room AS INTEGER), resident_id"
            ).fetchall()
            users = conn.execute("SELECT room_name, pin_hash, created_at, last_login FROM users").fetchall()
            picks = conn.execute("SELECT room_name, data, updated_at FROM picks").fetchall()
            assignments = conn.execute(
                "SELECT room_name, slot_id, machine_num, priority_awarded, assigned_at FROM assignments"
            ).fetchall()
        finally:
            conn.close()

        users_by_key = {row['room_name']: row for row in users}
        picks_by_key = {row['room_name']: row for row in picks}
        assignments_by_key = {row['room_name']: row for row in assignments}
        output = []
        for resident in residents:
            account_key = f"{sanitize_key(resident['room'])}__{sanitize_key(resident['name'])}"
            user = users_by_key.get(account_key)
            pick = picks_by_key.get(account_key)
            assignment = assignments_by_key.get(account_key)
            priorities = {}
            if pick:
                try:
                    priorities = json.loads(pick['data']).get('priorities', {}) or {}
                except (TypeError, ValueError, AttributeError):
                    priorities = {}
            output.append({
                'resident_id': resident['resident_id'],
                'room': resident['room'],
                'name': resident['name'],
                'phone': resident['phone'] or '',
                'payment_status': resident['payment_status'],
                'payment_method': resident['payment_method'] or '',
                'using_machine': bool(resident['using_machine']),
                'source_note': resident['source_note'] or '',
                'account_registered': bool(user),
                'pin_status': 'set' if user and user['pin_hash'] else 'not set',
                'account_created_at': user['created_at'] if user else None,
                'last_login': user['last_login'] if user else None,
                'priorities': priorities,
                'picks_updated_at': pick['updated_at'] if pick else None,
                'assignment': {
                    'slot': assignment['slot_id'],
                    'slot_label': format_slot_label(assignment['slot_id']),
                    'priority_awarded': assignment['priority_awarded'],
                    'assigned_at': assignment['assigned_at'],
                } if assignment else None,
            })
        _send_json(self, {'residents': output, 'imported_from': ROSTER_CSV_PATH.name})


    sync_admin_csv()


if __name__ == '__main__':
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', '8000'))
    except ValueError:
        raise SystemExit('Port must be an integer, for example: python3 server.py 8000')
    if not (1 <= port <= 65535):
        raise SystemExit('Port must be between 1 and 65535')

    bind_host = os.environ.get('HOST', '127.0.0.1')
    server_address = (bind_host, port)
    httpd = ThreadingHTTPServer(server_address, LaundryHandler)

    print("\n" + "=" * 64)
    print("  E BLOCK LAUNDRY SERVER")
    print("=" * 64)
    print(f"  Resident Portal:  http://{bind_host}:{port}/")
    print(f"  Admin Portal:     http://{bind_host}:{port}/admin?key={ADMIN_KEY}")
    print(f"  Admin Secret Key: {ADMIN_KEY} (saved in {ADMIN_KEY_PATH.name})")
    print("-" * 64)
    print("  HTTPS Proxy:      Configure Caddyfile for production / LAN access")
    print("=" * 64 + "\n")

    httpd.serve_forever()