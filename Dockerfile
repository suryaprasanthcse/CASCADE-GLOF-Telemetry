# One image for every CASCADE Lambda function; template.yaml picks each
# function's handler. This is the x86_64 manifest of the AWS Lambda
# Python 3.14 base image, pinned by digest so every build starts from the
# same bytes.
FROM public.ecr.aws/lambda/python:3.14@sha256:bccd6eafa55dc28ce23e605969ab12b690f581b2696c71866a3e25919c6fa7e2

# rasterio, pyproj and shapely ship GDAL, PROJ and GEOS inside their
# wheels. The one exception: rasterio's GDAL links against the system
# expat library, which the minimal AL2023 base image leaves out. The
# pinned base image fixes the AL2023 release, and each release's package
# repository is locked, so this always installs the same expat.
RUN dnf install -y expat && dnf clean all

# Python packages are pinned in requirements-lambda.txt; boto3 comes with
# the base image.
COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --only-binary=:all: \
        --requirement ${LAMBDA_TASK_ROOT}/requirements-lambda.txt \
        --target ${LAMBDA_TASK_ROOT}

COPY core/ ${LAMBDA_TASK_ROOT}/core/
COPY handlers/ ${LAMBDA_TASK_ROOT}/handlers/

CMD ["handlers.measure.handler"]
