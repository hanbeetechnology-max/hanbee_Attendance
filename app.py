import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
import mysql.connector
from mysql.connector import Error

load_dotenv()

app = Flask(__name__)
CORS(app)

# Aiven Database Credentials - loaded from environment / .env, never hardcoded
DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))

@app.route('/attendance', methods=['GET'])
def log_attendance():
    # Get the employee name sent by the ESP32
    employee_name = request.args.get('name')
    
    if not employee_name:
        return "Error: No name provided", 400

    try:
        # Connect securely to Aiven (ssl_disabled=False forces the required SSL)
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            port=DB_PORT,
            ssl_disabled=False 
        )
        
        if connection.is_connected():
            cursor = connection.cursor()
            
            # Insert the scan into the Hanbee_attendance table
            query = "INSERT INTO Hanbee_attendance (Name, date, time) VALUES (%s, CURDATE(), CURTIME())"
            cursor.execute(query, (employee_name,))
            connection.commit()
            
            cursor.close()
            connection.close()
            
            print(f"Success: Logged attendance for {employee_name}")
            return "Success", 200

    except Error as e:
        print(f"Database Error: {e}")
        return f"Database Error", 500


# -------------------------------------------------------------
# HR DASHBOARD API
# -------------------------------------------------------------
OFFICE_START = timedelta(hours=10)   # 10:00 AM IST
OFFICE_END = timedelta(hours=19)     # 7:00 PM IST
LUNCH_TARGET_SECONDS = 60 * 60       # 1 hour
TEA_TARGET_SECONDS = 30 * 60         # 30 minutes

# Aiven's server clock runs in UTC, but the office runs on IST (UTC+5:30).
# The ESP32 writes CURDATE()/CURTIME() straight from that UTC clock, so every
# stored date/time has to be shifted forward by this much before it means
# anything to an HR person reading the dashboard.
IST_OFFSET = timedelta(hours=5, minutes=30)

# Fixed scan sequence for one employee's day: 1st = check-in, 2nd/3rd = lunch
# out/in, 4th/5th = tea out/in, 6th = check-out. Every scan's meaning is
# determined by its position in *today's* sequence so far, not by whether
# it happens to be the last scan seen - a half-finished day (e.g. only 3
# scans in: check-in, lunch-out, lunch-in) must not be misread as "checked
# out" just because scan #3 is currently the last one on record.
BREAK_SEQUENCE = [
    ("Lunch Break", 1, 2, LUNCH_TARGET_SECONDS),
    ("Tea Break", 3, 4, TEA_TARGET_SECONDS),
]
CHECK_OUT_INDEX = 5


def get_db_connection():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        port=DB_PORT,
        ssl_disabled=False,
        connect_timeout=8,
    )


def parse_date_value(value):
    # Same driver quirk as parse_time_value, but for DATE columns.
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    return value


def parse_time_value(value):
    # mysql-connector-python has returned TIME columns as either timedelta
    # or a plain "HH:MM:SS" string depending on version/config - normalize
    # everything to timedelta so the rest of the code has one type to deal with.
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        hours, minutes, seconds = (value.split(":") + ["0", "0"])[:3]
        return timedelta(hours=int(hours), minutes=int(minutes), seconds=float(seconds))
    return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)


def format_hhmm(td):
    if td is None:
        return None
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_employee_summary(name, scan_times):
    # scan_times: sorted list of datetime.timedelta
    scan_count = len(scan_times)

    record = {
        "name": name,
        "status": "Present" if scan_count else "Absent",
        "scanCount": scan_count,
        "incomplete": scan_count != 6,
        "checkIn": None,
        "checkOut": None,
        "late": False,
        "leftEarly": False,
        "grossHours": None,
        "netHours": None,
        "extraHours": None,
        "totalBreakMinutes": 0,
        "breaks": [],
        "unmatchedScans": [],
    }

    if scan_count == 0:
        return record

    check_in = scan_times[0]
    record["checkIn"] = format_hhmm(check_in)
    record["late"] = check_in > OFFICE_START

    # Each break only appears once BOTH its out and in scans have actually
    # happened; if only the out-scan has happened so far, it's shown as
    # in-progress (no in time / duration yet) instead of being silently
    # dropped or misread as something else.
    total_break_seconds = 0
    for label, out_idx, in_idx, target_seconds in BREAK_SEQUENCE:
        if scan_count > in_idx:
            out_time, in_time = scan_times[out_idx], scan_times[in_idx]
            duration_seconds = max(0, (in_time - out_time).total_seconds())
            total_break_seconds += duration_seconds
            record["breaks"].append({
                "label": label,
                "out": format_hhmm(out_time),
                "in": format_hhmm(in_time),
                "durationMinutes": round(duration_seconds / 60),
                "overLimit": duration_seconds > target_seconds,
                "inProgress": False,
            })
        elif scan_count == out_idx + 1:
            record["breaks"].append({
                "label": label,
                "out": format_hhmm(scan_times[out_idx]),
                "in": None,
                "durationMinutes": None,
                "overLimit": False,
                "inProgress": True,
            })

    record["totalBreakMinutes"] = round(total_break_seconds / 60)

    # Check-out - and anything derived from it - only exists once the 6th
    # scan has actually happened, not just because a scan happens to be last.
    if scan_count > CHECK_OUT_INDEX:
        check_out = scan_times[CHECK_OUT_INDEX]
        record["checkOut"] = format_hhmm(check_out)
        record["leftEarly"] = check_out < OFFICE_END

        gross_seconds = (check_out - check_in).total_seconds()
        record["grossHours"] = round(gross_seconds / 3600, 2)
        record["netHours"] = round(record["grossHours"] - total_break_seconds / 3600, 2)

        if check_out > OFFICE_END:
            record["extraHours"] = round((check_out - OFFICE_END).total_seconds() / 3600, 2)

    # Scans past the expected 6 (check-in + 2 breaks + check-out) are unplanned extras.
    if scan_count > CHECK_OUT_INDEX + 1:
        record["unmatchedScans"] = [format_hhmm(t) for t in scan_times[CHECK_OUT_INDEX + 1:]]

    return record


@app.route('/api/attendance', methods=['GET'])
def attendance_summary():
    date_param = request.args.get('date')
    if date_param:
        try:
            target_date = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            return jsonify(error="date must be in YYYY-MM-DD format"), 400
    else:
        # "Today" also has to account for the UTC/IST gap - it's already
        # tomorrow in UTC for the last 5:30 hours of the IST day.
        target_date = (datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET).date()

    # Rows are stored against Aiven's UTC clock. A scan at, say, 23:15 UTC is
    # actually 04:45 IST the *next* day, so the previous UTC date can contain
    # rows that belong to today in IST - fetch both and filter after converting.
    prev_date = target_date - timedelta(days=1)

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        # The roster isn't stored anywhere - it's inferred from everyone who
        # has ever scanned a card, so new employees show up automatically
        # and nobody needs to be hardcoded here.
        cursor.execute("SELECT DISTINCT Name FROM Hanbee_attendance")
        roster = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "SELECT Name, date, time FROM Hanbee_attendance WHERE date IN (%s, %s) ORDER BY Name, date, time",
            (prev_date, target_date),
        )
        rows = cursor.fetchall()
        cursor.close()
        connection.close()
    except Error as e:
        print(f"Database Error: {e}")
        message = str(e).lower()
        if "timed out" in message or "timeout" in message:
            return jsonify(error="Database request timed out. Please try again."), 504
        return jsonify(error="Database error. Please try again later."), 500

    scans_by_employee = {name: [] for name in roster}
    for name, row_date, time_value in rows:
        utc_dt = datetime.combine(parse_date_value(row_date), datetime.min.time()) + parse_time_value(time_value)
        ist_dt = utc_dt + IST_OFFSET
        if ist_dt.date() != target_date:
            continue  # belongs to the adjacent IST day, not the one requested
        ist_time_of_day = ist_dt - datetime.combine(ist_dt.date(), datetime.min.time())
        scans_by_employee.setdefault(name, []).append(ist_time_of_day)

    employees = [
        build_employee_summary(name, sorted(times))
        for name, times in scans_by_employee.items()
    ]
    employees.sort(key=lambda r: r["name"])

    return jsonify(
        date=target_date.isoformat(),
        employees=employees,
        _debug={
            "prevDate": prev_date.isoformat(),
            "targetDate": target_date.isoformat(),
            "rawRowCount": len(rows),
            "rosterCount": len(roster),
        },
    )


if __name__ == '__main__':
    # Run the server on port 5000, accessible to any device on your WiFi network
    print("Starting Hanbee Attendance Server...")
    app.run(host='0.0.0.0', port=5000)