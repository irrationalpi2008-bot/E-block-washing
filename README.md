# E Block Laundry Allocation System

An automated, preference-based laundry slot scheduling and allocation platform designed to fairly distribute shared washing machine access across hostel residents.

This system was originally designed and built for the residents of E Block at the Indian Institute of Science (IISc), Bangalore, to eliminate weekly laundry contention, manual sign-up sheets, and slot collisions across more than 160 residents sharing communal washing machines.

---

## Background and Motivation

In university hostels and residential communities, shared laundry facilities are a common bottleneck. Peak hours (evenings and weekends) face severe contention, while daytime and late-night slots remain underutilized. Traditional first-come-first-serve systems or manual paper sign-up sheets suffer from:

- Rushes and race conditions when sign-up opens.
- Slot hogging and double-booking.
- Conflicts between residents with overlapping class or laboratory schedules.
- No fair fallback mechanism when high-demand slots are oversubscribed.

The E Block Laundry system solves this with a ranked-choice preference model solved by a Min-Cost Max-Flow (MCMF) bipartite network flow algorithm. Residents submit their top preferred timeslots, and the algorithm computes a globally fair, capacity-constrained allocation that maximizes resident satisfaction.

---

## Highlights and Architecture

- Zero Third-Party Python Dependencies: Built entirely with the Python 3 standard library (`http.server`, `sqlite3`, `hashlib`, `secrets`, `socket`, `csv`). No `pip install` or external packages are required.
- Zero Third-Party Frontend Frameworks: Responsive, clean interface constructed with semantic HTML5, CSS custom properties, and vanilla JavaScript.
- Built-in PDF Generation (`pdf_export.py`): A standalone, zero-dependency PDF canvas engine that compiles A4 landscape weekly allocation timetables directly from raw PDF primitives for printing and noticeboard posting.
- Concurrent SQLite in WAL Mode: SQLite engine configured with Write-Ahead Logging (WAL) and busy timeout handlers to support concurrent multi-device reads and writes.
- Cryptographic Authentication: Resident PINs are hashed using PBKDF2-HMAC-SHA256 with 100,000 iterations and unique 16-byte cryptographic salts. Admin operations are guarded by URL-safe 128-bit secret keys with constant-time verification.
- Brute-Force Rate Limiting: Sliding-window brute-force defense protects authentication endpoints by room and IP address.
- HTTPS Reverse-Proxy Ready: Pre-configured Caddyfile provides local or production HTTPS with HTTP/2, Zstandard/Gzip compression, and hardened security headers (HSTS, CSP, Frame Options, Sniff protection).

---

## Core Features

### Resident Portal (`/`)
- Account Onboarding: Residents locate their room and name from the official roster, setting a private 4-digit PIN on initial login.
- Ranked Preference Submission: Select up to 7 preferred 90-minute slots across the week (Monday through Sunday, 16 slots per day).
- Live Collision and Popularity Insights: Real-time counters indicate how many fellow residents have selected each slot.
- Allocation Timetable: Once allocations are published by administrators, residents view their assigned slot, assigned machine number (1, 2, or 3), awarded priority rank, and the complete building-wide schedule.

### Admin Console (`/admin`)
- Secure Access: Protected via administrative key (`admin_key.txt` or query parameter `?key=...`).
- Live Roster Inspection: Search and filter residents by room number, name, payment status, registration state, and assigned slot.
- Resident Management: Add new residents, update room assignments, record payment status (paid, pending, not paying), and toggle machine usage flags.
- PIN and Account Management: Reset individual forgotten PINs or delete resident login accounts while retaining roster records.
- Batch Reset Controls: Reset resident preferences between allocation cycles with single-click confirmation.
- Allocation Simulation and Publishing: Preview allocation metrics (satisfaction counts per priority tier) before publishing final results.
- Multi-Format Exports: Download building-wide assignments as standard CSV or print-ready A4 landscape PDF.

---

## Allocation Algorithm (Min-Cost Max-Flow)

The weekly schedule divides 7 days into 16 cycles of 90 minutes each (24 hours a day), yielding 112 timeslots. With 3 washing machines available per timeslot, the system provides 336 machine-slots per week.

The allocation engine runs in two phases:

1. Uncontested First-Choice Reservation:
   Any resident who selects a Priority 1 slot that has fewer than or equal to 3 total claimants is immediately granted that slot. This guarantees uncontested top choices without unnecessary graph overhead.

2. Capacitated Minimum-Cost Maximum-Flow:
   For all remaining residents and contested slots, a directed flow network is constructed:
   - Source node connected to each resident with capacity 1 and cost 0.
   - Resident node connected to their chosen slots with capacity 1 and quadratic cost $C(p) = p^2$, where $p \in [1, 7]$ is their priority rank. Quadratic penalization heavily disincentivizes assigning lower-ranked preferences when higher ones can be satisfied.
   - Resident node connected to all unchosen fallback slots with capacity 1 and a penalty cost of 100.
   - Each timeslot node connected to the Sink with capacity equal to remaining machine availability ($3 - \text{reserved}$) and cost 0.
   - The graph is solved using the Shortest Path Faster Algorithm (SPFA) cycle-canceling/augmenting path method.

This ensures that every participating resident is allocated exactly one machine slot while maximizing overall preference satisfaction across the hostel.

---

## Project Structure

```text
.
|-- Caddyfile             # Caddy reverse proxy configuration (HTTPS & security headers)
|-- LICENSE               # MIT License
|-- README.md             # Project documentation
|-- admin.html            # Administrator web console
|-- index.html            # Resident booking and allocation portal
|-- pdf_export.py         # Zero-dependency raw PDF generator for A4 landscape timetable
|-- roster.example.csv    # Example roster format for initial resident data seeding
`-- server.py             # HTTP server, SQLite database layer, MCMF allocation engine
```

---

## Getting Started

### Prerequisites
- Python 3.8 or higher.
- Caddy (optional, recommended for HTTPS and local network deployment).

No third-party Python packages are required.

### 1. Clone the Repository
```bash
git clone https://github.com/irrationalpi2008-bot/E-block-washing.git
cd E-block-washing
```

### 2. Prepare Resident Roster (Optional)
To pre-populate the resident list from a spreadsheet, create a file named `roster.csv` in the project root based on `roster.example.csv`:

```bash
cp roster.example.csv roster.csv
```

The CSV schema expects three columns:
- `Rooms`: Room number (integer).
- `Person A`: Resident details in the format `"Status- Full Name, Phone, PaymentMethod"` (e.g., `"Paid- Alex Johnson, 9876543210, UPI"`).
- `Person B`: Roommate details or `"NO ROOMMATE"`.

Residents can also be added individually through the Admin Console at any time.

### 3. Start the Server
Run the application backend:

```bash
python3 server.py
```

By default, the server binds to `127.0.0.1:8000`. On first launch, the server automatically:
- Initializes the SQLite database (`laundry.db`).
- Generates a secure random 128-bit admin secret key and saves it to `admin_key.txt`.
- Imports resident records if `roster.csv` is present.
- Displays the resident and admin URLs in the terminal.

To specify a custom port or bind to all network interfaces:

```bash
PORT=8080 HOST=0.0.0.0 python3 server.py
```

### 4. Enable HTTPS with Caddy (Recommended)
Running behind Caddy allows residents to access the portal over local Wi-Fi or a local area network (LAN) with HTTPS encryption:

```bash
# Start Caddy with default configuration (listens on :8443 with internal TLS)
caddy run --config Caddyfile
```

To configure a specific LAN IP address or domain name:

```bash
SITE_ADDRESS="192.168.1.100:8443" caddy run --config Caddyfile
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port for the Python backend HTTP server. |
| `HOST` | `127.0.0.1` | Network interface to bind the Python server. |
| `ROSTER_CSV` | `roster.csv` | Path to the roster CSV file for initial data import. |
| `LAUNDRY_DB` | `laundry.db` | Path to the SQLite database file. |
| `SITE_ADDRESS` | `:8443` | Caddy reverse-proxy listen address or domain name. |

---

## Administrative Operations

### Accessing the Admin Console
Navigate to:
```text
http://localhost:8000/admin?key=<YOUR_ADMIN_KEY>
```
Replace `<YOUR_ADMIN_KEY>` with the token found inside `admin_key.txt`.

### Clearing Test Data for a New Allocation Cycle
To reset preferences and assignments while preserving all permanent resident profiles and accounts:

1. In the Admin Console, click `[ CLEAR ALL PREFERENCES ]`.
2. Alternatively, via command line (stop server first):
   ```bash
   sqlite3 laundry.db "DELETE FROM picks; DELETE FROM assignments; DELETE FROM assignment_meta;"
   rm -f laundry.db-wal laundry.db-shm
   ```

### Resetting All Accounts and Data
To return the application to an uninitialized state:

```bash
rm -f laundry.db* admin_key.txt admin_roster.csv
```

Restarting `server.py` will re-initialize a fresh database and regenerate credentials.

---

## Security Considerations

- Secure Credentials: Keep `admin_key.txt` and `laundry.db` strictly private. They are excluded from version control via `.gitignore`.
- Rate Limiting: Authentication endpoints enforce a 5-minute backoff after 5 consecutive failed login attempts.
- Secret Masking: Admin secret tokens appearing in URL paths or query strings are automatically masked in server log output.
- Security Headers: The bundled Caddyfile includes `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and restrictive `Permissions-Policy` headers.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

