"""
wsgi.py — Entry point for Railway (and other hosting platforms).

Railway runs: python wsgi.py
The PORT environment variable is set automatically by Railway.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import database as db
db.init_db()   # Safe no-op if tables already exist

from dashboard.app import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
