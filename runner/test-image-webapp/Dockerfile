# M-3 (plan-m karar D): test image for FastAPI web apps (yigit-calendar).
# Versions are the project's own (.venv, 2026-11-18) + pytest + httpx.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN pip install --no-cache-dir \
    pytest==9.2.0 httpx==0.28.1 \
    fastapi==0.142.2 starlette==1.7.0 pydantic==2.13.5 pydantic_core==2.46.5 uvicorn==0.54.0 \
    anyio==4.15.1 h11==0.16.0 idna==3.20 typing_extensions==4.16.0 typing-inspection==0.4.4 \
    annotated-types==0.8.0 annotated-doc==0.0.5 certifi==2026.7.22 \
    pywebpush==2.5.0 py-vapid==1.9.4 http_ece==1.2.1 cryptography==50.0.1 cffi==2.1.1 pycparser==3.0 \
    requests==2.34.2 urllib3==2.8.0 charset-normalizer==3.5.2 \
    aiohttp==3.14.3 aiohappyeyeballs==2.7.1 aiosignal==1.4.0 attrs==26.1.0 frozenlist==1.8.0 \
    multidict==6.9.1 propcache==0.5.4 yarl==1.25.1 opentelemetry-api==1.45.0
USER 1000:1000
