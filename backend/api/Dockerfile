# Build context is backend/ (both api/ and shared/ are needed).
#   docker build -f api/Dockerfile backend/
#
# Layers are ordered least- to most-volatile so an edit to a router rebuilds
# only the final COPY, not the dependency install.
FROM public.ecr.aws/lambda/python:3.12

# 1. Third-party dependencies -- change rarely.
COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 2. Shared package -- changes occasionally.
COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

# 3. API source -- changes most often.
COPY api/ ${LAMBDA_TASK_ROOT}/api/

# Copied rather than pip-installed so `import shared.db` and `import api.main`
# resolve exactly as they do in local development and tests.
CMD ["api.main.handler"]
