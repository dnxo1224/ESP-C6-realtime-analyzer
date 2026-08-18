FROM python:3.10.19-slim
WORKDIR /app
COPY server/inference/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY server/inference/c6_pipeline /app/c6_pipeline
COPY server/inference/worker.py /app/
CMD ["python", "-u", "worker.py"]
