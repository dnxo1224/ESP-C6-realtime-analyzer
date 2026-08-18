FROM python:3.10.19-slim
WORKDIR /app
RUN pip install --no-cache-dir pymysql==1.1.2 cryptography==46.0.5
COPY server/inference/c6_pipeline/contract.py /app/c6_contract.py
COPY server/ingest/csi_ingest_server.py /app/
CMD ["python", "-u", "csi_ingest_server.py"]
