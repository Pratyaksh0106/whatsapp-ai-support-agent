#!/usr/bin/env bash
# Builds dist/lambda.zip for arm64 / Python 3.12 (boto3 is already in the Lambda runtime).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build dist && mkdir -p build dist
pip install -r requirements-lambda.txt -t build --platform manylinux2014_aarch64 \
  --python-version 3.12 --implementation cp --only-binary=:all: --upgrade -q
cp -r app build/app
(cd build && zip -qr ../dist/lambda.zip . -x "*__pycache__*")
echo "built dist/lambda.zip ($(du -h dist/lambda.zip | cut -f1))"
