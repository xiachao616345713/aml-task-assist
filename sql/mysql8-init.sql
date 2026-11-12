-- MySQL 8 初始化。先由 DBA 创建账号并授权该库；不要在脚本或仓库保存口令。
-- 应用表在这里管理；LangGraph 检查点表由固定版本的 MySQL Saver setup() 创建/升级。
CREATE DATABASE IF NOT EXISTS aml_task_assist
  CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE aml_task_assist;

CREATE TABLE IF NOT EXISTS conversations (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  title VARCHAR(100) NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY idx_conversations_owner (owner_id, updated_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS messages (
  id CHAR(32) PRIMARY KEY,
  conversation_id CHAR(32) NOT NULL,
  owner_id VARCHAR(128) NOT NULL,
  role VARCHAR(16) NOT NULL,
  content LONGTEXT NOT NULL,
  trace_id VARCHAR(64),
  created_at DOUBLE NOT NULL,
  KEY idx_messages_thread (conversation_id, owner_id, created_at DESC),
  CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB;

-- 教学页面的阶段归属。会话正文仍使用上面的 conversations/messages 表。
CREATE TABLE IF NOT EXISTS learning_conversation_stages (
  conversation_id CHAR(32) PRIMARY KEY,
  stage CHAR(2) NOT NULL,
  CONSTRAINT fk_learning_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS review_workflows (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  conversation_id CHAR(32),
  process_type VARCHAR(100) NOT NULL,
  task_id VARCHAR(255) NOT NULL,
  act_id VARCHAR(255) NOT NULL,
  status VARCHAR(40) NOT NULL,
  material_task_id CHAR(32),
  trace_id VARCHAR(64),
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  active_slot CHAR(64),
  UNIQUE KEY uq_workflow_active_slot (active_slot),
  KEY idx_workflows_owner (owner_id, updated_at DESC),
  KEY idx_workflows_task (owner_id, process_type, task_id, act_id),
  CONSTRAINT fk_workflow_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS material_tasks (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  workflow_id CHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  required_json JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY idx_material_owner (owner_id, updated_at DESC),
  CONSTRAINT fk_material_workflow FOREIGN KEY (workflow_id) REFERENCES review_workflows(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS attachments (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  material_task_id CHAR(32) NOT NULL,
  filename VARCHAR(200) NOT NULL,
  content_type VARCHAR(160) NOT NULL,
  size_bytes BIGINT NOT NULL,
  sha256 CHAR(64) NOT NULL,
  backend_path VARCHAR(1024) NOT NULL,
  created_at DOUBLE NOT NULL,
  KEY idx_attachments_material (material_task_id, owner_id, created_at),
  CONSTRAINT fk_attachment_material FOREIGN KEY (material_task_id) REFERENCES material_tasks(id)
) ENGINE=InnoDB;

-- 新上传资料保存不可变原件及提取正文，供带鉴权的来源弹窗读取。
CREATE TABLE IF NOT EXISTS attachment_contents (
  attachment_id CHAR(32) PRIMARY KEY,
  content LONGBLOB NOT NULL,
  text_content MEDIUMTEXT NOT NULL,
  CONSTRAINT fk_content_attachment FOREIGN KEY (attachment_id) REFERENCES attachments(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS audit_events (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  action VARCHAR(80) NOT NULL,
  target_id VARCHAR(255) NOT NULL,
  outcome VARCHAR(80) NOT NULL,
  trace_id VARCHAR(64),
  created_at DOUBLE NOT NULL,
  KEY idx_audit_owner (owner_id, created_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS submissions_v3 (
  task_id VARCHAR(255) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY idx_submissions_owner (owner_id, updated_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS traces_v2 (
  id CHAR(32) PRIMARY KEY,
  owner_id VARCHAR(128) NOT NULL,
  created_at DOUBLE NOT NULL,
  data LONGTEXT NOT NULL,
  conversation_id CHAR(32),
  workflow_id CHAR(32),
  name VARCHAR(255) NOT NULL,
  KEY idx_traces_owner_created (owner_id, created_at DESC),
  KEY idx_traces_conversation (owner_id, conversation_id, created_at DESC),
  KEY idx_traces_workflow (owner_id, workflow_id, created_at DESC),
  KEY idx_traces_name (owner_id, name, created_at DESC),
  CONSTRAINT chk_trace_json CHECK (JSON_VALID(data))
) ENGINE=InnoDB;

-- 本机调试配置；凭证只在服务端读取，不在接口响应中返回。
CREATE TABLE IF NOT EXISTS app_settings (
  id VARCHAR(32) PRIMARY KEY,
  business_token TEXT NOT NULL,
  model_name VARCHAR(255) NOT NULL,
  model_base_url VARCHAR(2048) NOT NULL,
  model_api_key TEXT NOT NULL,
  model_timeout DOUBLE NOT NULL,
  model_max_tokens BIGINT NOT NULL
) ENGINE=InnoDB;
