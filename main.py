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

# --- FastAPI aplikacija ---
app = FastAPI(title="Shelly Reports Service")

# --- Aplinkos kintamieji ---
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO   = os.getenv("EMAIL_TO")
SHELLY_HOST = os.getenv("SHELLY_HOST")
DEVICE_ID   = os.getenv("DEVICE_ID")
AUTH_KEY    = os.getenv("AUTH_KEY")

DAY_TARIFF = 0.305
NIGHT_TARIFF = 0.255

# --- Funkcija gauti hourly duomenis iš Shelly ---
def get_shelly_data(start: datetime, end: datetime):
    """
    Gražina hourly suvartojimą per laikotarpį.
    Formatu: {'2025-09-01': {0: 0.5, 1:0.6, ..., 23:0.7}, ...}
    """
    data = {}
    cur = start
    while cur <= end:
        day_str = cur.strftime("%Y-%m-%d")
        if day_str not in data:
            data[day_str] = {}
        hour = cur.hour
        try:
            url = f"https://{SHELLY_HOST}/device/status"
            params = {"id": DEVICE_ID, "auth_key": AUTH_KEY, "date": day_str, "hour": hour}
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            json_data = resp.json()
            kwh = json_data.get("kwh", 0)
        except Exception:
            kwh = 0
        data[day_str][hour] = kwh
        cur += timedelta(hours=1)
    return data

# --- Funkcija apskaičiuoti dienų suvartojimą su tarifais ---
def calculate_consumption(start: datetime, end: datetime):
    """
    Apskaičiuoja kiekvienos dienos kWh ir kainą.
    Dieninis tarifas: darbo dienos 7–23 val.
    Naktinis tarifas: 23–7 val. arba savaitgaliai.
    Grąžina dict: {'YYYY-MM-DD': {'kwh': x, 'eur': y, 'day_kwh': z, 'night_kwh': t}}
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

# --- PDF generavimas su lentele ir spalvotu grafiku ---
def generate_pdf_report(days: dict, filename: str, title: str):
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Spacer(1, 12))

    # Lentelė
    data = [["Data", "Suvartota kWh", "Kaina €", "Dieninis kWh", "Naktinis kWh"]]
    total_kwh, total_eur, total_day, total_night = 0, 0, 0, 0
    for d, vals in sorted(days.items()):
        data.append([
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
    data.append(["Iš viso", f"{total_kwh:.2f}", f"{total_eur:.2f}", f"{total_day:.2f}", f"{total_night:.2f}"])

    table = Table(data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.darkblue),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 24))

    # Grafikas su dieniniu ir naktiniu
    dates = [d for d, _ in sorted(days.items())]
    day_values = [vals["day_kwh"] for _, vals in sorted(days.items())]
    night_values = [vals["night_kwh"] for _, vals in sorted(days.items())]

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

# --- FastAPI endpoint'ai ---
@app.get("/")
def home():
    return {"status": "ok", "message": "Shelly Reports Service veikia 🚀"}

@app.get("/weekly_report")
def weekly_report():
    today = datetime.now()
    start = today - timedelta(days=today.weekday())  # pirmadienis
    end = start + timedelta(days=6, hours=23, minutes=59)
    days = calculate_consumption(start, end)
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    generate_pdf_report(days, pdf_file, "Savaitinė elektros ataskaita")
    send_email(pdf_file, "Savaitinė elektros ataskaita")
    return {"status": "ok", "message": "Savaitinė ataskaita išsiųsta"}

@app.get("/monthly_report")
def monthly_report():
    today = datetime.now()
    start = today.replace(day=1, hour=0, minute=0)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(seconds=1)
    days = calculate_consumption(start, end)
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    generate_pdf_report(days, pdf_file, "Mėnesio elektros ataskaita")
    send_email(pdf_file, "Mėnesio ataskaita")
    return {"status": "ok", "message": "Mėnesio ataskaita išsiųsta"}

# --- Uvicorn ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

