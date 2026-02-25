# Research: Unified Display Summary

## Decision 1: Helper function location

- **Decision**: Add `generate_display_summary()` to `src/utils.py`
- **Rationale**: `utils.py` is already imported by all modules that create DynamoDB items. Centralizes format logic.
- **Alternatives considered**: 
  - New module `src/display.py` — rejected (overkill for a single function)
  - Inline at each call site — rejected (DRY violation)

## Decision 2: Field name

- **Decision**: Use `display_summary` as the field name
- **Rationale**: Descriptive, matches the spec, no conflict with existing fields
- **Alternatives considered**: `summary`, `description` — both too generic and might conflict

## Decision 3: Size formatting

- **Decision**: Reuse `_format_size_human()` from `mcp_upload.py` by moving to `utils.py`
- **Rationale**: Already implemented, tested via upload tests. Avoids duplication.
- **Alternatives considered**: 
  - Keep in `mcp_upload.py` and import — creates circular dependency risk
  - Duplicate the function — DRY violation

## Decision 4: Backward compatibility approach

- **Decision**: `display_summary` is optional. Display code checks for it first, then falls back to existing logic.
- **Rationale**: Zero-downtime migration. Legacy items created before this change still display correctly.
- **Alternatives considered**: DynamoDB migration to backfill — rejected (unnecessary complexity, items have TTL and will expire)
