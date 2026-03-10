FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY viewer /app/viewer
COPY sample /app/sample
COPY tests /app/tests
COPY scripts /app/scripts
COPY bootstrap_db.py /app/bootstrap_db.py

RUN chmod +x /app/scripts/start.sh

EXPOSE 8000

ENTRYPOINT ["/app/scripts/start.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
