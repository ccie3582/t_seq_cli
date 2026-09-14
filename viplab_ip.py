import os
import sys
import configparser
import subprocess
import argparse
import cmd
import shlex
import time
import signal
import ipaddress
import json

try:
    import pyreadline3 as readline
except ImportError:
    try:
        import readline
    except ImportError:
        readline = None

HAVE_READLINE = readline is not None and hasattr(readline, "parse_and_bind")

# Enable ANSI colors in Windows cmd.exe
if os.name == 'nt':
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

class _StripANSI:
    """Wraps a file-like object and strips ANSI escape sequences on write."""
    _ansi_re = None

    def __init__(self, stream):
        self._stream = stream
        if _StripANSI._ansi_re is None:
            import re
            _StripANSI._ansi_re = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def write(self, text):
        self._stream.write(self._ansi_re.sub('', text).replace('\r\n', '\n').replace('\r', '\n'))
        self._stream.flush()

    def flush(self):
        self._stream.flush()

CONFIG_FILE = "viplab_ip.ini"

def is_ip(text):
    """Checks if a string is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(text.strip())
        return True
    except:
        return False

def get_config():
    config = configparser.ConfigParser(interpolation=None)
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    for section in ['Database', 'EasyIP']:
        if section not in config.sections():
            config.add_section(section)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)

def guided_setup_database(config):
    print("\n--- Database Settings ---")
    print("Press Enter to keep current value in [brackets].\n")
    host = input(f"  Host [{config.get('Database', 'host', fallback='127.0.0.1')}]: ") or config.get('Database', 'host', fallback='127.0.0.1')
    user = input(f"  User [{config.get('Database', 'user', fallback='root')}]: ") or config.get('Database', 'user', fallback='root')
    password = input(f"  Password (current: {'***' if config.get('Database', 'pass', fallback='') else 'None'}): ") or config.get('Database', 'pass', fallback='')
    db_name = input(f"  Database [{config.get('Database', 'name', fallback='viplab')}]: ") or config.get('Database', 'name', fallback='viplab')
    config.set('Database', 'host', host)
    config.set('Database', 'user', user)
    config.set('Database', 'pass', password)
    config.set('Database', 'name', db_name)
    save_config(config)
    print(f"  Saved to {CONFIG_FILE}")

def guided_setup_easyip(config):
    if not config.has_section('EasyIP'):
        config.add_section('EasyIP')
    print("\n--- EasyIP API Settings ---")
    print("Press Enter to keep current value in [brackets].\n")
    url = input(f"  URL [{config.get('EasyIP', 'url', fallback='http://10.2.168.229:8080/SOAP')}]: ") or config.get('EasyIP', 'url', fallback='http://10.2.168.229:8080/SOAP')
    user = input(f"  User [{config.get('EasyIP', 'user', fallback='SECURITY-INTEGRATION')}]: ") or config.get('EasyIP', 'user', fallback='SECURITY-INTEGRATION')
    password = input(f"  Password (current: {'***' if config.get('EasyIP', 'pass', fallback='') else 'None'}): ") or config.get('EasyIP', 'pass', fallback='')
    config.set('EasyIP', 'url', url)
    config.set('EasyIP', 'user', user)
    config.set('EasyIP', 'pass', password)
    save_config(config)
    print(f"  Saved to {CONFIG_FILE}")

def guided_setup_netbrain(config):
    if not config.has_section('NetBrain'):
        config.add_section('NetBrain')
    print("\n--- NetBrain API Settings ---")
    print("Press Enter to keep current value in [brackets].\n")
    url = input(f"  URL [{config.get('NetBrain', 'url', fallback='https://10.2.163.60')}]: ") or config.get('NetBrain', 'url', fallback='https://10.2.163.60')
    user = input(f"  User [{config.get('NetBrain', 'user', fallback='roberto.piersante@hcltech.com')}]: ") or config.get('NetBrain', 'user', fallback='roberto.piersante@hcltech.com')
    password = input(f"  Password (current: {'***' if config.get('NetBrain', 'pass', fallback='') else 'None'}): ") or config.get('NetBrain', 'pass', fallback='')
    config.set('NetBrain', 'url', url)
    config.set('NetBrain', 'user', user)
    config.set('NetBrain', 'pass', password)
    save_config(config)
    print(f"  Saved to {CONFIG_FILE}")

def guided_setup_vcenter(config):
    if not config.has_section('vCenter'):
        config.add_section('vCenter')
    default_endpoints = "172.29.166.135,172.29.166.132,172.29.107.4,172.29.107.5,172.29.201.39,172.29.201.36,172.31.3.68,172.31.3.69,172.29.131.4,172.29.131.5,172.29.191.132,172.29.191.133"
    print("\n--- vCenter API Settings ---")
    print("Press Enter to keep current value in [brackets].\n")
    user = input(f"  User [{config.get('vCenter', 'user', fallback='LabModRead')}]: ") or config.get('vCenter', 'user', fallback='LabModRead')
    password = input(f"  Password (current: {'***' if config.get('vCenter', 'pass', fallback='') else 'None'}): ") or config.get('vCenter', 'pass', fallback='')
    current_endpoints = config.get('vCenter', 'endpoints', fallback=default_endpoints)
    print(f"  Endpoints [{current_endpoints}]:")
    endpoints = input(f"  (comma-separated IPs): ") or current_endpoints
    config.set('vCenter', 'user', user)
    config.set('vCenter', 'pass', password)
    config.set('vCenter', 'endpoints', endpoints)
    save_config(config)
    print(f"  Saved to {CONFIG_FILE}")

def guided_setup_tcmt(config):
    if not config.has_section('TCMT'):
        config.add_section('TCMT')
    print("\n--- TCMT API Settings ---")
    print("Press Enter to keep current value in [brackets].\n")
    url = input(f"  URL [{config.get('TCMT', 'url', fallback='http://10.2.181.251:8008')}]: ") or config.get('TCMT', 'url', fallback='http://10.2.181.251:8008')
    user = input(f"  User [{config.get('TCMT', 'user', fallback='tcmtAPIlabmod')}]: ") or config.get('TCMT', 'user', fallback='tcmtAPIlabmod')
    password = input(f"  Password (current: {'***' if config.get('TCMT', 'pass', fallback='') else 'None'}): ") or config.get('TCMT', 'pass', fallback='')
    config.set('TCMT', 'url', url)
    config.set('TCMT', 'user', user)
    config.set('TCMT', 'pass', password)
    save_config(config)
    print(f"  Saved to {CONFIG_FILE}")

def show_scheduled_task(task_name):
    import platform
    import subprocess
    if platform.system() == 'Windows':
        try:
            result = subprocess.run(
                ['schtasks', '/Query', '/TN', task_name, '/FO', 'LIST', '/V'],
                capture_output=True, text=True)
            if result.returncode == 0:
                print(f"\n  Scheduled task '{task_name}':")
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith(('Task Name:', 'Status:', 'Next Run Time:', 'Schedule Type:', 'Start Time:', 'Days:', 'Months:', 'Repeat: Every:')):
                        if line.startswith('Repeat: Every:') and 'Disabled' in line:
                            continue
                        print(f"    {line}")
            else:
                print(f"\n  No scheduled task '{task_name}' found.")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        try:
            result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
            if result.returncode == 0:
                found = False
                for line in result.stdout.splitlines():
                    if task_name in line:
                        print(f"\n  Cron job '{task_name}':")
                        print(f"    {line}")
                        found = True
                if not found:
                    print(f"\n  No cron job '{task_name}' found.")
            else:
                print(f"\n  No crontab configured.")
        except Exception as e:
            print(f"  Error: {e}")


def cancel_scheduled_task(config, task_name):
    import platform
    import subprocess
    if platform.system() == 'Windows':
        try:
            result = subprocess.run(
                ['schtasks', '/Delete', '/TN', task_name, '/F'],
                capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  Task '{task_name}' deleted from Task Scheduler.")
            else:
                print(f"  No task '{task_name}' found (or already removed).")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        try:
            result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
            if result.returncode == 0:
                lines = [l for l in result.stdout.splitlines() if task_name not in l]
                new_crontab = '\n'.join(lines) + '\n' if lines else ''
                proc = subprocess.run(['crontab', '-'], input=new_crontab, capture_output=True, text=True)
                if proc.returncode == 0:
                    print(f"  Cron job '{task_name}' removed.")
                else:
                    print(f"  Error updating crontab: {proc.stderr.strip()}")
            else:
                print(f"  No crontab configured.")
        except Exception as e:
            print(f"  Error: {e}")

    section = 'Schedule'
    if config.has_section(section):
        config.remove_option(section, f'{task_name}_time')
        config.remove_option(section, f'{task_name}_interval')
        save_config(config)


def configure_scheduled_task(config, command, task_name, description):
    import platform
    import subprocess
    from datetime import datetime, timedelta

    print(f"\n--- Schedule: {description} ---")
    print(f"  Current system time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    show_scheduled_task(task_name)

    print("\n  Actions: [s]et schedule, [c]ancel schedule, [q]uit")
    choice = input("  Choice: ").strip().lower()
    if choice == 'c':
        cancel_scheduled_task(config, task_name)
        return
    elif choice != 's':
        return

    section = 'Schedule'
    if not config.has_section(section):
        config.add_section(section)

    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    current_date = config.get(section, f'{task_name}_date', fallback=tomorrow)
    current_time = config.get(section, f'{task_name}_time', fallback='02:00')
    current_interval = config.get(section, f'{task_name}_interval', fallback='168')

    print("\n  Press Enter to keep current value in [brackets].")
    print("  Time must be in 24-hour format.\n")

    first_date = input(f"  First run date (YYYY-MM-DD) [{current_date}]: ") or current_date
    start_time = input(f"  Start time (HH:MM, 24h) [{current_time}]: ") or current_time
    interval_h = input(f"  Repeat interval in hours [{current_interval}]: ") or current_interval

    try:
        first_dt = datetime.strptime(f"{first_date} {start_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        print("  Error: Invalid date/time format.")
        return
    try:
        h, m = start_time.split(':')
        int(h); int(m)
    except (ValueError, AttributeError):
        print("  Error: Invalid time format. Use HH:MM (24h).")
        return
    try:
        interval_int = int(interval_h)
    except ValueError:
        print("  Error: Interval must be a number of hours.")
        return

    config.set(section, f'{task_name}_date', first_date)
    config.set(section, f'{task_name}_time', start_time)
    config.set(section, f'{task_name}_interval', interval_h)
    save_config(config)

    script_path = os.path.abspath(__file__)
    python_path = sys.executable
    working_dir = os.path.dirname(script_path)

    if platform.system() == 'Windows':
        if interval_int < 24:
            sc_type, sc_mod = 'HOURLY', str(interval_int)
        elif interval_int < 168:
            sc_type, sc_mod = 'DAILY', str(max(1, interval_int // 24))
        elif interval_int % 168 == 0:
            sc_type, sc_mod = 'WEEKLY', str(interval_int // 168)
        else:
            sc_type, sc_mod = 'DAILY', str(max(1, interval_int // 24))
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International")
            win_fmt = winreg.QueryValueEx(key, "sShortDate")[0]
            winreg.CloseKey(key)
            py_fmt = win_fmt.replace('yyyy', '%Y').replace('yy', '%y').replace('MM', '%m').replace('M', '%m').replace('dd', '%d').replace('d', '%d')
            sd = first_dt.strftime(py_fmt)
        except Exception:
            sd = first_dt.strftime('%m/%d/%Y')
        scht_cmd = [
            'schtasks', '/Create', '/F',
            '/TN', task_name,
            '/TR', f'"{python_path}" "{script_path}" {command}',
            '/SC', sc_type,
            '/MO', sc_mod,
            '/ST', start_time,
            '/SD', sd,
        ]
        try:
            result = subprocess.run(scht_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  Windows Task Scheduler: task '{task_name}' created.")
                print(f"    First run: {first_date} {start_time}")
                print(f"    Repeat: {sc_type} every {sc_mod}")
            else:
                print(f"  Error creating scheduled task: {result.stderr.strip()}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        if interval_int < 24:
            hours = []
            start_h = int(h)
            cur = start_h
            while True:
                hours.append(cur % 24)
                cur += interval_int
                if cur % 24 == start_h and cur >= 24:
                    break
                if cur >= start_h + 24:
                    break
            hours_str = ','.join(str(x) for x in sorted(hours))
            cron_schedule = f'{m} {hours_str} * * *'
            schedule_desc = f"every {interval_int}h at :{m}"
        elif interval_int == 24:
            cron_schedule = f'{m} {h} * * *'
            schedule_desc = f"daily at {start_time}"
        elif interval_int % 168 == 0:
            dow = first_dt.strftime('%u')  # 1=Monday ... 7=Sunday
            dow_name = first_dt.strftime('%A')
            cron_schedule = f'{m} {h} * * {dow}'
            weeks = interval_int // 168
            if weeks == 1:
                schedule_desc = f"weekly on {dow_name} at {start_time}"
            else:
                schedule_desc = f"every {weeks} week(s) on {dow_name} at {start_time}"
        elif interval_int >= 672:  # ~28 days, use day-of-month
            dom = first_dt.day
            cron_schedule = f'{m} {h} {dom} * *'
            schedule_desc = f"monthly on day {dom} at {start_time}"
        else:
            dow = first_dt.strftime('%u')
            dow_name = first_dt.strftime('%A')
            cron_schedule = f'{m} {h} * * {dow}'
            schedule_desc = f"weekly on {dow_name} at {start_time} (closest fit for {interval_int}h)"

        cron_cmd = f'{cron_schedule} cd "{working_dir}" && "{python_path}" "{script_path}" {command}'
        try:
            result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
            existing = result.stdout if result.returncode == 0 else ''
            lines = [l for l in existing.splitlines() if task_name not in l]
            lines.append(f'{cron_cmd} # {task_name}')
            new_crontab = '\n'.join(lines) + '\n'
            proc = subprocess.run(['crontab', '-'], input=new_crontab, capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"  Cron job installed: task '{task_name}'")
                print(f"    First run: {first_date} {start_time}")
                print(f"    Schedule: {schedule_desc}")
                print(f"    Cron expression: {cron_schedule}")
            else:
                print(f"  Error setting crontab: {proc.stderr.strip()}")

            # Schedule first run via 'at' only if cron won't fire today
            now = datetime.now()
            cron_fires_today = False
            if interval_int < 24:
                cron_fires_today = True
            elif interval_int == 24:
                cron_fires_today = True
            elif first_dt.date() == now.date():
                cron_fires_today = (first_dt.strftime('%u') == now.strftime('%u'))

            if not cron_fires_today and first_dt > now:
                try:
                    at_cmd = f'cd "{working_dir}" && "{python_path}" "{script_path}" {command}\n'
                    at_time = first_dt.strftime('%H:%M %Y-%m-%d')
                    at_proc = subprocess.run(
                        ['at', at_time], input=at_cmd,
                        capture_output=True, text=True)
                    if at_proc.returncode == 0:
                        print(f"    First run scheduled via 'at' for {first_date} {start_time}")
                    else:
                        at_time_alt = f'{start_time} {first_date}'
                        at_proc2 = subprocess.run(
                            ['at', at_time_alt], input=at_cmd,
                            capture_output=True, text=True)
                        if at_proc2.returncode == 0:
                            print(f"    First run scheduled via 'at' for {first_date} {start_time}")
                except FileNotFoundError:
                    pass
        except Exception as e:
            print(f"  Error: {e}")

    print(f"  Settings saved to {CONFIG_FILE}")


def get_env(config):
    env = os.environ.copy()
    env["VIPLAB_DB_HOST"] = config.get('Database', 'host', fallback='127.0.0.1')
    env["VIPLAB_DB_USER"] = config.get('Database', 'user', fallback='root')
    env["VIPLAB_DB_PASS"] = config.get('Database', 'pass', fallback='')
    env["VIPLAB_DB_NAME"] = config.get('Database', 'name', fallback='viplab')

    if config.has_section('EasyIP'):
        env["EASYIP_URL"] = config.get('EasyIP', 'url', fallback='')
        env["EASYIP_USERNAME"] = config.get('EasyIP', 'user', fallback='')
        env["EASYIP_PASSWORD"] = config.get('EasyIP', 'pass', fallback='')

    if config.has_section('NetBrain'):
        env["NB_URL"] = config.get('NetBrain', 'url', fallback='')
        env["NB_USERNAME"] = config.get('NetBrain', 'user', fallback='')
        env["NB_PASSWORD"] = config.get('NetBrain', 'pass', fallback='')

    if config.has_section('vCenter'):
        env["VCENTER_USER"] = config.get('vCenter', 'user', fallback='')
        env["VCENTER_PASSWORD"] = config.get('vCenter', 'pass', fallback='')
        env["VCENTER_ENDPOINTS"] = config.get('vCenter', 'endpoints', fallback='')

    if config.has_section('TCMT'):
        env["TCMT_URL"] = config.get('TCMT', 'url', fallback='')
        env["TCMT_USERNAME"] = config.get('TCMT', 'user', fallback='')
        env["TCMT_PASSWORD"] = config.get('TCMT', 'pass', fallback='')

    env["PYTHONUNBUFFERED"] = "1"
    return env

def run_script(script_name, args, config):
    cmd_list = [sys.executable, script_name] + args
    try:
        # If stdout is redirected, pipe through sys.stdout (ANSI stripping active)
        stdout_target = subprocess.PIPE if hasattr(sys.stdout, '_stream') else None
        proc = subprocess.Popen(cmd_list, stdout=stdout_target, stderr=subprocess.STDOUT,
                                text=True, env=get_env(config))
        if stdout_target:
            for line in iter(proc.stdout.readline, ''):
                sys.stdout.write(line)
            proc.stdout.close()
        proc.wait()
        if proc.returncode != 0:
            print(f"(exit code: {proc.returncode})")
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
    except Exception as e:
        print(f"Error executing {script_name}: {e}")

def initialize_readline():
    """Initializes readline with tab completion and custom delimiters."""
    if not HAVE_READLINE:
        return
    try:
        if 'libedit' in (readline.__doc__ or ''):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        
        # Remove hyphen from delimiters so hyphenated commands complete as one word
        delims = readline.get_completer_delims()
        if '-' in delims:
            readline.set_completer_delims(delims.replace('-', ''))
    except:
        pass

class WindowsCompletingCmd(cmd.Cmd):
    def cmdloop(self, intro=None):
        if os.name == "nt" and not HAVE_READLINE:
            self._windows_cmdloop(intro)
            return
        super().cmdloop(intro)

    def _windows_cmdloop(self, intro=None):
        if intro is not None: self.intro = intro
        if self.intro: print(self.intro, end="" if self.intro.endswith("\n") else "\n")
        self.preloop()
        stop = None
        while not stop:
            try:
                line = self._windows_readline()
                line = self.precmd(line)
                stop = self.onecmd(line)
                stop = self.postcmd(stop, line)
            except KeyboardInterrupt: print("^C")
            except EOFError:
                print()
                stop = self.onecmd("EOF")
        self.postloop()

    def _windows_readline(self):
        import msvcrt
        buffer = []
        cursor = 0
        history = getattr(self, "_windows_history", [])
        history_index = len(history)
        last_rendered_len = 0
        def redraw():
            nonlocal last_rendered_len
            text = "".join(buffer)
            rendered_len = len(self.prompt) + len(text)
            clear_len = max(last_rendered_len, rendered_len)
            print("\r" + (" " * clear_len), end="", flush=True)
            print("\r" + self.prompt + text, end="", flush=True)
            last_rendered_len = rendered_len
            back = len(text) - cursor
            if back > 0: print("\b" * back, end="", flush=True)
        print(self.prompt, end="", flush=True)
        last_rendered_len = len(self.prompt)
        while True:
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                print()
                line = "".join(buffer)
                if line:
                    history.append(line)
                    self._windows_history = history[-100:]
                return line
            if char == "\x03": raise KeyboardInterrupt
            if char in ("\x04", "\x1a"): raise EOFError
            if char == "\t":
                cursor = self._complete_windows_buffer(buffer, cursor)
                redraw()
                continue
            if char == "\b":
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if char in ("\x00", "\xe0"):
                key = msvcrt.getwch()
                if key == "K" and cursor > 0: cursor -= 1; redraw()
                elif key == "M" and cursor < len(buffer): cursor += 1; redraw()
                elif key == "H" and history:
                    history_index = max(0, history_index - 1)
                    buffer[:] = list(history[history_index])
                    cursor = len(buffer)
                    redraw()
                elif key == "P" and history:
                    history_index = min(len(history), history_index + 1)
                    buffer[:] = list(history[history_index]) if history_index < len(history) else []
                    cursor = len(buffer)
                    redraw()
                elif key == "S" and cursor < len(buffer):
                    del buffer[cursor]
                    redraw()
                continue
            if char >= " ":
                buffer.insert(cursor, char)
                cursor += 1
                redraw()

    def _complete_windows_buffer(self, buffer, cursor):
        line = "".join(buffer)
        start = line.rfind(" ", 0, cursor) + 1
        word = line[start:cursor]
        matches = self.completenames(word) if start == 0 else self.completedefault(word, line, start, cursor)
        if not matches: return cursor
        if len(matches) == 1:
            replacement = matches[0]
            if start == 0: replacement += " "
            buffer[start:cursor] = list(replacement)
            return start + len(replacement)
        common = os.path.commonprefix(matches)
        if common and common != word:
            buffer[start:cursor] = list(common)
            return start + len(common)
        print("\n  " + "  ".join(matches))
        return cursor


class VIPLabBaseCmd(WindowsCompletingCmd):
    """Shared base for all VIPLab shell menus providing job management."""
    def __init__(self, parent_shell=None):
        super().__init__()
        self.parent_shell = parent_shell

    @staticmethod
    def _read_key():
        """Read a single keypress without requiring Enter (cross-platform)."""
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch == '\r':
                ch = '\n'
            return ch
        except ImportError:
            import tty, termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == '\r':
                    ch = '\n'
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def get_bg_jobs(self):
        return self.bg_jobs if hasattr(self, 'bg_jobs') else self.parent_shell.bg_jobs

    def do_status(self, arg):
        'Check background processes status'
        bg_jobs = self.get_bg_jobs()
        if not bg_jobs:
            print("No background jobs tracked.")
            return
        
        print("\nBackground Jobs Status:")
        print(f"{'Job Name':<20} | {'Status':<12} | {'PID':<8}")
        print("-" * 45)
        for name, job in bg_jobs.items():
            poll = job["proc"].poll()
            if poll is None:
                status = "RUNNING"
                pid = str(job["proc"].pid)
            elif poll == 0:
                status = "SUCCESS"
                pid = "-"
            else:
                status = f"FAILED ({poll})"
                pid = "-"
            print(f"{name:<20} | {status:<12} | {pid:<8}")
        print()

    def do_log(self, arg):
        'show the console output of a background job (usage: log <job_name>)'
        bg_jobs = self.get_bg_jobs()
        if not arg:
            print("Usage: log <job_name>")
            # Show all potential job names for clarity
            possible = ["simulator", "easyip", "netbrain", "vcenter", "tcmt"]
            print(f"Possible jobs: {', '.join(possible)}")
            if bg_jobs:
                tracked = list(bg_jobs.keys())
                print(f"Tracked jobs: {', '.join(tracked)}")
            return
        
        log_path = f"{arg}.log"
        if not os.path.exists(log_path):
            print(f"No log found for '{arg}'.")
            return

        # Determine status for header
        status_line = "Status: UNKNOWN"
        if arg in bg_jobs:
            poll = bg_jobs[arg]["proc"].poll()
            if poll is None:
                status_line = f"Status: RUNNING (PID: {bg_jobs[arg]['proc'].pid})"
            elif poll == 0:
                status_line = "Status: SUCCESS (Completed)"
            else:
                status_line = f"Status: FAILED (Exit Code: {poll})"

        print(f"\n--- {log_path} | {status_line} ---")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                print(f.read())
        except Exception as e:
            print(f"Error reading log: {e}")
        print("-----------\n")

    def complete_log(self, text, line, begidx, endidx):
        bg_jobs = self.get_bg_jobs()
        # Merge built-in possible names and currently tracked ones
        candidates = list(set(["simulator", "easyip", "netbrain", "vcenter", "tcmt"] + list(bg_jobs.keys())))
        return [n for n in candidates if n.startswith(text)]

    def do_stop(self, arg):
        'stop a running background job (usage: stop <job_name>)'
        bg_jobs = self.get_bg_jobs()
        if not arg:
            print("Usage: stop <job_name>")
            running = [n for n, j in bg_jobs.items() if j["proc"].poll() is None]
            if running:
                print(f"Running jobs: {', '.join(running)}")
            return
        
        if arg in bg_jobs:
            job = bg_jobs[arg]
            if job["proc"].poll() is None:
                print(f"Stopping {arg}...")
                job["proc"].terminate()
                try: job["proc"].wait(timeout=2)
                except: pass
                print(f"Job '{arg}' stopped.")
            else:
                print(f"Job '{arg}' is already stopped.")
            if job.get("file"):
                try: job["file"].close()
                except: pass
        else:
            print(f"No job named '{arg}' found.")

    def complete_stop(self, text, line, begidx, endidx):
        bg_jobs = self.get_bg_jobs()
        return [n for n, job in bg_jobs.items() if job["proc"].poll() is None and n.startswith(text)]

    def completenames(self, text, *ignored):
        dotags = [a[3:] for a in self.get_names() if a.startswith('do_')]
        names = [t.replace('_', '-') for t in dotags if t not in ['EOF']]
        return [a for a in names if a.startswith(text)]

    def completedefault(self, text, line, begidx, endidx):
        # Route to complete_<cmd> for argument completion
        parts = line.strip().split()
        if parts:
            cmd = parts[0].replace('-', '_')
            func = getattr(self, 'complete_' + cmd, None)
            if func:
                return func(text, line, begidx, endidx)
        return []

    def onecmd(self, line):
        import re, sys, io

        # Check for pipe: "| more" or "| grep <pattern>"
        pipe_match = re.search(r'\|\s*(more|grep)\b\s*(.*)?$', line)
        if pipe_match:
            pipe_cmd = pipe_match.group(1)
            pipe_arg = (pipe_match.group(2) or '').strip()
            line = line[:pipe_match.start()].rstrip()
            # Capture output
            old_stdout = sys.stdout
            buf = io.StringIO()
            sys.stdout = buf
            try:
                result = self._pipe_inner(line)
            finally:
                sys.stdout = old_stdout
            output = buf.getvalue()
            if pipe_cmd == 'grep':
                if not pipe_arg:
                    print("Usage: <command> | grep <pattern>")
                    return result
                case_insensitive = False
                if pipe_arg.startswith('-i '):
                    case_insensitive = True
                    pipe_arg = pipe_arg[3:].strip()
                try:
                    pat = re.compile(pipe_arg, re.IGNORECASE if case_insensitive else 0)
                except re.error as e:
                    print(f"Invalid pattern: {e}")
                    return result
                for ln in output.splitlines():
                    if pat.search(ln):
                        print(ln)
            elif pipe_cmd == 'more':
                import shutil
                term_h = shutil.get_terminal_size((80, 24)).lines - 1
                lines = output.splitlines()
                lines_shown = 0
                for i, ln in enumerate(lines):
                    print(ln)
                    lines_shown += 1
                    if lines_shown >= term_h and i + 1 < len(lines):
                        try:
                            sys.stdout.write("-- More -- (Space=next page, Enter=next line, q=quit) ")
                            sys.stdout.flush()
                            ch = self._read_key()
                            sys.stdout.write("\r" + " " * 55 + "\r")
                            sys.stdout.flush()
                            if ch == 'q':
                                break
                            elif ch == ' ':
                                lines_shown = 0
                            else:
                                lines_shown = term_h - 1
                        except (EOFError, KeyboardInterrupt):
                            print()
                            break
            return result

        # Check for output redirection: ">> filename" (overwrite) or "> filename" (append)
        redir_match = re.search(r'(?:^|\s)(>>|>)\s+(\S+)$', line)
        if redir_match:
            mode = 'w' if redir_match.group(1) == '>>' else 'a'
            filename = redir_match.group(2)
            line = line[:redir_match.start()]
            line = line.replace('/>>', '>>').replace('/>', '>')
            try:
                raw_file = open(filename, mode, encoding='utf-8')
            except OSError as e:
                print(f"Error: Cannot write to '{filename}' — {e}")
                return
            old_stdout = sys.stdout
            try:
                sys.stdout = _StripANSI(raw_file)
                result = self._execute_cmd(line)
            finally:
                sys.stdout.close()
                sys.stdout = old_stdout
            print(f"Output saved to {filename}")
            return result
        line = line.replace('/>>', '>>').replace('/>', '>')
        return self._execute_cmd(line)

    def _pipe_inner(self, line):
        """Run a command line through redirection/execute logic (for pipe capture)."""
        import re
        redir_match = re.search(r'(?:^|\s)(>>|>)\s+(\S+)$', line)
        if redir_match:
            line = line[:redir_match.start()]
        line = line.replace('/>>', '>>').replace('/>', '>')
        return self._execute_cmd(line)

    def _execute_cmd(self, line):
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"Error parsing command: {e}")
            return
        if not parts: return self.emptyline()

        # Check if the command is an IP (special context logic for main shell)
        if hasattr(self, 'current_ip') and is_ip(parts[0]):
            return self.default(line)

        command = parts[0].replace('-', '_')
        # Preserve original argument text to keep SQL quoting intact
        raw_args = line[len(parts[0]):].strip()
        return super().onecmd(command + " " + raw_args)

    def do_query(self, arg):
        'execute a raw SQL query against the VIPLab database'
        if not arg:
            print("Usage: query <SQL>")
            return
        import pymysql
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4"
            )
            with conn.cursor() as cursor:
                cursor.execute(arg)
                results = cursor.fetchall()
                if not results:
                    print("Success. 0 rows returned.")
                    conn.close()
                    return
                # Format as table
                cols = [desc[0] for desc in cursor.description]
                # Sanitize values (remove embedded newlines/carriage returns)
                def _sv(v):
                    return str(v).replace(chr(10), ' ').replace(chr(13), ' ') if v is not None else ''
                # Calculate column widths (header vs data)
                col_widths = [len(c) for c in cols]
                for row in results:
                    for i, val in enumerate(row):
                        w = len(_sv(val))
                        if w > col_widths[i]:
                            col_widths[i] = w
                # Build separator line
                sep = "+".join("-" * (w + 2) for w in col_widths)
                # Print header
                print(f"+{sep}+")
                header = " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(cols))
                print(f"| {header} |")
                print(f"+{sep}+")
                # Print rows
                for row in results:
                    vals = [_sv(v) for v in row]
                    line = " | ".join(v.ljust(col_widths[i]) for i, v in enumerate(vals))
                    print(f"| {line} |")
                print(f"+{sep}+")
                print(f"({len(results)} rows)")
            conn.close()
        except Exception as e: print(f"DB Error: {e}")

    def do_cmd(self, arg):
        'execute an OS command (e.g. cmd ls, cmd dir)'
        if not arg:
            print("Usage: cmd <command>")
            return
        import subprocess
        try:
            result = subprocess.run(arg, shell=True, capture_output=True, text=True)
            if result.stdout: print(result.stdout)
            if result.stderr: print(result.stderr)
            if result.returncode != 0:
                print(f"(exit code: {result.returncode})")
        except Exception as e:
            print(f"Error: {e}")


class APICollectShell(VIPLabBaseCmd):
    def __init__(self, parent):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.prompt = f"{parent.base_prompt}:api-collect> "
        initialize_readline()

    def do_easyip(self, arg):
        'Update EasyIP data (subnets then addresses) in background'
        self.parent_shell.run_background_job("easyip", ["easyip_subnets.py", "easyip_addresses.py"], detached=True)

    def do_netbrain(self, arg):
        'Update NetBrain data in background'
        scripts = [
            "scan_netbrain_interfaces.py",
            "search_netbrain_devices.py",
            "search_netbrain_neighbors.py",
            "get_Global_Endpoint_Table.py",
            "get_OneIPTable.py",
            "netbrain_devices_config.py",
        ]
        self.parent_shell.run_background_job("netbrain", scripts, detached=True)

    def do_vcenter(self, arg):
        'Update vCenter data in background'
        self.parent_shell.run_background_job("vcenter", ["scan_vcenter_API_v0.1.py"], detached=True)

    def do_tcmt(self, arg):
        'Collect tcmt data (Background)'
        self.parent_shell.run_background_job("tcmt", ["get_asset_inventory_2.py"], detached=True)

    def do_help(self, arg):
        print("\nAPI Collect Commands:")
        print(f"  {'easyip':<20} - Collect EasyIP data (Background)")
        print(f"  {'netbrain':<20} - Collect NetBrain data (Background)")
        print(f"  {'vcenter':<20} - Collect vCenter data (Background)")
        print(f"  {'tcmt':<20} - Collect tcmt data (Background)")
        print(f"  {'status':<20} - Check background jobs")
        print(f"  {'log <job>':<20} - View job logs")
        print(f"  {'stop <job>':<20} - Stop a background job")
        print(f"  {'exit':<20} - Return to main menu")

    def do_exit(self, arg):
        'Return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self): pass


class ConfigShell(VIPLabBaseCmd):
    def __init__(self, parent):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.prompt = f"{parent.base_prompt}:config> "
        initialize_readline()

    def do_database(self, arg):
        'Configure database connection'
        guided_setup_database(self.config)

    def do_easyip(self, arg):
        'Configure EasyIP API endpoint and credentials'
        guided_setup_easyip(self.config)

    def do_netbrain(self, arg):
        'Configure NetBrain API endpoint and credentials'
        guided_setup_netbrain(self.config)

    def do_vcenter(self, arg):
        'Configure vCenter API credentials'
        guided_setup_vcenter(self.config)

    def do_tcmt(self, arg):
        'Configure TCMT API endpoint and credentials'
        guided_setup_tcmt(self.config)

    def do_frequency(self, arg):
        'Configure API collection times and frequency'
        configure_scheduled_task(self.config, "api-collect", "VIPLab_API_Collect",
                                "Run API collection (easyip, netbrain, vcenter, tcmt)")

    def do_update_frequency(self, arg):
        'Configure IPAM update times and frequency'
        configure_scheduled_task(self.config, "write-batch report_easyip_best_batch_clean.csv",
                                "VIPLab_IPAM_Update",
                                "Write batch update to EasyIP server (report_easyip_best_batch_clean.csv)")

    def do_help(self, arg):
        print("\nConfig Commands:")
        print(f"  {'database':<20} - Configure database connection")
        print(f"  {'easyip':<20} - Configure EasyIP API endpoint and credentials")
        print(f"  {'netbrain':<20} - Configure NetBrain API endpoint and credentials")
        print(f"  {'vcenter':<20} - Configure vCenter API credentials")
        print(f"  {'tcmt':<20} - Configure TCMT API endpoint and credentials")
        print(f"  {'frequency':<20} - Configure API collection times and frequency")
        print(f"  {'update-frequency':<20} - Configure IPAM update times and frequency")
        print(f"  {'exit':<20} - Return to main menu")

    def do_exit(self, arg):
        'Return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self): pass



class VmNameShell(VIPLabBaseCmd):
    """Sub-shell for VM name/UUID queries."""
    def __init__(self, parent, vm_identifier):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.vm_identifier = vm_identifier
        self.prompt = f"{parent.base_prompt}:vm-name:{vm_identifier}> "
        initialize_readline()

    def do_info(self, arg):
        'Show matching VM record (use "extensive" to include NIC details)'
        import pymysql
        parts = (arg or "").split()
        if "?" in parts:
            print("Usage: info [extensive]")
            print("  (no option) - show VM record from all_vms")
            print("  extensive   - also list all NIC details from all_vm_nics")
            return
        extensive = "extensive" in parts
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM all_vms WHERE name = %s OR uuid = %s",
                               (self.vm_identifier, self.vm_identifier))
                rows = cursor.fetchall()
                if rows:
                    print(f"\n--- all_vms ({len(rows)} match) ---")
                    for i, row in enumerate(rows, 1):
                        if len(rows) > 1:
                            print(f"  --- #{i} ---")
                        for k, v in row.items():
                            if k == "ip_address":
                                print(f"  {k:<25}: {(str(v).strip() if v else '')}")
                            elif v is not None and str(v).strip():
                                print(f"  {k:<25}: {str(v).strip()}")
                        if extensive:
                            vcenter_ip = (row.get("vcenter_ip") or "").strip()
                            vm_id = (row.get("vm_id") or "").strip()
                            if vcenter_ip and vm_id:
                                cursor.execute("SELECT * FROM all_vm_nics WHERE vcenter_ip = %s AND vm_id = %s",
                                               (vcenter_ip, vm_id))
                                nic_rows = cursor.fetchall()
                                if nic_rows:
                                    skip_fields = {"vcenter_ip", "vm_id", "uuid", "bios_uuid", "instance_uuid", "name", "host_id", "host_name"}
                                    print(f"\n  --- NICs ({len(nic_rows)}) ---")
                                    for n, nic in enumerate(nic_rows, 1):
                                        print(f"    --- NIC #{n} ---")
                                        for k, v in nic.items():
                                            if k in skip_fields:
                                                continue
                                            if v is not None and str(v).strip():
                                                print(f"      {k:<25}: {str(v).strip()}")
                                else:
                                    print("\n  --- NICs: none found ---")
                else:
                    print(f"\n  No VM found matching '{self.vm_identifier}' (name or uuid).")
            conn.close()
            print()
        except Exception as e:
            print(f"Error: {e}")

    def complete_info(self, text, line, begidx, endidx):
        completions = ["extensive"]
        return [c for c in completions if c.startswith(text)]

    def default(self, line):
        new_id = line.strip()
        if ' ' in new_id:
            print("Error: VM name/UUID cannot contain spaces.")
            return
        self.vm_identifier = new_id
        self.prompt = f"{self.prompt.rsplit(':vm-name:', 1)[0]}:vm-name:{new_id}> "
        print(f"VM context set to: {self.vm_identifier}")

    def do_exit(self, arg):
        'return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def do_help(self, arg):
        print("\nVM Name Commands:")
        print(f"  {'info':<18} - show matching VM record from all_vms")
        print(f"  {'info extensive':<18} - also list all NIC details from all_vm_nics")
        print(f"  {'<name|uuid>':<18} - change VM context")
        print(f"  {'exit':<18} - return to main menu")
        print(f"\n  Current VM: {self.vm_identifier}")
        print()

    def emptyline(self): pass


class NbDeviceShell(VIPLabBaseCmd):
    """Sub-shell for NetBrain device queries."""
    def __init__(self, parent, hostname):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.hostname = hostname
        self.prompt = f"{parent.base_prompt}:nb-device:{hostname}> "
        initialize_readline()

    def _execute_cmd(self, line):
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"Error parsing command: {e}")
            return
        if not parts: return self.emptyline()
        if parts[0] == '?':
            return self.do_help('')
        command = parts[0].replace('-', '_')
        if hasattr(self, 'do_' + command):
            raw_args = line[len(parts[0]):].strip()
            return super(VIPLabBaseCmd, self).onecmd(command + " " + raw_args)
        return self.default(line)

    def do_info(self, arg):
        'show device info and interfaces (use "brief" for device info only)'
        brief = arg.strip().lower() == "brief" if arg else False
        import pymysql
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM netbrain_devices WHERE device_id = %s", (self.hostname,))
                device = cursor.fetchone()
                if device:
                    print(f"\n--- Device: {self.hostname} ---")
                    for key, value in device.items():
                        if value is not None and str(value).strip():
                            sv = str(value).replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
                            print(f"  {key:<25}: {sv}")
                else:
                    print(f"\nNo device found with hostname '{self.hostname}' in netbrain_devices.")

                if not brief:
                    cursor.execute("SELECT * FROM netbrain_interfaces WHERE device_name = %s", (self.hostname,))
                    interfaces = cursor.fetchall()
                    if interfaces:
                        print(f"\n--- Interfaces ({len(interfaces)} found) ---")
                        for i, intf in enumerate(interfaces, 1):
                            print(f"  --- Interface #{i} ---")
                            for key, value in intf.items():
                                if value is not None and str(value).strip():
                                    sv = str(value).replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
                                    print(f"    {key:<25}: {sv}")
                    else:
                        print(f"\nNo interfaces found for device '{self.hostname}' in netbrain_interfaces.")
            conn.close()
            print()
        except Exception as e:
            print(f"Error: {e}")

    def complete_info(self, text, line, begidx, endidx):
        completions = ["brief"]
        return [c for c in completions if c.startswith(text)]

    def do_configuration(self, arg):
        'display device configuration file from netbrain_configs'
        configs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netbrain_configs")
        cfg_path = os.path.join(configs_dir, f"{self.hostname}.cfg")
        if not os.path.isfile(cfg_path):
            print(f"No configuration file found for '{self.hostname}' in netbrain_configs/")
            return
        print(f"\n--- Configuration: {self.hostname} ---")
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                print(f.read())
        except OSError as e:
            print(f"Error reading file: {e}")

    def do_route_table(self, arg):
        'query NetBrain API for device routing table (ipv6|vpnv4|vpnv4-import|vpnv4-export|fortigate|multicast)'
        af = arg.strip().lower() if arg else ""
        if af == "?":
            print("Usage: route-table [address-family]")
            print("  (no option)     - IPv4 routing table (with VRF selection)")
            print("  ipv6            - IPv6 Route Table")
            print("  vpnv4           - MPLS VPNv4 Label table")
            print("  vpnv4-import    - BGP VPNv4 Imported Routes")
            print("  vpnv4-export    - BGP All VPNv4 Advertised Route Table")
            print("  fortigate       - Fortigate Route Table")
            print("  multicast       - Multicast Route Table")
            return
        args = [self.hostname]
        if af in ("ipv6", "vpnv4", "vpnv4-import", "vpnv4-export", "fortigate", "multicast"):
            args.insert(0, af)
        run_script("netbrain_route_table.py", args, self.config)

    def complete_route_table(self, text, line, begidx, endidx):
        completions = ["ipv6", "vpnv4", "vpnv4-import", "vpnv4-export", "fortigate", "multicast"]
        return [c for c in completions if c.startswith(text)]

    def _run_topology_query(self, sql, params):
        import pymysql
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                results = cursor.fetchall()
                if not results:
                    print("  No results found.")
                    conn.close()
                    return
                cols = [desc[0] for desc in cursor.description]
                col_widths = [len(c) for c in cols]
                for row in results:
                    for i, val in enumerate(row):
                        w = len(str(val) if val is not None else '')
                        if w > col_widths[i]:
                            col_widths[i] = w
                sep = "+".join("-" * (w + 2) for w in col_widths)
                print(f"+{sep}+")
                print("|" + "|".join(f" {c:<{col_widths[i]}} " for i, c in enumerate(cols)) + "|")
                print(f"+{sep}+")
                for row in results:
                    print("|" + "|".join(f" {(str(v) if v is not None else ''):<{col_widths[i]}} " for i, v in enumerate(row)) + "|")
                print(f"+{sep}+")
                print(f"  ({len(results)} row{'s' if len(results) != 1 else ''})")
            conn.close()
        except Exception as e:
            print(f"Error: {e}")

    def do_neighbors_exact(self, arg):
        'list neighbors of this device (exact match on topology table)'
        self._run_topology_query("SELECT * FROM netbrain_neighbors WHERE device_id = %s", (self.hostname,))

    def do_neighbors_fuzzy(self, arg):
        'list neighbors of devices LIKE this device name (fuzzy match)'
        self._run_topology_query("SELECT * FROM netbrain_neighbors WHERE device_id LIKE %s", (f"%{self.hostname}%",))

    def do_is_neighbor_exact(self, arg):
        'list devices that have this device as neighbor (exact match)'
        self._run_topology_query("SELECT * FROM netbrain_neighbors WHERE nbr_device = %s", (self.hostname,))

    def do_is_neighbor_fuzzy(self, arg):
        'list devices that have LIKE this device as neighbor (fuzzy match)'
        self._run_topology_query("SELECT * FROM netbrain_neighbors WHERE nbr_device LIKE %s", (f"%{self.hostname}%",))

    def default(self, line):
        new_host = line.strip()
        if ' ' in new_host:
            print(f"*** Unknown syntax: {line}")
            return
        self.hostname = new_host
        self.prompt = f"{self.prompt.rsplit(':nb-device:', 1)[0]}:nb-device:{new_host}> "
        print(f"Device context set to: {self.hostname}")

    def do_exit(self, arg):
        'return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def do_help(self, arg):
        print("\nNetBrain Device Commands:")
        print(f"  {'info':<22} - show device info and interfaces")
        print(f"  {'info brief':<22} - show device info only, skip interfaces")
        print(f"  {'configuration':<22} - display device configuration file")
        print(f"  {'route-table':<22} - query NetBrain API for IPv4 routing table")
        print(f"  {'route-table ipv6':<22} - IPv6 Route Table")
        print(f"  {'route-table vpnv4':<22} - MPLS VPNv4 Label table")
        print(f"  {'route-table vpnv4-import':<22} - BGP VPNv4 Imported Routes")
        print(f"  {'route-table vpnv4-export':<22} - BGP VPNv4 Advertised Routes")
        print(f"  {'route-table fortigate':<22} - Fortigate Route Table")
        print(f"  {'route-table multicast':<22} - Multicast Route Table")
        print(f"  {'neighbors-exact':<22} - list neighbors of this device (exact)")
        print(f"  {'neighbors-fuzzy':<22} - list neighbors LIKE this device (fuzzy)")
        print(f"  {'is-neighbor-exact':<22} - list devices with this as neighbor (exact)")
        print(f"  {'is-neighbor-fuzzy':<22} - list devices with LIKE this as neighbor (fuzzy)")
        print(f"  {'<hostname>':<22} - change device context")
        print(f"  {'exit':<22} - return to main menu")
        print(f"\n  Current device: {self.hostname}")
        print()

    def emptyline(self): pass


class MacAddressShell(VIPLabBaseCmd):
    """Sub-shell for MAC address queries."""
    def __init__(self, parent, mac_address):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.mac_address = mac_address
        self.mac_normalized = mac_address.replace(":", "").replace(".", "").replace("-", "").lower()
        # Build all format variants for matching
        h = self.mac_normalized
        self.mac_colon = ":".join(h[i:i+2] for i in range(0, 12, 2)).upper()
        self.mac_dot = ".".join(h[i:i+4] for i in range(0, 12, 4))
        self.prompt = f"{parent.base_prompt}:mac-address:{mac_address}> "
        initialize_readline()

    def do_info(self, arg):
        'Show matching records (use "brief" for compact, "extensive" for gateway interface details)'
        import pymysql
        parts = (arg or "").split()
        if "?" in parts:
            print("Usage: info [brief|extensive]")
            print("  (no option) - show all non-empty fields from matched tables")
            print("  brief       - compact: ipLoc/maskLen from interfaces, ip lanSegment from oneiptable")
            print("  extensive   - include gateway interface details from netbrain_interfaces")
            return
        brief = "brief" in parts
        extensive = "extensive" in parts
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM netbrain_devices WHERE LOWER(device_id) = %s",
                               (self.mac_colon.lower(),))
                nb_rows = cursor.fetchall()
                if nb_rows:
                    print(f"\n--- netbrain_devices ({len(nb_rows)} match) ---")
                    for row in nb_rows:
                        for k, v in row.items():
                            if v is not None and str(v).strip():
                                print(f"  {k:<25}: {str(v).strip()}")
                        if len(nb_rows) > 1:
                            print()
                else:
                    print("\n--- netbrain_devices: no match ---")

                cursor.execute("SELECT * FROM netbrain_interfaces WHERE LOWER(REPLACE(REPLACE(REPLACE(macAddr,':',''),'.',''),'-','')) = %s",
                               (self.mac_normalized,))
                ni_rows = cursor.fetchall()
                if ni_rows:
                    print(f"\n--- netbrain_interfaces ({len(ni_rows)} match) ---")
                    for i, row in enumerate(ni_rows, 1):
                        if brief:
                            ip_loc = str(row.get("ipLoc") or "").strip()
                            mask = str(row.get("maskLen") or "").strip()
                            print(f"  {ip_loc}/{mask}" if ip_loc else f"  (no ipLoc)")
                        else:
                            if len(ni_rows) > 1:
                                print(f"  --- #{i} ---")
                            for k, v in row.items():
                                if v is not None and str(v).strip():
                                    print(f"  {k:<25}: {str(v).strip()}")
                else:
                    print("\n--- netbrain_interfaces: no match ---")

                cursor.execute("SELECT * FROM oneiptable WHERE LOWER(REPLACE(REPLACE(REPLACE(mac,':',''),'.',''),'-','')) = %s",
                               (self.mac_normalized,))
                oip_rows = cursor.fetchall()
                if oip_rows:
                    print(f"\n--- oneiptable ({len(oip_rows)} match) ---")
                    for i, row in enumerate(oip_rows, 1):
                        if brief:
                            ip_val = str(row.get("ip") or "").strip()
                            lan = str(row.get("lanSegment") or "").strip()
                            print(f"  {ip_val} {lan}".rstrip())
                        else:
                            if len(oip_rows) > 1:
                                print(f"  --- #{i} ---")
                            for k, v in row.items():
                                if v is not None and str(v).strip():
                                    print(f"  {k:<25}: {str(v).strip()}")
                        if extensive:
                            gateway = (row.get("gateway") or "").strip()
                            if gateway and "." in gateway:
                                dev_name, intf_name = gateway.split(".", 1)
                                cursor.execute(
                                    "SELECT * FROM netbrain_interfaces WHERE device_name = %s AND name = %s",
                                    (dev_name, intf_name))
                                nb_intf_rows = cursor.fetchall()
                                if nb_intf_rows:
                                    print(f"    --- NetBrain Interface ({dev_name}.{intf_name}) ---")
                                    for nb_row in nb_intf_rows:
                                        for k, v in nb_row.items():
                                            if v is not None and str(v).strip():
                                                print(f"      {k:<25}: {str(v).strip()}")
                else:
                    print("\n--- oneiptable: no match ---")

                # all_vm_nics: mac_address match (name column included directly)
                cursor.execute("SELECT * FROM all_vm_nics WHERE LOWER(REPLACE(REPLACE(REPLACE(mac_address,':',''),'.',''),'-','')) = %s",
                               (self.mac_normalized,))
                nic_rows = cursor.fetchall()
                if nic_rows:
                    print(f"\n--- all_vm_nics ({len(nic_rows)} match) ---")
                    for i, row in enumerate(nic_rows, 1):
                        if brief:
                            vm_name = (row.get("name") or "").strip()
                            ipv4 = (row.get("ipv4_address") or "").strip()
                            print(f"  {vm_name} {ipv4}".rstrip() if vm_name else f"  {ipv4}")
                        else:
                            if len(nic_rows) > 1:
                                print(f"  --- #{i} ---")
                            for k, v in row.items():
                                if k in ("ipv4_address", "ipv6_address"):
                                    print(f"  {k:<25}: {(str(v).strip() if v else '')}")
                                elif v is not None and str(v).strip():
                                    print(f"  {k:<25}: {str(v).strip()}")
                else:
                    print("\n--- all_vm_nics: no match ---")

            conn.close()
            print()
        except Exception as e:
            print(f"Error: {e}")

    def default(self, line):
        import re
        mac_input = line.strip()
        mac_colon = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
        mac_dot = re.compile(r'^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$')
        if mac_colon.match(mac_input) or mac_dot.match(mac_input):
            self.mac_address = mac_input
            self.mac_normalized = mac_input.replace(":", "").replace(".", "").replace("-", "").lower()
            h = self.mac_normalized
            self.mac_colon = ":".join(h[i:i+2] for i in range(0, 12, 2)).upper()
            self.mac_dot = ".".join(h[i:i+4] for i in range(0, 12, 4))
            self.prompt = f"{self.prompt.rsplit(':mac-address:', 1)[0]}:mac-address:{mac_input}> "
            print(f"MAC context set to: {self.mac_address}")
        else:
            print(f"*** Unknown syntax: {line}")

    def complete_info(self, text, line, begidx, endidx):
        completions = ["brief", "extensive"]
        return [c for c in completions if c.startswith(text)]

    def do_exit(self, arg):
        'return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def do_help(self, arg):
        print("\nMAC Address Commands:")
        print(f"  {'info':<18} - show matching records from netbrain_devices, netbrain_interfaces, oneiptable")
        print(f"  {'info brief':<18} - compact: ipLoc/maskLen from interfaces, ip lanSegment from oneiptable")
        print(f"  {'info extensive':<18} - include gateway interface details from netbrain_interfaces")
        print(f"  {'<mac>':<18} - change MAC context (XX:XX:XX:XX:XX:XX or xxxx.xxxx.xxxx)")
        print(f"  {'exit':<18} - return to main menu")
        print(f"\n  Current MAC: {self.mac_address}")
        print()

    def emptyline(self): pass


class DownInterfacesShell(VIPLabBaseCmd):
    """Sub-shell for the down-interfaces report filters."""
    def __init__(self, parent):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        # Build prompt from the full shell hierarchy
        segments = ['down-interfaces']
        current = parent
        while current is not None:
            name = getattr(current, 'shell_name', None) or getattr(current, 'base_prompt', None)
            if name:
                segments.append(name)
            current = getattr(current, 'parent_shell', None)
        self.prompt = ":".join(reversed(segments)) + "> "
        initialize_readline()

    @staticmethod
    def _excel_safe(value):
        """Prefix values containing '/' with =\"\" to prevent Excel auto-conversion to date."""
        if isinstance(value, str) and '/' in value:
            return f'="{value}"'
        return value

    @staticmethod
    def _export_report(filename, intf_status, config):
        """Run the DB query and write a CSV report for the given intfStatus filter."""
        import csv
        import pymysql

        columns = [
            "id", "device_name", "interface_name", "site", "name",
            "intfStatus", "descr", "ipLoc", "maskLen", "physicalPort"
        ]

        try:
            conn = pymysql.connect(
                host=config.get('Database', 'host'),
                user=config.get('Database', 'user'),
                password=config.get('Database', 'pass'),
                database=config.get('Database', 'name'),
                charset="utf8mb4"
            )
            with conn.cursor() as cursor:
                where = f"WHERE intfStatus = '{intf_status}'" if intf_status else ""
                cursor.execute(f"""
                    SELECT {', '.join(columns)}
                    FROM netbrain_interfaces_down
                    {where}
                """)
                rows = cursor.fetchall()

            # Apply Excel-safe formatting to prevent date conversion
            rows = [tuple(DownInterfacesShell._excel_safe(v) for v in row) for row in rows]

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(rows)

            print(f"Report exported: {filename} ({len(rows)} rows)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_up_down(self, arg):
        'Interfaces administratively UP operatively DOWN'
        self._export_report("report_intf_up-down.csv", "up/down", self.config)

    def do_down_down(self, arg):
        'Interfaces administratively DOWN'
        self._export_report("report_intf_down-down.csv", "down/down", self.config)

    def do_both(self, arg):
        'All DOWN interfaces'
        self._export_report("report_intf_down.csv", None, self.config)

    def do_help(self, arg):
        print("\nDown Interfaces Filters:")
        print(f"  {'up-down':<20} - Interfaces administratively UP operatively DOWN")
        print(f"  {'down-down':<20} - Interfaces administratively DOWN")
        print(f"  {'both':<20} - All DOWN interfaces")
        print(f"  {'exit':<20} - Return to reports menu")
        print(f"  {'status':<20} - Check background jobs")
        print(f"  {'log <job>':<20} - View job logs")
        print(f"  {'stop <job>':<20} - Stop a background job")

    def do_exit(self, arg):
        'Return to reports menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self):
        pass


class ReportsShell(VIPLabBaseCmd):
    shell_name = 'reports'

    def __init__(self, parent):
        super().__init__(parent_shell=parent)
        self.config = parent.config
        self.prompt = f"{parent.base_prompt}:reports> "
        initialize_readline()

    def do_down_interfaces(self, arg):
        'Report: Down Interfaces'
        DownInterfacesShell(self).cmdloop()

    def do_sn_missing(self, arg):
        'Report: Missing Serial Numbers'
        import csv
        import pymysql

        filename = "report_sn_missing.csv"

        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4"
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM sn_not_in_tcmt ORDER BY matching_score DESC")
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]

            # Apply Excel-safe formatting to prevent date conversion
            safe_rows = []
            for row in rows:
                safe_rows.append(tuple(
                    f'="{v}"' if isinstance(v, str) and '/' in v else v
                    for v in row
                ))

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(safe_rows)

            print(f"Report exported: {filename} ({len(rows)} rows)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_multiple_hostnames(self, arg):
        'Report: Multiple Hostnames'
        import csv
        import pymysql

        filename = "report_multiple_hostnames.csv"

        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                cursor.execute("ALTER TABLE `all_addresses` ADD INDEX IF NOT EXISTS `idx_host_name` (`HOST_NAME`(100))")
                cursor.execute("""
                    SELECT a.ID, a.SHORT_IP_ADDRESS, a.HOST_NAME, c.OCCURRENCES,
                           a.NOTES, a.CREATED, a.UPDATED
                    FROM all_addresses a
                    JOIN (
                        SELECT HOST_NAME, COUNT(*) AS OCCURRENCES
                        FROM all_addresses
                        WHERE HOST_NAME IS NOT NULL AND HOST_NAME != ''
                        GROUP BY HOST_NAME
                        HAVING COUNT(*) > 1
                    ) c ON a.HOST_NAME = c.HOST_NAME
                    ORDER BY c.OCCURRENCES DESC, a.HOST_NAME, a.ID
                """)
                rows = cursor.fetchall()
                columns = ["ID", "SHORT_IP_ADDRESS", "HOST_NAME", "OCCURRENCES", "NOTES", "CREATED", "UPDATED"]

            rows = [tuple(
                f'="{v}"' if isinstance(v, str) and '/' in v else v
                for v in row
            ) for row in rows]

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(rows)

            print(f"Report exported: {filename} ({len(rows)} rows)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_multiple_asset_labels(self, arg):
        'Report: Multiple TCMT asset labels'
        import csv
        import pymysql

        filename = "report_multiple_asset_labels.csv"

        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4"
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM tcmt_repeated_asset_labels")
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]

            safe_rows = []
            for row in rows:
                safe_rows.append(tuple(
                    f'="{v}"' if isinstance(v, str) and '/' in v else v
                    for v in row
                ))

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(safe_rows)

            print(f"Report exported: {filename} ({len(rows)} rows)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_easyip_best_batch(self, arg):
        'Report: EasyIP update batch generation: EM_MATCHING_SCORE 100'
        import csv
        import pymysql

        filename = "report_easyip_best_batch.csv"

        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                cursor.execute("SHOW COLUMNS FROM all_addresses")
                shared_fields = [row[0] for row in cursor.fetchall()]

                print("Querying augmented records with EM_MATCHING_SCORE = 100...")
                fields_csv = ", ".join(f"`{f}`" for f in shared_fields)
                cursor.execute(f"SELECT {fields_csv} FROM all_addresses_data_augmented WHERE EM_MATCHING_SCORE = 100")
                aug_rows = cursor.fetchall()

                if not aug_rows:
                    print("No records found with EM_MATCHING_SCORE = 100.")
                    conn.close()
                    return

                id_idx = shared_fields.index('ID')
                aug_ids = [row[id_idx] for row in aug_rows]

                print(f"Found {len(aug_ids)} augmented records. Fetching originals...")
                placeholders = ",".join(["%s"] * len(aug_ids))
                cursor.execute(f"SELECT {fields_csv} FROM all_addresses WHERE ID IN ({placeholders})", aug_ids)
                orig_map = {}
                for row in cursor.fetchall():
                    orig_map[str(row[id_idx])] = row

            print("Comparing records...")
            output_rows = []
            compare_fields = [i for i, f in enumerate(shared_fields) if f != 'ID']

            for aug_row in aug_rows:
                rec_id = aug_row[id_idx]
                orig_row = orig_map.get(str(rec_id))
                if not orig_row:
                    continue
                changed = {}
                for i in compare_fields:
                    orig_str = '' if orig_row[i] is None else str(orig_row[i]).strip()
                    aug_str = '' if aug_row[i] is None else str(aug_row[i]).strip()
                    if orig_str != aug_str:
                        changed[shared_fields[i]] = aug_row[i]
                if changed:
                    changed['ID'] = rec_id
                    output_rows.append(changed)

            if not output_rows:
                print("No modified records found.")
                conn.close()
                return

            all_changed_fields = set()
            for r in output_rows:
                all_changed_fields.update(r.keys())
            columns = ['ID'] + sorted(all_changed_fields - {'ID'})

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for r in output_rows:
                    row = []
                    for col in columns:
                        v = r.get(col, '')
                        if isinstance(v, str) and '/' in v:
                            v = f'="{v}"'
                        row.append(v)
                    writer.writerow(row)

            print(f"Report exported: {filename} ({len(output_rows)} rows, {len(columns)} columns)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_easyip_best_batch_clean(self, arg):
        'Report: EasyIP update batch generation: EM_MATCHING_SCORE 100 - excluded 192.168.0.0/16'
        import csv
        import ipaddress
        import pymysql

        filename = "report_easyip_best_batch_clean.csv"
        exclude_net = ipaddress.ip_network("192.168.0.0/16")

        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                cursor.execute("SHOW COLUMNS FROM all_addresses")
                shared_fields = [row[0] for row in cursor.fetchall()]

                print("Querying augmented records with EM_MATCHING_SCORE = 100...")
                fields_csv = ", ".join(f"`{f}`" for f in shared_fields)
                cursor.execute(f"SELECT {fields_csv} FROM all_addresses_data_augmented WHERE EM_MATCHING_SCORE = 100")
                aug_rows = cursor.fetchall()

                if not aug_rows:
                    print("No records found with EM_MATCHING_SCORE = 100.")
                    conn.close()
                    return

                id_idx = shared_fields.index('ID')
                ip_idx = shared_fields.index('SHORT_IP_ADDRESS')

                # Filter out 192.168.0.0/16
                filtered_aug = []
                excluded = 0
                for row in aug_rows:
                    ip_val = row[ip_idx]
                    if ip_val:
                        try:
                            if ipaddress.ip_address(str(ip_val).strip()) in exclude_net:
                                excluded += 1
                                continue
                        except ValueError:
                            pass
                    filtered_aug.append(row)

                print(f"Excluded {excluded} records in 192.168.0.0/16.")

                if not filtered_aug:
                    print("No records remaining after exclusion.")
                    conn.close()
                    return

                aug_ids = [row[id_idx] for row in filtered_aug]

                print(f"Found {len(aug_ids)} augmented records. Fetching originals...")
                placeholders = ",".join(["%s"] * len(aug_ids))
                cursor.execute(f"SELECT {fields_csv} FROM all_addresses WHERE ID IN ({placeholders})", aug_ids)
                orig_map = {}
                for row in cursor.fetchall():
                    orig_map[str(row[id_idx])] = row

            print("Comparing records...")
            output_rows = []
            compare_fields = [i for i, f in enumerate(shared_fields) if f != 'ID']

            for aug_row in filtered_aug:
                rec_id = aug_row[id_idx]
                orig_row = orig_map.get(str(rec_id))
                if not orig_row:
                    continue
                changed = {}
                for i in compare_fields:
                    orig_str = '' if orig_row[i] is None else str(orig_row[i]).strip()
                    aug_str = '' if aug_row[i] is None else str(aug_row[i]).strip()
                    if orig_str != aug_str:
                        changed[shared_fields[i]] = aug_row[i]
                if changed:
                    changed['ID'] = rec_id
                    output_rows.append(changed)

            if not output_rows:
                print("No modified records found.")
                conn.close()
                return

            all_changed_fields = set()
            for r in output_rows:
                all_changed_fields.update(r.keys())
            columns = ['ID'] + sorted(all_changed_fields - {'ID'})
            columns = ['STATUS_MMA' if c == 'ALIVE' else 'RESPONSE_TIME' if c == 'LOCATION' else c for c in columns]

            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for r in output_rows:
                    row = []
                    for col in columns:
                        key = 'ALIVE' if col == 'STATUS_MMA' else 'LOCATION' if col == 'RESPONSE_TIME' else col
                        v = r.get(key, '')
                        if key == 'DEVICE_TYPE' and isinstance(v, str) and len(v) > 39:
                            v = v.rsplit("- ", 1)[-1]
                        if isinstance(v, str) and '/' in v:
                            v = f'="{v}"'
                        row.append(v)
                    writer.writerow(row)

            print(f"Report exported: {filename} ({len(output_rows)} rows, {len(columns)} columns)")
            conn.close()
        except Exception as e:
            print(f"Error exporting report: {e}")

    def do_help(self, arg):
        print("\nReports Commands:")
        print(f"  {'down-interfaces':<24} - Report: Down Interfaces")
        print(f"  {'sn-missing':<24} - Report: Missing Serial Numbers")
        print(f"  {'multiple-hostnames':<24} - Report: Multiple Hostnames")
        print(f"  {'multiple-asset-labels':<24} - Report: Multiple TCMT asset labels")
        print(f"  {'easyip-best-batch':<24} - EasyIP update batch generation: EM_MATCHING_SCORE 100")
        print(f"  {'easyip-best-batch-clean':<24} - EasyIP update batch: EM_MATCHING_SCORE 100 excl. 192.168.0.0/16")
        print(f"  {'status':<24} - Check background jobs")
        print(f"  {'log <job>':<24} - View job logs")
        print(f"  {'stop <job>':<24} - Stop a background job")
        print(f"  {'exit':<24} - Return to main menu")

    def do_exit(self, arg):
        'Return to main menu'
        return True

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self): pass


class VIPLabShell(VIPLabBaseCmd):
    intro = 'Welcome to the VIPLab IP Management Shell. Type help or ? to list commands.\n'
    base_prompt = 'viplab-ip'

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.bg_jobs = {} # {name: {"proc": proc, "log": log_path, "file": file_obj, "detached": bool}}
        self.current_ip = None
        self.current_subnet_id = None
        self.update_prompt()
        initialize_readline()

    def run_background_job(self, name, script_chain, detached=False):
        """Runs a script or chain of scripts in the background."""
        if name in self.bg_jobs and self.bg_jobs[name]["proc"].poll() is None:
            print(f"Error: Job '{name}' is already running (PID: {self.bg_jobs[name]['proc'].pid})")
            return

        log_path = f"{name}.log"
        python_exe = sys.executable
        # Build a shell-free launcher that runs the scripts sequentially.
        # For a single script we call it directly, for multiple scripts we pass
        # a small Python snippet to execute them in order and exit on first failure.
        try:
            # open log with line buffering where possible to make logs available sooner
            log_file = open(log_path, "w", encoding="utf-8", buffering=1)
            if len(script_chain) == 1:
                cmd = [python_exe, "-u", script_chain[0]]
            else:
                # Build python code that runs each script sequentially and exits on error
                parts = []
                for s in script_chain:
                    # Ensure each child python is invoked unbuffered (-u) so output
                    # is written promptly to the redirected log file. Use actual
                    # newline characters in the generated -c code (not literal '\\n').
                    parts.append(
                        f"rc = __import__('subprocess').call([{repr(python_exe)},{repr('-u')},{repr(s)}])\n"
                        f"if rc != 0: __import__('sys').exit(rc)\n"
                    )
                py_code = "".join(parts)
                cmd = [python_exe, "-u", "-c", py_code]

            # start_new_session=True makes it detached from the current shell group on Unix
            creationflags = 0
            popen_kwargs = dict(
                args=cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=get_env(self.config),
                text=True,
                bufsize=1
            )
            if detached and os.name != 'nt':
                popen_kwargs['start_new_session'] = True
            if detached and os.name == 'nt':
                # On Windows, create a new process group so it can be terminated separately
                popen_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

            proc = subprocess.Popen(**popen_kwargs)
            self.bg_jobs[name] = {"proc": proc, "log": log_path, "file": log_file, "detached": detached}
            
            msg = f"Job '{name}' started in background."
            if detached:
                msg += " This process is DETACHED and will continue running if you exit the CLI."
            print(f"{msg}\nUse 'log {name}' to see progress.")
        except Exception as e:
            print(f"Failed to start job '{name}': {e}")

    def update_prompt(self):
        if self.current_ip and self.current_subnet_id:
            self.prompt = f"{self.base_prompt}:{self.current_subnet_id}:{self.current_ip}> "
        elif self.current_ip:
            self.prompt = f"{self.base_prompt}:{self.current_ip}> "
        else:
            self.prompt = f"{self.base_prompt}> "

    def default(self, line):
        if is_ip(line):
            self.current_ip = line.strip()
            self.current_subnet_id = None
            import pymysql
            try:
                conn = pymysql.connect(
                    host=self.config.get('Database', 'host'),
                    user=self.config.get('Database', 'user'),
                    password=self.config.get('Database', 'pass'),
                    database=self.config.get('Database', 'name'),
                    charset="utf8mb4",
                )
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT DISTINCT SUBNET_ID FROM all_addresses WHERE SHORT_IP_ADDRESS = %s OR IP_ADDRESS = %s",
                        (self.current_ip, self.current_ip)
                    )
                    rows = cursor.fetchall()
                    subnet_ids = [str(r[0]) for r in rows if r[0]]
                conn.close()
            except:
                subnet_ids = []

            if len(subnet_ids) == 0:
                print(f"Target IP set to: {self.current_ip} (not found in all_addresses)")
            elif len(subnet_ids) == 1:
                self.current_subnet_id = subnet_ids[0]
                print(f"Target IP set to: {self.current_subnet_id}:{self.current_ip}")
            else:
                print(f"IP {self.current_ip} found in {len(subnet_ids)} subnets:")
                for i, sid in enumerate(subnet_ids, 1):
                    print(f"  {i}. Subnet ID: {sid}")
                while True:
                    try:
                        choice = input("Select subnet (number) or press Enter to skip: ").strip()
                        if not choice:
                            break
                        idx = int(choice) - 1
                        if 0 <= idx < len(subnet_ids):
                            self.current_subnet_id = subnet_ids[idx]
                            break
                        print(f"Invalid choice. Enter 1-{len(subnet_ids)} or Enter to skip.")
                    except ValueError:
                        print(f"Enter a number 1-{len(subnet_ids)} or Enter to skip.")
                if self.current_subnet_id:
                    print(f"Target set to: {self.current_subnet_id}:{self.current_ip}")
                else:
                    print(f"Target IP set to: {self.current_ip} (multiple subnets, none selected)")
            self.update_prompt()
        else:
            print(f"*** Unknown command: {line}")

    def _get_target_ip(self, arg):
        args = shlex.split(arg)
        if args:
            if is_ip(args[0]): return args[0], args[1:], self.current_subnet_id
            if self.current_ip: return self.current_ip, args, self.current_subnet_id
            return None, args, None
        return self.current_ip, [], self.current_subnet_id

    def do_help(self, arg):
        if arg: return super().do_help(arg)
        print("\nAvailable Commands:")
        commands = [
            ("config", "enter DB and server configuration parameters"),
            ("show-config", "show actual .ini configuration"),
            ("api-collect", "enter the API collect sub-menu"),
            ("nb-device", "enter the NetBrain device sub-menu (info, configuration, topology, route-table)"),
            ("mac-address", "enter the MAC address sub-menu (XX:XX:XX:XX:XX:XX or xxxx.xxxx.xxxx)"),
            ("vm-name", "enter the VM name sub-menu (name or UUID)"),
            ("reports", "enter the reports sub-menu"),
            ("ingest", "populate DB with json and csv files network related"),
            ("vcenter", "populate DB with vcenter json files"),
            ("correlation", "execute DB tables analysis and information enrichment"),
            ("query", "execute a raw SQL query against the VIPLab database"),
            ("cmd", "execute an OS command (e.g. cmd ls, cmd dir)"),
            ("find-subnet", "DB query of IPAM related information for an IP address"),
            ("subnet-id", "DB query of IPAM related information for a Subnet ID"),
            ("nb-device", "query to DB for NetBrain device information"),
            ("tcmt-candidates", "DB query of TCMT best candidate asset labels"),
            ("ip-info", 'query to DB for IP address inferred information (use "best" for preferred match only)'),
            ("ip-record", "shows IP address fields actual information and improvement (after)"),
            ("get-ip", "API request to IPAM server for an IP address record"),
            ("get-subnet", "API request to IPAM server for a subnet record"),
            ("add-ip", "Add an IP address record to IPAM server"),
            ("set-ip", "API write request to IPAM server (creates if not existing)"),
            ("write-batch", "Writes to IPAM API server a .csv batch file"),
            ("start-simulator", "start local instance of IPAM API server"),
            ("log <job>", "show console output of a background job"),
            ("stop <job>", "stop a running background job"),
            ("status", "Check background processes status"),
            ("clear-ip", "clear the current context IP address"),
            ("exit", "exit the shell")
        ]
        for cmd_name, desc in commands: print(f"  {cmd_name:<20} - {desc}")
        print()
        print("Output Redirection:")
        print(f"  {'>> <filename>':<20} - Save command output to file (overwrite)")
        print(f"  {'> <filename>':<20} - Append command output to file")
        print(f"  {'/>':<20} - Use /> for literal > operator (e.g. in SQL)")
        print()
        print("Context Management:\n  Just type an IP (e.g. 10.0.11.0) to set it as the session target.\n")

    def do_api_collect(self, arg):
        'enter the API collect sub-menu'
        APICollectShell(self).cmdloop()

    def do_reports(self, arg):
        'enter the reports sub-menu'
        ReportsShell(self).cmdloop()

    def do_vm_name(self, arg):
        'enter the VM name sub-menu for a given VM name or UUID'
        vm_input = arg.strip()
        if not vm_input:
            print("Usage: vm-name <name or uuid>")
            return
        if ' ' in vm_input:
            print("Error: VM name/UUID cannot contain spaces.")
            return
        VmNameShell(self, vm_input).cmdloop()

    def do_mac_address(self, arg):
        'enter the MAC address sub-menu for a given MAC'
        import re
        mac_input = arg.strip()
        if not mac_input:
            print("Usage: mac-address <XX:XX:XX:XX:XX:XX or xxxx.xxxx.xxxx>")
            return
        # Validate MAC format
        mac_colon = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
        mac_dot = re.compile(r'^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$')
        if not mac_colon.match(mac_input) and not mac_dot.match(mac_input):
            print("Error: Invalid MAC address format. Use XX:XX:XX:XX:XX:XX or xxxx.xxxx.xxxx")
            return
        MacAddressShell(self, mac_input).cmdloop()

    def do_config(self, arg):
        'enter the configuration sub-menu'
        ConfigShell(self).cmdloop()

    def do_show_config(self, arg):
        'Show actual .ini configuration (passwords masked)'
        if not os.path.exists(CONFIG_FILE): print("Config not found."); return
        print(f"\n--- {CONFIG_FILE} ---")
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                stripped = line.strip().lower()
                if stripped.startswith('pass') and '=' in line:
                    key, _, _ = line.partition('=')
                    print(f"{key}= ***")
                else:
                    print(line, end='')

    def do_ingest(self, arg):
        'populate DB with json and csv files network related'
        run_script("ingest_to_viplab_v2.py", shlex.split(arg), self.config)

    def do_vcenter(self, arg):
        'populate DB with vcenter json files'
        run_script("ingest_vcenter_data.py", shlex.split(arg), self.config)

    def do_correlation(self, arg):
        'execute DB tables analysis and information enrichment'
        run_script("ingest_to_viplab_v4.py", shlex.split(arg), self.config)


    def do_nb_device(self, arg):
        'enter NetBrain device context submenu (usage: nb-device <hostname>)'
        hostname = arg.strip() if arg else ""
        if not hostname or hostname == "?":
            print("Usage: nb-device <hostname>")
            print("  Enters a device context submenu with commands:")
            print("    info              - show device info and interfaces")
            print("    info brief        - show device info only")
            print("    configuration     - display device configuration file")
            print("    route-table       - query NetBrain API for routing table")
            return
        NbDeviceShell(self, hostname).cmdloop()

    def do_find_subnet(self, arg):
        'DB query of IPAM related information for an IP address'
        ip, extra, _sid = self._get_target_ip(arg)
        if not ip: 
            print("Error: No IP provided.")
            return
        run_script("find_easyip_subnet.py", [ip] + extra, self.config)

    def do_subnet_id(self, arg):
        'DB query of IPAM related information for a Subnet ID'
        args = shlex.split(arg)
        if not args:
            print("Error: No Subnet ID provided.")
            return
        run_script("find_easyip_subnet.py", ["--subnet-id"] + args, self.config)

    def do_tcmt_candidates(self, arg):
        'DB query of TCMT best candidate asset labels'
        ip, extra, _sid = self._get_target_ip(arg)
        if not ip:
            print("Error: No IP provided.")
            return
        import pymysql
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn.cursor() as cursor:
                sql = ("SELECT SUBNET_ID, CANDIDATE_ASSET_LABELS, TCMT_MATCHING_SCORE, "
                       "TCMT_ASSOCIATION_PATH, TCMT_ASSET_ID, HOST_NAME "
                       "FROM all_addresses_data_augmented "
                       "WHERE (SHORT_IP_ADDRESS = %s OR IP_ADDRESS = %s)")
                params = [ip, ip]
                if _sid:
                    sql += " AND SUBNET_ID = %s"
                    params.append(str(_sid))
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                if not rows:
                    print(f"No records found for IP {ip}.")
                    conn.close()
                    return

                for row in rows:
                    subnet = row.get("SUBNET_ID")
                    candidate = row.get("CANDIDATE_ASSET_LABELS")
                    score = row.get("TCMT_MATCHING_SCORE")
                    path = row.get("TCMT_ASSOCIATION_PATH")
                    asset_id = row.get("TCMT_ASSET_ID")
                    host = row.get("HOST_NAME")

                    print(f"--- Subnet {subnet} ---")
                    print(f"  HOST_NAME              : {host or ''}")
                    print(f"  CANDIDATE_ASSET_LABELS : {candidate or '(none)'}")
                    print(f"  TCMT_MATCHING_SCORE    : {score or ''}")
                    print(f"  TCMT_ASSOCIATION_PATH  : {path or ''}")
                    print(f"  TCMT_ASSET_ID          : {asset_id or ''}")

                    if asset_id:
                        ids = [a.strip() for a in str(asset_id).split("-") if a.strip()]
                        placeholders = ",".join(["%s"] * len(ids))
                        cursor.execute(f"SELECT asset_ID, asset_label, responsible FROM tcmt_dump WHERE asset_ID IN ({placeholders})", ids)
                        tcmt_rows = cursor.fetchall()
                        if tcmt_rows:
                            for t in tcmt_rows:
                                print(f"    -> {t.get('asset_ID')} | {t.get('asset_label')} | {t.get('responsible')}")
                    print()
            conn.close()
        except Exception as e:
            print(f"Error: {e}")

    def do_ip_info(self, arg):
        'Usage: ip-info [best|brief|extensive] [IP]\n  best      - show only the preferred match\n  brief     - compact one-line-per-match listing\n  extensive - search IP in all NetBrain device configuration files'
        parts = shlex.split(arg) if arg else []
        if "?" in parts:
            print("Usage: ip-info [best|brief|extensive] [IP]")
            print("  best      - show only the preferred match")
            print("  brief     - compact one-line-per-match listing")
            print("  extensive - search IP in all NetBrain device configuration files")
            return
        best_only = False
        brief = False
        extensive = False
        if "best" in parts:
            best_only = True
            parts.remove("best")
        if "brief" in parts:
            brief = True
            parts.remove("brief")
        if "extensive" in parts:
            extensive = True
            parts.remove("extensive")
        ip, extra, _sid = self._get_target_ip(" ".join(parts))
        if not ip:
            print("Error: No IP provided.")
            return
        if extensive:
            import re
            configs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netbrain_configs")
            if not os.path.isdir(configs_dir):
                print(f"No netbrain_configs directory found at {configs_dir}")
                return
            ip_pattern = re.compile(r'(?<![0-9.])' + re.escape(ip) + r'(?![0-9.])')
            matches_found = 0
            print(f"--- Extensive search for '{ip}' in device configurations ---")
            for fname in sorted(os.listdir(configs_dir)):
                if not fname.endswith(".cfg"):
                    continue
                fpath = os.path.join(configs_dir, fname)
                device = fname[:-4]
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if ip_pattern.search(line):
                                matches_found += 1
                                print(f"  {device} (line {lineno}): {line.rstrip()}")
                except OSError:
                    pass
            if matches_found == 0:
                print("  No matches found in device configurations.")
            else:
                print(f"  ({matches_found} match{'es' if matches_found != 1 else ''} found)")
            return
        if best_only:
            extra = ["--best"] + extra
        if brief:
            import sys
            old_stdout = sys.stdout
            search_ip = ip
            class _BriefFilter:
                _stream = old_stdout
                _marked_first = False
                _state = None
                _header = ""
                _fields = {}
                _vm_nics = []
                _cur_nic = {}

                def _flush(self):
                    if not self._state:
                        return
                    d = self._fields
                    details = []
                    if self._state == 'vm':
                        if self._cur_nic:
                            self._vm_nics.append(self._cur_nic)
                        matched_nic = None
                        for nic in self._vm_nics:
                            if nic.get('ipv4_address') and search_ip in nic['ipv4_address']:
                                matched_nic = nic
                                break
                        if not matched_nic and self._vm_nics:
                            matched_nic = self._vm_nics[0]
                        nic_id = matched_nic.get('nic_id', '') if matched_nic else ''
                        network = matched_nic.get('network', '') if matched_nic else ''
                        details = [
                            f"vcenter  : {d.get('VCenter IP','')}",
                            f"vm-id    : {d.get('VM-ID','')}",
                            f"name     : {d.get('VM Name','')}",
                            f"nic      : {nic_id}",
                            f"network  : {network}",
                        ]
                    elif self._state == 'host_nic':
                        details = [
                            f"vcenter  : {d.get('VCenter IP','')}",
                            f"host     : {d.get('Host Name','')}",
                            f"nic      : {d.get('NIC Key','')}",
                        ]
                    elif self._state == 'netbrain':
                        details = [
                            f"device   : {d.get('device_name','')}",
                            f"intf     : {d.get('name','')}",
                        ]
                        if d.get('mplsVrf'):
                            details.append(f"vrf      : {d.get('mplsVrf')}")
                    elif self._state == 'oneip':
                        details = [
                            f"gateway  : {d.get('gateway','')}",
                        ]
                    suffix = " *" if not self._marked_first else ""
                    self._marked_first = True
                    old_stdout.write(self._header + suffix + "\n")
                    for line in details:
                        old_stdout.write("    " + line + "\n")
                    old_stdout.flush()
                    self._state = None
                    self._fields = {}
                    self._header = ""
                    self._vm_nics = []
                    self._cur_nic = {}

                def write(self, text):
                    for line in text.split("\n"):
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if "Virtual Machine Found" in line:
                            self._flush()
                            self._state = 'vm'
                            self._header = stripped
                            self._vm_nics = []
                            self._cur_nic = {}
                        elif "Physical Host NIC Match #" in line:
                            self._flush()
                            self._state = 'host_nic'
                            self._header = stripped
                        elif "Physical Host Match #" in line and "NIC" not in line:
                            self._flush()
                            self._state = 'host'
                            self._header = stripped
                        elif "NetBrain Interface Match #" in line:
                            self._flush()
                            self._state = 'netbrain'
                            self._header = stripped
                        elif "OneIP Match #" in line:
                            self._flush()
                            self._state = 'oneip'
                            self._header = stripped
                        elif self._state:
                            if self._state == 'vm' and "NIC #" in stripped and stripped.startswith("---"):
                                if self._cur_nic:
                                    self._vm_nics.append(self._cur_nic)
                                self._cur_nic = {}
                            elif ":" in stripped and not stripped.startswith("---"):
                                key, _, val = stripped.partition(":")
                                key = key.strip()
                                val = val.strip()
                                if self._state == 'vm':
                                    if key in ('VCenter IP', 'VM-ID', 'VM Name'):
                                        self._fields[key] = val
                                    elif key in ('nic_id', 'network', 'ipv4_address'):
                                        self._cur_nic[key] = val
                                elif self._state == 'host_nic':
                                    if key in ('VCenter IP', 'Host Name', 'NIC Key'):
                                        self._fields[key] = val
                                elif self._state == 'netbrain':
                                    if key in ('device_name', 'name') and key not in self._fields:
                                        self._fields[key] = val
                                    elif key == 'mplsVrf':
                                        self._fields['mplsVrf'] = val
                                elif self._state == 'oneip':
                                    if key.lower() == 'gateway':
                                        self._fields['gateway'] = val

                def flush(self):
                    self._flush()
                    old_stdout.flush()
            sys.stdout = _BriefFilter()
        run_script("find_ip_info.py", [ip] + extra, self.config)
        if brief:
            sys.stdout.flush()
            sys.stdout = old_stdout
            print()
            print('"*" marks best match')

    def complete_ip_info(self, text, line, begidx, endidx):
        completions = ["best", "brief", "extensive"]
        parts = line.split()
        if len(parts) <= 2:
            return [c for c in completions if c.startswith(text)]
        return []

    def do_ip_record(self, arg):
        'shows IP address fields actual information and improvement (after)'
        args = shlex.split(arg)
        if args and args[0] not in ("before", "after"):
            args = ["before"] + args
        elif not args:
            args = ["before"]
        if len(args) >= 2:
            script_args = args
            if self.current_subnet_id: script_args += ["--subnet-id", str(self.current_subnet_id)]
            run_script("ip_enrichment.py", script_args, self.config)
        elif len(args) == 1 and self.current_ip:
            script_args = [args[0], self.current_ip]
            if self.current_subnet_id: script_args += ["--subnet-id", str(self.current_subnet_id)]
            run_script("ip_enrichment.py", script_args, self.config)
        else: print("Usage: ip-record [before|after] [IP]")

    def do_get_ip(self, arg):
        'API request to IPAM server for an IP address record'
        ip, extra, _sid = self._get_target_ip(arg)
        if not ip:
            print("Error: No IP provided.")
            return
        script_args = [ip] + extra
        if _sid:
            script_args += ["--subnet-id", str(_sid)]
        run_script("get_easyip_address.py", script_args, self.config)

    def do_get_subnet(self, arg):
        'API request to IPAM server for a subnet record'
        ip, extra, _sid = self._get_target_ip(arg)
        if _sid:
            import pymysql
            try:
                conn = pymysql.connect(
                    host=self.config.get('Database', 'host'),
                    user=self.config.get('Database', 'user'),
                    password=self.config.get('Database', 'pass'),
                    database=self.config.get('Database', 'name'),
                    charset="utf8mb4",
                )
                with conn.cursor() as cursor:
                    cursor.execute("SELECT SHORT_SUBNET FROM all_subnets WHERE ID = %s", (_sid,))
                    row = cursor.fetchone()
                conn.close()
                subnet_ip = row[0].strip() if row and row[0] else ip
            except Exception as e:
                print(f"Warning: Could not resolve subnet IP: {e}")
                subnet_ip = ip
            run_script("get_easyip_subnet.py", ["--subnet-id", _sid, subnet_ip] + extra, self.config)
        elif ip:
            run_script("get_easyip_subnet.py", [ip] + extra, self.config)
        else:
            print("Error: No IP or subnet ID available.")

    def do_add_ip(self, arg):
        'Add an IP address record to IPAM server (creates if not existing, optionally sets fields)'
        ip, extra, _sid = self._get_target_ip(arg)
        if not ip:
            print("Error: No IP provided.")
            print("Usage: add-ip <ip> [FIELD=VALUE ...]")
            return
        cmd_args = [ip] + extra
        if _sid:
            cmd_args = ["--subnet-id", _sid] + cmd_args
        run_script("set_easyip_address.py", cmd_args, self.config)

    def do_set_ip(self, arg):
        'API write request to IPAM server (updates fields, creates record if not existing)'
        ip, extra, _sid = self._get_target_ip(arg)
        if not ip:
            print("Error: No IP provided.")
            return
        if not extra:
            print("Error: No fields provided.")
            print("Usage: set-ip <ip> FIELD=VALUE [FIELD=VALUE ...]")
            print("  To just add an IP without fields, use: add-ip <ip>")
            return
        cmd_args = [ip] + extra
        if _sid:
            cmd_args = ["--subnet-id", _sid] + cmd_args
        run_script("set_easyip_address.py", cmd_args, self.config)

    def do_write_batch(self, arg):
        'Writes to IPAM API server a .csv batch file'
        import csv
        import time
        import shlex

        if not arg.strip():
            print("Error: No CSV file provided. Usage: write-batch <file.csv>")
            return

        filename = arg.strip()
        if not os.path.isfile(filename):
            print(f"Error: File '{filename}' not found.")
            return

        try:
            with open(filename, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as e:
            print(f"Error reading CSV: {e}")
            return

        if not rows:
            print("Error: CSV file is empty.")
            return

        if 'ID' not in rows[0]:
            print("Error: CSV file must contain an 'ID' column.")
            return

        fields = [f for f in rows[0].keys() if f != 'ID']
        if not fields:
            print("Error: CSV file has no fields to update (only ID found).")
            return

        print(f"Batch file: {filename}")
        print(f"Records: {len(rows)}")
        print(f"Fields to update: {', '.join(fields)}")
        print()

        # Resolve SUBNET_ID and SHORT_IP_ADDRESS for each record from the DB
        import pymysql
        subnet_map = {}
        ip_map = {}
        try:
            conn = pymysql.connect(
                host=self.config.get('Database', 'host'),
                user=self.config.get('Database', 'user'),
                password=self.config.get('Database', 'pass'),
                database=self.config.get('Database', 'name'),
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                ids = [row.get('ID', '').strip() for row in rows if row.get('ID', '').strip()]
                if ids:
                    placeholders = ",".join(["%s"] * len(ids))
                    cursor.execute(f"SELECT ID, SUBNET_ID, SHORT_IP_ADDRESS FROM all_addresses WHERE ID IN ({placeholders})", ids)
                    for r in cursor.fetchall():
                        subnet_map[str(r[0])] = str(r[1])
                        ip_map[str(r[0])] = str(r[2]).strip() if r[2] else None
            conn.close()
        except Exception as e:
            print(f"Warning: Could not load record info from DB: {e}")

        success = 0
        errors = 0
        for i, row in enumerate(rows, 1):
            rec_id = row.get('ID', '').strip()
            if not rec_id:
                print(f"  [{i}/{len(rows)}] Skipped: missing ID")
                errors += 1
                continue

            updates = []
            for f in fields:
                val = row.get(f, '').strip()
                if val.startswith('="') and val.endswith('"'):
                    val = val[2:-1]
                if val:
                    updates.append(f"{f}={val}")

            if not updates:
                print(f"  [{i}/{len(rows)}] Skipped ID {rec_id}: no fields to update")
                continue

            # Resolve IP from CSV field or DB lookup
            ip = row.get('SHORT_IP_ADDRESS', '').strip()
            if ip and ip.startswith('="') and ip.endswith('"'):
                ip = ip[2:-1]
            if not ip:
                ip = ip_map.get(rec_id)
            if not ip:
                print(f"  [{i}/{len(rows)}] Skipped ID {rec_id}: no IP address found")
                errors += 1
                continue

            cmd_args = [ip] + updates
            subnet_id = subnet_map.get(rec_id)
            if subnet_id:
                cmd_args = ["--subnet-id", subnet_id] + cmd_args

            print(f"  [{i}/{len(rows)}] set_easyip_address.py {ip} {' '.join(updates)}")
            run_script("set_easyip_address.py", cmd_args, self.config)
            success += 1

            if i < len(rows):
                time.sleep(0.5)

        print(f"\nBatch complete: {success} processed, {errors} errors/skipped.")

    def complete_write_batch(self, text, line, begidx, endidx):
        import glob
        csv_files = glob.glob("*.csv")
        if text:
            return [f for f in csv_files if f.startswith(text)]
        return csv_files

    def do_status(self, arg):
        'Check background processes status'
        super().do_status(arg)

    def do_clear_ip(self, arg):
        'clear the current context IP address and subnet'
        self.current_ip = None
        self.current_subnet_id = None
        self.update_prompt()
        print("Cleared.")

    def do_clear_subnet(self, arg):
        'clear the current context subnet ID'
        self.current_subnet_id = None
        self.update_prompt()
        print("Subnet cleared.")

    def do_start_simulator(self, arg):
        'start local instance of IPAM API server'
        self.run_background_job("simulator", ["easyip_simulator.py"])

    def do_log_simulator(self, arg):
        'show the console output of the simulator server'
        self.do_log("simulator")

    def do_stop_simulator(self, arg):
        'stop local instance of IPAM API server'
        self.do_stop("simulator")

    def cleanup_bg_jobs(self):
        """Cleans up non-detached background processes."""
        for name in list(self.bg_jobs.keys()):
            job = self.bg_jobs[name]
            if job.get("detached"):
                # Spare detached jobs but close the file handle for this session
                if job.get("file"):
                    try: job["file"].close()
                    except: pass
                continue
                
            self.do_stop(name)
            log_path = f"{name}.log"
            if os.path.exists(log_path):
                try: os.remove(log_path)
                except: pass

    def do_exit(self, arg):
        'Exit the shell (or clear IP context if one is set)'
        if self.current_ip:
            self.current_ip = None
            self.update_prompt()
            print("IP context cleared.")
            return False
        self.cleanup_bg_jobs()
        print("Goodbye!")
        return True

    def do_EOF(self, arg):
        'Exit the shell'
        self.cleanup_bg_jobs()
        print()
        return True

    def emptyline(self): pass

def main():
    config = get_config()
    if not config.get('Database', 'pass', fallback='') or not config.get('EasyIP', 'pass', fallback=''):
        guided_setup_database(config)
        guided_setup_easyip(config)
        guided_setup_netbrain(config)
        guided_setup_vcenter(config)
        guided_setup_tcmt(config)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", action="store_true")
    parser.add_argument("command", nargs="?")
    parser.add_argument("args", nargs="*")

    # Use parse_known_args to allow passing flags (like --url) to sub-scripts
    args, unknown = parser.parse_known_args()

    if args.config:
        guided_setup_database(config)
        guided_setup_easyip(config)
        guided_setup_netbrain(config)
        guided_setup_vcenter(config)
        guided_setup_tcmt(config)
    
    # Combine positional args and unknown flags
    all_args = args.args + unknown

    commands_map = {
        "find-subnet": "find_easyip_subnet.py", "subnet-id": "find_easyip_subnet.py", "ip-info": "find_ip_info.py", "ip-record": "ip_enrichment.py",
        "start-simulator": "easyip_simulator.py", "get-ip": "get_easyip_address.py", "get-subnet": "get_easyip_subnet.py", "set-ip": "set_easyip_address.py", "add-ip": "set_easyip_address.py",
        "ingest": "ingest_to_viplab_v2.py", "vcenter": "ingest_vcenter_data.py", "correlation": "ingest_to_viplab_v4.py",
    }
    
    if args.command:
        if args.command in commands_map: run_script(commands_map[args.command], all_args, config)
        elif args.command == "api-collect":
            scripts = [
                "easyip_subnets.py", "easyip_addresses.py",
                "scan_netbrain_interfaces.py", "search_netbrain_devices.py",
                "search_netbrain_neighbors.py", "get_Global_Endpoint_Table.py",
                "get_OneIPTable.py", "netbrain_devices_config.py",
                "scan_vcenter_API_v0.1.py",
                "get_asset_inventory_2.py",
                "ingest_to_viplab_v2.py",
                "ingest_vcenter_data.py",
                "ingest_to_viplab_v4.py",
            ]
            for script in scripts:
                print(f"--- Running {script} ---")
                run_script(script, [], config)
        elif args.command == "write-batch":
            csv_file = all_args[0] if all_args else "report_easyip_best_batch_clean.csv"
            shell = VIPLabShell(config)
            shell.do_write_batch(csv_file)
        elif args.command == "query":
            shell = VIPLabShell(config)
            shell.do_query(" ".join(all_args))
        else: print(f"Unknown: {args.command}")
    else:
        try: VIPLabShell(config).cmdloop()
        except KeyboardInterrupt: print("\nGoodbye!")

if __name__ == "__main__":
    main()
