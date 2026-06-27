FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY build_db.py db.py server.py ./

# kf.db is mounted at runtime (see docker-compose.yml)
# Build it first with: docker run --rm -v /home/dh/kf_data:/data kf-mcp python build_db.py --docs /data/kf_raw/.../docs --registers /data/kf_raw/.../registers --db /data/kf.db

EXPOSE 8001

CMD ["python", "server.py", "--db", "/data/kf.db", "--host", "0.0.0.0", "--port", "8001"]
