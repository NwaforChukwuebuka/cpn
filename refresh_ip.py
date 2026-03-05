"""
Standalone script to refresh (rotate) IP for an AdsPower profile.
Delegates to modules.adspower_refresh.

Usage:
  python refresh_ip.py --profile k17oolu5
  python refresh_ip.py -p k17oolu5
  python refresh_ip.py --profile k17oolu5 --headless
"""
from modules.adspower_refresh import main

if __name__ == "__main__":
    exit(main())
