# Build context is the repository root (see docker-compose.yml) so the shared library can be copied in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/libs
WORKDIR /srv

COPY services/customer-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY libs/common /srv/libs/common
COPY services/customer-service/db ./db
COPY services/customer-service/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
