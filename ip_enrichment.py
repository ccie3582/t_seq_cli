import os
import sys
import json
import pymysql

import configparser

# Configuration
config = configparser.ConfigParser(interpolation=None)
config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'viplab_ip.ini')
if os.path.exists(config_file):
    config.read(config_file)

# Database configuration
DB_HOST = os.getenv("VIPLAB_DB_HOST", config.get('Database', 'host', fallback='127.0.0.1'))
DB_USER = os.getenv("VIPLAB_DB_USER", config.get('Database', 'user', fallback='root'))
DB_PASS = os.getenv("VIPLAB_DB_PASS", config.get('Database', 'pass', fallback='Dicembre2021%'))
DB_NAME = os.getenv("VIPLAB_DB_NAME", config.get('Database', 'name', fallback='viplab'))

def connect():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

def main():
    import argparse
    parser = argparse.ArgumentParser(description="IP enrichment viewer")
    parser.add_argument("keyword", choices=["before", "after"])
    parser.add_argument("ip", help="IP address to look up")
    parser.add_argument("--subnet-id", help="Filter to a specific SUBNET_ID")
    args = parser.parse_args()

    keyword = args.keyword
    search_ip = args.ip
    subnet_id = args.subnet_id

    conn = connect()
    try:
        with conn.cursor() as cursor:
            if keyword == "before":
                if subnet_id:
                    cursor.execute("SELECT * FROM `all_addresses` WHERE SHORT_IP_ADDRESS = %s AND SUBNET_ID = %s", (search_ip, subnet_id))
                else:
                    cursor.execute("SELECT * FROM `all_addresses` WHERE SHORT_IP_ADDRESS = %s", (search_ip,))
                results = cursor.fetchall()
                if results:
                    for i, row in enumerate(results, 1):
                        if len(results) > 1:
                            print(f"=== Record #{i} ===")
                        print(json.dumps(row, indent=4, default=str))
                        if i < len(results):
                            print()
                else:
                    print(json.dumps({"error": f"IP {search_ip} not found in all_addresses"}, indent=4))

            elif keyword == "after":
                # Get the augmented records
                if subnet_id:
                    cursor.execute("SELECT * FROM `all_addresses_data_augmented` WHERE SHORT_IP_ADDRESS = %s AND SUBNET_ID = %s", (search_ip, subnet_id))
                else:
                    cursor.execute("SELECT * FROM `all_addresses_data_augmented` WHERE SHORT_IP_ADDRESS = %s", (search_ip,))
                after_records = cursor.fetchall()

                if not after_records:
                    print(json.dumps({"error": f"IP {search_ip} not found in all_addresses_data_augmented"}, indent=4))
                    return

                # Get all original records for comparison
                if subnet_id:
                    cursor.execute("SELECT * FROM `all_addresses` WHERE SHORT_IP_ADDRESS = %s AND SUBNET_ID = %s", (search_ip, subnet_id))
                else:
                    cursor.execute("SELECT * FROM `all_addresses` WHERE SHORT_IP_ADDRESS = %s", (search_ip,))
                before_records = cursor.fetchall()

                for idx, after_rec in enumerate(after_records, 1):
                    # Find matching before record (by ID if possible, otherwise first)
                    before_rec = {}
                    if before_records:
                        rec_id = after_rec.get("ID")
                        for br in before_records:
                            if br.get("ID") and br["ID"] == rec_id:
                                before_rec = br
                                break
                        if not before_rec:
                            before_rec = before_records[0] if idx <= len(before_records) else before_records[-1]

                    # ANSI escape codes for colors
                    GREEN = "\033[32m"
                    RESET = "\033[0m"

                    # Identify which fields are new or changed
                    changed_keys = set()
                    for key, value in after_rec.items():
                        val_after = str(value) if value is not None else ""
                        if key not in before_rec:
                            changed_keys.add(key)
                        else:
                            val_before = str(before_rec.get(key)) if before_rec.get(key) is not None else ""
                            if val_after != val_before:
                                changed_keys.add(key)

                    # Generate formatted JSON lines
                    json_str = json.dumps(after_rec, indent=4, default=str)
                    lines = json_str.splitlines()

                    # Process lines and apply color to those containing changed keys
                    colored_lines = []
                    for line in lines:
                        highlighted = False
                        for key in changed_keys:
                            key_prefix = f'    "{key}":'
                            if line.startswith(key_prefix):
                                colored_lines.append(f"{GREEN}{line}{RESET}")
                                highlighted = True
                                break
                        if not highlighted:
                            colored_lines.append(line)

                    if len(after_records) > 1:
                        print(f"=== Record #{idx} ===")
                    print("\n".join(colored_lines))
                    if idx < len(after_records):
                        print()
            else:
                print(f"Error: Unknown keyword '{keyword}'. Use 'before' or 'after'.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
