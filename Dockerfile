FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
ENV LLM_PROVIDER=gemini
ENV PORT=8080
ENV CONFIG_FILE=/app/config.json

EXPOSE 8080

CMD ["python", "app.py"]