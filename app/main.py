2.1 app/main.py
# -*- coding: utf-8 -*-
# FastAPI paslauga Cloud Run’ui:
# - /health                      (GET)  – sveikatos patikra
# - /ingest                      (POST) – paima dabartinį Shelly skaitiklį ir išsaugo Firestore
# - /report/weekly               (POST) – generuoja PR. savaitės PDF ir siunčia el. paštu
# - /report/monthly              (POST) – generuoja PR. mėnesio PDF ir siunčia el. paštu
# - /report/preview?period=...   (GET)  – grąžina PDF atsisiuntimui (be siuntimo el. paštu)

import os
import io
import math
import smtplib
import requests
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Optional

from fastapi import FastAPI, Response, HTTPException, Request
from pydantic import BaseModel

from google.cloud import firestore

import matplotlib
matplotlib.use("Agg")  # be GUI
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ======== Konfigūracija iš aplinkos ========
TIMEZONE = os.getenv("TIMEZONE", "Europe/Vilnius")
TZ = ZoneInfo(TIMEZONE)

SHELLY_HOST = os.getenv("shelly-194-eu.shelly.cloud")              
SHELLY_DEVICE_ID = os.getenv("ece334ead5dc")    
SHELLY_AUTH_KEY = os.getenv("MzQ2YWJmdWlk0F428DE366B537A585CF8B251087C642B7615A5590F1D1824894430A1BE25024353D85D4AF79EA89")      
TARIFF_DAY_EUR = float(os.getenv("TARIFF_DAY_EUR", "0.305"))
TARIFF_NIGHT_EUR = float(os.getenv("TARIFF_NIGHT_EUR", "0.255"))

EMAIL_USER = os.getenv("ledartuab@gmail.com")               
EMAIL_PASS = os.getenv("ahiuzhqonqqdkesf")              
EMAIL_TO = os.getenv("petras@ledart.lt", EMAIL_USER)       
SENDER_NAME = os.getenv("Shelly ataskaitos", "Shelly Ataskaitos")

TASK_TOKEN = os.getenv("labai_slapta_123")                # paprastas apsaugos tokenas Scheduler’iui

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")

# PDF šriftas – DejaVu Sans (apima lietuviškus simbolius); Dockerfile įdiegs
pdfmetrics.registerFont(TTFont('DejaVu', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))

app = FastAPI(title="Shelly Pro 3EM Reports")

# Firestore klientas (naudojamas numatytojo service account teisių kontekste)
db = firestore.Client(project=PROJECT_ID)
SAMPLES_COL = db.collection("energy_samples")  # dokumentai: { ts: datetime(UTC), total_wh: float }

# ========= Pagalbinės funkcijos =========

def _require_token(request: Request):
    if not TASK_TOKEN:
        return
    # Leidžiam per antraštę arba query param
    token = request.headers.get("X-Task-Token") or request.query_params.get("token")
    if token != TASK_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized (bad token)")


def shelly_fetch_total_wh() -> Tuple[datetime, float]:
    """Paimame dabartinę emdata:0.total_act (Wh) per Shelly Cloud endpoint.
       Grąžina (device_time_utc, total_wh).
    """
    if not (SHELLY_HOST and SHELLY_DEVICE_ID and SHELLY_AUTH_KEY):
        raise RuntimeError("Trūksta SHELLY_* aplinkos kintamųjų")

    url = f"https://{SHELLY_HOST}/device/status"
    data = {"device_id": SHELLY_DEVICE_ID, "auth_key": SHELLY_AUTH_KEY}
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    if not j.get("isok"):
        raise RuntimeError(f"Shelly cloud error: {j}")

    st = j["data"]["device_status"]
    # total_act (Wh) laikome emdata:0
    emd = st.get("emdata:0") or {}
    total_wh = float(emd.get("total_act"))

    # Naudojam įrenginio unixtime kaip matavimo laiką
    sys = st.get("sys") or {}
    unixtime = int(sys.get("unixtime"))
    ts_utc = datetime.fromtimestamp(unixtime, tz=timezone.utc)
    return ts_utc, total_wh


def store_sample(ts_utc: datetime, total_wh: float) -> str:
    doc = {"ts": ts_utc, "total_wh": total_wh, "device_id": SHELLY_DEVICE_ID}
    ref = SAMPLES_COL.add(doc)[1]
    return ref.id


def query_samples(start_utc: datetime, end_utc: datetime):
    """Paimam imtį su apsauga prieš/po ribų interpoliacijai."""
    # -2h iki +2h, kad turėtume aplinkinių taškų
    qs = (SAMPLES_COL
          .where("ts", ">=", start_utc - timedelta(hours=2))
          .where("ts", "<=", end_utc + timedelta(hours=2))
          .order_by("ts")
          .stream())
    rows = [(d.to_dict()["ts"], float(d.to_dict()["total_wh"])) for d in qs]
    return rows


def interpolate_value(points: List[Tuple[datetime, float]], target: datetime) -> Optional[float]:
    """Linijinė interpoliacija bendrame sąraše (ts, value). Grąžina None jei negalima."""
    if not points:
        return None
    # jei tiksliai turim tašką
    for t, v in points:
        if t == target:
            return v
    # rasti aplinkinius
    before = [p for p in points if p[0] <= target]
    after = [p for p in points if p[0] >= target]
    if not before or not after:
        return None
    t0, v0 = before[-1]
    t1, v1 = after[0]
    if t1 == t0:
        return v0
    ratio = (target - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + ratio * (v1 - v0)


def is_weekend(dt_local: datetime) -> bool:
    return dt_local.weekday() >= 5  # 5=Šešt, 6=Sekt


def current_tariff(dt_local: datetime) -> str:
    if is_weekend(dt_local):
        return "night"
    h = dt_local.hour
    if 7 <= h < 23:
        return "day"
    return "night"


def next_tariff_boundary(dt_local: datetime) -> datetime:
    """Sekantis tarifo perjungimo momentas lokaliu laiku."""
    date = dt_local.date()
    if is_weekend(dt_local):
        # savaitgalį – naktinis visą parą, kita riba – kitos dienos 00:00
        return datetime.combine(date + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    # darbo diena: 07:00 ir 23:00 ribos
    b1 = datetime.combine(date, datetime.min.time(), tzinfo=TZ).replace(hour=7)
    b2 = datetime.combine(date, datetime.min.time(), tzinfo=TZ).replace(hour=23)

    if dt_local < b1:
        return b1
    if dt_local < b2:
        return b2
    # po 23:00 – kita riba rytoj 07:00
    return datetime.combine(date + timedelta(days=1), datetime.min.time(), tzinfo=TZ).replace(hour=7)


def split_interval_by_tariff(t0_utc: datetime, t1_utc: datetime, wh: float) -> Tuple[float, float]:
    """Paskirsto energiją [t0;t1] į dieninį/naktinį pagal trukmę (proporcingai)."""
    if wh <= 0 or t1_utc <= t0_utc:
        return 0.0, 0.0
    total_secs = (t1_utc - t0_utc).total_seconds()
    day_wh = 0.0
    night_wh = 0.0
    cur_local = t0_utc.astimezone(TZ)
    end_local = t1_utc.astimezone(TZ)
    while cur_local < end_local:
        boundary = next_tariff_boundary(cur_local)
        seg_end_local = min(boundary, end_local)
        seg_secs = (seg_end_local - cur_local).total_seconds()
        seg_wh = wh * (seg_secs / total_secs)
        if current_tariff(cur_local) == "day" and not is_weekend(cur_local):
            day_wh += seg_wh
        else:
            night_wh += seg_wh
        cur_local = seg_end_local
    return day_wh, night_wh


def build_series_with_edges(points: List[Tuple[datetime, float]], start_utc: datetime, end_utc: datetime) -> List[Tuple[datetime, float]]:
    """Įterpia sintetinius taškus ties start/end ribomis (interpoliacija)."""
    pts = sorted(points, key=lambda x: x[0])
    v_start = interpolate_value(pts, start_utc)
    v_end = interpolate_value(pts, end_utc)
    if v_start is not None:
        pts = [(start_utc, v_start)] + [p for p in pts if start_utc < p[0] < end_utc]
    else:
        pts = [p for p in pts if start_utc <= p[0] < end_utc]
    if v_end is not None:
        pts = pts + [(end_utc, v_end)]
    return sorted(pts, key=lambda x: x[0])


def aggregate_period(start_local: datetime, end_local: datetime) -> Dict:
    """Suskaičiuoja dieninį/naktinį ir dienines eilutes periode."""
    # į UTC
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    raw = query_samples(start_utc, end_utc)
    if len(raw) < 2:
        raise RuntimeError("Per mažai duomenų Firestore (patikrink /ingest grafiką)")

    series = build_series_with_edges(raw, start_utc, end_utc)
    # pereinam poromis
    day_wh = night_wh = 0.0
    per_day: Dict[str, Dict[str, float]] = {}  # key YYYY-MM-DD → {day_wh, night_wh}

    for i in range(len(series) - 1):
        t0, v0 = series[i]
        t1, v1 = series[i + 1]
        dwh = v1 - v0
        if dwh < 0 or dwh > 1e7:  # atmesti akivaizdžius šuolius
            continue
        d_day, d_night = split_interval_by_tariff(t0, t1, dwh)
        day_wh += d_day
        night_wh += d_night
        # Suvestinė pagal kalendorinę dieną lokaliu laiku (skaičiuojam proporcingai)
        cur = t0
        total_secs = (t1 - t0).total_seconds()
        cur_local = cur.astimezone(TZ)
        end_local = t1.astimezone(TZ)
        while cur_local < end_local:
            # dienos pabaiga (lokali)
            day_end = datetime.combine(cur_local.date() + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
            seg_end = min(day_end, end_local)
            seg_secs = (seg_end - cur_local).total_seconds()
            seg_wh = dwh * (seg_secs / total_secs)
            seg_day, seg_night = split_interval_by_tariff(cur, cur + timedelta(seconds=seg_secs), seg_wh)
            key = cur_local.date().isoformat()
            per_day.setdefault(key, {"day_wh": 0.0, "night_wh": 0.0})
            per_day[key]["day_wh"] += seg_day
            per_day[key]["night_wh"] += seg_night
            cur = cur + timedelta(seconds=seg_secs)
            cur_local = seg_end

    res = {
        "day_kwh": day_wh / 1000.0,
        "night_kwh": night_wh / 1000.0,
        "total_kwh": (day_wh + night_wh) / 1000.0,
        "day_eur": (day_wh / 1000.0) * TARIFF_DAY_EUR,
        "night_eur": (night_wh / 1000.0) * TARIFF_NIGHT_EUR,
        "total_eur": (day_wh / 1000.0) * TARIFF_DAY_EUR + (night_wh / 1000.0) * TARIFF_NIGHT_EUR,
        "per_day": []  # sąrašas eilučių lentelei/grafikams
    }
    # suformuojam dienų sąrašą nuosekliai
    d = start_local.date()
    while d <= end_local.date():
        key = d.isoformat()
        day_wh = per_day.get(key, {}).get("day_wh", 0.0)
        night_wh = per_day.get(key, {}).get("night_wh", 0.0)
        row = {
            "date": key,
            "day_kwh": day_wh / 1000.0,
            "night_kwh": night_wh / 1000.0,
            "total_kwh": (day_wh + night_wh) / 1000.0,
            "day_eur": (day_wh / 1000.0) * TARIFF_DAY_EUR,
            "night_eur": (night_wh / 1000.0) * TARIFF_NIGHT_EUR,
            "total_eur": (day_wh / 1000.0) * TARIFF_DAY_EUR + (night_wh / 1000.0) * TARIFF_NIGHT_EUR,
        }
        res["per_day"].append(row)
        d += timedelta(days=1)
    return res


def fmt_eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ")


def fmt_kwh(x: float) -> str:
    return f"{x:,.3f} kWh".replace(",", ".")


def make_charts(per_day: List[Dict]) -> Tuple[str, str]:
    """Sukuria du grafikus PNG ir grąžina kelią (path) į failus."""
    dates = [row["date"] for row in per_day]
    total_kwh = [row["total_kwh"] for row in per_day]
    day_kwh = [row["day_kwh"] for row in per_day]
    night_kwh = [row["night_kwh"] for row in per_day]

    # 1) Dienų suvartojimas (stulpelinė)
    plt.figure(figsize=(8, 3))
    plt.bar(dates, total_kwh)
    plt.title("Dienos suvartojimas (kWh)")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    p1 = "/tmp/chart_daily.png"
    plt.savefig(p1, dpi=160)
    plt.close()

    # 2) Dieninis vs naktinis (stacked)
    plt.figure(figsize=(8, 3))
    plt.bar(dates, night_kwh, label="Naktinis")
    plt.bar(dates, day_kwh, bottom=night_kwh, label="Dieninis")
    plt.title("Dieninis vs naktinis (kWh)")
    plt.xticks(rotation=45, ha="right")
    plt.legend()
    plt.tight_layout()
    p2 = "/tmp/chart_daynight.png"
    plt.savefig(p2, dpi=160)
    plt.close()

    return p1, p2


def build_pdf(period_title: str, start_local: datetime, end_local: datetime, agg: Dict) -> bytes:
    """Sugeneruoja PDF kaip bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles["Title"].fontName = 'DejaVu'
    styles["Normal"].fontName = 'DejaVu'
    styles["Heading2"].fontName = 'DejaVu'

    flow = []
    title = f"Elektros suvartojimo ataskaita – {period_title}"
    flow.append(Paragraph(title, styles["Title"]))
    flow.append(Spacer(1, 6))
    sub = f"Laikotarpis: {start_local.strftime('%Y-%m-%d %H:%M')} – {end_local.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE})"
    flow.append(Paragraph(sub, styles["Normal"]))
    flow.append(Spacer(1, 10))

    # Suvestinė
    summary_data = [
        ["Dieninis", fmt_kwh(agg["day_kwh"]), fmt_eur(agg["day_eur"])],
        ["Naktinis", fmt_kwh(agg["night_kwh"]), fmt_eur(agg["night_eur"])],
        ["Iš viso", f"{agg['total_kwh']:.3f} kWh", fmt_eur(agg["total_eur"])],
    ]
    t = Table([["Tarifas", "Kiekis", "Kaina"]] + summary_data, hAlign='LEFT')
    t.setStyle(TableStyle([
        ('FONT', (0,0), (-1,0), 'DejaVu', 10),
        ('FONT', (0,1), (-1,-1), 'DejaVu', 9),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f0f3f6')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#212121')),
        ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#cfd8dc')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#fafafa')])
    ]))
    flow.append(t)
    flow.append(Spacer(1, 10))

    # Lentele po dieną
    day_rows = [["Data", "Dieninis (kWh)", "Naktinis (kWh)", "Iš viso (kWh)", "Kaina (€)"]]
    for row in agg["per_day"]:
        day_rows.append([
            row["date"],
            f"{row['day_kwh']:.3f}",
            f"{row['night_kwh']:.3f}",
            f"{row['total_kwh']:.3f}",
            f"{row['total_eur']:.2f}"
        ])
    t2 = Table(day_rows, hAlign='LEFT')
    t2.setStyle(TableStyle([
        ('FONT', (0,0), (-1,0), 'DejaVu', 9),
        ('FONT', (0,1), (-1,-1), 'DejaVu', 8),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#e3f2fd')),
        ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#bbdefb')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f7fbff')])
    ]))
    flow.append(t2)
    flow.append(Spacer(1, 10))

    # Grafikai
    p1, p2 = make_charts(agg["per_day"])
    flow.append(Image(p1, width=170*mm, height=60*mm))
    flow.append(Spacer(1, 6))
    flow.append(Image(p2, width=170*mm, height=60*mm))

    doc.build(flow)
    return buf.getvalue()


def send_email(subject: str, body: str, pdf_bytes: bytes, filename: str):
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        raise RuntimeError("Nesukonfigūruotas el. paštas (EMAIL_USER/PASS/TO)")

    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{EMAIL_USER}>"
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(EMAIL_USER, EMAIL_PASS)
        smtp.send_message(msg)


# ========= API maršrutai =========
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ingest")
async def ingest(request: Request):
    _require_token(request)
    ts_utc, total_wh = shelly_fetch_total_wh()
    doc_id = store_sample(ts_utc, total_wh)
    return {"stored": True, "doc_id": doc_id, "ts": ts_utc.isoformat(), "total_wh": total_wh}


def last_week_period_now() -> Tuple[datetime, datetime, str]:
    now = datetime.now(TZ)
    # praėjusios savaitės (pirmadienis–sekmadienis)
    # dabartinės savaitės pirmadienis:
    this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_mon = this_mon - timedelta(days=7)
    prev_sun_end = this_mon - timedelta(seconds=1)
    period_title = f"Savaitė {prev_mon.date()} – {prev_sun_end.date()}"
    return prev_mon, prev_sun_end, period_title


def last_month_period_now() -> Tuple[datetime, datetime, str]:
    now = datetime.now(TZ)
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev_end = first_this - timedelta(seconds=1)
    first_prev = first_this.replace(month=first_this.month-1 if first_this.month>1 else 12,
                                    year=first_this.year if first_this.month>1 else first_this.year-1,
                                    day=1)
    period_title = f"{first_prev.strftime('%Y-%m')}"
    return first_prev, last_prev_end, period_title


@app.post("/report/weekly")
async def report_weekly(request: Request):
    _require_token(request)
    start_local, end_local, title = last_week_period_now()
    agg = aggregate_period(start_local, end_local)
    pdf_bytes = build_pdf(f"{title}", start_local, end_local, agg)
    subject = f"Savaitinė ataskaita {start_local.date()}–{end_local.date()}"
    body = (
        f"Dieninis: {agg['day_kwh']:.3f} kWh ({agg['day_eur']:.2f} €)\n"
        f"Naktinis: {agg['night_kwh']:.3f} kWh ({agg['night_eur']:.2f} €)\n"
        f"Iš viso: {agg['total_kwh']:.3f} kWh ({agg['total_eur']:.2f} €)\n"
    )
    send_email(subject, body, pdf_bytes, f"savaites-ataskaita-{start_local.date()}-{end_local.date()}.pdf")
    return {"emailed": True, "period": [start_local.isoformat(), end_local.isoformat()], "summary": agg}


@app.post("/report/monthly")
async def report_monthly(request: Request):
    _require_token(request)
    start_local, end_local, title = last_month_period_now()
    agg = aggregate_period(start_local, end_local)
    pdf_bytes = build_pdf(f"Mėnesis {title}", start_local, end_local, agg)
    subject = f"Mėnesio ataskaita {title}"
    body = (
        f"Dieninis: {agg['day_kwh']:.3f} kWh ({agg['day_eur']:.2f} €)\n"
        f"Naktinis: {agg['night_kwh']:.3f} kWh ({agg['night_eur']:.2f} €)\n"
        f"Iš viso: {agg['total_kwh']:.3f} kWh ({agg['total_eur']:.2f} €)\n"
    )
    send_email(subject, body, pdf_bytes, f"menesio-ataskaita-{title}.pdf")
    return {"emailed": True, "period": [start_local.isoformat(), end_local.isoformat()], "summary": agg}


@app.get("/report/preview")
async def report_preview(period: str = "weekly"):
    if period == "weekly":
        start_local, end_local, title = last_week_period_now()
        title = f"{title} (peržiūra)"
    elif period == "monthly":
        start_local, end_local, title = last_month_period_now()
        title = f"Mėnesis {title} (peržiūra)"
    else:
        raise HTTPException(400, "period must be weekly|monthly")
    agg = aggregate_period(start_local, end_local)
    pdf_bytes = build_pdf(title, start_local, end_local, agg)
    return Response(content=pdf_bytes, media_type="application/pdf")
    import os
from flask import Flask
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import tempfile

app = Flask(__name__)

EMAIL_USER = os.getenv("ledartuab@gmail.com")
EMAIL_PASS = os.getenv("ahiuzhqonqqdkesf")
EMAIL_TO   = os.getenv("petras@ledart.lt")

@app.get("/test_email")
def test_email():
    try:
        # Sugeneruoja paprastą PDF
        tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        c = canvas.Canvas(tmpfile.name, pagesize=A4)
        c.drawString(100, 750, "Testinė PDF ataskaita iš Cloud Run 🚀")
        c.save()

        # Paruošia laišką
        msg = MIMEMultipart()
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = "Testinis laiškas su PDF"

        with open(tmpfile.name, "rb") as f:
            part = MIMEApplication(f.read(), Name="testas.pdf")
            part["Content-Disposition"] = 'attachment; filename="testas.pdf"'
            msg.attach(part)

        # SMTP prisijungimas prie Gmail
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)

        return {"status": "OK", "message": "Laiškas išsiųstas"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)


