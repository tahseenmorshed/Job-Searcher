# JobSeeker: one image that serves the web page and the API from uvicorn on port 8000.
# Secrets (.env), the database (data/) and resume.md are passed in at run time, never built in.

# ---- Stage 1: build the web page into plain files (web/dist) ----
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- Stage 2: the image that runs ----
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py *.toml profile.md ./
COPY --from=web /web/dist ./web/dist
EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
