import os
import smtplib
import tempfile
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import uvicorn

# FastAPI aplikacija
app = FastAPI(title="Shelly Reports Service")

# --- Aplinkos kintamieji (pildomi Cloud Run Variables & Secrets) ---
EMAIL_USER = os.getenv("ledartuab@gmail.com")   # pvz. tavo Gmail adresas
EMAIL_PASS = os.getenv("ahiuzhqonqqdkesf")   # Gmail App Password
EMAIL_TO   = os.getenv("petras@ledart.lt")     # kam siųsti ataskaitą
SHELLY_HOST = os.getenv("shelly-194-eu.shelly.cloud") # pvz. shelly-194-eu.shelly.cloud
DEVICE_ID   = os.getenv("ece334ead5dc")   # tavo Shelly device_id
AUTH_KEY    = os.getenv("MzQ2YWJmdWlk0F428DE366B537A585CF8B251087C642B7615A5590F1D1824894430A1BE25024353D85D4AF79EA89")    # tavo Shelly auth_key


@app.get("/")
def root():
    """Sveikinimo endpointas – patikrinti ar servisas gyvas"""
    return {"status": "ok", "message": "Shelly Reports Service veikia 🚀"}


@app.get("/test_email")
def test_email():
    """Testinis endpointas – išsiunčia paprastą PDF į Gmail"""
    try:
        # 1. Sugeneruojam paprastą PDF failą
        tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        c = canvas.Canvas(tmpfile.name, pagesize=A4)
        c.setFont("Helvetica", 14)
        c.drawString(100, 750, "Testinė PDF ataskaita iš Cloud Run 🚀")
        c.save()

        # 2. Paruošiam laišką
        msg = MIMEMultipart()
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = "Testinis laiškas su PDF"

        with open(tmpfile.name, "rb") as f:
            part = MIMEApplication(f.read(), Name="testas.pdf")
            part["Content-Disposition"] = 'attachment; filename="testas.pdf"'
            msg.attach(part)

        # 3. SMTP prisijungimas prie Gmail
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)

        return {"status": "ok", "message": "Laiškas išsiųstas"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


# Cloud Run paleidimo entrypoint
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
