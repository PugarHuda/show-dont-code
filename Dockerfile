# Run Show, Don't Code on your own machine: the review app, every export, the MCP server, and the pipeline for your
# own videos with your own Meta Model API key, so footage never leaves the lab. (The hosted demo's upload and
# protocols.io-import endpoints run on Modal; here the pipeline runs from the command line, see README.)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt PyLabRobot==0.2.2
# own step, as on Modal: mcp lifts jsonschema past opentrons' pin, and app.py imports it only if present
RUN pip install --no-cache-dir mcp==2.3.0 pypdfium2==4.30.0 arctrl==3.2.3 sila2==0.14.0
COPY app.py ./
COPY sdc sdc
COPY web web
ENV OT_API_CONFIG_DIR=/tmp/opentrons PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
VOLUME /app/out
EXPOSE 8000 50052
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
