import os
import smtplib
import tempfile
from datetime import datetime, timedelta
import requests
import matplotlib.pyplot as plt
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import uvicorn

# FastAPI aplikacija
app = FastAPI(title="Shelly Reports Service")

# --- Aplinkos kintamieji ---
EMAIL_USER = os.getenv("ledartuab@gmail.com")
EMAIL_PASS = os.getenv("ahiuzhqonqqdkesf")
EMAIL_TO   = os.getenv("petras@ledart.lt")
SHELLY_HOST = os.getenv("shelly-194-eu.shelly.cloud")
DEVICE_ID   = os.getenv("ece334ead5dc")
AUTH_KEY    = os.getenv("MzQ2YWJmdWlk0F428DE366B537A585CF8B251087C642B7615A5590F1D1824894430A1BE25024353D85D4AF79EA89")

DAY_TARIFF = 0.305
NIGHT_TARIFF = 0.255

# funkcija paimti duomenis iš Shelly
def get_shelly_data():
    url = f"https://{SHELLY_HOST}/device/status"
    params = {"id": DEVICE_ID, "auth_key": AUTH_KEY}
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if not data.get("isok"):
        raise Exception("Shelly API klaida")
    return data

# paskaičiuoja kiekvienos dienos suvartojimą ir kainą
def calculate_consumption(start: datetime, end: datetime):
    # demo paprastinimui — imituojam, kad per valandą Shelly gražina "1 kWh"
    # realiai čia iteruotum per Shelly emdata
    days = {}
    cur = start
    while cur <= end:
        weekday = cur.weekday()  # 0=pirmadienis
        hour = cur.hour
        kwh = 1.0  # demo
        if weekday < 5 and 7 <= hour < 23:  # darbo diena, dieninis
            price = kwh * DAY_TARIFF
            ttype = "Dieninis"
        else:  # naktinis
            price = kwh * NIGHT_TARIFF
            ttype = "Naktinis"
        d = cur.strftime("%Y-%m-%d")
        if d not in days:
            days[d] = {"kwh": 0, "eur": 0}
        days[d]["kwh"] += kwh
        days[d]["eur"] += price
        cur += timedelta(hours=1)
    return days

# PDF generavimas
def generate_pdf_report(days: dict, filename: str, title: str):
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Spacer(1, 12))

    data = [["Data", "Suvartota kWh", "Kaina €"]]
    total_kwh, total_eur = 0, 0
    for d, vals in sorted(days.items()):
        data.append([d, f"{vals['kwh']:.2f}", f"{vals['eur']:.2f}"])
        total_kwh += vals["kwh"]
        total_eur += vals["eur"]
    data.append(["Iš viso", f"{total_kwh:.2f}", f"{total_eur:.2f}"])

    table = Table(data, hAlign="LEFT")
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
    plt.figure(figsize=(8, 4))
    plt.bar(dates, values)
    plt.xticks(rotation=45)
    plt.ylabel("kWh")
    plt.title(title)
    plt.tight_layout()
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name)
    plt.close()

    from reportlab.platypus import Image
    elements.append(Image(chart_file.name, width=400, height=200))

    doc.build(elements)

# siųsti PDF paštu
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

# --- FastAPI routes ---
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
    # kitą mėnesį
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(seconds=1)
    days = calculate_consumption(start, end)
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    generate_pdf_report(days, pdf_file, "Mėnesio elektros ataskaita")
    send_email(pdf_file, "Mėnesio elektros ataskaita")
    return {"status": "ok", "message": "Mėnesio ataskaita išsiųsta"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
