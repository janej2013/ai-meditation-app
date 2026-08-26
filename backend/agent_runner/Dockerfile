# Build context is backend/ (shared/, agent/ and agent_runner/ are needed).
#   docker build -f agent_runner/Dockerfile backend/
#
# Not the Lambda base image the API uses. That image pairs with Mangum,
# which turns one invocation into one buffered response. A companion turn
# streams, and Lambda response streaming for a Python app is done by the
# Lambda Web Adapter: an extension that receives the invocation, forwards
# it to this HTTP server on localhost, and streams the reply back
# (AWS_LWA_INVOKE_MODE=response_stream). Locally the adapter is inert and
# this is an ordinary uvicorn container.
FROM public.ecr.aws/docker/library/python:3.12-slim

# Pinned: the adapter's behaviour is part of the deployment, not a moving target.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080 \
    AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 1. Third-party dependencies -- change rarely.
COPY agent_runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 2. Shared package and the agent -- change occasionally.
COPY shared/ shared/
COPY agent/ agent/

# 3. The harness -- changes most often.
COPY agent_runner/ agent_runner/

CMD ["uvicorn", "agent_runner.main:app", "--host", "0.0.0.0", "--port", "8080"]
