-- V0.7.0__add_trace_metrics_columns.sql
-- Description: traces_v2 增加看板聚合摘要列（状态、耗时、token、调用次数），
--              避免统计时解析 LONGTEXT data；不改变既有 data JSON 结构。
-- Author: jarytech
-- Date: 2026-11-12

ALTER TABLE traces_v2
    ADD COLUMN status VARCHAR(16) NULL AFTER name,
    ADD COLUMN duration_ms DOUBLE NULL AFTER status,
    ADD COLUMN input_tokens BIGINT NULL AFTER duration_ms,
    ADD COLUMN output_tokens BIGINT NULL AFTER input_tokens,
    ADD COLUMN total_tokens BIGINT NULL AFTER output_tokens,
    ADD COLUMN model_calls BIGINT NULL AFTER total_tokens,
    ADD COLUMN tool_calls BIGINT NULL AFTER model_calls,
    ADD COLUMN error_count BIGINT NULL AFTER tool_calls;

CREATE INDEX idx_traces_owner_status_created ON traces_v2 (owner_id, status, created_at DESC);

-- 回填既有数据：从 data JSON 中抽取摘要字段。JSON 表达到列的映射仅覆盖存在字段的记录。
UPDATE traces_v2
SET
    status = JSON_UNQUOTE(JSON_EXTRACT(data, '$.status')),
    total_tokens = COALESCE(JSON_EXTRACT(data, '$.total_tokens.total'), 0),
    input_tokens = COALESCE(JSON_EXTRACT(data, '$.total_tokens.input'), 0),
    output_tokens = COALESCE(JSON_EXTRACT(data, '$.total_tokens.output'), 0),
    model_calls = COALESCE(JSON_EXTRACT(data, '$.model_calls'), 0),
    tool_calls = COALESCE(JSON_EXTRACT(data, '$.tool_calls'), 0),
    error_count = COALESCE(JSON_EXTRACT(data, '$.error_count'), 0),
    duration_ms = COALESCE(JSON_EXTRACT(data, '$.duration_ms'), 0)
WHERE status IS NULL;
