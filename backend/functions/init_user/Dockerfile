# Build context is backend/ (both this handler and shared/ are needed).
#   docker build -f functions/init_user/Dockerfile backend/
#
# Layers are ordered least- to most-volatile so editing the handler rebuilds
# only the final COPY.
FROM public.ecr.aws/lambda/python:3.12

# 1. Third-party dependencies -- change rarely.
COPY functions/init_user/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 2. Shared package -- changes occasionally.
COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

# 3. Handler -- changes most often.
COPY functions/init_user/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]
