# savas-test-python (plan-i karar C): tek kullanımlık test container'ı.
# Orchestrator ile aynı temel image (digest), yalnız pytest. Ağsız koşar.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN pip install --no-cache-dir pytest==9.2.0
USER 1000:1000
