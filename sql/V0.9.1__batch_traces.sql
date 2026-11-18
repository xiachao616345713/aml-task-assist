-- 在助手数据库执行，关联创建、上传和处理的独立Trace，避免混入轮询与查询。
CREATE TABLE IF NOT EXISTS batch_trace_links (
 trace_id CHAR(32) PRIMARY KEY, batch_id CHAR(32) NOT NULL,
 owner_id VARCHAR(128) NOT NULL, phase VARCHAR(32) NOT NULL, created_at DOUBLE NOT NULL,
 KEY idx_batch_trace_owner (batch_id, owner_id, created_at),
 CONSTRAINT fk_batch_trace FOREIGN KEY (batch_id) REFERENCES material_batches(id)
) ENGINE=InnoDB;
