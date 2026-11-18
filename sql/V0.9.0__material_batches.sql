-- 在助手数据库执行；独立保存批量材料、匹配依据及实际提交快照。
CREATE TABLE IF NOT EXISTS material_batches (
 id CHAR(32) PRIMARY KEY, owner_id VARCHAR(128) NOT NULL,
 conversation_id CHAR(32) NOT NULL, process_type VARCHAR(100) NOT NULL,
 status VARCHAR(32) NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 KEY idx_batch_owner (owner_id, created_at),
 KEY idx_batch_conversation (conversation_id, owner_id),
 CONSTRAINT fk_batch_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS batch_sources (
 id CHAR(32) PRIMARY KEY, batch_id CHAR(32) NOT NULL, owner_id VARCHAR(128) NOT NULL,
 parent_id CHAR(32), filename VARCHAR(500) NOT NULL, content_type VARCHAR(160) NOT NULL,
 sha256 CHAR(64) NOT NULL, size_bytes BIGINT NOT NULL, content LONGBLOB NOT NULL,
 text_content LONGTEXT NOT NULL, extraction JSON, error VARCHAR(500), backend_path VARCHAR(1024),
 created_at DOUBLE NOT NULL,
 KEY idx_batch_source (batch_id, owner_id),
 CONSTRAINT fk_source_batch FOREIGN KEY (batch_id) REFERENCES material_batches(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS batch_submissions (
 id CHAR(32) PRIMARY KEY, batch_id CHAR(32) NOT NULL, owner_id VARCHAR(128) NOT NULL,
 process_type VARCHAR(100) NOT NULL, task_id VARCHAR(255), act_id VARCHAR(255),
 status VARCHAR(32) NOT NULL, data JSON NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 KEY idx_batch_item (batch_id, owner_id),
 KEY idx_submission_record (process_type, act_id, created_at),
 KEY idx_submission_task (process_type, task_id, created_at),
 CONSTRAINT fk_submission_batch FOREIGN KEY (batch_id) REFERENCES material_batches(id)
) ENGINE=InnoDB;
