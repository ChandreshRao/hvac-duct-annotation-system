#!/bin/sh
set -e

# Seeding the database from hardcoded references if requested
echo "Seeding database from bootstrap_db.py..."
python3 bootstrap_db.py

# Execute the CMD passed from Docker
exec "$@"
