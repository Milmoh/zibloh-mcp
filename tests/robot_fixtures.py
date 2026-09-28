"""A minimal robot that follows the contract (final JSON line + sys.exit(1) on error)."""

GOOD_MAIN = '''\
import json, sys

def main():
    print(json.dumps({"status": "success", "log_message": "ok", "processed_count": 0}), flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "error", "log_message": str(exc)}), flush=True)
        sys.exit(1)
'''
