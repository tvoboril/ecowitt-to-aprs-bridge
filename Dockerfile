FROM python:3.12-slim

WORKDIR /app
COPY bridge.py .

# No ports exposed — bridge makes outbound connections only
CMD ["python", "-u", "bridge.py"]
