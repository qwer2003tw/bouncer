"""
Regression test: trust session bypasses rate limit (#31)
Sprint 40 Task 1

Test that when _check_trust_session is moved before _check_rate_limit in the pipeline,
trust sessions bypass rate limiting even when the rate limit would normally fire.

This test verifies the code structure to ensure the pipeline ordering is correct.
"""

import sys
import os
import re


def test_pipeline_order_trust_before_rate_limit():
    """
    Regression test: verify _check_trust_session is called before _check_rate_limit
    in the mcp_execute normal pipeline (not the escalate path).

    This ensures that trust sessions bypass rate limiting (#31).
    """
    # Read the mcp_execute.py source code
    src_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'mcp_execute.py')
    with open(src_path, 'r') as f:
        source_code = f.read()

    # Extract the else block (normal pipeline) only
    # Find the section from "else:" to the next dedented comment "# Phase 4"
    else_match = re.search(r'else:\s*result\s*=\s*\((.*?)\)\s*\n\s*#\s*Phase\s*4', source_code, re.DOTALL)
    assert else_match is not None, "Could not find the normal pipeline (else block)"

    normal_pipeline = else_match.group(1)

    # In the normal pipeline, find the positions of trust_session and rate_limit
    trust_pos = normal_pipeline.find('_check_trust_session(ctx)')
    rate_limit_pos = normal_pipeline.find('_check_rate_limit(ctx)')

    # Both should exist in the normal pipeline
    assert trust_pos != -1, "_check_trust_session not found in normal pipeline"
    assert rate_limit_pos != -1, "_check_rate_limit not found in normal pipeline"

    # trust_session should come BEFORE rate_limit
    assert trust_pos < rate_limit_pos, \
        f"Pipeline ordering error: _check_trust_session (pos {trust_pos}) should be called " \
        f"before _check_rate_limit (pos {rate_limit_pos}) in the normal pipeline"


def test_pipeline_contains_both_checks():
    """
    Verify that both _check_trust_session and _check_rate_limit exist in the pipeline.
    """
    src_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'mcp_execute.py')
    with open(src_path, 'r') as f:
        source_code = f.read()

    # Verify both functions are defined
    assert '_check_trust_session' in source_code, "_check_trust_session function not found"
    assert '_check_rate_limit' in source_code, "_check_rate_limit function not found"

    # Verify both are called in the pipeline
    pipeline_section = source_code[source_code.find('else:'):source_code.find('# Phase 4')]
    assert '_check_trust_session(ctx)' in pipeline_section, \
        "_check_trust_session not called in pipeline"
    assert '_check_rate_limit(ctx)' in pipeline_section, \
        "_check_rate_limit not called in pipeline"
