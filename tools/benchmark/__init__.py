"""Benchmark and evidence tooling for the deployed CASCADE pipeline.

run_batch  runs the 2023 hindcast back to back (needs AWS access)
collect    pulls raw, unredacted evidence for a time window (needs AWS access)
redact     turns raw evidence into the publishable evidence/ folder
analyze    rebuilds the README benchmark tables from evidence/ (offline)
"""
