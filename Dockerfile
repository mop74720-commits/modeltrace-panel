FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home panel
COPY --chown=panel:panel . .
RUN mkdir -p /app/config && chown panel:panel /app/config
USER panel
ENV BIND_HOST=0.0.0.0 PORT=7860 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=3)"
CMD ["python", "app.py"]
