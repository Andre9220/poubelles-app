FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Les dépendances d'abord : la couche reste en cache tant que le fichier ne bouge pas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installation sur l'écran d'accueil : copie les icônes dans le dossier statique
# de Streamlit et injecte les balises PWA dans son index.html.
COPY pwa/ ./pwa/
RUN python pwa/installer.py

COPY *.py ./

RUN mkdir -p /app/data/uploads/profils /app/data/uploads/poubelles

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health')"

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--server.maxUploadSize=10", \
     "--browser.gatherUsageStats=false"]
