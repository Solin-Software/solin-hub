FROM python:3.13.16-alpine3.24
COPY ask.py landing.html /app/
# The code registry (CODES_FILE); a named volume copies this ownership when first created.
RUN mkdir /data && chown nobody /data
VOLUME /data
USER nobody
ENV PYTHONUNBUFFERED=1
CMD ["python", "-I", "/app/ask.py"]
