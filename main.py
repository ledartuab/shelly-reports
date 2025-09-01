import os
import csv
import tempfile
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import smtplib
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import uvicorn

app = FastAPI(title="Shelly CSV Reports Service")

# --- Aplinkos kintamieji ---
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO   = os.getenv("EMAIL_TO")
CSV_FILE   = os.getenv("CSV_FILE", "shelly_data.csv")

DAY_TARIFF = 0.305
NIGHT_TARIFF = 0.255

# --- Funkcijos ---
def read_csv_data(file_path):
    """Perskaito CSV ir gražina listą dict su datetime ir Wh"""
    data = []
    with open(file_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        for row in reader:
            try:
                dt = datetime.strptime(row['Time'], "%d/%m/%Y %H:%M")
                wh = float(row['Wh'])
                data.append({'datetime': dt, 'Wh': wh})
            except:
                continue
    return data

def calculate_consumption(data):
    """Suskaičiuoja kiekvienos dienos kWh ir kainą su dieniniu/naktiniu tarifu"""
    days = {}
    for entry in data:
        dt = entry['datetime']
        wh = entry['Wh']
        kwh = wh / 1000
        weekday = dt.weekday()
        hour = dt.hour

        # Naktinis tarifas: savaitgaliai arba naktis darbo dienomis
        if weekday >= 5 or hour < 7 or hour >= 23:
            price = kwh * NIGHT_TARIFF
            tariff_type = "Naktinis"
        else:
            price = kwh * DAY_TARIFF
            tariff_type = "Dieninis"

        d = dt.strftime("%Y-%m-%d")
        if d not in days:
            days[d] = {"kwh": 0, "eur": 0}
        days[d]["kwh"] += kwh
        days[d]["eur"] += price
    return days

def generate_pdf_report(days, filename, title):
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Spacer(1, 12))

    data_table = [["Data", "Suvartota kWh", "Kaina €"]]
    total_kwh, total_eur = 0, 0
    for d, vals in sorted(days.items()):
        data_table.append([d, f"{vals['kwh']:.2f}", f"{vals['eur']:.2f}"])
        total_kwh += vals["kwh"]
        total_eur += vals["eur"]
    data_table.append(["Iš viso", f"{total_kwh:.2f}", f"{total_eur:.2f}"])

    table = Table(data_table, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 24))

    # grafikas
    dates = [d for d, _ in sorted(days.items())]
    values = [vals["kwh"] for _, vals in sorted(days.items())]
    plt.figure(figsize=(10, 5))
    plt.bar(dates, values, color='skyblue')
    plt.xticks(rotation=45)
    plt.ylabel("kWh")
    plt.title(title)
    plt.tight_layout()

    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name)
    plt.close()
    elements.append(Image(chart_file.name, width=400, height=200))

    doc.build(elements)

def send_email(pdf_file, subject):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject

    with open(pdf_file, "rb") as f:
        part = MIMEApplication(f.read(), Name=os.path.basename(pdf_file))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(pdf_file)}"'
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)

def get_previous_month_date_range():
    today = datetime.now()
    first_day_this_month = today.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(seconds=1)
    start_prev_month = last_day_prev_month.replace(day=1, hour=0, minute=0, second=0)
    end_prev_month = last_day_prev_month.replace(hour=23, minute=59, second=59)
    return start_prev_month, end_prev_month

def filter_data_for_previous_month(data):
    start, end = get_previous_month_date_range()
    return [d for d in data if start <= d['datetime'] <= end]

# --- FastAPI routes ---
@app.get("/")
def home():
    return {"status": "ok", "message": "Shelly CSV Reports Service veikia 🚀"}

@app.get("/previous_month_report")
def previous_month_report():
    all_data = read_csv_data(CSV_FILE)
    month_data = filter_data_for_previous_month(all_data)
    days = calculate_consumption(month_data)
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    title = f"Praėjusio mėnesio ataskaita ({get_previous_month_date_range()[0].strftime('%Y-%m')})"
    generate_pdf_report(days, pdf_file, title)
    send_email(pdf_file, title)
    return {"status": "ok", "message": "Praėjusio mėnesio ataskaita išsiųsta"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
