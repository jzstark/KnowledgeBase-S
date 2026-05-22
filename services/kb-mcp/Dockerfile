FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kb_mcp ./kb_mcp

ENV KB_API_BASE=http://api:8000
ENV PORT=7878

EXPOSE 7878

CMD ["python", "-m", "kb_mcp"]
