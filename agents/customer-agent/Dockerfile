# Build context is the repository root (see docker-compose.yml) so the shared libraries can be copied in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/libs
WORKDIR /srv

COPY agents/customer-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY libs/common /srv/libs/common
COPY libs/a2a_core /srv/libs/a2a_core
COPY agents/customer-agent/customer_agent ./customer_agent

EXPOSE 8000
CMD ["uvicorn", "customer_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
