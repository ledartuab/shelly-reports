import os
import smtplib
import tempfile
from datetime import datetime, timedelta
import requests
import matplotlib.pyplot as plt
from fastapi import FastAPI
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import uvicorn

# --- FastAPI ---
app = FastAPI(title="Shelly Reports Service")

# --- Aplinkos kintamieji ---
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO   = os.getenv("EMAIL_TO")
DEVICE_ID  = os.getenv("DEVICE_ID")
AUTH_KEY   = os.getenv("AUTH_KEY")

DAY_TARIFF = 0.305
NIGHT_TARIFF = 0.255

# --- Gauti hourly duomenis iš Shelly Cloud Pro (EMData) ---
def get_shelly_data(start: datetime, end: datetime):
    """
    Gražina hourly kWh per dienas.
    Formatu: {'YYYY-MM-DD': {0: 0.5, 1:0.6, ..., 23:0.7}, ...}
    """
    data = {}
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    url = f"https://shelly-194-eu.shelly.cloud/device/{DEVICE_ID}/history"
    params = {
        "auth_key": AUTH_KEY,
        "period": "hour",
        "date_from": start_str,
        "date_to": end_str
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        json_data = resp.json()
        for day, hours in json_data.items():
            data[day] = {int(h): float(kwh) for h, kwh in hours.items()}
    except Exception as e:
        print("Klaida gaunant Shelly duomenis:", e)
        # Jei klaida, demo duomenys
        cur = start
        while cur <= end:
            d = cur.strftime("%Y-%m-%d")
            data[d] = {h: 0 for h in range(24)}
            cur += timedelta(days=1)
    return data

# --- Apskaičiuoti dienų suvartojimą su tarifais ---
def calculate_consumption(start: datetime, end: datetime):
    """
    Dieninis/naktinis tarifas pagal darbo dienas/savaitgalius.
    Grąžina: {'YYYY-MM-DD': {'kwh': x, 'eur': y, 'day_kwh': z, 'night_kwh': t}}
    """
    days = {}
    shelly_data = get_shelly_data(start, end)
    cur = start
    while cur <= end:
        d = cur.strftime("%Y-%m-%d")
        weekday = cur.weekday()
        hour = cur.hour
        kwh = shelly_data.get(d, {}).get(hour, 0)
        if weekday < 5 and 7 <= hour < 23:
            price = kwh * DAY_TARIFF
            day_kwh = kwh
            night_kwh = 0
        else:
            price = kwh * NIGHT_TARIFF
            day_kwh = 0
            night_kwh = kwh
        if d not in days:
            days[d] = {"kwh": 0, "eur": 0, "day_kwh": 0, "night_kwh": 0}
        days[d]["kwh"] += kwh
        days[d]["eur"] += price
        days[d]["day_kwh"] += day_kwh
        days[d]["night_kwh"] += night_kwh
        cur += timedelta(hours=1)
    return days

# --- PDF generavimas su lentele ir grafiku ---
def generate_pdf_report(days: dict, filename: str, title: str):
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Spacer(1, 12))

    # Lentelė
    data_table = [["Data", "Suvartota kWh", "Kaina €", "Dieninis kWh", "Naktinis kWh"]]
    total_kwh, total_eur, total_day, total_night = 0,0,0,0
    for d, vals in sorted(days.items()):
        data_table.append([
            d,
            f"{vals['kwh']:.2f}",
            f"{vals['eur']:.2f}",
            f"{vals['day_kwh']:.2f}",
            f"{vals['night_kwh']:.2f}"
        ])
        total_kwh += vals['kwh']
        total_eur += vals['eur']
        total_day += vals['day_kwh']
        total_night += vals['night_kwh']
    data_table.append(["Iš viso", f"{total_kwh:.2f}", f"{total_eur:.2f}", f"{total_day:.2f}", f"{total_night:.2f}"])

    table = Table(data_table, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.darkblue),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 24))

    # Grafikas
    dates = [d for d, _ in sorted(days.items())]
    day_values = [vals['day_kwh'] for _, vals in sorted(days.items())]
    night_values = [vals['night_kwh'] for _, vals in sorted(days.items())]

    plt.figure(figsize=(10,4))
    plt.bar(dates, night_values, color='blue', label='Naktinis')
    plt.bar(dates, day_values, bottom=night_values, color='orange', label='Dieninis')
    plt.xticks(rotation=45)
    plt.ylabel("kWh")
    plt.title(title)
    plt.legend()
    plt.tight_layout()

    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name)
    plt.close()
    elements.append(Image(chart_file.name, width=500, height=250))

    doc.build(elements)

# --- Siųsti PDF paštu ---
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

# --- Endpoint praėjusio mėnesio ataskaitai ---
@app.get("/previous_month_report")
def previous_month_report():
    today = datetime.now()
    first_day_this_month = today.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(seconds=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    days = calculate_consumption(first_day_prev_month, last_day_prev_month)
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    generate_pdf_report(days, pdf_file, f"Elektros ataskaita: {first_day_prev_month.strftime('%B %Y')}")
    send_email(pdf_file, f"Elektros ataskaita: {first_day_prev_month.strftime('%B %Y')}")
    return {"status": "ok", "message": "Praėjusio mėnesio ataskaita išsiųsta"}

# --- Uvicorn ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
