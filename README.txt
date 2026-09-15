E Block Laundry - Quick Reference
==================================

For complete documentation, architecture, and setup instructions, see README.md.

STARTING THE APPLICATION
------------------------

1. Start Python backend:
   python3 server.py

2. Start Caddy reverse proxy (for HTTPS on LAN/Wi-Fi):
   caddy run --config Caddyfile

Local URLs:
- Resident Portal: http://localhost:8000/ or https://localhost:8443/
- Admin Portal:    http://localhost:8000/admin or https://localhost:8443/admin (key required)


NETWORK / LAN ACCESS
--------------------

To check your local Wi-Fi / LAN IP address:
   ip -4 -o addr show scope global

To run Caddy with your local IP address:
   SITE_ADDRESS="<YOUR_IP>:8443" caddy run --config Caddyfile


RESETTING PREFERENCES BETWEEN ALLOCATION CYCLES
-----------------------------------------------

Stop Python and Caddy, then run:
   sqlite3 laundry.db "DELETE FROM picks; DELETE FROM assignments; DELETE FROM assignment_meta;"
   rm -f laundry.db-wal laundry.db-shm

To reset user accounts as well:
   sqlite3 laundry.db "DELETE FROM users; DELETE FROM picks; DELETE FROM assignments; DELETE FROM assignment_meta;"
   rm -f laundry.db-wal laundry.db-shm


SECURITY NOTES
--------------

- Keep admin_key.txt and laundry.db private. Do not commit them to version control.
- Caddy's local certificate may show a self-signed certificate warning on first visit.
  Choose the browser option to proceed or trust Caddy's root certificate.
