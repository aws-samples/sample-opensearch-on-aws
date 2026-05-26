#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
set -euo pipefail

echo "=== Starting OpenSearch container ==="
docker compose up -d --wait

echo "=== Installing required Python packages ==="
pip install -r requirements.txt

echo "=== Setting up test data with larger dataset ==="
python setup_test_env.py --doc-count 10000 --missing-percentage 15 --port 9201 --source-index large_source --target-index large_target

echo "=== Running index comparison ==="
python compare_indices.py --auth-mode none --source large_source --target large_target --output large_missing_ids.txt --port 9201 --scroll-size 2000

echo "=== Verifying results ==="
echo "Expected missing IDs: $(wc -l < expected_missing_ids.txt)"
echo "Found missing IDs:    $(wc -l < large_missing_ids.txt)"
if diff -q expected_missing_ids.txt large_missing_ids.txt >/dev/null; then
    echo "PASS: missing-ID sets match"
else
    echo "FAIL: missing-ID sets differ"
    diff expected_missing_ids.txt large_missing_ids.txt | head -20
    exit 1
fi

echo "=== Test completed ==="
echo "You can stop the OpenSearch container with: docker compose down"
