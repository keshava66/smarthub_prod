#!/bin/bash

LOG_FILE="/projects/var/lib/sdc-resources/parameters/RID/SmartHub/DQE/BDC/quarterly/logs/run_$(date '+%Y%m%d_%H%M%S').log"
mkdir -p /projects/var/lib/sdc-resources/parameters/RID/SmartHub/DQE/BDC/quarterly/logs
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== Starting SmartHub Pipeline: $(date) ==="

# -- Create venv if it doesn't exist ------------------------------------------
VENV_PATH="/projects/var/lib/sdc-resources/parameters/RID/SmartHub/venv"
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating venv..."
    python3 -m venv "$VENV_PATH"
else
    echo "Venv already exists, skipping creation."
fi

# -- Activate venv -------------------------------------------------------------
echo "Activating venv..."
source "$VENV_PATH/bin/activate"

# -- Change directory & install dependencies -----------------------------------
echo "Changing directory..."
cd /projects/var/lib/sdc-resources/parameters/RID/SmartHub/DQE/BDC/quarterly

echo "Installing requirements..."
pip install -r requirements.txt --quiet

# -- Run BDC validation (all rules) before generating Quarterly report -------------
echo "Running BDC validation (EXECUTE_ALLRULES) on ${BACKEND_HOST}..."

BACKEND_HOST="https://awsbjvnval002j.jnj.com:8000"
VALIDATION_URL="${BACKEND_HOST}/api/run-validation?mode=EXECUTE_ALLRULES"
VALIDATION_RETURNCODE=-1

while IFS= read -r line; do
    if [[ "$line" == data:* ]]; then
        json="${line#data: }"
        msg=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null)
        [ -n "$msg" ] && echo "[VALIDATION] $msg"
        type=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('type',''))" 2>/dev/null)
        if [[ "$type" == "done" ]]; then
            VALIDATION_RETURNCODE=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('returncode',-1))" 2>/dev/null)
            break
        fi
    fi
done < <(curl -sN --max-time 36000 "$VALIDATION_URL")

if [ "$VALIDATION_RETURNCODE" != "0" ]; then
    echo "FAILED: BDC validation failed with exit code $VALIDATION_RETURNCODE. Aborting quarterly report."
    exit 1
fi

echo "BDC validation completed successfully."

# -- Run Python script ---------------------------------------------------------
echo "Running Email_quarterly_report.py..."
python Email_quarterly_report.py
SCRIPT_EXIT=$?

# -- Send full run log to secondary email list ---------------------------------
echo "Sending run log email..."
python3 - "$LOG_FILE" <<'PYEOF'
import sys, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log_file = sys.argv[1]

with open(log_file, "r") as f:
    log_content = f.read()

sender    = "donotreply@gra-smarthub-dqe.jnj.com"
to_addrs  = ["email_id", "email_id"]
cc_addrs  = ["email_id", "email_id"]

msg = MIMEMultipart()
msg["From"]    = sender
msg["To"]      = ", ".join(to_addrs)
msg["Cc"]      = ", ".join(cc_addrs)
msg["Subject"] = "DQE Quarterly Report - Pipeline Run Log"

body = f"<html><body><pre style='font-family:monospace;font-size:13px;'>{log_content}</pre></body></html>"
msg.attach(MIMEText(body, "html"))

all_recipients = to_addrs + cc_addrs
with smtplib.SMTP("localhost") as server:
    server.sendmail(sender, all_recipients, msg.as_string())

print("Log email sent successfully.")
PYEOF

if [ "$SCRIPT_EXIT" -ne 0 ]; then
    echo "FAILED: Email_quarterly_report.py exited with code $SCRIPT_EXIT."
    exit 1
fi

# -- Detect output report ------------------------------------------------------
echo "Checking for Quarterly report..."
REPORTS_DIR="/projects/var/lib/sdc-resources/parameters/RID/SmartHub/DQE/BDC/quarterly/reports"
ARCHIVE_DIR="/projects/var/lib/sdc-resources/parameters/RID/SmartHub/DQE/BDC/quarterly/reports/archive"

mkdir -p "$ARCHIVE_DIR"

# Covers both: Quarterly_report_First_run_*.xlsx and Quarterly_report_*_to_*.xlsx
ATTACHMENT=$(ls "$REPORTS_DIR"/Quarterly_report_*.xlsx 2>/dev/null | head -1)

if [ -z "$ATTACHMENT" ]; then
  echo "FAILED: No Quarterly report found in $REPORTS_DIR!"
  exit 1
fi

echo "Found report: $ATTACHMENT"

# -- Parse dates from filename for subject -------------------------------------
# Filename format: Quarterly_report_01_May_2026_to_20_May_2026.xlsx
REPORT_FILENAME=$(basename "$ATTACHMENT" .xlsx)
REPORT_BODY="${REPORT_FILENAME#Quarterly_report_}"

_parse_date() {
    # Takes a token like "01_May_2026" and returns "01 May 2026"
    local part="$1"
    echo "$part" | tr '_' ' '
}

if [[ "$REPORT_BODY" == First_run_* ]]; then
    TO_PART="${REPORT_BODY#First_run_}"
    TO_DATE=$(_parse_date "$TO_PART")
    EMAIL_SUBJECT="DQE RTE Quarterly Report: $TO_DATE (First Run)"
    body="<html><body><p>Hi everyone,</p><p>Please find the attached <b>RTE Quarterly report</b> generated on <b>$TO_DATE (First Run)</b>.</p><p>Regards,<br>GRA-SmartHub</p></body></html>"
else
    FROM_PART=$(echo "$REPORT_BODY" | sed 's/_to_.*//')
    TO_PART=$(echo "$REPORT_BODY" | sed 's/.*_to_//')
    FROM_DATE=$(_parse_date "$FROM_PART")
    TO_DATE=$(_parse_date "$TO_PART")
    EMAIL_SUBJECT="DQE RTE Quarterly Report: $FROM_DATE to $TO_DATE"
    body="<html><body><p>Hi everyone,</p><p>Please find the attached <b>RTE Quarterly report</b> generated from <b>$FROM_DATE</b> to <b>$TO_DATE</b>.</p><p>Regards,<br>GRA-SmartHub</p></body></html>"
fi

echo "Email subject: $EMAIL_SUBJECT"

# -- Send email via Python (supports HTML body + attachment) -------------------
echo "Sending email..."

python3 - "$EMAIL_SUBJECT" "$body" "$ATTACHMENT" <<'PYEOF'
import sys
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

subject    = sys.argv[1]
body       = sys.argv[2]
attachment = sys.argv[3]

sender    = "donotreply@gra-smarthub-dqe.jnj.com"
to_addrs  = ["email_id", "email_id"]
cc_addrs  = ["email_id", "email_id"]

msg = MIMEMultipart()
msg["From"]    = sender
msg["To"]      = ", ".join(to_addrs)
msg["Cc"]      = ", ".join(cc_addrs)
msg["Subject"] = subject

msg.attach(MIMEText(body, "html"))

with open(attachment, "rb") as f:
    part = MIMEBase("application", "octet-stream")
    part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{os.path.basename(attachment)}"'
    )
    msg.attach(part)

all_recipients = to_addrs + cc_addrs
with smtplib.SMTP("localhost") as server:
    server.sendmail(sender, all_recipients, msg.as_string())

print("Email sent successfully.")
PYEOF

if [ $? -ne 0 ]; then
  echo "FAILED: Email send failed!"
  exit 1
fi

# -- Archive the report --------------------------------------------------------
echo "Archiving report..."
mv "$ATTACHMENT" "$ARCHIVE_DIR/$(basename "$ATTACHMENT")"
echo "Report moved to archive: $ARCHIVE_DIR/$(basename "$ATTACHMENT")"

# -- Deactivate venv -----------------------------------------------------------
deactivate

echo "=== Pipeline Completed Successfully: $(date) ==="