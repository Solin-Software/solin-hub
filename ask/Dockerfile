FROM python:3.13.16-alpine3.24
COPY ask.py landing.html /app/
USER nobody
ENV PYTHONUNBUFFERED=1
CMD ["python", "-I", "/app/ask.py"]
